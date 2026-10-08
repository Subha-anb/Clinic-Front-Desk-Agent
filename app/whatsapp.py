"""WhatsApp Cloud API: parse inbound webhooks and send replies / templates."""

from __future__ import annotations

import httpx

GRAPH_URL = "https://graph.facebook.com/v21.0"


def extract_messages(payload: dict) -> list[dict]:
    """Return [{id, from, text, name}] for inbound text messages; ignore statuses and media."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                if msg.get("type") == "text":
                    text = msg["text"]["body"]
                elif msg.get("type") == "button":
                    text = msg["button"].get("text", "")
                elif msg.get("type") == "interactive":
                    reply = msg["interactive"].get("button_reply") or msg["interactive"].get("list_reply") or {}
                    text = reply.get("title", "")
                else:
                    text = None  # media / location: handled by a canned reply
                out.append({"id": msg["id"], "from": msg["from"], "text": text, "name": names.get(msg["from"])})
    return out


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str):
        self._headers = {"Authorization": f"Bearer {token}"}
        self._url = f"{GRAPH_URL}/{phone_number_id}/messages"

    async def _post(self, body: dict) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._url, headers=self._headers, json={"messaging_product": "whatsapp", **body})
            resp.raise_for_status()

    async def send_text(self, to: str, text: str) -> None:
        await self._post({"to": to, "type": "text", "text": {"body": text[:4000], "preview_url": False}})

    async def send_template(self, to: str, template: str, params: list[str], lang: str = "en") -> None:
        await self._post({
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": lang},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}],
            },
        })
