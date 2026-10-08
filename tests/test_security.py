import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from app.security.audit import AuditLog, RateLimiter
from app.security.pii import mask_phone, patient_ref, redact
from app.security.webhook import verify_bearer, verify_meta_signature


class Redaction(unittest.TestCase):
    def test_redacts_identifiers(self):
        text = "I'm Priya, +91 98765 43210, priya@mail.com, Aadhaar 1234 5678 9012, PAN ABCDE1234F, DOB 12/05/1990"
        out = redact(text)
        for secret in ("98765", "priya@mail.com", "1234 5678 9012", "ABCDE1234F", "12/05/1990"):
            self.assertNotIn(secret, out)
        self.assertIn("[PHONE]", out)
        self.assertIn("Priya", out)  # names are kept: needed for booking, low sensitivity alone

    def test_keeps_clinical_numbers(self):
        self.assertIn("102", redact("fever of 102"))
        self.assertIn("650", redact("Dolo 650"))


class Pseudonymisation(unittest.TestCase):
    def test_stable_and_format_independent(self):
        a = patient_ref("+91 98765 43210", "pep")
        self.assertEqual(a, patient_ref("919876543210", "pep"))
        self.assertEqual(a, patient_ref("9876543210", "pep"))
        self.assertNotIn("9876", a)

    def test_pepper_matters(self):
        self.assertNotEqual(patient_ref("9876543210", "a"), patient_ref("9876543210", "b"))

    def test_mask(self):
        self.assertEqual(mask_phone("+919876543210"), "+91******10")


class WebhookAuth(unittest.TestCase):
    def test_valid_signature(self):
        body = b'{"entry": []}'
        sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_meta_signature(body, sig, "secret"))

    def test_tampered_or_missing(self):
        body = b'{"entry": []}'
        sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertFalse(verify_meta_signature(body + b" ", sig, "secret"))
        self.assertFalse(verify_meta_signature(body, None, "secret"))
        self.assertFalse(verify_meta_signature(body, sig, None))  # unset secret fails closed

    def test_bearer(self):
        self.assertTrue(verify_bearer("Bearer tok", "tok"))
        self.assertFalse(verify_bearer("Bearer nope", "tok"))
        self.assertFalse(verify_bearer("Bearer tok", None))


class AuditAndRate(unittest.TestCase):
    def test_audit_is_redacted(self):
        path = Path(tempfile.mkdtemp()) / "a.jsonl"
        AuditLog(path).record("message", "p_x", message="call me on 9876543210")
        entry = json.loads(path.read_text())
        self.assertNotIn("9876543210", entry["message"])

    def test_rate_limit(self):
        rl = RateLimiter(max_messages=2, window_seconds=60)
        self.assertTrue(rl.allow("p", 0))
        self.assertTrue(rl.allow("p", 1))
        self.assertFalse(rl.allow("p", 2))
        self.assertTrue(rl.allow("p", 61))


if __name__ == "__main__":
    unittest.main()
