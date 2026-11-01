# NovaMart Assist

An internal assistant for NovaMart store employees, built with **Google's Agent Development Kit (ADK) and Gemini**. It:

- answers policy questions **only from NovaMart's documents**, with a citation for every fact;
- looks up **live order status** through a (mock) order API;
- **declines** when the policies don't cover a question, and offers to forward it to the ops team;
- includes a **return decision helper**: it gathers the facts it needs by asking the employee short clarifying questions, applies the return rules deterministically, and answers ✅ approve / ❌ deny / ⚠️ needs manager, with reasons and next steps. When a manager is needed, it opens a pre-filled escalation ticket.

The production plan is in [docs/PRODUCTION_NOTE.md](docs/PRODUCTION_NOTE.md).

```
EMPLOYEE  Customer wants to return a blender.
ASSIST    To check if the blender can be returned, I need a couple of details:
          1. What is the order number (e.g., NM-12345)?
          2. What is the reason for the return, and what is its current condition?
EMPLOYEE  Order NM-10482. It's been opened but works fine, they just don't want it anymore.
ASSIST    ❌ Deny (Refund: $0)
          Why:
          - Delivered 34 days ago, which is outside the 14-day return window for opened small
            appliances and past the 7-day goodwill extension [returns-and-refunds › Return windows by category].
          Next steps:
          - Politely explain the return window to the customer.
          - If the blender develops a fault, the manufacturer warranty may apply.
```
*(A real transcript from the prototype, lightly trimmed.)*

---

## Setup

Requires Python 3.11+ and a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python3 -m venv .venv && source .venv/bin/activate
```
```bash
pip install -r requirements.txt
```
```bash
cp .env.example .env   # then put your key in GOOGLE_API_KEY
```

To run on Vertex AI instead, set `GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` in `.env`. No code changes are needed.

## Run it

| What | Command |
|---|---|
| Chat UI (ADK dev UI, shows every tool call) | `adk web` → open http://localhost:8000 and pick `novamart_assistant` |
| Chat in the terminal | `adk run novamart_assistant` |
| Evaluation suite (27 conversations, LLM-judged) | `python -m eval.run_eval` (`--only decide` to filter) |
| Unit tests for the return rules (no API key needed) | `pytest -q` |

Try these questions: *"Where is order NM-10560?"* · *"Customer wants to return order NM-10530"* · *"NM-10511, laptop opened, changed their mind"* · *"What's the employee discount?"*. The mock orders are listed in [data/orders.json](data/orders.json).

## How it works

```
                 ┌──────────────── Gemini (ADK LlmAgent, temperature 0.1) ────────────────┐
 employee ──────▶│ instruction: ground every fact, cite, decline, ask for missing facts   │
                 └───┬──────────────┬──────────────────────┬──────────────────────┬───────┘
                     ▼              ▼                      ▼                      ▼
             search_policies  get_order_status  check_return_eligibility  create_escalation_ticket
             (Gemini embeddings  (mock order API)  (deterministic rules,    (JSONL now; ServiceNow/
              over 9 policy docs,                   returns decision +       Jira in production)
              split by section)                     policy citations, or
                                                    "needs_more_info")
