"""Review UI + API for the ticket desk. Run with: python -m ticket_desk  (http://localhost:8080)"""

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .store import compose_outgoing, parse_ticket_request
from .worker import TicketDesk

desk = TicketDesk()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    watcher = asyncio.create_task(desk.watch_inbox())
    yield
    watcher.cancel()


app = FastAPI(title="NovaMart Ticket Desk", lifespan=lifespan)


class NewTicket(BaseModel):
    subject: str
    body: str
    sender: str = ""


class ReviewerMessage(BaseModel):
    text: str


def _get(ticket_id: str) -> dict:
    ticket = desk.store.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, f"No ticket {ticket_id}")
    return ticket


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/tickets")
def list_tickets():
    return [{k: t[k] for k in ("id", "subject", "from", "status", "label", "received_at")} for t in desk.store.all()]


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    ticket = _get(ticket_id)
    return {**ticket, "outgoing": compose_outgoing(ticket) if ticket["draft"] else None}


@app.post("/api/tickets", status_code=202)
def create_ticket(new: NewTicket):
    try:
        request = parse_ticket_request({"from": new.sender, "subject": new.subject, "body": new.body})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    path = desk.store.drop_in_inbox(request)  # same path as a file dropped by hand
    return {"queued": path.name}


@app.post("/api/tickets/{ticket_id}/messages")
async def reviewer_message(ticket_id: str, message: ReviewerMessage):
    ticket = _get(ticket_id)
    if ticket["status"] in ("sent", "drafting", "revising"):
        raise HTTPException(409, f"Ticket is {ticket['status']}")
    if not message.text.strip():
        raise HTTPException(422, "Empty message")
    return await desk.reviewer_message(ticket_id, message.text.strip())


@app.post("/api/tickets/{ticket_id}/approve")
async def approve(ticket_id: str):
    _get(ticket_id)
    try:
        return await desk.approve(ticket_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
