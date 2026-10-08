"""HTTP entry point: WhatsApp webhook + internal endpoints for n8n.

Run: uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from clinic_store import StoreError, make_store

from . import followups
from .agent import FrontDeskAgent
from .config import ROOT, get_settings
from .rag import KnowledgeBase
from .security.audit import AuditLog
from .security.pii import mask_phone
from .security.webhook import verify_bearer, verify_meta_signature
from .tool_gateway import LocalToolGateway, MCPToolGateway
from .whatsapp import WhatsAppClient, extract_messages

log = logging.getLogger("clinic")
settings = get_settings()
store = make_store(settings)
wa = WhatsAppClient(settings.whatsapp_token or "", settings.whatsapp_phone_number_id or "")
_seen: OrderedDict[str, None] = OrderedDict()  # recent WhatsApp message ids (webhooks are retried)
state: dict = {}


async def notify_staff(handoff_id: str, category: str, priority: str) -> None:
    """Tell n8n a ticket exists. IDs and priority only; staff read the details in the restricted sheet."""
    if not settings.n8n_handoff_webhook_url:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            settings.n8n_handoff_webhook_url,
            json={"handoff_id": handoff_id, "category": category, "priority": priority},
            headers={"Authorization": f"Bearer {settings.internal_api_token}"},
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    llm = None
    if settings.llm_enabled:
        from .llm import ClaudeClient

        llm = ClaudeClient(settings.anthropic_model, settings.clinic)
    if settings.use_mcp:
        gateway = await MCPToolGateway(cwd=str(ROOT)).__aenter__()
    else:
        gateway = LocalToolGateway(store)
    state["agent"] = FrontDeskAgent(
        settings, KnowledgeBase(settings.knowledge_dir, settings.clinic), store, gateway,
        AuditLog(settings.data_dir / "audit.jsonl"), llm=llm, notifier=notify_staff,
    )
    yield
    if isinstance(gateway, MCPToolGateway):
        await gateway.__aexit__(None, None, None)


app = FastAPI(title="Clinic Front Desk Agent", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/webhook")
async def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and settings.whatsapp_verify_token and q.get("hub.verify_token") == settings.whatsapp_verify_token:
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403)


@app.post("/webhook")
async def inbound(request: Request, background: BackgroundTasks, x_hub_signature_256: str | None = Header(None)):
    raw = await request.body()
    if not verify_meta_signature(raw, x_hub_signature_256, settings.whatsapp_app_secret):
        raise HTTPException(401)
    for msg in extract_messages(await request.json()):
        if msg["id"] in _seen:
            continue
        _seen[msg["id"]] = None
        if len(_seen) > 5000:
            _seen.popitem(last=False)
        background.add_task(_process, msg)
    return {"ok": True}  # acknowledge fast; Meta retries slow webhooks


async def _process(msg: dict) -> None:
    try:
        if msg["text"] is None:
            reply = "I can only read text messages. Please type your question, or call us on " + settings.clinic["phone"] + "."
        else:
            reply = (await state["agent"].handle(msg["from"], msg["text"], msg["name"])).text
        await wa.send_text(msg["from"], reply)
    except Exception:
        log.exception("failed to process message from %s", mask_phone(msg["from"]))


# ---- internal endpoints for n8n --------------------------------------------------------------
def _auth(authorization: str | None) -> None:
    if not verify_bearer(authorization, settings.internal_api_token):
        raise HTTPException(401)


@app.get("/internal/followups/due")
async def followups_due(kind: str, authorization: str | None = Header(None)):
    _auth(authorization)
    if kind not in followups.KINDS:
        raise HTTPException(400, "unknown kind")
    return {"items": followups.due(store, kind)}


@app.post("/internal/followups/send")
async def followups_send(body: dict, authorization: str | None = Header(None)):
    _auth(authorization)
    kind, appt_id = body.get("kind"), body.get("appointment_id")
    if kind not in followups.KINDS:
        raise HTTPException(400, "unknown kind")
    if {"appointment_id": appt_id, "kind": kind} not in followups.due(store, kind):
        return {"sent": False, "reason": "not due, already sent, or opted out"}
    appt = store.get_appointment(appt_id)
    patient = store.get_patient(appt["patient_ref"])
    await wa.send_template(patient["phone"], followups.TEMPLATES[kind], followups.template_params(store, appt, kind))
    store.mark_followup_sent(appt_id, kind)
    return {"sent": True}


@app.post("/internal/visits/{appointment_id}")
async def log_visit(appointment_id: str, body: dict, authorization: str | None = Header(None)):
    """Front desk / CRM marks a visit completed or no_show (also available as the MCP log_visit tool)."""
    _auth(authorization)
    try:
        return store.log_visit(appointment_id, body.get("status", ""))
    except StoreError as exc:
        raise HTTPException(400, str(exc))


@app.get("/healthz")
async def health():
    return {"ok": True}
