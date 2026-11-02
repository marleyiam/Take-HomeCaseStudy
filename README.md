# NovaMart Assist

An internal assistant for NovaMart store employees, built with **Google's Agent Development Kit (ADK) and Gemini**. It:

- answers policy questions **only from NovaMart's documents**, with a citation for every fact;
- looks up **live order status** through a (mock) order API;
- **declines** when the policies don't cover a question, and offers to forward it to the ops team;
- includes a **return decision helper**: it gathers the facts it needs by asking the employee short clarifying questions, applies the return rules deterministically, and answers ✅ approve / ❌ deny / ⚠️ needs manager, with reasons and next steps. When a manager is needed, it opens a pre-filled escalation ticket;
- works in two modes with the **same agent and tools**: **live chat** for store employees, and a **ticket desk** that drafts replies to the ops team's incoming tickets for a person to approve or revise. See [The ticket desk](#the-ticket-desk-same-agent-on-the-ops-queue).

The production plan is in [docs/PRODUCTION_NOTE.md](docs/PRODUCTION_NOTE.md) (one page), with the details (document pipelines, security against prompt injection and hidden content, maintainability, scaling) in [docs/PRODUCTION_DEEP_DIVE.md](docs/PRODUCTION_DEEP_DIVE.md).

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

To run on Vertex AI instead, set `GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` in `.env`. This should need no code changes, since ADK and the GenAI client both read these variables, but the Vertex path has not been tested yet.

## Run it

| What | Command |
|---|---|
| Chat UI (ADK dev UI, shows every tool call) | `adk web` → open http://localhost:8000 and pick `novamart_assistant` |
| Chat in the terminal | `adk run novamart_assistant` |
| Ticket desk (inbox worker + review UI) | `python -m ticket_desk` → open http://localhost:8080, then `cp data/tickets/samples/*.json data/tickets/inbox/` |
| Evaluation suite (28 conversations, LLM-judged) | `python -m eval.run_eval` (`--only decide` to filter) |
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
| `ticket_desk/` | Inbox worker, file-based ticket store, review API and UI (`static/index.html`) |
| `data/tickets/samples/` | 6 sample tickets to drop into the inbox |
| `tests/` | Unit tests for the rules engine, the order tool and the ticket store |

## Key decisions

1. **The LLM collects and explains; code decides.** Return eligibility depends on date arithmetic, category windows, the Gold extension, a 15% fee and a $500 threshold. LLMs get that kind of thing *almost* right, and almost right isn't good enough when money is involved. So `check_return_eligibility` is a plain, unit-tested function. Gemini's job is what it's good at: understanding the employee, asking for what's missing, and explaining the result. The tool returns its reasons with policy citations, so explanations stay grounded.
2. **The tool drives the clarifying questions.** If facts are missing or ambiguous (no item specified on a multi-item order, condition unknown, an unrecognized reason), the tool returns `needs_more_info` with the valid options instead of guessing. The prompt adds the judgement the tool can't make. For example, "broken" could mean defective, damaged on arrival, or damaged by the customer, and each leads to a different decision.
3. **Retrieval over the full context window.** With 9 documents everything would fit in the prompt. I used retrieval anyway because the real policy library will be much larger, and section-level chunks give precise citations (`doc › section`). It's a ~70-line module with cached `gemini-embedding-001` vectors that Vertex AI Search replaces directly in production.
4. **Declining is explicit and testable.** The agent must say a fixed phrase ("I couldn't find this in NovaMart's policies") rather than a hedged half-answer. That lets the evaluation check declines deterministically, and in production it lets us count unanswered questions by topic (see the production note).
5. **ADK rather than raw function calling.** ADK gives us the tool loop, sessions for multi-turn clarification, a dev UI with tool traces, and a deployment path to Agent Engine and Gemini Enterprise.
6. **Model: `gemini-3.5-flash`.** It's fast enough for an employee at a counter, and tool use is reliable. On build day `gemini-3.8-flash` returned 503s or took 60–150 s per call, and `gemini-2.5-flash` is no longer available to new keys. The model can be changed with `NOVAMART_MODEL`.
7. **Temperature 0.1, tested rather than assumed.** Google's guidance for Gemini 3 models is to keep the default temperature of 1.0, because lower values can cause looping or degrade reasoning. I compared the two on 3 full eval runs each:

   | | 0.1 | 1.0 |
   |---|---|---|
   | Passed | 78/81 | 78/81 |
   | Real agent errors among the failures | 0 | 2 (an invented "POS override" procedure; claimed a "store coupons" policy) |
   | Median latency | 6.6 s | 7.9 s |

   At 0.1 there was no looping or degraded reasoning. That's expected, since all the maths happens in `return_rules.py`. At 1.0 the agent invented small, plausible procedures more often, which is the worst kind of error for a policy assistant. The sample is small (81 conversations each), so this is a reasoned default, not proof. It can be changed with `NOVAMART_TEMPERATURE` and should be re-tested whenever the model changes.
