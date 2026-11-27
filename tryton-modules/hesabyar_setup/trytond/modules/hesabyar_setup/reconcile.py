# This file is part of HesabYar.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
# This module is licensed under the GNU General Public License v3 or later,
# like all Tryton modules.  See LICENSE in this package.
"""Reconcile move lines over the REST API (US-522, Phase 5-3 Wave B).

``account.move.line.reconcile`` is a plain classmethod — not a button,
not in ``__rpc__`` — so the REST dispatcher refuses it with 403. The
stock path to it is the Reconcile wizard, which cannot run over REST
(wizards need JSON-RPC). HesabYar's settlement engine needs it for
the net-vs-gross wrinkle (docs/tryton-spikes-520.md §5): a tranche
payment is NET while the invoice line is GROSS, so the clearing
``succeed`` cannot reconcile the invoice line on its own. The three
lines that DO sum to zero are [invoice line (gross debit) + fee line
(fee+VAT credit) + clearing leg (net credit)] — all on the gateway
receivable account, all party=gateway — and reconciling them keeps
dunning/aging exact per line (option (a) of the spikes doc; the
fallback, option (b), would leave gross lines showing overdue after
their tranche was paid).

``reconcile_rest`` is the same pattern as ``process_rest`` in this
module: a thin RPC-exposed wrapper that browses the given line ids
and calls stock ``Line.reconcile`` with access checks disabled for
the duration of the call — exactly the scope the Reconcile wizard's
``execute`` RPC disables them for (``trytond/wizard/wizard.py``:
``'execute': RPC(readonly=..., check_access=False)``). No engine
logic is duplicated: ``Line.reconcile`` + ``Reconciliation.check_lines``
still enforce same-account, same-party, zero-sum and valid-line-state,
and raise the engine's own ``ReconciliationError`` when a set does
not reconcile. The adapter pre-validates the same conditions by
reading the lines first, so an engine error here is a backstop, not
the normal path.

Call shape: the no-id REST action route
(``POST /<db>/rest/model/account.move.line/reconcile_rest``) with a
JSON body ``{"line_ids": [...]}`` — the route calls the method with
the body as kwargs, so the ids arrive as plain ints and the wrapper
browses them. The wrapper returns the reconciliation's id/number plus
the reconciled line ids for the caller's audit trail.
"""
from trytond.exceptions import UserError
from trytond.pool import PoolMeta
from trytond.rpc import RPC
from trytond.transaction import without_check_access


class MoveLine(metaclass=PoolMeta):
    __name__ = 'account.move.line'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__rpc__.update({
            'reconcile_rest': RPC(readonly=False, instantiate=0),
        })

    @classmethod
    def reconcile_rest(cls, line_ids):
        """Reconcile the given move lines (by id) via REST.

        At least two lines are required; every id must exist. The
        lines must satisfy the engine's own reconciliation rules
        (same account, same party, debits == credits, all lines in
        'valid' state — a balanced draft move's lines are valid, so
        the clearing leg reconciles even before its move is posted).
        Returns the created reconciliation's id and number.
        """
        if not line_ids or len(set(line_ids)) < 2:
            raise UserError(
                'reconcile_rest needs at least two distinct move lines.')
        lines = cls.search([('id', 'in', list(line_ids))])
        if len(lines) != len(set(line_ids)):
            found = {line.id for line in lines}
            missing = sorted(set(line_ids) - found)
            raise UserError(
                'reconcile_rest: move lines not found: '
                f"{', '.join(str(line_id) for line_id in missing)}"
            )
        with without_check_access():
            reconciliations = cls.reconcile(lines)
        reconciliation = reconciliations[0] if reconciliations else None
        return {
            'reconciliation_id': (
                reconciliation.id if reconciliation else None),
            'number': reconciliation.number if reconciliation else None,
            'line_ids': [line.id for line in lines],
            }
