"""Google Calendar + Google Sheets backend.

* Calendar holds appointments. Event titles contain only the doctor and a short patient ref,
  never the patient's name, phone or reason, so anyone with calendar access sees minimal data.
* The Sheet (restricted to front-desk staff) holds three tabs:
    Patients  : patient_ref | phone | name | opted_out
    Visits    : appointment_id | patient_ref | doctor | start | status | reason_category | followups_sent | updated_at
    Handoffs  : handoff_id | patient_ref | category | priority | summary | status | created_at
Authentication is a service account with access to only this calendar and this sheet.
"""

from __future__ import annotations

import json
from datetime import timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .base import ClinicStore, new_id

SCOPES = ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/spreadsheets"]
TAG = "clinic-agent"


class GoogleStore(ClinicStore):
    def __init__(self, clinic: dict, cfg: dict):
        super().__init__(clinic)
        creds = service_account.Credentials.from_service_account_file(cfg["service_account_file"], scopes=SCOPES)
        self.cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        self.calendar_id = cfg["calendar_id"]
        self.sheet_id = cfg["sheet_id"]

    # ---- Calendar: appointments ---------------------------------------------------------------
    def _events(self, **filters) -> list[dict]:
        now = self.now()
        items, token = [], None
        while True:
            resp = self.cal.events().list(
                calendarId=self.calendar_id,
                privateExtendedProperty=[f"app={TAG}", *[f"{k}={v}" for k, v in filters.items()]],
                timeMin=(now - timedelta(days=14)).isoformat(),
                timeMax=(now + timedelta(days=61)).isoformat(),
                singleEvents=True, showDeleted=False, pageToken=token, maxResults=250,
            ).execute()
            items.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                return items

    @staticmethod
    def _appt(event: dict) -> dict:
        return json.loads(event["extendedProperties"]["private"]["appt"])

    def _load_appointments(self) -> list[dict]:
        return [self._appt(e) for e in self._events()]

    def _load_appointment(self, appointment_id: str) -> dict | None:
        events = self._events(appt_id=appointment_id)
        return self._appt(events[0]) if events else None

    def _save_appointment(self, appt: dict) -> None:
        cancelled = appt["status"] == "cancelled"
        body = {
            "summary": f"{'[CANCELLED] ' if cancelled else ''}{appt['doctor']} - patient {appt['patient_ref'][-6:]}",
            "start": {"dateTime": appt["start"], "timeZone": self.clinic["timezone"]},
            "end": {"dateTime": appt["end"], "timeZone": self.clinic["timezone"]},
            "transparency": "transparent" if cancelled else "opaque",
            "extendedProperties": {"private": {"app": TAG, "appt_id": appt["id"], "appt": json.dumps(appt)}},
        }
        existing = self._events(appt_id=appt["id"])
        if existing:
            self.cal.events().update(calendarId=self.calendar_id, eventId=existing[0]["id"], body=body).execute()
        else:
            self.cal.events().insert(calendarId=self.calendar_id, body=body).execute()
        self._upsert_row("Visits", appt["id"], [
            appt["id"], appt["patient_ref"], appt["doctor"], appt["start"], appt["status"],
            appt["reason_category"], ",".join(appt["followups_sent"]), self.now().isoformat(),
        ])

    # ---- Sheets helpers ------------------------------------------------------------------------
    def _rows(self, tab: str) -> list[list[str]]:
        resp = self.sheets.values().get(spreadsheetId=self.sheet_id, range=f"{tab}!A2:H").execute()
        return resp.get("values", [])

    def _upsert_row(self, tab: str, key: str, row: list) -> None:
        for i, existing in enumerate(self._rows(tab), start=2):
            if existing and existing[0] == key:
                self.sheets.values().update(
                    spreadsheetId=self.sheet_id, range=f"{tab}!A{i}", valueInputOption="RAW", body={"values": [row]}
                ).execute()
                return
        self.sheets.values().append(
            spreadsheetId=self.sheet_id, range=f"{tab}!A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row]},
        ).execute()

    # ---- Patients, opt-outs, handoffs -----------------------------------------------------------
    def get_patient(self, patient_ref: str) -> dict | None:
        for r in self._rows("Patients"):
            if r and r[0] == patient_ref:
                r = r + [""] * (4 - len(r))
                return {"phone": r[1], "name": r[2] or None, "opted_out": r[3] == "TRUE"}
        return None

    def upsert_patient(self, patient_ref: str, phone: str, name: str | None = None) -> None:
        current = self.get_patient(patient_ref) or {"phone": "", "name": None, "opted_out": False}
        self._upsert_row("Patients", patient_ref, [
            patient_ref, phone or current["phone"], name or current["name"] or "", "TRUE" if current["opted_out"] else "FALSE",
        ])

    def set_opt_out(self, patient_ref: str, opted_out: bool) -> None:
        current = self.get_patient(patient_ref) or {"phone": "", "name": None}
        self._upsert_row("Patients", patient_ref, [patient_ref, current["phone"], current["name"] or "", "TRUE" if opted_out else "FALSE"])

    def create_handoff(self, patient_ref: str, category: str, priority: str, summary: str) -> str:
        hid = new_id("HND")
        self._upsert_row("Handoffs", hid, [hid, patient_ref, category, priority, summary, "open", self.now().isoformat()])
        return hid

    def open_handoffs(self, patient_ref: str) -> list[dict]:
        return [
            {"id": r[0], "category": r[2], "status": r[5]}
            for r in self._rows("Handoffs") if len(r) > 5 and r[1] == patient_ref and r[5] == "open"
        ]
