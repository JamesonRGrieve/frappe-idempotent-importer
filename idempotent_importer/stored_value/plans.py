# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a source balance change is applied in Square, moving only by what the source changed since the last
sync (money or points spent in Square are never put back). The same rules as wp-woo-idempotent-importer's
StoredValue/GiftCardPlan and LoyaltyPlan.

Gift cards: a card Square holds as PENDING (never loaded) is activated with the whole balance; a drop at the
source is an ADJUST_DECREMENT, at most what the card still holds in Square (anything more was spent in both
stores, and is reported, never taken below zero); a rise is an ADJUST_INCREMENT only when the operator allows
it, since Square charges a load fee on it in the US, Canada and Australia, and is otherwise held and
reported; a card Square holds in any other state than ACTIVE is left alone and reported. A disabled or
expired source card is given a target of zero by its writer.

Loyalty: the AdjustLoyaltyPoints that brings the account to its source balance, never below zero (Square
refuses a negative balance); a drop bigger than the account holds is reported."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ACTIVATE, INCREMENT, DECREMENT = "ACTIVATE", "ADJUST_INCREMENT", "ADJUST_DECREMENT"
PENDING, ACTIVE = "PENDING", "ACTIVE"
ZERO = Decimal(0)


@dataclass(frozen=True)
class GiftCardStep:
	"""``step`` is the activity to write (None for none), ``loaded`` the source balance Square holds once it
	is done, ``held`` why (part of) the change isn't written."""

	step: str | None
	amount: Decimal
	loaded: Decimal
	held: str | None = None


def gift_card_step(
	target: Decimal, loaded: Decimal, square_state: str, square_balance: Decimal, load_increases: bool
) -> GiftCardStep:
	if square_state == PENDING:
		return GiftCardStep(ACTIVATE, target, target) if target > 0 else GiftCardStep(None, ZERO, ZERO)
	change = target - loaded
	if change == 0:
		return GiftCardStep(None, ZERO, loaded)
	if square_state != ACTIVE:
		return GiftCardStep(
			None,
			ZERO,
			loaded,
			f"The source balance changed by {change}, but the card is {square_state} in Square.",
		)
	if change > 0:
		if load_increases:
			return GiftCardStep(INCREMENT, change, target)
		return GiftCardStep(
			None,
			ZERO,
			loaded,
			f"The source balance rose by {change}; loading it into Square is switched off (Square charges a load fee).",
		)
	drop = -change
	if drop <= square_balance:
		return GiftCardStep(DECREMENT, drop, target)
	held = (
		f"The source balance fell by {drop} but the card holds only {square_balance} in Square: "
		f"{drop - square_balance} was spent in both stores."
	)
	if square_balance == 0:
		return GiftCardStep(None, ZERO, target, held)
	return GiftCardStep(DECREMENT, square_balance, target, held)


@dataclass(frozen=True)
class PointsStep:
	points: int
	loaded: int
	held: str | None = None


def points_step(target: int, loaded: int, square_balance: int) -> PointsStep:
	change = target - loaded
	if change >= 0 or -change <= square_balance:
		return PointsStep(change, target)
	return PointsStep(
		-square_balance,
		target,
		f"The source balance fell by {-change} points but the account holds only {square_balance} in Square: "
		f"{-change - square_balance} were spent in both programs.",
	)
