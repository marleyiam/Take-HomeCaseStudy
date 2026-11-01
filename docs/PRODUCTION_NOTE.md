# Production note — NovaMart Assist on Google's enterprise stack

## 1. Gemini Enterprise vs. custom build: both, with a clear split
- **Gemini Enterprise is the front door.** Employees already sign in with Google. They get one assistant with permission-aware search over connected sources, and plain **policy Q&A needs no custom code**.
- **Custom ADK agents handle workflows.** The return decision helper needs live order data, deterministic rules, multi-turn fact gathering and write actions. A no-code agent can't test or govern that well. It is this prototype, hardened, deployed on **Vertex AI Agent Engine** and **registered in Gemini Enterprise**, so employees see one assistant. Later workflows (lost orders, price match) follow the same pattern.
- Going fully custom would mean rebuilding the UI, connectors and access control. Going fully Gemini Enterprise would leave money-related decisions in untested prompts.

## 2. Connecting real data
- **Policies** stay where Store Ops writes them (SharePoint / Drive), synced by a Gemini Enterprise connector (Vertex AI Search data store). This replaces the prototype's local index; the `search_policies` interface stays the same.
- **Orders:** a **read-only API behind Apigee** in front of the order management system, called with a dedicated service account. It returns only the fields the agent needs (no address or payment data).
- **Return rules** move from Python constants into a **versioned rules file owned by Store Ops** and reviewed together with the policy text, so the code and the documents can't drift apart.
- **Escalations** go to the real ticketing tool (ServiceNow / Jira).

## 3. Security and governance
- **Identity:** single sign-on (SSO). The agent acts **on behalf of the employee**, so document permissions apply and order access is scoped by role.
- **Least privilege:** tools are read-only except ticket creation. **The agent never moves money.** Refunds stay in the POS, and $500+ and goodwill decisions stay with a manager.
- **Data protection:** VPC Service Controls, customer-managed encryption keys (CMEK) and data residency. Under enterprise terms, prompts are not used for training. **Model Armor** screens for prompt injection and PII leaks.
- **Audit:** every conversation, tool call and decision is logged to BigQuery with the employee ID and the policy version used. Prompts, model versions and rules are released through CI.

## 4. Evaluating and monitoring quality
- **Before release:** this repo's eval set (grounded answers, tool use, declines, clarifications, decisions, adversarial cases) is a **CI release gate**, run on every change to the prompt, model, rules or documents, with a minimum pass rate per category. The rules engine has its own unit tests. At scale, move to the Vertex AI Gen AI Evaluation Service.
- **In production:** ADK OpenTelemetry traces go to Cloud Trace, with dashboards on BigQuery. Key metrics:
  - **Deflection:** questions resolved without the ops team, against a pre-launch ticket baseline. This is the business KPI.
  - **Declines and escalations by topic:** a backlog of documents for Store Ops to write.
  - **Manager override rate:** if managers often reverse the agent, the rules or the policy are wrong.
  - **Employee feedback** (thumbs up/down), latency and cost.
- **Ongoing review:** each week an LLM judge checks a sample of conversations for groundedness, with human review of the disagreements. Failures become eval cases. Model versions are pinned, and upgrades go through the eval gate and then a canary rollout.

**Rollout:** a 4-week pilot in 3–5 stores, measured on ops ticket volume and employee satisfaction, then a phased rollout.
