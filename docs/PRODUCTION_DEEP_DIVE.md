# Production deep dive

The [production note](PRODUCTION_NOTE.md) is the one-page plan. This document goes further into the questions that decide whether the prototype survives contact with NovaMart's real environment. For each topic it says what the prototype does today and what production needs.

## 1. Local prototype vs. a real GCP environment

The prototype calls Gemini with an **AI Studio API key**. That was deliberate: a reviewer can clone the repo and run it in five minutes. Setting up Vertex AI just to try a proof of concept would be overkill.

As soon as the agent runs in any GCP environment (dev, staging or production), it should move to **Vertex AI**, which in this code is a configuration switch (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`). It isn't tested yet. Vertex AI gives us:

- **Identity instead of secrets.** Service accounts and Workload Identity replace a long-lived API key sitting in a `.env` file.
- **Audit and control.** Cloud Audit Logs, VPC Service Controls, customer-managed encryption keys and data residency.
- **Enterprise capacity.** Quotas per project, **provisioned throughput** for predictable latency, and spend on NovaMart's GCP bill with budget alerts.
- **The rest of the platform.** Vertex AI Search for documents, Agent Engine for hosting, the Gen AI Evaluation Service, and BigQuery for logs and analytics, all in the same project and permission model.

## 2. Documents and data pipelines

**Today:** 9 Markdown files with clean `##` headings, so chunking by heading gives precise citations for free.

**In production** documents are messier: PDFs exported from Word, scanned memos, web pages, spreadsheets, slide decks and Confluence pages, from several sources at once. A pipeline has to:

1. **Parse each format.** Vertex AI Search provides layout-aware parsing, and OCR for scans, so tables and headings survive.
2. **Chunk by structure, not by character count.** Keep section headings attached to each chunk, so citations stay meaningful.
3. **Normalize and tag.** Strip boilerplate and record owner, source, version, **effective date** and region or store type on every chunk. Effective dates matter more than they look: a return should be judged by the policy in force when the item was bought, not today's.
4. **Deduplicate and retire.** When a policy is replaced, the old version must stop being served as current.
5. **Validate before publishing.** Reject documents with no owner, an expired date or hidden content (see Security), and report them to Store Ops.

### Choosing parsing, chunking and embeddings

Google offers two managed paths, with different defaults (checked against Google Cloud's documentation in November 2026; the products are being renamed, so confirm against the current pages):

| | Vertex AI Search data store (now "Agent Search") | Vertex AI RAG Engine |
|---|---|---|
| Parsing | Digital, OCR and **layout** parsers | Default parser, **Document AI layout parser** (best for documents with images or charts), or an **LLM parser** that uses Gemini (handles tables, flowcharts and sections spanning slides) |
| Chunking | Layout-based: chunk size 100–500 tokens (default 500), optional **ancestor headings** added to each chunk | Fixed-size: default 1,024 tokens with 256 overlap, configurable; layout-aware when the layout parser is used |
| Embeddings | Managed for you | You choose the embedding model per corpus |
| Vector store | Managed | Managed (Spanner-backed) or your own (e.g. Weaviate, Vertex AI Vector Search) |
| Fits best when | You want Gemini Enterprise connectors and permission-aware search with little to configure | You need to tune retrieval yourself, or keep vectors in a specific database |

**Starting point:** the Vertex AI Search data store, because it plugs into Gemini Enterprise's connectors and respects document permissions, and those matter more than fine control on day one. Use the **layout parser with ancestor headings**, which is the managed version of what the prototype already does by hand: it splits on `##` headings and embeds each chunk with its document title and section name. Move to RAG Engine only if measured retrieval quality calls for it.

**How to choose with evidence instead of taste:** the eval cases already record which policy section each answer must cite (the `cites` checks). That makes a retrieval benchmark for free: for each question, is the right section in the top results? Run it for each candidate parser, chunk size and embedding model, and keep the best. A rule of thumb to test, not assume: smaller chunks suit short, precise rules like "14 days if opened", while layout chunks keep tables, such as the return-window table, in one piece.

**Structured data** shouldn't be forced into text search. The product catalog, recall list or store directory live in **BigQuery** and get dedicated, typed tools (`get_recall_status(sku)`), the same way orders get an API. Each source has its own sync job (scheduled, or triggered by a change event), and a freshness check alerts when a source stops updating.

## 3. More complex flows

The return flow is already a small state machine: collect facts → decide → (manager approval) → reply. Today that state lives in the conversation, and in the ticket desk's status field. Flows that run for hours or days, like "wait for the manager, then tell the store", need **explicit states, saved checkpoints and built-in pauses for human approval**.

**LangGraph** is strong exactly there: graph-defined flows with durable checkpoints and human-approval interrupts, and Agent Engine can host it. ADK offers the same building blocks in its own workflow agents. My default is to stay on ADK, for one stack and native Gemini Enterprise registration. I'd reach for LangGraph only if a flow genuinely needs complex branching and long pauses that ADK makes awkward. Either way the rules engine, tools and eval cases carry over unchanged.

## 4. Security

**Blast radius first.** The agent's tools are read-only, except opening a ticket. Refund decisions are computed in code, not by the model, and money only moves in the POS after a person acts. Every ticket reply is approved by a reviewer. So even a successful attack can produce, at worst, a wrong *draft*.

**PII.**
- Tool results carry only what's needed: first name, items and dates, never addresses or payment details.
- **Sensitive Data Protection** redacts PII from logs and from what's stored for evaluation.
- Retention limits on conversation logs.
- Reviewers and employees only see orders their role allows.

**Prompt injection, direct and indirect.** Injections can come from an employee's message or, more dangerously, from content the agent reads: a ticket forwarded from a customer email, or a policy document someone edited.
- **Treat all of it as data.** The instruction already says so, and Model Armor screens prompts, retrieved passages and replies for injection and jailbreak patterns.
- **Hidden instructions in documents** are stripped at ingestion: white-on-white or tiny text, hidden Word fields, HTML comments, alt text, document metadata, and **invisible Unicode** (zero-width and "tag" characters that can carry instructions a human never sees). Text is re-rendered to plain visible text, and a document whose visible and extracted text differ is quarantined for review.
- **Steganography in images** (instructions hidden in a picture or a scanned page) matters only if images are fed to a multimodal model. OCR output goes through the same filters, and image content is never treated as instructions.
- **Trust the source, not just the content.** Only Store Ops-owned locations are indexed, and changes go through their normal approval.

No single filter catches everything. The protection is the combination: least privilege, deterministic decisions, human approval, and monitoring for unusual tool use.

## 5. Adaptability

- **New file types** plug into the ingestion pipeline (section 2) without touching the agent, because the agent only sees passages with citations.
- **Unstructured content** (emails, chat transcripts, scanned notes) gets OCR and layout parsing, and is tagged as lower-authority than official policies, so the agent prefers the policy when they conflict.
- **New sources** get their own connector or sync job and a small set of eval cases, so we know when retrieval from that source degrades.
- **Multiple languages:** Gemini handles them, but each language gets its own eval cases before launch.

## 6. Maintainability

- **One source of truth per kind of change.**
  - Code, prompts and eval cases are in Git.
  - Business rules move from Python constants to a **versioned rules file owned by Store Ops**, changed through a pull request that must also update the matching policy text and its tests.
  - Documents are versioned by their source system.
- **CI/CD:** every change runs the unit tests and the evaluation suite with a minimum pass rate per category, then deploys to Agent Engine with a canary rollout.
- **Traceability:** every answer logs the model, prompt, rules and document versions it used, so "why did it say that last Tuesday?" has an answer.
- **More flows:** each new flow is a module with its tools, its rules and its eval cases. A coordinating agent routes requests to the right specialist, and Gemini Enterprise shows them as one assistant.
- **More users:** roles (cashier, service desk, store manager, ops reviewer) map to IAM groups. Tools check the role before returning data or allowing an action.

## 7. Scalability

**Prototype limits, stated plainly.** The ticket desk is one process that polls a folder, keeps state in JSON files and SQLite, and locks tickets in memory. That's fine for a demo, and wrong for production.

| Pressure | What changes |
|---|---|
| Tickets arriving continuously | The ticket system publishes to **Pub/Sub**. Workers on Cloud Run or Agent Engine scale with the queue. Processing is **idempotent by ticket id** (a duplicate event doesn't create a second draft), failures retry and then land in a dead-letter queue, and SLA-sensitive tickets are prioritized. |
| Gemini capacity | Provisioned throughput for the baseline, plus rate limiting with backpressure so peaks queue up instead of failing. The model stays a fast Flash model, and context caching cuts the cost of repeating the long instruction. |
| Documents changing continuously | **Incremental indexing** in Vertex AI Search, with a freshness target, e.g. a policy change is live within an hour. A document change also triggers the evaluation cases that cite it. |
| Many reviewers and escalations | Ticket ownership and assignment come from ServiceNow or Jira, not from our UI. Session state moves to a managed store (Vertex AI sessions or Cloud SQL). Escalations land in managers' queues, with dashboards by store and topic. |
| Cost | About 4 model calls per ticket today. We track cost per resolved ticket against the cost of an ops-team reply, with budget alerts. |

## What carries over from the prototype

| Kept as is | Replaced |
|---|---|
| Rules engine and its unit tests | Local Markdown index → Vertex AI Search |
| Tool interfaces (`search_policies`, `get_order_status`, …) | Mock orders → Apigee order API |
| Agent instructions (chat and ticket mode) | JSON/SQLite ticket store → ServiceNow/Jira + managed sessions |
| Eval cases, checks and judge rubric | Eval runner → Vertex AI Gen AI Evaluation Service |
| Label-from-tool-results and human-approval design | AI Studio key → Vertex AI with service accounts |
