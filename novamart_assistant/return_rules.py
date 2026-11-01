"""Deterministic return-eligibility rules.

The LLM is good at gathering facts from a conversation and explaining a decision,
but it should not do date arithmetic or apply fee percentages on its own. This
module encodes the rules from returns-and-refunds.md,
damaged-defective-and-wrong-items.md, novarewards-loyalty.md and
manager-approvals-and-escalation.md, and reports which policy section each step
came from so the agent can cite it.
"""

CATEGORY_WINDOW_DAYS = {"general": 30, "small_appliance": 30, "electronics": 15, "apparel": 60}
OPENED_SMALL_APPLIANCE_WINDOW_DAYS = 14
GOLD_EXTENSION_DAYS = 15
GOODWILL_GRACE_DAYS = 7
ARRIVAL_ISSUE_REPORT_DAYS = 14
RESTOCKING_FEE_RATE = 0.15
MANAGER_REFUND_THRESHOLD = 500.00

CONDITIONS = {
    "unopened": "Sealed, never opened",
    "opened_like_new": "Opened but unused / like new, all parts and tags included",
    "used_or_worn": "Used, or apparel worn / tags removed",
    "damaged_by_customer": "Physical damage caused after delivery (drop, liquid, missing parts)",
}
REASONS = {
    "changed_mind": "Customer no longer wants it (no fault with the item)",
    "defective": "Item fails in normal use (does not power on, stops working)",
    "damaged_on_arrival": "Item arrived broken or damaged",
    "wrong_item": "Customer received a different item than ordered",
}

RETURNS = "returns-and-refunds"
DAMAGED = "damaged-defective-and-wrong-items"
APPROVALS = "manager-approvals-and-escalation"


