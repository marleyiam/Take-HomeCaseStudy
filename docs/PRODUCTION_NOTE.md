# Production note: NovaMart Assist on Google Cloud

### 1. Gemini Enterprise plus custom agents
**Gemini Enterprise** is the front door: one assistant with single sign-on and permission-aware search, so plain policy questions need no custom code. Workflows that touch live data or money (returns, lost orders) run as **custom ADK agents on Agent Engine**, registered in Gemini Enterprise; new workflows are added the same way. Every GCP environment uses **Vertex AI** rather than an API key: service accounts, audit logs, enterprise quotas.

### 2. Connecting the data
Policies stay in original sources (SharePoint, Drive, Confluence) and are indexed by **Vertex AI Search**, using Google's managed parsers, layout-aware chunking and embeddings. Each chunk carries owner, version and effective date. Orders come through an API gateway, returning only the fields the agent needs; catalog and recall data come from **BigQuery**; escalations go to Help Desk. If document volume spikes, ingestion becomes event-driven (Pub/Sub, Cloud Run, Document AI).

### 3. Security and governance
The agent reads data and opens tickets but **never moves money**: refunds stay in the POS, and goodwill decisions stay with a manager. Personal data is kept out of tool results and redacted from logs. **Model Armor** screens prompts, retrieved passages and replies for injection, and ingestion strips hidden content from documents. Every answer is logged to BigQuery with the user, tools and versions used.

### 4. Quality over time
The eval suite and unit tests from this repo become a **CI release gate** for any change to prompts, models, rules or documents; return rules move to a versioned file owned by Store Ops. We track **deflection**, **draft acceptance**, **declines by topic** and **manager overrides**. Each week an LLM judge reviews sampled conversations, people check the disagreements, and failures become new eval cases.

### 5. Rollout
**Phase 1, ticket desk (4 weeks):** stores keep opening tickets, the assistant drafts replies and the ops team approves them, so we measure accuracy with no change for stores. **Phase 2, chat pilot in 3–5 stores:** once about 90% of drafts are approved unedited, then store by store.