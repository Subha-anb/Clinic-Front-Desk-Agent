"""Tool definitions shared by the MCP server and the in-process gateway.

PATIENT_TOOL_SPECS is what Claude sees. Patient identity (patient_ref, phone) is never in these
schemas: the gateway injects it server-side, so the model cannot act on another patient's data.
"""

from __future__ import annotations

from clinic_store.base import PARTS_OF_DAY, REASON_CATEGORIES, ClinicStore


def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


PATIENT_TOOL_SPECS = [
    _spec("list_doctors", "List the clinic's doctors and services with their ids and specialties.", {}, []),
    _spec(
        "find_available_slots",
        "Find open appointment slots on a date. Returns slot_ids to use with book_appointment.",
        {
            "date": {"type": "string", "description": "YYYY-MM-DD in the clinic's timezone"},
            "doctor_id": {"type": "string", "description": "Doctor id from list_doctors, or \"any\""},
            "part_of_day": {"type": "string", "enum": ["any", *PARTS_OF_DAY]},
        },
        ["date", "doctor_id", "part_of_day"],
    ),
    _spec(
        "book_appointment",
        "Book a slot for the patient on this WhatsApp number.",
        {
            "slot_id": {"type": "string"},
            "patient_name": {"type": "string", "description": "Name of the person being seen"},
            "reason_category": {"type": "string", "enum": REASON_CATEGORIES},
        },
        ["slot_id", "patient_name", "reason_category"],
    ),
    _spec("list_my_appointments", "List this patient's upcoming appointments.", {}, []),
    _spec(
        "cancel_appointment",
        "Cancel one of this patient's appointments.",
        {"appointment_id": {"type": "string"}},
        ["appointment_id"],
    ),
    _spec(
        "reschedule_appointment",
        "Move one of this patient's appointments to a new slot.",
        {"appointment_id": {"type": "string"}, "new_slot_id": {"type": "string"}},
        ["appointment_id", "new_slot_id"],
    ),
]
PATIENT_TOOLS = {s["name"] for s in PATIENT_TOOL_SPECS}


def run_patient_tool(store: ClinicStore, name: str, args: dict, patient_ref: str, phone: str):
    """Execute a patient tool with identity supplied by the server, not the model."""
    if name == "list_doctors":
        return store.doctors()
    if name == "find_available_slots":
        doctor_id = args.get("doctor_id") or "any"
        part = args.get("part_of_day") or "any"
        return store.available_slots(
            args["date"], None if doctor_id == "any" else doctor_id, None if part == "any" else part
        )
    if name == "book_appointment":
        return store.book(patient_ref, phone, args["slot_id"], args["patient_name"], args["reason_category"])
    if name == "list_my_appointments":
        return store.list_appointments(patient_ref)
    if name == "cancel_appointment":
        return store.cancel(patient_ref, args["appointment_id"])
    if name == "reschedule_appointment":
        return store.reschedule(patient_ref, args["appointment_id"], args["new_slot_id"])
    raise ValueError(f"Tool '{name}' is not available to patients")
