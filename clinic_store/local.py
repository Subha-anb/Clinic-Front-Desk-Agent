"""JSON-file backend for local development and tests. Not for production patient data."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .base import ClinicStore, new_id


class LocalStore(ClinicStore):
    def __init__(self, clinic: dict, path: Path):
        super().__init__(clinic)
        self.path = path
        self._lock = threading.RLock()
        if not path.exists():
            self._write({"patients": {}, "appointments": {}, "handoffs": {}})

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _load_appointments(self) -> list[dict]:
        with self._lock:
            return list(self._read()["appointments"].values())

    def _load_appointment(self, appointment_id: str) -> dict | None:
        with self._lock:
            return self._read()["appointments"].get(appointment_id)

    def _save_appointment(self, appt: dict) -> None:
        with self._lock:
            data = self._read()
            data["appointments"][appt["id"]] = appt
            self._write(data)

    def upsert_patient(self, patient_ref: str, phone: str, name: str | None = None) -> None:
        with self._lock:
            data = self._read()
            p = data["patients"].setdefault(patient_ref, {"phone": phone, "name": None, "opted_out": False})
            if phone:
                p["phone"] = phone
            if name:
                p["name"] = name
            self._write(data)

    def get_patient(self, patient_ref: str) -> dict | None:
        with self._lock:
            return self._read()["patients"].get(patient_ref)

    def set_opt_out(self, patient_ref: str, opted_out: bool) -> None:
        with self._lock:
            data = self._read()
            data["patients"].setdefault(patient_ref, {"phone": "", "name": None, "opted_out": False})["opted_out"] = opted_out
            self._write(data)

    def create_handoff(self, patient_ref: str, category: str, priority: str, summary: str) -> str:
        with self._lock:
            data = self._read()
            hid = new_id("HND")
            data["handoffs"][hid] = {
                "id": hid, "patient_ref": patient_ref, "category": category, "priority": priority,
                "summary": summary, "status": "open", "created_at": self.now().isoformat(),
            }
            self._write(data)
            return hid

    def open_handoffs(self, patient_ref: str) -> list[dict]:
        with self._lock:
            return [h for h in self._read()["handoffs"].values() if h["patient_ref"] == patient_ref and h["status"] == "open"]
