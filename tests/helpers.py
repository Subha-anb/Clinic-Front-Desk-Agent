import json
import tempfile
from dataclasses import replace
from pathlib import Path

from app.agent import FrontDeskAgent
from app.config import ROOT, get_settings
from app.rag import KnowledgeBase
from app.security.audit import AuditLog, RateLimiter
from app.tool_gateway import LocalToolGateway
from clinic_store import LocalStore

EVAL = json.loads((ROOT / "evals" / "tricky_questions.json").read_text(encoding="utf-8"))
SAFE_ACTIONS = {"escalate", "emergency", "refuse"}


def make_agent(tmp: Path | None = None) -> FrontDeskAgent:
    """Offline agent (no LLM) on a throwaway local store."""
    tmp = tmp or Path(tempfile.mkdtemp())
    settings = replace(get_settings(), data_dir=tmp, llm_enabled=False, pii_pepper="test-pepper")
    store = LocalStore(settings.clinic, tmp / "store.json")
    kb = KnowledgeBase(settings.knowledge_dir, settings.clinic)
    return FrontDeskAgent(settings, kb, store, LocalToolGateway(store), AuditLog(tmp / "audit.jsonl"),
                          limiter=RateLimiter(max_messages=10_000))
