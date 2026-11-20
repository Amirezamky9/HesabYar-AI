"""Packaging & shipping bounded context (US-515, Phase 5 wave 2, stream K1).

The user's own requested feature (separate from the design-eng spec):
packaging materials tracked with reorder timing and per-order
packaging + shipping costs. See ``docs/packaging.md`` for the full
design, including the two corrections this story established against
the phased design:

- materials are Tryton ``goods`` with ``consumable=False`` — the
  Tryton ``consumable`` flag *excludes* a product from order points,
  the supply wizard and inventory counts, so the design's
  "consumable" is a business classification (operating expense, never
  depreciation), not the Tryton flag;
- engine-level analytic tagging of consumption moves is impossible
  (``analytic.account.entry`` origins are sale/invoice lines only),
  so consumption and shipment costs carry the order's analytic
  account as a HesabYar-side tag and the per-order cost report
  merges Tryton analytic lines with HesabYar-side records.
"""

from __future__ import annotations

from hesabyar.domain.packaging import calculator, coverage, errors, models, ports

__all__ = ["calculator", "coverage", "errors", "models", "ports"]
