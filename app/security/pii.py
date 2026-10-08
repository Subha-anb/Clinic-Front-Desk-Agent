"""PII handling: redaction for logs and LLM prompts, and pseudonymous patient references.

Principle: the LLM and the logs never need a patient's phone number, email or ID numbers.
The phone number is replaced by a keyed hash (patient_ref) at the edge; only the store
that actually sends WhatsApp messages keeps the mapping back to the phone number.
"""

from __future__ import annotations

import hashlib
import hmac
import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # Aadhaar: 12 digits, optionally grouped 4-4-4
    ("AADHAAR", re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    # Phone numbers: +91 98765 43210, 098765-43210, 9876543210 ...
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d{1,3}[ -]?)?(?:\d[ -]?){9,11}\d(?!\w)")),
    ("DOB", re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}\b")),
]


def redact(text: str) -> str:
    """Replace direct identifiers with typed placeholders, e.g. [PHONE]."""
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[{label}]", text)
    return text


def contains_pii(text: str) -> bool:
    return any(p.search(text) for _, p in _PATTERNS)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return digits[-12:] if len(digits) > 10 else "91" + digits[-10:]


def patient_ref(phone: str, pepper: str) -> str:
    """Stable, non-reversible patient reference derived from the phone number."""
    digest = hmac.new(pepper.encode(), normalize_phone(phone).encode(), hashlib.sha256)
    return "p_" + digest.hexdigest()[:20]


def mask_phone(phone: str) -> str:
    digits = normalize_phone(phone)
    return "+" + digits[:2] + "******" + digits[-2:]
