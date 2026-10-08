"""Request authentication for inbound webhooks and internal (n8n) calls."""

from __future__ import annotations

import hashlib
import hmac


def verify_meta_signature(raw_body: bytes, signature_header: str | None, app_secret: str | None) -> bool:
    """Verify WhatsApp Cloud API's X-Hub-Signature-256 header (HMAC-SHA256 of the raw body)."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def verify_bearer(auth_header: str | None, token: str | None) -> bool:
    if not token or not auth_header or not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header.removeprefix("Bearer "), token)