def evaluate_return(order: dict, item: dict, condition: str, reason: str) -> dict:
    days = order["delivered_days_ago"]
    reasons: list[str] = []
    refs: list[str] = []

    def result(decision: str, fee: float = 0.0, next_steps: str = "") -> dict:
        refund = round(item["price"] - fee, 2) if decision != "DENY" else 0.0
        if decision == "APPROVE" and refund > MANAGER_REFUND_THRESHOLD:
            decision = "NEEDS_MANAGER_APPROVAL"
            reasons.append(f"Eligible, but refunds above ${MANAGER_REFUND_THRESHOLD:.0f} need store manager approval.")
            refs.append(f"{APPROVALS} › Situations that need a store manager")
            next_steps = "Call the store manager to approve before processing. Do not promise the refund to the customer yet."
        return {
            "decision": decision,
            "item": item["name"],
            "sku": item["sku"],
            "item_price": item["price"],
            "days_since_delivery": days,
            "restocking_fee": round(fee, 2),
            "refund_amount": refund,
            "refund_method": _refund_method(order) if decision != "DENY" else None,
            "reasons": reasons,
            "policy_refs": refs,
            "next_steps": next_steps,
        }

    # 1. Arrival problems reported in time override almost everything, including Final Sale.
    if reason in ("damaged_on_arrival", "wrong_item"):
        if days <= ARRIVAL_ISSUE_REPORT_DAYS:
            reasons.append(f"{REASONS[reason]} reported {days} days after delivery (limit {ARRIVAL_ISSUE_REPORT_DAYS}): full refund or replacement, no fee, even for Final Sale.")
            refs.append(f"{DAMAGED} › Damaged on arrival or wrong item shipped")
            return result("APPROVE", next_steps="Photograph the damage / wrong item and attach it to the return in the POS. Offer refund or replacement.")
        reasons.append(f"Arrival issue reported after {days} days (limit {ARRIVAL_ISSUE_REPORT_DAYS}); evaluated as a standard return.")
        refs.append(f"{DAMAGED} › Damaged on arrival or wrong item shipped")
        reason = "changed_mind"

    # 2. Hard exclusions.
    if item.get("final_sale"):
        reasons.append("Item is Final Sale and cannot be returned.")
        refs.append(f"{RETURNS} › Items that cannot be returned")
        return result("DENY", next_steps="Explain that Final Sale items are not returnable.")
    if condition == "damaged_by_customer":
        reasons.append("Damage caused by the customer is not returnable.")
        refs.append(f"{RETURNS} › Items that cannot be returned")
        refs.append(f"{DAMAGED} › How to tell \"defective\" from \"damaged by customer\"")
        return result("DENY", next_steps="If unsure whether it is a defect or customer damage, ask the store manager to inspect the item.")

    # 3. Return window.
    category = item["category"]
    window = CATEGORY_WINDOW_DAYS[category]
    if category == "small_appliance" and condition != "unopened":
        window = OPENED_SMALL_APPLIANCE_WINDOW_DAYS
    window_note = f"{category.replace('_', ' ')} window is {window} days"
    if order["customer"]["loyalty_tier"] == "gold":
        window += GOLD_EXTENSION_DAYS
        window_note += f" + {GOLD_EXTENSION_DAYS} days NovaRewards Gold extension = {window} days"
        refs.append("novarewards-loyalty › Tiers")
    refs.append(f"{RETURNS} › Return windows by category")
    within_window = days <= window

    # 4. Defects: inside the window → full refund; outside → warranty, not the store.
    if reason == "defective":
        if within_window:
            reasons.append(f"Defective item, {days} days since delivery; {window_note}: full refund, no restocking fee.")
            refs.append(f"{DAMAGED} › Defective items (stopped working)")
            return result("APPROVE", next_steps="Process a full refund or an exchange, customer's choice.")
        reasons.append(f"Defective item, but {days} days since delivery is past the {window_note}.")
        refs.append(f"{DAMAGED} › Defective items (stopped working)")
        refs.append("warranty-and-novacare › Manufacturer warranty")
        return result("DENY", next_steps="Direct the customer to the 1-year manufacturer warranty (print the support contact from the POS) or their NovaCare plan if they have one.")

    # 5. Change of mind.
    if condition == "used_or_worn" and category != "electronics":
        reasons.append("Change-of-mind returns must be unused / like new (apparel: unworn with tags).")
        refs.append(f"{RETURNS} › Return windows by category")
        return result("DENY", next_steps="Explain the condition requirement. If the item is actually faulty, re-check it as a defect.")
    fee = 0.0
    if category == "electronics" and condition != "unopened":
        fee = item["price"] * RESTOCKING_FEE_RATE
    if within_window:
        reasons.append(f"{days} days since delivery; {window_note}: within the window.")
        if fee:
            reasons.append(f"Opened electronics returned for change of mind: {RESTOCKING_FEE_RATE:.0%} restocking fee (${fee:.2f}).")
            refs.append(f"{RETURNS} › Restocking fee")
        return result("APPROVE", fee=fee, next_steps="Process the return in the POS.")
    if days <= window + GOODWILL_GRACE_DAYS:
        reasons.append(f"{days} days since delivery is {days - window} day(s) past the {window_note}, inside the {GOODWILL_GRACE_DAYS}-day goodwill range only a manager can approve.")
        refs.append(f"{APPROVALS} › Situations that need a store manager")
        decision = result("APPROVE", fee=fee)
        decision["decision"] = "NEEDS_MANAGER_APPROVAL"
        decision["next_steps"] = "Ask the store manager for a goodwill exception. Do not promise it to the customer."
        return decision
    reasons.append(f"{days} days since delivery is past the {window_note} (and past the {GOODWILL_GRACE_DAYS}-day goodwill range).")
    return result("DENY", next_steps="Explain the return window. If the item is faulty, the manufacturer warranty may apply.")


def _refund_method(order: dict) -> str:
    if order["payment_method"] == "cash":
        return "Cash up to $200; above that, store credit or a mailed check (customer's choice)"
    return f"Original payment method ({order['payment_method'].replace('_', ' ')}), 3–5 business days"
