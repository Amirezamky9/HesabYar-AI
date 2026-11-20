"""Approximate price calculator (US-515) — pure functions, no I/O.

Goods + packaging (current cost) + shipment (carrier rate) + margin.
The result is ALWAYS labelled approximate and NEVER auto-applies to
an invoice — there is no invoice interaction on this path at all.

Rounding: a packaging line's cost is ``quantity x unit cost`` where
the unit cost is the engine's average cost price (possibly
fractional per unit). Line totals round HALF_UP to whole Rials and
the rounding is visible on the quote (the line's exact cost is not
hidden — the quote carries the unit cost, so the rounding is
reproducible). Margin percent applies to the subtotal
(goods + packaging + shipment).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from hesabyar.domain.packaging.errors import InvalidPriceQuoteError
from hesabyar.domain.packaging.models import PriceQuote, PriceQuoteLine


@dataclass(frozen=True, slots=True)
class PackagingQuoteInput:
    """One packaging material going into a quote."""

    material_id: str
    material_name: str
    quantity: Decimal
    unit_cost_irr: Decimal
    quantity_on_hand: Decimal
    stock_known: bool = True


@dataclass(frozen=True, slots=True)
class PriceQuoteRequest:
    """Inputs for one approximate quote."""

    goods_cost_irr: int
    packaging: tuple[PackagingQuoteInput, ...] = ()
    shipment_cost_irr: int = 0
    carrier_name: str | None = None
    margin_percent: Decimal | None = None
    margin_fixed_irr: int | None = None


def _round_irr(exact: Decimal) -> int:
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_price_quote(request: PriceQuoteRequest) -> PriceQuote:
    """Compute an approximate quote from current costs and entered rates."""
    if request.goods_cost_irr < 0:
        raise InvalidPriceQuoteError("Goods cost must be non-negative.")
    if request.shipment_cost_irr < 0:
        raise InvalidPriceQuoteError("Shipment cost must be non-negative.")
    if request.margin_percent is not None and request.margin_fixed_irr is not None:
        raise InvalidPriceQuoteError(
            "Give either a margin percent or a fixed margin amount, not both."
        )
    if request.margin_percent is not None and request.margin_percent < 0:
        raise InvalidPriceQuoteError("Margin percent must be non-negative.")
    if request.margin_fixed_irr is not None and request.margin_fixed_irr < 0:
        raise InvalidPriceQuoteError("Fixed margin must be non-negative.")

    lines: list[PriceQuoteLine] = []
    warnings: list[str] = []
    packaging_cost = 0
    for item in request.packaging:
        if item.quantity <= 0:
            raise InvalidPriceQuoteError(
                f"Packaging quantity for {item.material_name!r} must be greater than zero."
            )
        if item.unit_cost_irr < 0:
            raise InvalidPriceQuoteError(
                f"Unit cost for {item.material_name!r} must be non-negative."
            )
        line_cost = _round_irr(item.quantity * item.unit_cost_irr)
        stale = item.stock_known and item.quantity_on_hand <= 0
        if stale:
            # Zero stock → the cost price is the last purchase price,
            # flagged stale (design §هـ.۱.۸): the quote uses it but says so.
            warnings.append(
                f"Material {item.material_name!r} is out of stock; "
                "its cost is the last purchase price (stale)."
            )
        elif not item.stock_known:
            warnings.append(
                f"Stock level for {item.material_name!r} is unknown "
                "(no packaging location configured)."
            )
        lines.append(
            PriceQuoteLine(
                material_id=item.material_id,
                material_name=item.material_name,
                quantity=item.quantity,
                unit_cost_irr=item.unit_cost_irr,
                line_cost_irr=line_cost,
                stale_price=stale,
            )
        )
        packaging_cost += line_cost

    subtotal = request.goods_cost_irr + packaging_cost + request.shipment_cost_irr
    if request.margin_percent is not None:
        margin = _round_irr(Decimal(subtotal) * request.margin_percent / Decimal(100))
    elif request.margin_fixed_irr is not None:
        margin = request.margin_fixed_irr
    else:
        margin = 0
    return PriceQuote(
        goods_cost_irr=request.goods_cost_irr,
        packaging_cost_irr=packaging_cost,
        shipment_cost_irr=request.shipment_cost_irr,
        margin_irr=margin,
        total_irr=subtotal + margin,
        approximate=True,
        lines=tuple(lines),
        warnings=tuple(warnings),
        margin_percent=request.margin_percent,
        carrier_name=request.carrier_name,
    )
