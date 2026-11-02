"""File-based ticket storage. Stand-in for the real ticketing system (ServiceNow, Jira, a shared inbox).

    data/tickets/inbox/       drop a ticket here (JSON) and the desk picks it up
    data/tickets/processed/   original inbox files, kept for traceability
    data/tickets/rejected/    inbox files that could not be parsed
    data/tickets/store/       one JSON file per ticket with its full state
    data/tickets/outbox/      approved replies, i.e. what would be sent back to the store
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from novamart_assistant.prompts import DECLINE_PHRASE

# Ticket lifecycle: drafting -> awaiting_review <-> revising -> sent   (error if an agent run fails)
STATUSES = ("drafting", "awaiting_review", "revising", "sent", "error")
OUTCOMES = ("answered", "needs_info", "needs_manager", "policy_gap")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ticket_request(raw: dict) -> dict:
    """Validates an incoming ticket. Required: subject, body. Optional: from."""
    if not isinstance(raw, dict):
        raise ValueError("ticket must be a JSON object")
    subject, body = str(raw.get("subject", "")).strip(), str(raw.get("body", "")).strip()
    if not subject or not body:
        raise ValueError("ticket needs a non-empty 'subject' and 'body'")
    return {"from": str(raw.get("from", "")).strip() or "Unknown store", "subject": subject, "body": body}


def derive_label(trace: list[dict], declared_outcome: str, reply: str) -> str:
    """Queue label for a ticket. Tool results win over what the agent declared, because they are deterministic:
    the last decisive tool result in the conversation decides; otherwise the agent's own outcome is used."""
    label = None
    for step in trace:
        result = step.get("result") or {}
        if step["tool"] == "check_return_eligibility":
            if result.get("status") == "decided":
                label = result["decision"]  # APPROVE / DENY / NEEDS_MANAGER_APPROVAL
            elif result.get("status") in ("needs_more_info", "not_found", "invalid_order_id"):
                label = "NEEDS_INFO"
        elif step["tool"] == "create_escalation_ticket" and result.get("status") == "created":
            label = "ESCALATED"
    if label:
        return label
    if DECLINE_PHRASE.lower() in reply.lower():
        return "POLICY_GAP"
    return {"needs_info": "NEEDS_INFO", "needs_manager": "NEEDS_MANAGER_APPROVAL",
            "policy_gap": "POLICY_GAP"}.get(declared_outcome, "ANSWERED")


def compose_outgoing(ticket: dict) -> str:
    """The message the store receives: their request quoted verbatim (by code, not the LLM), then the reply."""
    quoted = "\n".join(f"> {line}" if line else ">" for line in ticket["body"].splitlines())
    reply = ticket["draft"]["reply"] if ticket.get("draft") else ""
    return f"Re: {ticket['subject']}\n\n{ticket['from']} wrote:\n{quoted}\n\n{reply}\n\n— NovaMart Operations"


class TicketStore:
    def __init__(self, root: Path):
        self.root = root
        self.inbox, self.processed, self.rejected = root / "inbox", root / "processed", root / "rejected"
        self.tickets, self.outbox = root / "store", root / "outbox"
        for d in (self.inbox, self.processed, self.rejected, self.tickets, self.outbox):
            d.mkdir(parents=True, exist_ok=True)

    def _path(self, ticket_id: str) -> Path:
        return self.tickets / f"{ticket_id}.json"

    def next_id(self) -> str:
        numbers = [int(p.stem.split("-")[1]) for p in self.tickets.glob("T-*.json")]
        return f"T-{max(numbers, default=0) + 1:04d}"

    def create(self, request: dict, source: str) -> dict:
        ticket = {
            "id": self.next_id(), **request, "source": source, "received_at": now(),
            "status": "drafting", "label": None, "draft": None, "drafts": [],
            "thread": [], "trace": [], "sent_at": None, "error": None,
        }
        self.save(ticket)
        return ticket

    def save(self, ticket: dict) -> None:
        tmp = self._path(ticket["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(ticket, indent=2, default=str))
        tmp.replace(self._path(ticket["id"]))  # atomic, so the UI never reads a half-written file

    def get(self, ticket_id: str) -> dict | None:
        path = self._path(ticket_id)
        return json.loads(path.read_text()) if path.exists() else None

    def all(self) -> list[dict]:
        return sorted((json.loads(p.read_text()) for p in self.tickets.glob("T-*.json")),
                      key=lambda t: t["id"], reverse=True)

    def drop_in_inbox(self, request: dict) -> Path:
        """Same path as a file dropped by hand: the UI's "new ticket" form writes here too."""
        path = self.inbox / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
        path.write_text(json.dumps(request, indent=2))
        return path

    def send(self, ticket: dict) -> Path:
        ticket["status"], ticket["sent_at"] = "sent", now()
        path = self.outbox / f"{ticket['id']}.json"
        path.write_text(json.dumps({"ticket_id": ticket["id"], "to": ticket["from"], "subject": f"Re: {ticket['subject']}",
                                    "message": compose_outgoing(ticket), "sent_at": ticket["sent_at"]}, indent=2))
        self.save(ticket)
        return path
