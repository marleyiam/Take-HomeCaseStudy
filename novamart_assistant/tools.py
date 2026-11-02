"""Tools exposed to the agent. ADK turns each function's signature + docstring into a Gemini function declaration."""

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from . import config
from .retrieval import get_index
from .return_rules import CONDITIONS, REASONS, evaluate_return

ORDER_ID_PATTERN = re.compile(r"^NM-\d{5}$")


def _load_orders() -> dict[str, dict]:
    """Mock order API backend. Offsets are turned into real dates relative to today."""
    raw = json.loads(config.ORDERS_PATH.read_text())["orders"]
    today = date.today()
    orders = {}
    for o in raw:
        o = dict(o)
        o["placed_date"] = (today - timedelta(days=o["placed_days_ago"])).isoformat()
        if "delivered_days_ago" in o:
            o["delivered_date"] = (today - timedelta(days=o["delivered_days_ago"])).isoformat()
        if "estimated_delivery_in_days" in o:
            o["estimated_delivery_date"] = (today + timedelta(days=o["estimated_delivery_in_days"])).isoformat()
        orders[o["order_id"]] = o
    return orders


def _normalize_order_id(order_id: str) -> str:
    """Accepts '10482', 'nm10482', 'NM 10482' etc. as NM-10482."""
    match = re.fullmatch(r"(?:NM)?[-\s]?(\d{5})", order_id.strip(), re.IGNORECASE)
    return f"NM-{match.group(1)}" if match else order_id.strip()


def search_policies(query: str) -> dict:
    """Searches NovaMart's internal policy documents (returns, exchanges, warranty, price match,
    gift cards, loyalty, shipping, manager approvals).

    Args:
        query: A natural-language description of what you need, e.g. "restocking fee opened laptop".

    Returns:
        The 4 closest policy passages, each with a `citation` to quote in the answer and a
        similarity `score`. The closest passages are returned even when nothing is relevant, so
        check that the text actually answers the question before using it.
    """
    hits = get_index().search(query, k=4)
    return {
        "results": [
            {"citation": c.citation, "document": c.doc_title, "score": round(s, 3), "text": c.text}
            for c, s in hits
        ]
    }


def get_order_status(order_id: str) -> dict:
    """Looks up a NovaMart online order in the order system.

    Args:
        order_id: The order number, format NM-12345.

    Returns:
        Order status, dates, shipping/tracking details, items (sku, name, category, price,
        final_sale) and the customer's loyalty tier. Returns status "not_found" if it does not exist.
    """
    oid = _normalize_order_id(order_id)
    if not ORDER_ID_PATTERN.match(oid):
        return {"status": "invalid_order_id", "message": f"'{order_id}' is not a valid order number (format NM-12345)."}
    order = _load_orders().get(oid)
    if order is None:
        return {"status": "not_found", "message": f"No order {oid} in the order system."}
    public = {k: v for k, v in order.items() if not k.endswith("_days_ago") and k != "estimated_delivery_in_days"}
    return {"status": "found", "order": public}