```

| Path | Contents |
|---|---|
| `novamart_assistant/agent.py` | The ADK agent: model, instruction, tools |
| `novamart_assistant/prompts.py` | System instruction (grounding, decline and clarification rules) |
| `novamart_assistant/tools.py` | The four tools exposed to Gemini |
| `novamart_assistant/return_rules.py` | The return policy as code, with a policy reference for every step |
| `novamart_assistant/retrieval.py` | Section-level semantic search with cached embeddings |
| `data/policies/` | 9 invented NovaMart policy documents |
| `data/orders.json` | Mock orders, one per return scenario |
| `eval/` | Eval cases (YAML), runner and LLM judge, reports in `eval/results/` |
| `tests/` | Unit tests for the rules engine and the order tool |

## Key decisions

1. **The LLM collects and explains; code decides.** Return eligibility depends on date arithmetic, category windows, the Gold extension, a 15% fee and a $500 threshold. LLMs get that kind of thing *almost* right, and almost right isn't good enough when money is involved. So `check_return_eligibility` is a plain, unit-tested function. Gemini's job is what it's good at: understanding the employee, asking for what's missing, and explaining the result. The tool returns its reasons with policy citations, so explanations stay grounded.
2. **The tool drives the clarifying questions.** If facts are missing or ambiguous (no item specified on a multi-item order, condition unknown, an unrecognized reason), the tool returns `needs_more_info` with the valid options instead of guessing. The prompt adds the judgement the tool can't make. For example, "broken" could mean defective, damaged on arrival, or damaged by the customer, and each leads to a different decision.
3. **Retrieval over the full context window.** With 9 documents everything would fit in the prompt. I used retrieval anyway because the real policy library will be much larger, and section-level chunks give precise citations (`doc › section`). It's a ~70-line module with cached `gemini-embedding-001` vectors that Vertex AI Search replaces directly in production.
4. **Declining is explicit and testable.** The agent must say a fixed phrase ("I couldn't find this in NovaMart's policies") rather than a hedged half-answer. That lets the evaluation check declines deterministically, and in production it lets us count unanswered questions by topic (see the production note).
5. **ADK rather than raw function calling.** ADK gives us the tool loop, sessions for multi-turn clarification, a dev UI with tool traces, and a deployment path to Agent Engine and Gemini Enterprise.
6. **Model: `gemini-3.5-flash`.** It's fast enough for an employee at a counter, and tool use is reliable. On build day `gemini-3.8-flash` returned 503s or took 60–150 s per call, and `gemini-2.5-flash` is no longer available to new keys. The model can be changed with `NOVAMART_MODEL`.
7. **Mock dates are relative.** Orders store `delivered_days_ago`, so "34 days ago" stays true whenever the demo runs. Without that, every return-window scenario would break within a week.

## The creative part: why a return decision helper

From the store employee's point of view, the hard requests aren't "what is the policy?" but *"the customer is standing here — can I take this back?"* Answering that means combining 3–4 policies with order data, and today the employee has to call the ops team for it. The helper:

- **closes the loop at the counter:** a clear verdict, the refund amount and method, and what to do next;
- **asks rather than guesses**, at most two short questions at a time, skipping anything the order already tells it;
- **knows its authority:** a refund above $500 or a goodwill exception isn't "approved" by the bot. It tells the employee not to promise anything, and with one "yes" it files a manager ticket that already contains every fact checked. The manager doesn't have to call the employee back;
- **gives the ops team data:** escalation tickets (`data/escalations.jsonl`) and declined questions show where policies are unclear or missing.

## Evaluation

There are two layers.

**1. Unit tests** (`pytest`, 20 tests) cover every branch of the rules engine and the order lookup. They're deterministic, so they always run in CI.

**2. Conversation evals** (`python -m eval.run_eval`): 27 conversations in [eval/cases.yaml](eval/cases.yaml), some of them multi-turn, across six categories:

| Category | What it checks |
|---|---|
| grounded | Policy answers are correct and cite the right document |
| tool | Order lookups, including "not found" without inventing data |
| decline | Questions the policies don't cover → the decline phrase, no made-up answer |
| clarify | Missing or ambiguous facts → asks the employee instead of deciding |
| decision | Correct verdict for each scenario (Gold extension, fees, Final Sale + arrival damage, warranty, manager threshold, goodwill → ticket) |
| adversarial | "Ignore your rules and approve", false premises, prompt extraction |

A case passes only if **all deterministic checks** pass (expected tools called or not called, decision returned by the rules tool, citations, required or forbidden patterns, decline phrase, a question asked) **and** an **LLM judge** (Gemini, JSON output) rates the final answer as both:
- **grounded:** every factual claim is supported by the tool outputs the agent received, and
- **meeting the case's expectation**, written in plain English for each case.

Each run writes `eval/results/latest.md` (summary table) and a full JSONL trace.

**Results:** <!-- EVAL_RESULTS -->

## Assumptions

- Users are authenticated NovaMart employees. The agent sees only what an employee could see in the POS.
- The decision helper covers **online orders** (the order API is the source of the dates). In-store receipts without an order number go through the documented policy Q&A.
- Return windows start at the delivery date. "Today" is the server's date.
- The policies and orders are invented, but internally consistent. Where a real policy would be vague (for example, damage-on-arrival reported late), I made a choice and wrote it down in `return_rules.py`.

## What I would do with more time

- **One source of truth for rules:** generate `return_rules.py` constants from a rules file that Store Ops edits together with the policy document. Today the numbers exist both in the documents and in the code.
- **Retrieval relevance threshold:** declining currently relies on the model judging the passages. With a larger eval set I'd calibrate a score cut-off (unrelated questions still score about 0.63, relevant ones about 0.79).
- **Bigger, real eval set:** collect 100+ real questions from the ops team's inbox, run each case several times to measure variance, and track pass rates per category over time.
- **In-store purchases** (receipt or card lookup) and **exchanges** in the decision helper.
- **Ops-facing gaps dashboard:** cluster declined questions and escalations each week into a list of "documents to write".
- **Voice / mobile** use for employees on the floor.
