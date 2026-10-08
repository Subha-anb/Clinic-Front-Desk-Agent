"""The front-desk agent: one inbound WhatsApp message in, one reply out.

Pipeline
  1. identity    phone -> pseudonymous patient_ref; rate limit
  2. guard (in)  rules -> Claude classifier -> most restrictive verdict wins
  3. route       emergency / escalate / refuse / opt-out -> fixed templates + staff handoff
                 booking -> Claude tool loop over MCP tools (or deterministic offline flow)
                 logistics -> RAG answer grounded on clinic documents
  4. guard (out) every model-written reply is checked; failures become a staff handoff
  5. audit       redacted decision log
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Awaitable, Callable

from clinic_store import ClinicStore, StoreError
from mcp_server.tools import PATIENT_TOOL_SPECS

from .config import Settings
from .guardrails import Action, Category, Verdict, check_output, classify_rules
from .guardrails.policy import (
    emergency_message,
    escalation_message,
    most_restrictive,
    opt_out_message,
    privacy_message,
    refusal_message,
)
from .rag import KnowledgeBase
from .security.audit import AuditLog, RateLimiter
from .security.pii import patient_ref, redact
from .tool_gateway import ToolGateway

MAX_MESSAGE_CHARS = 1000
MIN_RETRIEVAL_SCORE = 2.0
HISTORY_TURNS = 6
HISTORY_TTL_SECONDS = 30 * 60

_WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_SERVICE_HINTS = {
    "dr-iyer": ("child", "children", "kid", "son", "daughter", "baby", "paediatric", "pediatric"),
    "dr-mehta": ("diabetes", "sugar", "thyroid", "endocrin"),
    "lab": ("blood test", "lab", "urine"),
    "imaging": ("scan", "ultrasound", "x-ray", "xray"),
}


def _match_doctor(lower: str, doctors: list[dict]) -> str:
    """Offline doctor picker: explicit name first, then service keywords, else any doctor."""
    for d in doctors:
        if d["name"].split()[-1].lower() in lower or d["doctor_id"] in lower:
            return d["doctor_id"]
    for doctor_id, hints in _SERVICE_HINTS.items():
        if any(h in lower for h in hints):
            return doctor_id
    return "any"


Notifier = Callable[[str, str, str], Awaitable[None]]  # (handoff_id, category, priority)


@dataclass
class AgentReply:
    text: str
    action: Action
    category: Category
    handoff_id: str | None = None
    reasons: list[str] = field(default_factory=list)


class _Sessions:
    """Short-lived, redacted conversation memory for logistics/booking turns only."""

    def __init__(self):
        self._data: dict[str, tuple[float, deque]] = {}

    def get(self, ref: str) -> list[dict]:
        ts, turns = self._data.get(ref, (0.0, deque()))
        if time.time() - ts > HISTORY_TTL_SECONDS:
            self._data.pop(ref, None)
            return []
        return list(turns)

    def add(self, ref: str, user: str, assistant: str) -> None:
        turns = deque(self.get(ref), maxlen=HISTORY_TURNS * 2)
        turns.extend([{"role": "user", "content": redact(user)}, {"role": "assistant", "content": assistant}])
        self._data[ref] = (time.time(), turns)

    def clear(self, ref: str) -> None:
        self._data.pop(ref, None)


class FrontDeskAgent:
    def __init__(
        self,
        settings: Settings,
        kb: KnowledgeBase,
        store: ClinicStore,
        gateway: ToolGateway,
        audit: AuditLog,
        llm=None,
        notifier: Notifier | None = None,
        limiter: RateLimiter | None = None,
    ):
        self.settings = settings
        self.clinic = settings.clinic
        self.kb = kb
        self.store = store
        self.gateway = gateway
        self.audit = audit
        self.llm = llm
        self.notifier = notifier
        self.limiter = limiter or RateLimiter()
        self.sessions = _Sessions()

    # ------------------------------------------------------------------------------------------
    async def handle(self, phone: str, text: str, profile_name: str | None = None) -> AgentReply:
        ref = patient_ref(phone, self.settings.pii_pepper)
        text = (text or "").strip()[:MAX_MESSAGE_CHARS]

        if not self.limiter.allow(ref, time.time()):
            return self._log(ref, text, AgentReply(
                f"You've sent a lot of messages in a short time. Please call us on {self.clinic['phone']} if you need help now.",
                Action.REFUSE, Category.INJECTION, reasons=["rate limited"],
            ))
        if re.fullmatch(r"\s*start\s*", text.lower()):
            self.store.set_opt_out(ref, False)
            return self._log(ref, text, AgentReply("Reminders are back on. How can I help?", Action.ANSWER, Category.LOGISTICS))

        verdict = await self._classify(text)
        action = verdict.action

        if action is Action.EMERGENCY:
            hid = await self._handoff(ref, text, verdict, priority="urgent")
            reply = AgentReply(emergency_message(self.clinic), action, verdict.category, hid, verdict.reasons)
        elif action is Action.ESCALATE:
            hid = await self._handoff(ref, text, verdict, priority="normal")
            msg = privacy_message if verdict.category is Category.PRIVACY else escalation_message
            reply = AgentReply(msg(self.clinic, hid), action, verdict.category, hid, verdict.reasons)
        elif action is Action.REFUSE:
            reply = AgentReply(refusal_message(), action, verdict.category, reasons=verdict.reasons)
        elif action is Action.OPT_OUT:
            self.store.set_opt_out(ref, True)
            reply = AgentReply(opt_out_message(), action, verdict.category, reasons=verdict.reasons)
        elif action is Action.BOOK:
            reply = await self._book(ref, phone, text, verdict, profile_name)
        else:
            reply = await self._answer(ref, text, verdict)

        if reply.action in (Action.ANSWER, Action.BOOK):
            self.sessions.add(ref, text, reply.text)
        else:
            # Don't carry clinical content into later LLM context.
            self.sessions.clear(ref)
        return self._log(ref, text, reply)

    # ------------------------------------------------------------------------------------------
    async def _classify(self, text: str) -> Verdict:
        verdict = classify_rules(text)
        if self.llm and verdict.category not in (Category.EMERGENCY, Category.OPT_OUT):
            verdict = most_restrictive(verdict, await self.llm.classify(redact(text)))
        return verdict

    async def _handoff(self, ref: str, text: str, verdict: Verdict, priority: str) -> str:
        hid = await self.gateway.create_handoff(ref, verdict.category.value, priority, redact(text)[:500])
        if self.notifier:
            try:
                await self.notifier(hid, verdict.category.value, priority)
            except Exception:  # the ticket exists; a failed alert must not break the reply
                self.audit.record("notify_failed", ref, handoff_id=hid)
        return hid

    async def _guarded(self, ref: str, text: str, verdict: Verdict, candidate: str, grounding: str) -> AgentReply:
        """Run the output guard; anything unsafe or empty becomes a staff handoff."""
        out = check_output(candidate, grounding, allowed_contacts=[self.clinic["phone"]])
        if candidate and out.ok:
            return AgentReply(candidate, verdict.action, verdict.category, reasons=verdict.reasons)
        blocked = Verdict(Category.CLINICAL, verdict.reasons + ["output guard: " + "; ".join(out.reasons or ["empty"])], "output")
        hid = await self._handoff(ref, text, blocked, priority="normal")
        return AgentReply(escalation_message(self.clinic, hid), Action.ESCALATE, Category.CLINICAL, hid, blocked.reasons)

    # ---- logistics: RAG ------------------------------------------------------------------------
    async def _answer(self, ref: str, text: str, verdict: Verdict) -> AgentReply:
        hits = self.kb.search(text, k=3)
        if not hits or hits[0][1] < MIN_RETRIEVAL_SCORE:
            hid = await self.gateway.create_handoff(ref, "logistics_unanswered", "low", redact(text)[:500])
            return AgentReply(
                f"I don't have that information, so I've asked our front desk to reply (reference {hid}). "
                f"You can also call us on {self.clinic['phone']}.",
                Action.ESCALATE, verdict.category, hid, verdict.reasons + ["no grounding"],
            )
        documents = "\n\n".join(c.render() for c, _ in hits)
        if not self.llm:
            # Offline: quote the approved text verbatim (top chunk, plus close runners-up for multi-part questions).
            best = hits[0][1]
            parts = [f"{c.title}: {c.text}" for c, s in hits if s >= 0.8 * best]
            return AgentReply("\n\n".join(parts), Action.ANSWER, verdict.category, reasons=verdict.reasons)

        result = await self.llm.answer(redact(text), documents, self.sessions.get(ref))
        if result.get("needs_staff") or not result.get("grounded"):
            verdict = Verdict(Category.CLINICAL if result.get("needs_staff") else verdict.category,
                              verdict.reasons + ["llm: needs staff / ungrounded"], verdict.source)
            return await self._guarded(ref, text, verdict, "", documents)
        return await self._guarded(ref, text, verdict, result["answer"], documents)

    # ---- booking -------------------------------------------------------------------------------
    async def _book(self, ref: str, phone: str, text: str, verdict: Verdict, profile_name: str | None) -> AgentReply:
        if verdict.staff_review:
            # Booking is fine, but a symptom was mentioned: let a nurse glance at it.
            await self._handoff(ref, text, Verdict(Category.BOOKING, verdict.reasons), priority="low")
        if not self.llm:
            return await self._book_offline(ref, phone, text, verdict, profile_name)

        tool_log: list[str] = []

        async def call_tool(name: str, args: dict):
            result = await self.gateway.call_patient_tool(name, args, ref, phone)
            tool_log.append(json.dumps(result, default=str))
            return result

        now = self.store.now()
        reply = await self.llm.run_booking(
            redact(text), self.sessions.get(ref), PATIENT_TOOL_SPECS, call_tool, today=now.strftime("%A %Y-%m-%d"),
        )
        grounding = "\n".join(tool_log) + "\n" + json.dumps(self.store.doctors())
        return await self._guarded(ref, text, verdict, reply or "", grounding)

    async def _book_offline(self, ref: str, phone: str, text: str, verdict: Verdict, profile_name: str | None) -> AgentReply:
        """Deterministic booking flow used when no LLM is configured (and in tests)."""
        lower = text.lower()

        def ok(msg: str) -> AgentReply:
            return AgentReply(msg, Action.BOOK, verdict.category, reasons=verdict.reasons)

        try:
            if m := re.search(r"\bbook\s+([\w-]+@[\d\-T:]+)", text, re.I):
                patient = self.store.get_patient(ref) or {}
                appt = await self.gateway.call_patient_tool(
                    "book_appointment",
                    {"slot_id": m.group(1), "patient_name": patient.get("name") or profile_name or "WhatsApp patient", "reason_category": "general_consultation"},
                    ref, phone,
                )
                return ok(f"Booked: {appt['doctor']}, {appt['when']}. Your appointment id is {appt['appointment_id']}. "
                          "Reply CANCEL <id> to cancel.")
            if m := re.search(r"\bcancel\s+(APT-[0-9A-F]+)", text, re.I):
                appt = await self.gateway.call_patient_tool("cancel_appointment", {"appointment_id": m.group(1).upper()}, ref, phone)
                return ok(f"Cancelled your appointment with {appt['doctor']} on {appt['when']}.")
            if "my appointment" in lower or "cancel" in lower or "reschedul" in lower:
                appts = await self.gateway.call_patient_tool("list_my_appointments", {}, ref, phone)
                if not appts:
                    return ok("You have no upcoming appointments.")
                lines = [f"{a['appointment_id']}: {a['doctor']}, {a['when']}" for a in appts]
                return ok("Your upcoming appointments:\n" + "\n".join(lines) +
                          "\nReply CANCEL <id> to cancel, then book a new slot to reschedule.")
        except StoreError as exc:
            return ok(str(exc))

        doctor_id = _match_doctor(lower, self.store.doctors())
        part = next((p for p in ("morning", "afternoon", "evening") if p in lower), "any")
        today = self.store.now().date()
        start = today + timedelta(days=1) if "tomorrow" in lower else today
        weekday = next((i for i, d in enumerate(_WEEKDAY_NAMES) if re.search(rf"\b{d}", lower)), None)
        slots: list[dict] = []
        for i in range(14):
            day = start + timedelta(days=i)
            if weekday is not None and day.weekday() != weekday:
                continue
            slots += await self.gateway.call_patient_tool(
                "find_available_slots", {"date": day.isoformat(), "doctor_id": doctor_id, "part_of_day": part}, ref, phone)
            if len(slots) >= 3:
                break
        if not slots:
            return ok(f"I couldn't find a free slot in the next two weeks. Please call us on {self.clinic['phone']}.")
        lines = [f"{s['doctor']}, {s['start']} -> reply BOOK {s['slot_id']}" for s in slots[:3]]
        return ok("Here are the next available slots:\n" + "\n".join(lines))

    # ------------------------------------------------------------------------------------------
    def _log(self, ref: str, text: str, reply: AgentReply) -> AgentReply:
        self.audit.record(
            "message", ref, message=text, action=reply.action.value, category=reply.category.value,
            reasons=[redact(r) for r in reply.reasons], handoff_id=reply.handoff_id,
        )
        return reply
