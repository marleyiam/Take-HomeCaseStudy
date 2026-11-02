# NovaMart Assist

An internal assistant for NovaMart store employees, built with **Google's Agent Development Kit (ADK) and Gemini**. Employees chat with it directly. It answers policy questions **only from NovaMart's documents** (with citations), looks up **live order status**, **declines** what the policies don't cover, and decides returns with a **rules engine**: ✅ approve / ❌ deny / ⚠️ needs manager.

**The creative part** is a [ticket desk](#creative-part-closing-the-loop-for-the-ops-team): the same agent drafts replies to the ops team's incoming tickets, and a person approves, escalates or revises them.

Production plan: [docs/PRODUCTION_NOTE.md](docs/PRODUCTION_NOTE.md).

## Setup

Python 3.11+ and a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python3 -m venv .venv && source .venv/bin/activate
```
```bash
pip install -r requirements.txt
```
```bash
cp .env.example .env   # then put your key in GOOGLE_API_KEY
```

## Run it

| What | Command |
|---|---|
| **Chat** (dev UI with tool traces) | `adk web` → http://localhost:8000, pick `novamart_assistant` (or `adk run novamart_assistant`) |
| **Ticket desk** | `python -m ticket_desk` → http://localhost:8080 (loads the 6 sample tickets on first start; `--no-samples` for an empty desk) |
| Unit tests (no API key) | `pytest -q` |
| Evaluation (28 conversations) | `python -m eval.run_eval` (`--only <case>` to filter) |

Try in chat: *"Customer wants to return a blender"* · *"NM-10511, laptop opened, changed their mind"* · *"Where is order NM-10575?"* · *"What's the employee discount?"*. Mock orders are in [data/orders.json](data/orders.json).

## How it works

```
employee (chat) or ticket (desk) ──▶ Gemini agent (ADK) ──▶ search_policies         9 policy docs, section-level embeddings
                                                          ├─▶ get_order_status        mock order API
                                                          ├─▶ check_return_eligibility rules engine (plain Python)
                                                          └─▶ create_escalation_ticket JSONL now, ServiceNow/Jira later
```

`novamart_assistant/` holds the agents, prompts, tools, rules and retrieval; `ticket_desk/` the inbox worker and review UI; `eval/` the cases and judge; `tests/` the unit tests.

## Key decisions

1. **The LLM collects and explains; code decides.** Return windows, the Gold extension, the 15% fee and the $500 threshold live in `return_rules.py`, a unit-tested function that returns its reasons with policy citations. Almost-right date maths isn't acceptable when money is involved.
2. **The tool drives clarification.** Missing or ambiguous facts come back as `needs_more_info` with valid options, so the agent asks instead of guessing ("broken" could mean defective, damaged on arrival, or customer damage).
3. **Retrieval, even with 9 documents.** The real library will be larger, and section-level chunks give precise citations. Vertex AI Search replaces this module in production.
4. **Declining is a fixed phrase,** so it's testable and countable by topic.
5. **`gemini-3.5-flash` at temperature 0.1.** It's fast and reliable at tool use. Google advises 1.0 for Gemini 3, so I compared them over 3 full eval runs each: same pass rate, but at 1.0 the agent invented two small procedures, and at 0.1 none. It's a small sample, so this is a default to re-test, not proof.
6. **Mock dates are relative** (`delivered_days_ago`), so the return scenarios never expire.

## Creative part: closing the loop for the ops team

Chat serves store employees, but the **operations team** is the one overwhelmed, and requests keep arriving as tickets until staff adopt chat. The ticket desk puts the same agent on that queue, so **no request is left without a next step**:

- **Every ticket gets a draft:** the store's request quoted verbatim (by code), then the agent's reply with citations, plus a note only the reviewer sees.
- **Missing facts don't stall it:** the draft asks for them, and the reviewer adds the store's answer to get a new draft version.
- **Manager decisions don't disappear:** one click escalates with the facts copied from the tool results, and the reply carries the reference.
- **A person approves everything,** which also measures draft accuracy before anyone relies on it.
- **Queue labels come from tool results** (Approve, Deny, Needs manager, Needs info, Policy gap), not from the model's own claim.

**Verified by hand:** the 6 sample tickets got the expected labels; a reviewer's fact turned *Needs info* into ✅ Approve $59; approving wrote to the outbox. **Not yet:** no eval cases for ticket mode, and tickets are local files.

**In production**, the desk joins the ticketing tool stores already use (ServiceNow, Jira, Zendesk): webhooks feed Pub/Sub, our code writes drafts back as internal notes (through the vendor's API or MCP), and the store's reply on the ticket triggers the next draft.

## Evaluation

- **Unit tests (39):** every branch of the rules engine, the order tool, and the ticket desk's labelling, quoting and escalation logic.
- **Conversation evals (28 cases):** grounded answers, order lookups, declines, clarifying questions, return decisions and adversarial prompts. A case passes only if its deterministic checks pass (tools called, rules decision, citations, decline phrase) **and** a Gemini Pro judge rates the reply grounded in the tool outputs and meeting the case's expectation.
- **Results:** 78/81 over 3 runs. All 3 failures were judge errors or borderline, on reading the transcripts.
- **What it caught:**
  - invented procedures ("refund goes to the original payment method");
  - questions that couldn't change the outcome;
  - a judge that didn't know today's date.
  A manual demo found one more gap, which became a new case.
- **Cost:** about 100 API calls per run. The judge is skipped when a deterministic check already failed, and `--rejudge` re-grades saved transcripts without re-running the agent.

## Assumptions

- Users are authenticated employees; the decision helper covers online orders, with return windows counted from delivery.
- Policies and orders are invented but consistent. Where real policy would be vague, the choice is written down in `return_rules.py`.
- The Vertex AI path (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`) should need no code changes, but is untested.

## What I would do with more time

- Stressing more edge cases (conflicting documents)
- Turn a "no" into a next step: what would change the answer, the best option left, and a line to say to the customer.
- Enforce the policy's abuse rule (more than 5 returns in 30 days needs a manager).
- A weekly "documents to write" report from declined questions and escalations.
