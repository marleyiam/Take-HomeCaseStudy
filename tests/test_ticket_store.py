"""Unit tests for the ticket desk's deterministic parts (no API key needed)."""

import json

import pytest

from novamart_assistant.prompts import DECLINE_PHRASE
from ticket_desk.store import TicketStore, compose_outgoing, derive_label, parse_ticket_request


def step(tool, **result):
    return {"tool": tool, "result": result}


@pytest.mark.parametrize(
    "trace, outcome, reply, label",
    [
        ([step("check_return_eligibility", status="decided", decision="DENY")], "answered", "", "DENY"),
        # Tool evidence beats what the agent declared.
        ([step("check_return_eligibility", status="decided", decision="NEEDS_MANAGER_APPROVAL")], "answered", "",
         "NEEDS_MANAGER_APPROVAL"),
        ([step("check_return_eligibility", status="needs_more_info")], "answered", "", "NEEDS_INFO"),
        # The last decisive result wins: info was missing, then the reviewer supplied it.
        ([step("check_return_eligibility", status="needs_more_info"),
          step("check_return_eligibility", status="decided", decision="APPROVE")], "needs_info", "", "APPROVE"),
        ([step("check_return_eligibility", status="decided", decision="NEEDS_MANAGER_APPROVAL"),
          step("create_escalation_ticket", status="created")], "needs_manager", "", "ESCALATED"),
        ([step("search_policies")], "answered", f"{DECLINE_PHRASE}.", "POLICY_GAP"),
        ([step("get_order_status", status="found")], "answered", "It ships tomorrow.", "ANSWERED"),
        ([], "needs_info", "Which order is it?", "NEEDS_INFO"),
    ],
)
def test_derive_label(trace, outcome, reply, label):
    assert derive_label(trace, outcome, reply) == label


def test_parse_ticket_request_validates():
    assert parse_ticket_request({"subject": " Hi ", "body": "x"}) == {"from": "Unknown store", "subject": "Hi", "body": "x"}
    for bad in ({"subject": "", "body": "x"}, {"subject": "x"}, ["not", "a", "dict"]):
        with pytest.raises(ValueError):
            parse_ticket_request(bad)


def test_outgoing_message_quotes_the_request_verbatim():
    ticket = {"subject": "Return", "from": "Store #1", "body": "Line one\n\nLine two",
              "draft": {"reply": "❌ Deny"}}
    message = compose_outgoing(ticket)
    assert "> Line one\n>\n> Line two" in message
    assert message.index("> Line two") < message.index("❌ Deny")


def test_store_lifecycle(tmp_path):
    store = TicketStore(tmp_path)
    first = store.create({"from": "S", "subject": "a", "body": "b"}, source="x.json")
    second = store.create({"from": "S", "subject": "c", "body": "d"}, source="y.json")
    assert (first["id"], second["id"]) == ("T-0001", "T-0002")
    assert [t["id"] for t in store.all()] == ["T-0002", "T-0001"]

    first["draft"] = {"reply": "ok"}
    out = store.send(first)
    sent = json.loads(out.read_text())
    assert sent["to"] == "S" and "ok" in sent["message"]
    assert store.get("T-0001")["status"] == "sent"


def test_drop_in_inbox_writes_a_parseable_file(tmp_path):
    store = TicketStore(tmp_path)
    path = store.drop_in_inbox({"from": "S", "subject": "a", "body": "b"})
    assert path.parent == store.inbox
    assert parse_ticket_request(json.loads(path.read_text()))["subject"] == "a"
