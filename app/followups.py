"""Follow-up selection logic. n8n decides *when* to run; this module decides *who* is due.

Keeping the selection here (not in n8n) means opt-outs, open clinical handoffs and
"already sent" checks are enforced in one tested place, and n8n never handles phone numbers.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from clinic_store import ClinicStore

KINDS = ("reminder", "checkin", "review")

# WhatsApp template names (must be pre-approved in Meta Business Manager).
TEMPLATES = {
    "reminder": "appointment_reminder",   # {{1}} name, {{2}} doctor, {{3}} when, {{4}} prep link
    "checkin": "post_visit_checkin",       # {{1}} name, {{2}} doctor
    "review": "review_request",            # {{1}} name, {{2}} review url
}


def due(store: ClinicStore, kind: str, now: datetime | None = None) -> list[dict]:
    now = now or store.now()
    if kind == "reminder":
        tomorrow = now.date() + timedelta(days=1)
        start = datetime.combine(tomorrow, time(0), store.tz)
        candidates = store.appointments_between(start, start + timedelta(days=1), "booked")
    elif kind == "checkin":
        candidates = store.appointments_between(now - timedelta(hours=36), now - timedelta(hours=18), "completed")
    elif kind == "review":
        candidates = [
            a for a in store.appointments_between(now - timedelta(hours=96), now - timedelta(hours=60), "completed")
            # Never ask for a review from someone with an open clinical question or a bad check-in.
            if "checkin" in a["followups_sent"] and not store.open_handoffs(a["patient_ref"])
        ]
    else:
        raise ValueError(f"kind must be one of {KINDS}")

    out = []
    for a in candidates:
        patient = store.get_patient(a["patient_ref"]) or {}
        if kind in a["followups_sent"] or patient.get("opted_out") or not patient.get("phone"):
            continue
        out.append({"appointment_id": a["id"], "kind": kind})
    return out


def template_params(store: ClinicStore, appt: dict, kind: str) -> list[str]:
    patient = store.get_patient(appt["patient_ref"]) or {}
    name = (patient.get("name") or "there").split()[0]
    when = datetime.fromisoformat(appt["start"]).strftime("%a %d %b, %I:%M %p")
    if kind == "reminder":
        return [name, appt["doctor"], when, "Reply here if you need to reschedule."]
    if kind == "checkin":
        return [name, appt["doctor"]]
    return [name, store.clinic["review_url"]]
