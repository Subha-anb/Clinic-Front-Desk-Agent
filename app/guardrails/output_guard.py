"""Layer 3 of the guardrail: inspect every LLM-written reply before it reaches the patient.

Anything that looks like medical advice, a dose, reassurance or a diagnosis is blocked unless
that exact wording is in the clinic-approved source the answer was grounded on (for example,
"Plain water is allowed" comes straight from the pre-visit instructions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..security.pii import _PATTERNS as PII_PATTERNS
from .input_guard import MEDICATION, normalize

DOSE = re.compile(r"\b\d+(\.\d+)?\s?(mg|mcg|ml|g|iu|units?|tablets?|tabs?|pills?|capsules?|drops|puffs)\b")
ADVICE = re.compile(
    "|".join(
        [
            r"\byou (should|must|can|could|may) (take|stop|skip|continue|increase|reduce|double|halve|avoid|use|start)\b",
            r"\b(take|stop|skip|continue|increase|reduce) (your|the|this|that) (medicine|medication|tablet|dose|pill)",
            r"\bit('s| is) (safe|fine|okay|ok|normal|nothing serious|not serious|harmless)\b",
            r"\b(nothing|no need) to worry\b", r"\bdon'?t worry\b", r"\bsounds like\b",
            r"\byou (may|might|probably|likely|could) have\b", r"\b(diagnos\w*|symptoms? of)\b",
            r"\bi (recommend|suggest|advise)\b", r"\bshould (go away|settle|resolve|heal)\b",
            r"\b(is|are) (within|in) (the )?normal range\b", r"\bside effects?\b",
        ]
    )
)


@dataclass
class OutputVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_output(answer: str, grounding: str, allowed_contacts: list[str] | None = None) -> OutputVerdict:
    """Block replies with medical content that isn't verbatim from the approved grounding text."""
    text, _ = normalize(answer)
    source, _ = normalize(grounding)
    reasons: list[str] = []

    for rx, label in ((DOSE, "dose"), (ADVICE, "medical advice"), (MEDICATION, "medication")):
        for m in rx.finditer(text):
            if m.group(0) not in source:
                reasons.append(f"{label}: '{m.group(0)}'")

    # Never echo identifiers other than the clinic's own published contact details.
    allowed = {re.sub(r"\D", "", c) for c in (allowed_contacts or [])}
    for label, rx in PII_PATTERNS:
        for m in rx.finditer(answer):
            if re.sub(r"\D", "", m.group(0)) not in allowed:
                reasons.append(f"pii leak: {label}")

    return OutputVerdict(ok=not reasons, reasons=reasons)
