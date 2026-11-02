"""Unit tests for the ticket desk's deterministic parts (no API key needed)."""

import json

import pytest

from novamart_assistant.prompts import DECLINE_PHRASE
from novamart_assistant.tools import check_return_eligibility, get_order_status
from ticket_desk.store import TicketStore, build_escalation, compose_outgoing, derive_label, parse_ticket_request


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
        # A full answer that declines only one sub-point is not a policy gap.
        ([step("get_order_status", status="found")], "answered",
         f"You can offer a refund after 3 business days.\n4. Compensation: {DECLINE_PHRASE}.", "ANSWERED"),
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


def laptop_ticket(**overrides):
    """A ticket whose trace holds the real tool results for the over-$500 laptop return."""
    trace = [{"tool": "get_order_status", "result": get_order_status("NM-10511")},
             {"tool": "check_return_eligibility",
              "result": check_return_eligibility("NM-10511", "", "opened_like_new", "changed_mind")}]
    ticket = {"subject": "Laptop return", "from": "Store #087", "body": "Opened, changed mind.",
              "label": "NEEDS_MANAGER_APPROVAL", "trace": trace, "escalation": None,
              "draft": {"reply": "⚠️ Needs manager approval", "outcome": "needs_manager"}}
    return {**ticket, **overrides}


def test_manager_escalation_is_built_from_tool_results():
    args = build_escalation(laptop_ticket())
    assert args["escalation_type"] == "manager_approval"
    assert args["order_id"] == "NM-10511"
    # The numbers come from the rules engine, not from the LLM's draft.
    assert "refund $1019.15" in args["facts_checked"]
    assert "Restocking fee $179.85" in args["facts_checked"]
    assert "manager-approvals-and-escalation › Situations that need a store manager" in args["facts_checked"]
    assert "Approve or decline the $1019.15 refund" in args["recommended_action"]
    assert "Call the store manager" not in args["recommended_action"]  # addressed to the manager, not the store


def test_policy_gap_escalation_lists_the_sections_searched():
    ticket = laptop_ticket(label="POLICY_GAP", trace=[{"tool": "search_policies",
                                                        "result": {"citations": ["price-match › Who we match"]}}])
    args = build_escalation(ticket)
    assert args["escalation_type"] == "knowledge_gap"
    assert "price-match › Who we match" in args["facts_checked"]
    assert args["order_id"] == ""


@pytest.mark.parametrize("overrides", [
    {"label": "DENY"},                                   # nothing to escalate
    {"escalation": {"ticket_id": "ESC-1", "routed_to": "store_manager"}},  # already escalated
])
def test_escalation_is_refused_when_it_does_not_apply(overrides):
    with pytest.raises(ValueError):
        build_escalation(laptop_ticket(**overrides))


def test_outgoing_message_carries_the_escalation_reference():
    ticket = laptop_ticket(escalation={"ticket_id": "ESC-ABC123", "routed_to": "store_manager"})
    message = compose_outgoing(ticket)
    assert "Store manager approval requested: ticket ESC-ABC123." in message
    assert message.index("ESC-ABC123") < message.index("— NovaMart Operations")


def test_escalation_reference_is_not_repeated_when_the_reply_already_names_it():
    ticket = laptop_ticket(escalation={"ticket_id": "ESC-ABC123", "routed_to": "store_manager"},
                           draft={"reply": "Opened ESC-ABC123 for the manager.", "outcome": "needs_manager"})
    assert compose_outgoing(ticket).count("ESC-ABC123") == 1