def check_return_eligibility(order_id: str, sku: str = "", item_condition: str = "", return_reason: str = "") -> dict:
    """Decides whether an item from an online order can be returned, applying NovaMart's return
    rules deterministically (windows, Gold extension, restocking fee, Final Sale, manager thresholds).

    Only call this once you know all four facts. If something is missing or ambiguous, the result
    has status "needs_more_info" and says what to ask the employee.

    Args:
        order_id: The order number, format NM-12345.
        sku: SKU of the item being returned. Can be empty if the order has a single item.
        item_condition: One of: unopened, opened_like_new, used_or_worn, damaged_by_customer.
        return_reason: One of: changed_mind, defective, damaged_on_arrival, wrong_item.

    Returns:
        decision (APPROVE / DENY / NEEDS_MANAGER_APPROVAL), refund amount and method, restocking
        fee, the reasons and the policy sections they come from, and next steps for the employee.
    """
    lookup = get_order_status(order_id)
    if lookup["status"] != "found":
        return lookup
    order = _load_orders()[lookup["order"]["order_id"]]

    if order["status"] != "delivered":
        return {
            "status": "not_returnable_yet",
            "order_status": order["status"],
            "message": "Only delivered orders can be returned. A 'processing' order can be cancelled instead; a shipped order must be delivered first.",
            "policy_refs": ["orders-shipping-and-cancellations › Cancelling an order"],
        }

    missing = {}
    items = order["items"]
    item = None
    if sku:
        item = next((i for i in items if i["sku"].lower() == sku.strip().lower()), None)
        if item is None:
            missing["sku"] = {"problem": f"SKU '{sku}' is not in this order.",
                              "options": [{"sku": i["sku"], "name": i["name"]} for i in items]}
    elif len(items) == 1:
        item = items[0]
    else:
        missing["sku"] = {"problem": "The order has several items; ask which one is being returned.",
                          "options": [{"sku": i["sku"], "name": i["name"]} for i in items]}
    # Condition doesn't matter for an arrival problem reported in time: it's a full refund either way.
    arrival_issue_in_time = return_reason in ("damaged_on_arrival", "wrong_item") and order["delivered_days_ago"] <= 14
    if item_condition not in CONDITIONS and not arrival_issue_in_time:
        missing["item_condition"] = {"problem": "Unknown or missing condition.", "options": CONDITIONS}
    if return_reason not in REASONS:
        missing["return_reason"] = {"problem": "Unknown or missing reason.", "options": REASONS}
    if missing:
        return {"status": "needs_more_info", "missing": missing}

    return {"status": "decided", **evaluate_return(order, item, item_condition, return_reason)}


def submit_draft(reply_to_store: str, outcome: str, note_to_reviewer: str = "") -> dict:
    """Saves your draft reply to the current ticket so the ops-team reviewer can approve it. Call it again with
    the full text whenever the reply changes; the latest call replaces the previous draft.

    Args:
        reply_to_store: The complete reply to the store employee (decision, why with citations, next steps,
            or the questions they must answer). Don't quote their original request; it is added automatically.
        outcome: One of: answered (the reply resolves the request), needs_info (the reply asks the store for
            missing facts), needs_manager (a manager must approve), policy_gap (the policies don't cover it).
        note_to_reviewer: Optional note only the reviewer sees: assumptions, risks, escalation recommendation.

    Returns:
        Confirmation that the draft was saved.
    """
    # The ticket desk reads the arguments from the function-call event, so nothing needs to be stored here.
    return {"status": "saved", "message": "Draft saved and shown to the reviewer."}


def create_escalation_ticket(
    escalation_type: str,
    customer_request: str,
    facts_checked: str,
    recommended_action: str,
    order_id: str = "",
) -> dict:
    """Opens a ticket for the store manager or the operations team. Only call this after the
    employee has agreed to escalate.

    Args:
        escalation_type: One of: manager_approval (refund > $500, goodwill exception, no-receipt > $50),
            knowledge_gap (question not covered by the policies), order_issue (lost / not received / system error).
        customer_request: What the customer or employee is asking for, in one or two sentences.
        facts_checked: Facts already established (dates, condition, amounts, policy sections, tool results).
        recommended_action: What the assistant recommends, or "none" for a knowledge gap.
        order_id: The order number if there is one.

    Returns:
        The ticket id and the expected response time.
    """
    valid = {"manager_approval", "knowledge_gap", "order_issue"}
    if escalation_type not in valid:
        return {"status": "error", "message": f"escalation_type must be one of {sorted(valid)}"}
    ticket = {
        "ticket_id": f"ESC-{uuid.uuid4().hex[:6].upper()}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "escalation_type": escalation_type,
        "order_id": order_id or None,
        "customer_request": customer_request,
        "facts_checked": facts_checked,
        "recommended_action": recommended_action,
        "routed_to": "store_manager" if escalation_type == "manager_approval" else "operations_team",
    }
    config.ESCALATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with config.ESCALATIONS_PATH.open("a") as f:
        f.write(json.dumps(ticket) + "\n")
    return {"status": "created", "ticket_id": ticket["ticket_id"], "routed_to": ticket["routed_to"],
            "expected_response": "within 4 business hours"}
