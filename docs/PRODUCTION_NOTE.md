# Production note: NovaMart Assist on Google's enterprise stack

### 1. Gemini Enterprise as the front door, custom agents for the workflows

Store employees already sign in with Google, so **Gemini Enterprise is where they meet the assistant**. It gives them permission-aware search over the policy library out of the box, so plain policy questions need no custom code at all.

The workflows that involve live data and money are different. Return decisions, lost orders and price matches need order lookups, deterministic rules and a human in the loop. Those run as **custom ADK agents on Vertex AI Agent Engine**, registered in Gemini Enterprise so employees still see one assistant. The same agents also work the ops team's existing ticket queue, drafting replies that a person approves.

The prototype runs on an AI Studio API key because anyone can clone and run it in minutes. Any real GCP environment moves to **Vertex AI**: service accounts instead of long-lived keys, audit logs, enterprise quotas and billing, and direct access to Vertex AI Search, Agent Engine, the evaluation service and BigQuery.

### 2. Connecting NovaMart's data

Policies stay where Store Ops writes them (SharePoint, Drive, Confluence) and are indexed by **Vertex AI Search**. Unlike our tidy Markdown files, real policies arrive as PDFs, Word files, web pages and scans, so ingestion parses each format, chunks by layout and tags every chunk with its owner, version and effective date. Structured data, like the product catalog or the recall list, comes from **BigQuery** through dedicated tools rather than text search.

Orders come from a **read-only API behind Apigee**, returning only the fields the agent needs. Return rules move into a versioned rules file owned by Store Ops, and escalations go into ServiceNow or Jira.

### 3. Security

The agent can read and open tickets, but it **never moves money**. Refunds stay in the POS, and the $500 and goodwill decisions stay with a manager. That limits the damage of any mistake or attack.

**PII** is minimized at the source (no addresses or payment data in tool results) and redacted from logs. Tickets, emails and documents are all treated as untrusted. **Model Armor** screens prompts, retrieved passages and replies for injection. Ingestion strips hidden content, such as invisible Unicode, white-on-white text, comments and metadata, before a document can reach the model. Every outgoing ticket reply is still approved by a person.

### 4. Quality, change and scale

The evaluation suite from this repo becomes a **release gate in CI**. It runs whenever prompts, models, rules or documents change, alongside the unit tests for the rules. Prompts, rules and models are versioned, and every answer is logged with the versions it used, so any answer can be traced back.

In production we track:
- **Deflection:** questions resolved without the ops team.
- **Draft acceptance:** ticket replies approved without edits.
- **Declines by topic:** documentation Store Ops should write.
- **Manager override rate:** where our rules or policies are wrong.

Each week an LLM judge checks a sample of conversations, people review the disagreements, and confirmed failures become new eval cases.

At volume, new tickets arrive through Pub/Sub instead of a folder. Workers scale horizontally and skip tickets already in progress. Session state moves to a managed store, and **provisioned throughput** keeps latency predictable.

### 5. Rollout

We start with the **ticket desk on the ops team's existing queue**. Nobody in the stores changes their habits, and every reply is reviewed, so we can measure accuracy safely. Then comes a **four-week chat pilot in 3 to 5 stores**, then a gradual rollout.

More detail on each of these points: [PRODUCTION_DEEP_DIVE.md](PRODUCTION_DEEP_DIVE.md).
