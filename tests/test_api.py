"""HTTP-level tests for the WhatsApp webhook and the n8n endpoints. Skipped without FastAPI installed."""

import hashlib
import hmac
import json
import os
import tempfile
import unittest

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

SECRET, VERIFY, TOKEN = "app-secret", "verify-me", "internal-token"


@unittest.skipIf(TestClient is None, "fastapi not installed")
class WebhookAndInternalApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update({
            "DATA_DIR": tempfile.mkdtemp(), "STORE_BACKEND": "local", "USE_MCP": "false", "LLM_ENABLED": "false",
            "WHATSAPP_APP_SECRET": SECRET, "WHATSAPP_VERIFY_TOKEN": VERIFY, "INTERNAL_API_TOKEN": TOKEN,
            "PII_PEPPER": "test",
        })
        from app.config import get_settings

        get_settings.cache_clear()
        from app import main

        cls.main = main
        cls.sent = []

        async def fake_send_text(to, text):
            cls.sent.append(("text", to, text))

        async def fake_send_template(to, template, params, lang="en"):
            cls.sent.append(("template", to, template, params))

        main.wa.send_text = fake_send_text
        main.wa.send_template = fake_send_template
        cls.client = TestClient(main.app).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def post_message(self, text, msg_id, sender="919000000001"):
        body = json.dumps({"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": sender, "profile": {"name": "Alice"}}],
            "messages": [{"id": msg_id, "from": sender, "type": "text", "text": {"body": text}}],
        }}]}]}).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})

    def test_verify_handshake(self):
        ok = self.client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42"})
        self.assertEqual(ok.text, "42")
        bad = self.client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
        self.assertEqual(bad.status_code, 403)

    def test_rejects_unsigned(self):
        r = self.client.post("/webhook", content=b'{"entry": []}', headers={"X-Hub-Signature-256": "sha256=00"})
        self.assertEqual(r.status_code, 401)

    def test_clinical_message_escalates_and_dedupes(self):
        self.sent.clear()
        self.assertEqual(self.post_message("Can I take paracetamol before my scan?", "wamid.1").status_code, 200)
        self.post_message("Can I take paracetamol before my scan?", "wamid.1")  # Meta retry
        self.assertEqual(len(self.sent), 1)
        self.assertIn("nursing team", self.sent[0][2])

    def test_internal_requires_token(self):
        self.assertEqual(self.client.get("/internal/followups/due", params={"kind": "reminder"}).status_code, 401)
        r = self.client.get("/internal/followups/due", params={"kind": "reminder"}, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("items", r.json())

    def test_followup_send_only_when_due(self):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        r = self.client.post("/internal/followups/send", json={"appointment_id": "APT-NOPE", "kind": "reminder"}, headers=auth)
        self.assertFalse(r.json()["sent"])
        r = self.client.post("/internal/visits/APT-NOPE", json={"status": "completed"}, headers=auth)
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
