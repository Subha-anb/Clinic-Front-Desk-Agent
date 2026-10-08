"""Guardrail policy: categories, the action each one maps to, and the fixed patient-facing messages.

Escalation and emergency replies are fixed templates, never LLM-generated, so the wording
that matters most can't drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Category(str, Enum):
    EMERGENCY = "emergency"      # possible life-threatening situation or crisis
    PRIVACY = "privacy"          # asks for someone else's records / data
    CLINICAL = "clinical"        # medicines, symptoms, results, treatment: staff only
    INJECTION = "injection"      # tries to change the assistant's rules, no clinical content
    OPT_OUT = "opt_out"
    BOOKING = "booking"
    LOGISTICS = "logistics"


class Action(str, Enum):
    EMERGENCY = "emergency"
    ESCALATE = "escalate"
    REFUSE = "refuse"
    OPT_OUT = "opt_out"
    BOOK = "book"
    ANSWER = "answer"


# Most restrictive first. When layers disagree, the earliest category in this list wins.
SEVERITY = [
    Category.EMERGENCY,
    Category.PRIVACY,
    Category.CLINICAL,
    Category.INJECTION,
    Category.OPT_OUT,
    Category.BOOKING,
    Category.LOGISTICS,
]

ACTION_FOR = {
    Category.EMERGENCY: Action.EMERGENCY,
    Category.PRIVACY: Action.ESCALATE,
    Category.CLINICAL: Action.ESCALATE,
    Category.INJECTION: Action.REFUSE,
    Category.OPT_OUT: Action.OPT_OUT,
    Category.BOOKING: Action.BOOK,
    Category.LOGISTICS: Action.ANSWER,
}


@dataclass
class Verdict:
    category: Category
    reasons: list[str] = field(default_factory=list)
    source: str = "rules"
    # Booking request that mentions a symptom: book a slot, but never comment on the symptom,
    # and flag it for staff review.
    staff_review: bool = False

    @property
    def action(self) -> Action:
        return ACTION_FOR[self.category]


def most_restrictive(*verdicts: Verdict) -> Verdict:
    ordered = sorted(verdicts, key=lambda v: SEVERITY.index(v.category))
    winner = ordered[0]
    return Verdict(
        category=winner.category,
        reasons=[r for v in verdicts for r in v.reasons],
        source="+".join(v.source for v in verdicts),
        staff_review=any(v.staff_review for v in verdicts),
    )


def emergency_message(clinic: dict) -> str:
    e = clinic["emergency"]
    return (
        "This sounds like it could be an emergency. Please call "
        f"{e['general']} or an ambulance on {e['ambulance']} right now, or go to the nearest "
        "emergency department. Do not wait for a reply on WhatsApp.\n\n"
        f"If you are struggling emotionally or thinking about harming yourself, you can talk to "
        f"someone now at {e['mental_health']} (free, 24x7).\n\n"
        "I have also alerted our clinic staff."
    )


def escalation_message(clinic: dict, ticket_id: str) -> str:
    return (
        "I'm the clinic's front-desk assistant, so I can't advise on medicines, symptoms, "
        "test results or treatment. I've passed your question to our nursing team "
        f"(reference {ticket_id}). They will reply on this chat {clinic['staff_response_time']}.\n\n"
        f"Until then, please don't start, stop or change any medicine on your own. If it is "
        f"urgent, call us on {clinic['phone']}, or {clinic['emergency']['general']} in an emergency."
    )


def privacy_message(clinic: dict, ticket_id: str) -> str:
    return (
        "For privacy reasons I can only discuss your own appointments here, and I can't share "
        "anyone else's records, reports or prescriptions. I've asked our front-desk team to "
        f"help (reference {ticket_id}). They may need the patient's written authorisation. "
        f"You can also call us on {clinic['phone']}."
    )


def refusal_message() -> str:
    return (
        "I can only help with appointments, clinic timings, fees, directions and how to prepare "
        "for your visit. What can I help you with?"
    )


def opt_out_message() -> str:
    return "You won't receive reminders or follow-up messages from us any more. Reply START to turn them back on."