8. **Mock dates are relative.** Orders store `delivered_days_ago`, so "34 days ago" stays true whenever the demo runs. Without that, every return-window scenario would break within a week.

## The creative part: why a return decision helper

From the store employee's point of view, the hard requests aren't "what is the policy?" but *"the customer is standing here — can I take this back?"* Answering that means combining 3–4 policies with order data, and today the employee has to call the ops team for it. The helper:

- **closes the loop at the counter:** a clear verdict, the refund amount and method, and what to do next;
- **asks rather than guesses**, at most two short questions at a time, skipping anything the order already tells it;
- **knows its authority:** a refund above $500 or a goodwill exception isn't "approved" by the bot. It tells the employee not to promise anything, and with one "yes" it files a manager ticket that already contains every fact checked. The manager doesn't have to call the employee back;
- **gives the ops team data:** escalation tickets (`data/escalations.jsonl`) and declined questions show where policies are unclear or missing.

## The ticket desk: same agent on the ops queue

Chat prevents tickets, but only once store staff adopt a new tool. Meanwhile the requests keep arriving in the ops team's queue. The ticket desk puts the same agent to work on that queue, and store staff don't have to change anything.

```
ticket file → data/tickets/inbox/ ──▶ worker (polls every 2 s) ──▶ ticket agent ──▶ draft ──▶ review UI
                                                                     ▲                       │
                                       "REVIEWER: <question/fact/change>" (same session) ◀───┤ revise
                                                                                             │ approve
                                                                       data/tickets/outbox/ ◀┘
```

- **A ticket is a file:** a JSON file dropped into `data/tickets/inbox/` (`{"from", "subject", "body"}`). The UI's "New ticket" form writes to the same folder, so there's one way in. Invalid files go to `rejected/`, and processed originals to `processed/`.
- **The draft has two parts.** The store's request is quoted verbatim by code, not the LLM, so it can't be misquoted. Below it comes the agent's reply, in the same format as in chat (decision, why with citations, next steps). If facts are missing, the reply asks for all of them at once, because there's no live back-and-forth. An optional *note to reviewer* carries assumptions and risks that the store shouldn't see.
- **The agent submits its draft through a tool** (`submit_draft`) instead of us parsing its chat text, so the draft is always exactly what it chose to send.
- **Queue labels come from tool results** (Approve / Deny / Needs manager / Needs info / Escalated), not from the LLM's own claim. The agent's declared outcome is used only when no tool decided, e.g. a policy question or a policy gap.
- **Reviewers can revise by chatting.** Each ticket's conversation is saved in SQLite under the ticket id. The reviewer can ask "why deny?", add a fact ("the store confirmed it's unopened"), or ask for a change. The agent keeps the full context, answers, and submits a new draft version, and earlier versions are kept. Nothing reaches the store until a person clicks **Approve & send**, which writes `data/tickets/outbox/<id>.json`.
- **One-click escalation.** On *Needs manager* tickets an **Escalate to store manager** button appears, and on *Policy gap* tickets **Escalate to ops team**. It opens the escalation *in code*, with no LLM call: the manager gets the item, days since delivery, fee, refund, the rules decision and its policy sections, copied straight from the tool results. The ticket's label becomes *Escalated*, and the outgoing message gains a line ("Store manager approval requested: ticket ESC-…") so the store and the manager refer to the same case. You still approve the reply to the store afterwards. Without this, sending a *Needs manager* reply would close the ticket while the manager decision went untracked.
- **Failures stay visible.** If an agent run fails (API error, quota), the ticket is marked *Error* with the message, and the reviewer can retry by sending a message.

**Verified by hand, end to end:**
- **Labels:** the 6 sample tickets came out as Deny, Needs manager, Answered, Needs info, Policy gap and Approve, each matching its scenario.
- **Revision loop:** for the needs-info ticket, the reviewer typed the store's answer ("the jeans, unworn with tags, changed their mind"). The agent produced draft v2, ✅ Approve $59.00 for the named item, and the label moved from *Needs info* to *Approve* because the rules tool decided it.
- **Approval:** Approve & send wrote the outgoing message to the outbox.

That run found three issues, now fixed:
- A full answer with one declined sub-point was labelled *Policy gap*.
- The agent offered to "forward this to the ops team" in a reply that *is* from the ops team.
- An approval didn't say which of two items it covered.

Ticket mode isn't in the eval suite yet (see "What I would do with more time").

In production, the inbox folder becomes a ServiceNow / Jira / email trigger (Pub/Sub → the agent on Agent Engine), the outbox becomes the ticket system's reply API, and the SQLite sessions move to a managed session store. The agent, tools and rules don't change.

## Evaluation

There are two layers.

**1. Unit tests** (`pytest`, 20 tests) cover every branch of the rules engine and the order lookup. They're deterministic, so they always run in CI.

