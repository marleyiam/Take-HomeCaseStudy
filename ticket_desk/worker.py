"""Runs the ticket agent: drafts a reply for each new inbox ticket, and revises it when the reviewer chats."""

import asyncio
import json
import logging
from collections import defaultdict

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from novamart_assistant import config
from novamart_assistant.agent import ticket_agent

from novamart_assistant.tools import create_escalation_ticket

from .store import TicketStore, build_escalation, derive_label, now, parse_ticket_request

log = logging.getLogger("ticket_desk")
APP_NAME, USER_ID = "ticket_desk", "ops_team"
TICKETS_ROOT = config.ROOT / "data" / "tickets"


def _summarize(tool: str, response: dict) -> dict:
    """Keeps the trace readable: policy search results are reduced to their citations."""
    if tool == "search_policies":
        return {"citations": [r["citation"] for r in response.get("results", [])]}
    return response


class TicketDesk:
    def __init__(self, store: TicketStore | None = None):
        self.store = store or TicketStore(TICKETS_ROOT)
        # Sessions live in SQLite, keyed by ticket id: a reviewer can continue the agent's conversation
        # about a ticket even after the server restarts.
        self.sessions = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{self.store.root / 'sessions.db'}")
        self.runner = Runner(app_name=APP_NAME, agent=ticket_agent, session_service=self.sessions)
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------ inbox

    async def watch_inbox(self, interval: float = 2.0) -> None:
        while True:
            for path in sorted(self.store.inbox.glob("*.json")):
                await self.ingest(path)
            await asyncio.sleep(interval)

    async def ingest(self, path) -> dict | None:
        try:
            request = parse_ticket_request(json.loads(path.read_text()))
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("Rejected %s: %s", path.name, exc)
            path.replace(self.store.rejected / path.name)
            return None
        ticket = self.store.create(request, source=path.name)
        path.replace(self.store.processed / f"{ticket['id']}-{path.name}")
        message = (f"New ticket {ticket['id']} from {ticket['from']}.\n"
                   f"Subject: {ticket['subject']}\n\n{ticket['body']}")
        await self._run_agent(ticket["id"], message)
        return self.store.get(ticket["id"])

    # ------------------------------------------------------------ reviewer actions

    async def reviewer_message(self, ticket_id: str, text: str) -> dict:
        async with self.locks[ticket_id]:
            ticket = self.store.get(ticket_id)
            ticket["thread"].append({"role": "reviewer", "text": text, "at": now()})
            ticket["status"] = "revising"
            self.store.save(ticket)
        message = f"REVIEWER: {text}"
        if ticket.get("escalation"):  # escalations made with the button happen outside the agent's session
            esc = ticket["escalation"]
            message = f"(Context: escalation {esc['ticket_id']} is already open, routed to {esc['routed_to']}.)\n{message}"
        await self._run_agent(ticket_id, message)
        return self.store.get(ticket_id)

    async def escalate(self, ticket_id: str) -> dict:
        """The reviewer's Escalate button: opens the escalation in code, with facts from the tool results."""
        async with self.locks[ticket_id]:
            ticket = self.store.get(ticket_id)
            if ticket["status"] != "awaiting_review":
                raise ValueError(f"ticket {ticket_id} is {ticket['status']}, not awaiting review")
            result = create_escalation_ticket(**build_escalation(ticket))
            if result.get("status") != "created":
                raise ValueError(result.get("message", "escalation failed"))
            self._record_escalation(ticket, result)
            ticket["trace"].append({"tool": "create_escalation_ticket", "result": result, "at": now()})
            ticket["thread"].append({"role": "system", "at": now(),
                                     "text": f"Escalated by the reviewer: {result['ticket_id']} → {result['routed_to'].replace('_', ' ')}."})
            ticket["label"] = derive_label(ticket["trace"], ticket["draft"]["outcome"], ticket["draft"]["reply"])
            self.store.save(ticket)
            return ticket

    @staticmethod
    def _record_escalation(ticket: dict, result: dict) -> None:
        ticket["escalation"] = {"ticket_id": result["ticket_id"], "type": result.get("escalation_type"),
                                "routed_to": result["routed_to"], "created_at": now()}

    async def approve(self, ticket_id: str) -> dict:
        async with self.locks[ticket_id]:
            ticket = self.store.get(ticket_id)
            if ticket["status"] != "awaiting_review" or not ticket["draft"]:
                raise ValueError(f"ticket {ticket_id} is {ticket['status']}, not awaiting review")
            self.store.send(ticket)
            return ticket

    # ------------------------------------------------------------ agent run

    async def _run_agent(self, ticket_id: str, message: str) -> None:
        async with self.locks[ticket_id]:
            if not await self.sessions.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=ticket_id):
                await self.sessions.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=ticket_id)
            steps, submitted, text = [], None, ""
            try:
                content = types.Content(role="user", parts=[types.Part(text=message)])
                async for event in self.runner.run_async(user_id=USER_ID, session_id=ticket_id, new_message=content):
                    for call in event.get_function_calls():
                        if call.name == "submit_draft":
                            submitted = dict(call.args or {})
                    for resp in event.get_function_responses():
                        if resp.name != "submit_draft":
                            steps.append({"tool": resp.name, "result": _summarize(resp.name, resp.response), "at": now()})
                    if event.is_final_response() and event.content and event.content.parts:
                        text += "".join(p.text or "" for p in event.content.parts if not getattr(p, "thought", False))
            except Exception as exc:  # noqa: BLE001 — keep the ticket visible with the error instead of losing it
                log.exception("Agent run failed for %s", ticket_id)
                ticket = self.store.get(ticket_id)
                ticket["status"], ticket["error"] = "error", str(exc)[:500]
                self.store.save(ticket)
                return

            ticket = self.store.get(ticket_id)
            ticket["trace"].extend(steps)
            for step in steps:  # the reviewer may also ask the agent to escalate in chat
                if step["tool"] == "create_escalation_ticket" and step["result"].get("status") == "created":
                    self._record_escalation(ticket, step["result"])
            if text.strip():
                ticket["thread"].append({"role": "agent", "text": text.strip(), "at": now()})
            if submitted is None and not ticket["draft"]:
                # The agent should always submit a draft; if it didn't, show its message rather than nothing.
                submitted = {"reply_to_store": text.strip(),
                             "note_to_reviewer": "The agent did not submit a draft; this is its last message."}
            if submitted:
                ticket["draft"] = {
                    "version": len(ticket["drafts"]) + 1,
                    "reply": submitted.get("reply_to_store", ""),
                    "note": submitted.get("note_to_reviewer", ""),
                    "outcome": submitted.get("outcome", ""),
                    "created_at": now(),
                }
                ticket["drafts"].append(ticket["draft"])
            if ticket["draft"]:
                ticket["label"] = derive_label(ticket["trace"], ticket["draft"]["outcome"], ticket["draft"]["reply"])
            ticket["status"], ticket["error"] = "awaiting_review", None
            self.store.save(ticket)
