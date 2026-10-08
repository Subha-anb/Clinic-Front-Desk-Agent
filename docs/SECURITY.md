# Security, privacy and safety

## 1. Clinical safety (the non-negotiable)

The assistant is a front desk, not a clinician. It answers logistics and hands everything clinical
to staff. This is enforced by three layers. A model is the *second* line of defence, never the only one.

| Layer | Where | What it does | Failure mode |
|---|---|---|---|
| 1. Rules | `app/guardrails/input_guard.py` | Deterministic patterns: emergencies, medicines (generic, Indian brands, fuzzy misspellings), doses, symptoms + advice-seeking, result interpretation, risk modifiers (pregnancy, diabetes, child, post-procedure), privacy, prompt injection. Normalises NFKC, zero-width chars, leetspeak, dotted letters. | Biased to escalate. |
| 2. Claude classifier | `app/llm.py` `classify` | Structured-output classification with the same categories. Catches paraphrases and other languages that rules miss. | **Can only make a verdict stricter.** Refusal maps to clinical. API error keeps the rules verdict. |
| 3. Output guard | `app/guardrails/output_guard.py` | Every model-written reply is scanned for doses, medicine names, reassurance ("it's normal", "don't worry"), diagnosis language and PII. Allowed only if the exact wording is in the approved source. | Blocked reply becomes a staff handoff. |

Also:
- **Fixed templates** for emergency, escalation and privacy replies. The wording that matters most
  is never generated.
- **Grounding.** Answers come only from `knowledge/` and `config/clinic.json`. `grounded=false`
  goes to staff instead of guessing.
- **No clinical memory.** Conversation history is kept only for logistics/booking turns. A
  clinical turn clears it, so clinical content never leaks into later prompts.
- **Booking with a symptom** ("bad cough, book me with Dr. Rao") is allowed, but the assistant
  never comments on the symptom, and a low-priority ticket lets a nurse glance at it.
- **Emergencies** give 112 / 108 / Tele-MANAS straight away and open an *urgent* ticket. They
  skip the LLM entirely, so there's no latency or failure point.

The eval (`evals/tricky_questions.json`, `scripts/run_guardrail_eval.py`) is a release gate: any
clinical question that gets `answer`/`book` fails CI.

## 2. Data protection

| Data | Where it lives | Who sees it |
|---|---|---|
| Phone number | Patients tab of the restricted Google Sheet (or local store) | Front desk; the WhatsApp sender. **Never** the LLM, logs, Calendar or n8n. |
| `patient_ref` = HMAC-SHA256(phone, `PII_PEPPER`) | Everywhere else | Non-reversible without the pepper |
| Name | Patients tab | Front desk; passed to the booking tool if the patient gives it |
| Reason for visit | Enum only (`general_consultation`, `lab_test`, ...) | No free-text symptoms are stored with bookings |
| Message text | Redacted copy in the Handoffs tab and audit log | Staff only |
| Calendar event | `Dr. Rao - patient 3f9a1c` | Minimal: no name, phone or reason |

Controls:
- **Redaction** (`app/security/pii.py`): phone, email, Aadhaar, PAN and DOB are replaced before any
  LLM call and before writing the audit log or a handoff.
- **Identity is injected server-side.** Tool schemas Claude sees have no patient fields. The
  gateway (`app/tool_gateway.py`) drops any identity keys the model sends and injects the
  verified sender's `patient_ref`. The store checks ownership on every cancel/reschedule and
  returns the same error for "not found" and "not yours" (no enumeration).
- **Tool allowlist.** The model can only call the six patient tools. `log_visit` and
  `create_staff_handoff` are not exposed to it.
- **MCP server over stdio**, launched as a child process. It isn't reachable from the network.
- **Webhook authenticity.** `X-Hub-Signature-256` HMAC is verified over the raw body with a
  constant-time compare. A missing secret fails closed. Message IDs are deduplicated (Meta retries).
- **Internal API** (`/internal/*`) for n8n requires a bearer token with a constant-time compare.
  n8n receives appointment IDs only, never phone numbers or message content.
- **Abuse limits.** 20 messages / 10 minutes per patient, 1,000 characters per message, at most 3
  upcoming bookings, a 60-day booking horizon, and a 6-step cap on the booking tool loop.
- **Secrets** come only from environment variables or a secret manager (see `.env.example`).
  `.env` and service-account files are git-ignored.
- **Least privilege.** The Google service account is shared on one calendar and one sheet only,
  with `calendar.events` + `spreadsheets` scopes.
- **Logging.** The app logs masked phones (`+91******10`). The audit log is append-only JSONL with
  redacted text.

## 3. Consent and retention

- Follow-ups use pre-approved WhatsApp templates. `STOP` opts out and `START` opts back in. The
  opt-out is checked at send time in `app/followups.py`, not only in n8n.
- Review requests are suppressed if the patient has an open clinical ticket.
- Suggested retention: the audit log for 1 year, conversation history 30 minutes (in memory), handoffs
  closed after 90 days, and the visit log per your medical-records policy. Align with the DPDP Act
  2023 (India) or HIPAA/GDPR as applicable, and sign a data processing agreement with each vendor
  (Meta, Anthropic, Google).

## 4. Known limits / next steps

- The rules are English-centric. Tamil/Hindi messages rely on the Claude classifier, so add
  language-specific term lists and eval cases.
- The local JSON store is for development only. In production use the Google backend or a CRM
  with encryption at rest.
- Message dedupe and rate limits are in-process. Use Redis when running more than one replica.
- The WhatsApp profile name is used as a fallback patient name in the offline booking flow.
