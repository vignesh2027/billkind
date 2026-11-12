"""Explicit state machines for bills and contributions. Anything not listed raises."""

from __future__ import annotations


class IllegalTransition(Exception):
    pass


BILL_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"SUBMITTED", "WITHDRAWN"},
    "SUBMITTED": {"REVIEW", "LISTED", "REJECTED", "WITHDRAWN"},
    "REVIEW": {"LISTED", "REJECTED", "WITHDRAWN"},
    "LISTED": {"FUNDING", "FUNDED", "WITHDRAWN", "EXPIRED", "REVIEW"},
    "FUNDING": {"FUNDED", "WITHDRAWN", "EXPIRED", "REVIEW"},
    "FUNDED": {"PAYOUT_SENT", "PAYOUT_FAILED", "WITHDRAWN"},
    "PAYOUT_SENT": {"PAID", "PAYOUT_FAILED"},
    # PAID: an unclaimed payout (bill marked PAYOUT_FAILED) that the biller claims later still pays the bill.
    "PAYOUT_FAILED": {"PAYOUT_SENT", "WITHDRAWN", "PAID"},
    "PAID": set(),
    "REJECTED": set(),
    "WITHDRAWN": set(),
    "EXPIRED": set(),
}

CONTRIBUTION_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"CAPTURED", "DENIED", "CANCELLED"},
    "CAPTURED": {"PARTIALLY_REFUNDED", "REFUNDED"},
    "PARTIALLY_REFUNDED": {"PARTIALLY_REFUNDED", "REFUNDED"},
    "REFUNDED": set(),
    "DENIED": set(),
    # A buyer can approve after the 15 minute reservation lapsed; the capture still counts and
    # any excess is refunded automatically.
    # CREATED again: a lapsed reservation whose capture PayPal holds as PENDING is held again until it settles.
    "CANCELLED": {"CAPTURED", "CREATED"},
}

# Bills that sponsors may pay into.
OPEN_FOR_FUNDING = {"LISTED", "FUNDING"}
# Bills whose captured money must be refunded if they leave the happy path.
HOLDS_MONEY = {"FUNDING", "FUNDED", "PAYOUT_FAILED"}

BILL_LABELS = {
    "DRAFT": "Draft",
    "SUBMITTED": "Checking",
    "REVIEW": "In human review",
    "REJECTED": "Not accepted",
    "LISTED": "Open for sponsors",
    "FUNDING": "Partly funded",
    "FUNDED": "Fully funded",
    "PAYOUT_SENT": "Paying the biller",
    "PAID": "Paid to biller",
    "PAYOUT_FAILED": "Payout problem",
    "WITHDRAWN": "Withdrawn",
    "EXPIRED": "Expired",
}


def check(table: dict[str, set[str]], current: str, new: str) -> None:
    if current not in table:
        raise IllegalTransition(f"unknown state {current}")
    if new not in table[current]:
        raise IllegalTransition(f"{current} -> {new} is not allowed")


def check_bill(current: str, new: str) -> None:
    check(BILL_TRANSITIONS, current, new)


def check_contribution(current: str, new: str) -> None:
    check(CONTRIBUTION_TRANSITIONS, current, new)
