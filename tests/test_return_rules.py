"""Unit tests for the deterministic part of the agent. Run with: pytest -q (no API key needed)."""

import pytest

from novamart_assistant.tools import check_return_eligibility, get_order_status


@pytest.mark.parametrize(
    "order_id, sku, condition, reason, decision, refund",
    [
        ("NM-10482", "", "opened_like_new", "changed_mind", "DENY", 0.0),  # opened small appliance: 14-day window
        ("NM-10511", "", "opened_like_new", "changed_mind", "NEEDS_MANAGER_APPROVAL", 1019.15),  # fee, then > $500
        ("NM-10544", "", "opened_like_new", "changed_mind", "APPROVE", 126.65),  # Gold extension + 15% fee
        ("NM-10544", "", "unopened", "changed_mind", "APPROVE", 149.00),  # no fee when unopened
        ("NM-10433", "", "opened_like_new", "defective", "DENY", 0.0),  # past window -> warranty
        ("NM-10455", "", "opened_like_new", "changed_mind", "NEEDS_MANAGER_APPROVAL", 45.00),  # goodwill range
        ("NM-10590", "", "unopened", "changed_mind", "DENY", 0.0),  # Final Sale
        ("NM-10590", "", "", "damaged_on_arrival", "APPROVE", 39.00),  # arrival damage beats Final Sale
        ("NM-10620", "", "", "damaged_on_arrival", "NEEDS_MANAGER_APPROVAL", 649.00),  # > $500
        ("NM-10530", "AP-JKT-RN-M", "used_or_worn", "changed_mind", "DENY", 0.0),  # worn apparel
        ("NM-10530", "AP-JNS-SL-32", "opened_like_new", "changed_mind", "APPROVE", 59.00),
        ("NM-10482", "", "damaged_by_customer", "defective", "DENY", 0.0),
    ],
)
def test_decisions(order_id, sku, condition, reason, decision, refund):
    result = check_return_eligibility(order_id, sku, condition, reason)
    assert result["status"] == "decided"
    assert result["decision"] == decision
    assert result["refund_amount"] == pytest.approx(refund)
    assert result["policy_refs"]


def test_multi_item_order_asks_which_item():
    result = check_return_eligibility("NM-10530", "", "unopened", "changed_mind")
    assert result["status"] == "needs_more_info"
    assert set(result["missing"]) == {"sku"}
    assert len(result["missing"]["sku"]["options"]) == 2


def test_missing_facts_are_reported():
    result = check_return_eligibility("NM-10482")
    assert set(result["missing"]) == {"item_condition", "return_reason"}


def test_undelivered_orders_are_not_returnable():
    assert check_return_eligibility("NM-10602", "", "unopened", "changed_mind")["status"] == "not_returnable_yet"


@pytest.mark.parametrize("raw", ["NM-10482", "nm10482", "NM 10482", "10482"])
def test_order_id_normalization(raw):
    assert get_order_status(raw)["order"]["order_id"] == "NM-10482"


def test_unknown_order():
    assert get_order_status("NM-99999")["status"] == "not_found"
    assert get_order_status("hello")["status"] == "invalid_order_id"
