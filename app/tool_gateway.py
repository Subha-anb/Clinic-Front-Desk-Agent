"""How the agent reaches the clinic tools: in-process (dev/tests) or through the MCP server.

Either way, the gateway is the security boundary for tool calls:
* only tools in the patient allowlist can be called by the model;
* any identity fields the model tries to send are dropped, and the verified sender's
  patient_ref/phone are injected instead.
"""

from __future__ import annotations

import json
import os
import sys
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack

from clinic_store import ClinicStore, StoreError
from mcp_server.tools import PATIENT_TOOLS, run_patient_tool

_IDENTITY_KEYS = {"patient_ref", "patient_phone", "phone"}
_SERVER_ENV = {"DATA_DIR", "STORE_BACKEND", "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_CALENDAR_ID", "GOOGLE_SHEET_ID"}


class ToolGateway(ABC):
    async def call_patient_tool(self, name: str, args: dict, patient_ref: str, phone: str):
        if name not in PATIENT_TOOLS:
            raise StoreError(f"Tool '{name}' is not available.")
        clean = {k: v for k, v in args.items() if k not in _IDENTITY_KEYS}
        return await self._call_patient_tool(name, clean, patient_ref, phone)

    @abstractmethod
    async def _call_patient_tool(self, name: str, args: dict, patient_ref: str, phone: str): ...

    @abstractmethod
    async def create_handoff(self, patient_ref: str, category: str, priority: str, summary: str) -> str: ...


class LocalToolGateway(ToolGateway):
    def __init__(self, store: ClinicStore):
        self.store = store

    async def _call_patient_tool(self, name, args, patient_ref, phone):
        return run_patient_tool(self.store, name, args, patient_ref, phone)

    async def create_handoff(self, patient_ref, category, priority, summary):
        return self.store.create_handoff(patient_ref, category, priority, summary)


class MCPToolGateway(ToolGateway):
    """Talks to `python -m mcp_server.server` over stdio. Use as an async context manager."""

    _NEEDS_IDENTITY = {"book_appointment", "list_my_appointments", "cancel_appointment", "reschedule_appointment"}

    def __init__(self, cwd: str):
        self.cwd = cwd
        self._stack = AsyncExitStack()
        self.session = None

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        # The server gets storage settings only: no Anthropic or WhatsApp credentials.
        env = get_default_environment() | {k: v for k, v in os.environ.items() if k in _SERVER_ENV}
        params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=self.cwd, env=env)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def _call(self, name: str, args: dict):
        result = await self.session.call_tool(name, args)
        texts = [c.text for c in result.content if hasattr(c, "text")]
        if result.is_error:
            raise StoreError(" ".join(texts))
        structured = result.structured_content
        if structured is not None:
            # MCPServer wraps non-object return types as {"result": ...}
            return structured["result"] if set(structured) == {"result"} else structured
        try:
            parsed = [json.loads(t) for t in texts]
        except json.JSONDecodeError:
            return " ".join(texts)
        return parsed[0] if len(parsed) == 1 else parsed

    async def _call_patient_tool(self, name, args, patient_ref, phone):
        if name in self._NEEDS_IDENTITY:
            args = {**args, "patient_ref": patient_ref, "patient_phone": phone}
        return await self._call(name, args)

    async def create_handoff(self, patient_ref, category, priority, summary):
        result = await self._call(
            "create_staff_handoff",
            {"patient_ref": patient_ref, "category": category, "priority": priority, "summary": summary},
        )
        return result["handoff_id"]
