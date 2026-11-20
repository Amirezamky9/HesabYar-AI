"""Days-of-cover computation (US-515) — pure functions, no I/O.

The number the user actually wants: "when will it run out?"
Computed HesabYar-side from recent consumption moves (design
§هـ.۱.۲): on-hand quantity divided by the average daily consumption
over the window (default 30 days).
"""

from __future__ import annotations

from decimal import Decimal

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.packaging.models import DaysOfCover


def compute_days_of_cover(
    *,
    material_id: str,
    quantity_on_hand: Decimal,
    consumed_in_window: Decimal,
    window_days: int,
    last_consumption_date: FiscalDate | None = None,
) -> DaysOfCover:
    """Compute days-of-cover for one material.

    - ``avg_daily = consumed_in_window / window_days`` (the window is
      the full window, not just days with consumption — a material
      consumed twice in 30 days averages over 30 days).
    - No consumption in the window → ``days_of_cover`` is None: there
      is nothing to project from, and inventing a number would lie.
    - Negative stock → negative cover (already out; the UI warns).
    """
    if window_days < 1:
        raise ValueError("window_days must be at least 1")
    if consumed_in_window < 0:
        raise ValueError("consumed_in_window must be non-negative")
    avg_daily = consumed_in_window / Decimal(window_days)
    if avg_daily <= 0:
        days: float | None = None
    else:
        days = float(quantity_on_hand / avg_daily)
    return DaysOfCover(
        material_id=material_id,
        quantity_on_hand=quantity_on_hand,
        window_days=window_days,
        consumed_in_window=consumed_in_window,
        avg_daily_consumption=avg_daily,
        days_of_cover=days,
        last_consumption_date=last_consumption_date,
    )
