# Architecture

```
 Patient (WhatsApp)
        │  webhook (HMAC-verified)
        ▼
 ┌────────────────────────── app/main.py (FastAPI) ───────────────────────────┐
 │  dedupe · background task · send reply                                      │
 │                                                                             │
 │  app/agent.py  FrontDeskAgent.handle(phone, text)                           │
 │   1. phone ──HMAC──► patient_ref         rate limit, 1000-char cap          │
 │   2. INPUT GUARD                                                            │
 │        rules (input_guard.py) ──► Claude classifier (llm.py) ──► strictest  │
 │   3. ROUTE                                                                  │
 │        emergency ─► fixed template + URGENT handoff ───────┐                │
 │        clinical / privacy ─► fixed template + handoff ─────┤                │
 │        injection ─► refusal       opt-out ─► STOP          │                │
 │        booking ─► Claude tool loop ─► ToolGateway ─────────┼──► MCP server  │
 │        logistics ─► BM25 RAG (rag/) ─► Claude grounded answer               │
 │   4. OUTPUT GUARD (output_guard.py) on every model-written reply            │
 │        fail ─► handoff + fixed template                                     │
 │   5. AUDIT (redacted JSONL)                                                 │
 │                                                                             │
 │  /internal/followups/due|send  ◄── bearer token ── n8n (schedules)          │
 └─────────────────────────────────────────────────────────────────────────────┘
                     │ stdio                                   ▲
                     ▼                                         │ handoff_id + priority
 ┌──────── mcp_server/server.py (FastMCP) ────────┐     n8n: staff alert email
 │ list_doctors · find_available_slots ·          │
 │ book / list / cancel / reschedule (ownership)  │
 │ log_visit · create_staff_handoff (agent/staff) │
 └──────────────────────┬─────────────────────────┘
                        ▼
        clinic_store/  (local JSON  |  Google Calendar + Sheets)
```

## Components

| Path | Role |
|---|---|
| `config/clinic.json` | Clinic facts and doctor schedules: the single source of truth for slots *and* RAG |
| `knowledge/*.md` | Approved FAQs, policies and pre-visit instructions. Edit these, not prompts. |
| `app/guardrails/` | `policy.py` (categories, severity, fixed messages), `input_guard.py` (rules), `output_guard.py` |
| `app/llm.py` | Claude calls: classifier, grounded answer, booking tool loop. All fail closed. |
| `app/rag/retriever.py` | BM25 over heading-level chunks plus generated schedule chunks |
| `app/tool_gateway.py` | Tool allowlist + identity injection; local or MCP transport |
| `mcp_server/` | MCP server and shared tool specs |
| `clinic_store/` | Booking rules (slots, ownership, caps) + persistence backends |
| `app/followups.py` | Who is due for reminder / check-in / review (opt-outs, duplicates, open tickets) |
| `n8n/*.json` | Importable schedules + staff alert workflow |

## Model usage

All calls use `claude-opus-5-5` at `effort: low` with server-side refusal fallbacks
(`fallbacks: "default"`).
- **Classifier**: JSON-schema structured output `{category, rationale}`.
- **Answer**: structured output `{answer, grounded, needs_staff}` with documents in the prompt.
- **Booking**: strict tool use over six patient tools, at most 6 steps.

Set `ANTHROPIC_MODEL` to change the model. Re-run `scripts/run_guardrail_eval.py --llm` before
switching.

## Follow-up lifecycle

```
booked ──(day before, 18:00)──► reminder template
   │
visit ──front desk / CRM: POST /internal/visits/{id} {status: completed|no_show}  (or MCP log_visit)
   │
completed ──(+18–36 h, 11:00)──► check-in template ──patient reply──► normal guardrail pipeline
   │                                                              (clinical reply opens a ticket)
   └──(+60–96 h, 17:00, check-in sent, no open ticket)──► review request
```