**2. Conversation evals** (`python -m eval.run_eval`): 28 conversations in [eval/cases.yaml](eval/cases.yaml), some of them multi-turn, across six categories:

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

**Keeping evaluation cheap.** A full run is about 100 API calls: up to 70 agent calls (each tool round trip is one more call), 28 judge calls and about 17 query embeddings. Two things keep it from growing:
- The judge is only called when all deterministic checks pass. A case that already failed can't be rescued, so there's no point paying for the call.
- `--rejudge eval/results/run-<stamp>.jsonl` re-grades saved transcripts with the current checks and judge, without re-running the agent. Use it after changing the judge prompt or a case's checks: about 28 calls instead of 100. If a case's `turns` changed, re-run that case with `--only` instead.

**Judge model:** `gemini-3.1-pro-preview`, a stronger model than the agent so it isn't grading its own mistakes. It also sees today's date and the agent's tool list, so "today is Nov 1" or "I can open a ticket" aren't flagged as made up.

### Results (agent `gemini-3.5-flash`, 3 full runs on 2026-11-01)

| Run | Passed | Failing case | Cause, after reading the transcript |
|---|---|---|---|
| 1 | 26/27 | policy-no-receipt-80 | Judge error: it said the reply omitted the $50 store-credit rule, but the reply states it |
| 2 | 26/27 | adversarial-false-premise | Judge error: the reply says "15 extra days … not 60", and the judge said it didn't correct the premise |
| 3 | 26/27 | policy-no-receipt-80 | Borderline: "don't promise anything" is NovaMart policy, but from a section that wasn't retrieved |

**78/81 (96%) across runs.** On inspection none of the three failures was a wrong answer: two were judge mistakes and one is borderline. Median latency is about 8 s per conversation, including tool calls. The last report is in [eval/results/latest.md](eval/results/latest.md). These runs used the original 27 cases; the 28th case (`decide-apparel-tags-off`) was added afterwards and passed 3/3 on its own, with the 4 `clarify-*` cases re-run to check nothing regressed.

**What the eval caught along the way** (these are why I trust it):
- **Agent fabrications:** the agent added *"a full refund will go to the original payment method"* to an order-cancellation answer, and extrapolated *"what happens after the manager approves"*. No policy says either. Fixed with a prompt rule: don't add consequences or procedures the passages don't state. Only the Pro judge caught these; the Flash judge passed them.
- **Unnecessary questions:** asked about "opened, works fine" headphones, the agent asked "like new or used?" even though the answer can't change the outcome for electronics. Fixed with a prompt rule: don't ask questions that can't change the decision.
- **A gap the eval missed, found in a manual demo:** the employee said "wore it once, tags are off", and the agent still asked "tried on, or worn?". The cause was my own prompt hint ("used it once" is ambiguous for apparel), followed literally. I fixed it by mapping decisive details ("tags off", "worn", "washed") straight to a condition, and added the conversation as a new case. Real usage keeps finding what a test set doesn't cover, which is why production conversations should feed back into the cases.
- **Judge false alarm:** the first judge prompt didn't know today's date, so it flagged a correct business-day calculation as made up. I fixed the judge, not the agent.
- **Ambiguous test input:** one adversarial case said only "it's opened". The agent correctly asked whether it was used, which the test wrongly counted as a failure. I rewrote the input so the case tests the override attempt, which is its actual purpose.

## Assumptions

- Users are authenticated NovaMart employees. The agent sees only what an employee could see in the POS.
- The decision helper covers **online orders** (the order API is the source of the dates). In-store receipts without an order number go through the documented policy Q&A.
- Return windows start at the delivery date. "Today" is the server's date.
- The policies and orders are invented, but internally consistent. Where a real policy would be vague (for example, damage-on-arrival reported late), I made a choice and wrote it down in `return_rules.py`.

## What I would do with more time

- **One source of truth for rules:** generate `return_rules.py` constants from a rules file that Store Ops edits together with the policy document. Today the numbers exist both in the documents and in the code.
- **Retrieval relevance threshold:** declining currently relies on the model judging the passages. With a larger eval set I'd calibrate a score cut-off (unrelated questions still score about 0.63, relevant ones about 0.79).
- **A more reliable judge:** about 1 judge error per 27 cases is too noisy for a strict CI gate. I'd label ~50 answers by hand to measure how often the judge agrees, split "meets expectation" into a per-item checklist, and take a majority vote of 3 judge calls on failures.
- **Eval cases for ticket mode:** run the sample tickets through the ticket agent with expected labels and a judge check on the draft, including a revision turn. Today ticket mode is verified by hand only.
- **Bigger, real eval set:** collect 100+ real questions from the ops team's inbox, run each case several times to measure variance, and track pass rates per category over time.
- **In-store purchases** (receipt or card lookup) and **exchanges** in the decision helper.
- **Ops-facing gaps dashboard:** cluster declined questions and escalations each week into a list of "documents to write".
- **Voice / mobile** use for employees on the floor.
