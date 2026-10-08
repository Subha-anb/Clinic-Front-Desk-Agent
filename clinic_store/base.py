"""Storage-agnostic clinic operations: slot generation, ownership checks and booking rules.

Backends implement the small persistence API at the bottom (`_load_*` / `_save_*`).
Every patient-facing operation takes `patient_ref` and checks ownership, so a patient can
only see, cancel or move their own appointments, whatever the LLM asks for.
"""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta, timezone, tzinfo

REASON_CATEGORIES = ["general_consultation", "follow_up", "lab_test", "scan", "vaccination", "other"]
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PARTS_OF_DAY = {"morning": (time(0), time(12)), "afternoon": (time(12), time(17)), "evening": (time(17), time(23, 59))}


class StoreError(Exception):
    """A business-rule failure that is safe to show to the model / patient."""


def clinic_tz(clinic: dict) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(clinic["timezone"])
    except Exception:  # Windows without the tzdata package: fall back to IST
        return timezone(timedelta(hours=5, minutes=30))


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


class ClinicStore(ABC):
    def __init__(self, clinic: dict):
        self.clinic = clinic
        self.tz = clinic_tz(clinic)
        self._doctors = {d["id"]: d for d in clinic["doctors"]}

    # ---- read-only clinic info --------------------------------------------------------
    def doctors(self) -> list[dict]:
        return [{"doctor_id": d["id"], "name": d["name"], "specialty": d["specialty"]} for d in self._doctors.values()]

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _slot_starts(self, doctor: dict, day: date) -> list[datetime]:
        spans = doctor["schedule"].get(_WEEKDAYS[day.weekday()], [])
        step = timedelta(minutes=doctor["slot_minutes"])
        starts = []
        for span in spans:
            a, b = (datetime.combine(day, time.fromisoformat(x), self.tz) for x in span.split("-"))
            while a + step <= b:
                starts.append(a)
                a += step
        return starts

    @staticmethod
    def slot_id(doctor_id: str, start: datetime) -> str:
        return f"{doctor_id}@{start.strftime('%Y-%m-%dT%H:%M')}"

    def parse_slot(self, slot_id: str) -> tuple[dict, datetime]:
        try:
            doctor_id, stamp = slot_id.split("@", 1)
            doctor = self._doctors[doctor_id]
            start = datetime.strptime(stamp, "%Y-%m-%dT%H:%M").replace(tzinfo=self.tz)
        except (ValueError, KeyError):
            raise StoreError(f"Unknown slot '{slot_id}'. Use find_available_slots to get valid slot ids.")
        if start not in self._slot_starts(doctor, start.date()):
            raise StoreError("That time is not in the doctor's schedule.")
        return doctor, start

    def available_slots(
        self, date_str: str, doctor_id: str | None = None, part_of_day: str | None = None, limit: int = 6
    ) -> list[dict]:
        try:
            day = date.fromisoformat(date_str)
        except ValueError:
            raise StoreError("date must be YYYY-MM-DD")
        if doctor_id and doctor_id not in self._doctors:
            raise StoreError(f"Unknown doctor_id '{doctor_id}'. Use list_doctors.")
        if day > self.now().date() + timedelta(days=60):
            raise StoreError("Bookings open 60 days in advance.")
        taken = self._taken_slot_ids(day)
        earliest = self.now() + timedelta(minutes=30)
        window = PARTS_OF_DAY.get(part_of_day or "", (time(0), time(23, 59)))
        out = []
        for doc in self._doctors.values():
            if doctor_id and doc["id"] != doctor_id:
                continue
            for start in self._slot_starts(doc, day):
                sid = self.slot_id(doc["id"], start)
                if start < earliest or sid in taken or not (window[0] <= start.time() < window[1]):
                    continue
                out.append({"slot_id": sid, "doctor": doc["name"], "start": start.strftime("%a %d %b, %I:%M %p")})
                if len(out) >= limit:
                    return out
        return out

    # ---- patient operations (ownership-checked) ----------------------------------------------
    def book(self, patient_ref: str, phone: str, slot_id: str, patient_name: str, reason_category: str) -> dict:
        if reason_category not in REASON_CATEGORIES:
            reason_category = "other"
        doctor, start = self.parse_slot(slot_id)
        if start < self.now():
            raise StoreError("That slot is in the past.")
        if slot_id in self._taken_slot_ids(start.date()):
            raise StoreError("Sorry, that slot was just taken. Please pick another.")
        upcoming = [a for a in self.list_appointments(patient_ref) if a["status"] == "booked"]
        if len(upcoming) >= 3:
            raise StoreError("You already have 3 upcoming appointments. Please cancel one or call the clinic.")
        self.upsert_patient(patient_ref, phone, patient_name)
        appt = {
            "id": new_id("APT"),
            "patient_ref": patient_ref,
            "doctor_id": doctor["id"],
            "doctor": doctor["name"],
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=doctor["slot_minutes"])).isoformat(),
            "slot_id": slot_id,
            "status": "booked",
            "reason_category": reason_category,
            "followups_sent": [],
            "created_at": self.now().isoformat(),
        }
        self._save_appointment(appt)
        return self._public(appt)

    def list_appointments(self, patient_ref: str, upcoming_only: bool = True) -> list[dict]:
        now = self.now()
        appts = [a for a in self._load_appointments() if a["patient_ref"] == patient_ref]
        if upcoming_only:
            appts = [a for a in appts if datetime.fromisoformat(a["start"]) >= now and a["status"] == "booked"]
        return [self._public(a) for a in sorted(appts, key=lambda a: a["start"])]

    def _owned(self, patient_ref: str, appointment_id: str) -> dict:
        appt = self._load_appointment(appointment_id)
        # Same error whether the appointment doesn't exist or belongs to someone else.
        if not appt or appt["patient_ref"] != patient_ref:
            raise StoreError("No appointment with that id on your number.")
        return appt

    def cancel(self, patient_ref: str, appointment_id: str) -> dict:
        appt = self._owned(patient_ref, appointment_id)
        if appt["status"] != "booked":
            raise StoreError(f"That appointment is already {appt['status']}.")
        appt["status"] = "cancelled"
        self._save_appointment(appt)
        return self._public(appt)

    def reschedule(self, patient_ref: str, appointment_id: str, new_slot_id: str) -> dict:
        old = self._owned(patient_ref, appointment_id)
        if old["status"] != "booked":
            raise StoreError(f"That appointment is already {old['status']}.")
        patient = self.get_patient(patient_ref) or {}
        # Free the old slot first (so the 3-booking cap doesn't block a move), restore it on failure.
        old["status"] = "cancelled"
        self._save_appointment(old)
        try:
            return self.book(patient_ref, patient.get("phone", ""), new_slot_id, patient.get("name", ""), old["reason_category"])
        except StoreError:
            old["status"] = "booked"
            self._save_appointment(old)
            raise

    # ---- staff / automation operations -------------------------------------------------------
    def log_visit(self, appointment_id: str, status: str) -> dict:
        if status not in ("completed", "no_show"):
            raise StoreError("status must be completed or no_show")
        appt = self._load_appointment(appointment_id)
        if not appt:
            raise StoreError("Unknown appointment")
        appt["status"] = status
        appt["visit_logged_at"] = self.now().isoformat()
        self._save_appointment(appt)
        return self._public(appt)

    def appointments_between(self, start: datetime, end: datetime, status: str) -> list[dict]:
        return [
            a for a in self._load_appointments()
            if a["status"] == status and start <= datetime.fromisoformat(a["start"]) < end
        ]

    def get_appointment(self, appointment_id: str) -> dict | None:
        """Staff/automation lookup (no ownership check). Never exposed to the model."""
        return self._load_appointment(appointment_id)

    def mark_followup_sent(self, appointment_id: str, kind: str) -> None:
        appt = self._load_appointment(appointment_id)
        if appt and kind not in appt["followups_sent"]:
            appt["followups_sent"].append(kind)
            self._save_appointment(appt)

    @staticmethod
    def _public(appt: dict) -> dict:
        start = datetime.fromisoformat(appt["start"])
        return {
            "appointment_id": appt["id"],
            "doctor": appt["doctor"],
            "when": start.strftime("%a %d %b %Y, %I:%M %p"),
            "status": appt["status"],
        }

    # ---- persistence API for backends -------------------------------------------------------
    def _taken_slot_ids(self, day: date) -> set[str]:
        return {
            a["slot_id"] for a in self._load_appointments()
            if a["status"] == "booked" and datetime.fromisoformat(a["start"]).date() == day
        }

    @abstractmethod
    def _load_appointments(self) -> list[dict]: ...
    @abstractmethod
    def _load_appointment(self, appointment_id: str) -> dict | None: ...
    @abstractmethod
    def _save_appointment(self, appt: dict) -> None: ...
    @abstractmethod
    def upsert_patient(self, patient_ref: str, phone: str, name: str | None = None) -> None: ...
    @abstractmethod
    def get_patient(self, patient_ref: str) -> dict | None: ...
    @abstractmethod
    def set_opt_out(self, patient_ref: str, opted_out: bool) -> None: ...
    @abstractmethod
    def create_handoff(self, patient_ref: str, category: str, priority: str, summary: str) -> str: ...
    @abstractmethod
    def open_handoffs(self, patient_ref: str) -> list[dict]: ...
