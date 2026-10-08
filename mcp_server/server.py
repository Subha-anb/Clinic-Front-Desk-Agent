"""MCP server exposing clinic booking and visit-logging tools over Google Calendar / Sheets.

Run: python -m mcp_server.server   (stdio transport)

It is launched as a child process by the agent and is not exposed on the network. Patient tools
take patient_ref/patient_phone, which the agent's gateway fills in from the verified WhatsApp
sender, never from model output.
"""

from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.config import get_settings
from clinic_store import StoreError, make_store

from .tools import run_patient_tool

mcp = MCPServer("clinic-front-desk")
store = make_store(get_settings())


def business_errors(fn):
    """Surface rule violations ("slot taken") to the model; anything else stays a hidden crash."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoreError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


@mcp.tool()
@business_errors
def list_doctors() -> list[dict]:
    """List the clinic's doctors and services."""
    return store.doctors()


@mcp.tool()
@business_errors
def find_available_slots(date: str, doctor_id: str = "any", part_of_day: str = "any") -> list[dict]:
    """Find open slots on a date (YYYY-MM-DD). part_of_day: any | morning | afternoon | evening."""
    args = {"date": date, "doctor_id": doctor_id, "part_of_day": part_of_day}
    return run_patient_tool(store, "find_available_slots", args, "", "")


@mcp.tool()
@business_errors
def book_appointment(patient_ref: str, patient_phone: str, slot_id: str, patient_name: str, reason_category: str) -> dict:
    """Book a slot for a patient (identity injected by the agent)."""
    return store.book(patient_ref, patient_phone, slot_id, patient_name, reason_category)


@mcp.tool()
@business_errors
def list_my_appointments(patient_ref: str, patient_phone: str) -> list[dict]:
    """List a patient's upcoming appointments."""
    return store.list_appointments(patient_ref)


@mcp.tool()
@business_errors
def cancel_appointment(patient_ref: str, patient_phone: str, appointment_id: str) -> dict:
    """Cancel one of the patient's own appointments."""
    return store.cancel(patient_ref, appointment_id)


@mcp.tool()
@business_errors
def reschedule_appointment(patient_ref: str, patient_phone: str, appointment_id: str, new_slot_id: str) -> dict:
    """Move one of the patient's own appointments to a new slot."""
    return store.reschedule(patient_ref, appointment_id, new_slot_id)


@mcp.tool()
@business_errors
def log_visit(appointment_id: str, status: str) -> dict:
    """Staff/automation: mark a visit completed or no_show (writes the Visits sheet)."""
    return store.log_visit(appointment_id, status)


@mcp.tool()
@business_errors
def create_staff_handoff(patient_ref: str, category: str, priority: str, summary: str) -> dict:
    """Agent-only: open a staff ticket for a clinical, privacy or emergency message."""
    return {"handoff_id": store.create_handoff(patient_ref, category, priority, summary)}


if __name__ == "__main__":
    mcp.run()
