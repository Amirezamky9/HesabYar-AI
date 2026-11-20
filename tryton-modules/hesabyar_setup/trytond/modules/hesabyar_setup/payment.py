# This file is part of HesabYar.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
# This module is licensed under the GNU General Public License v3 or later,
# like all Tryton modules.  See LICENSE in this package.
"""Expose the payment ``process`` step over the REST API.

The payment workflow's forward step (submitted/approved -> processing)
is the ``process`` method, which Tryton only reaches through the
``account.payment.process`` wizard -- and wizards are not reachable via
REST. The ``proceed`` button looks like the forward step but is not:
its button state makes it visible only for succeeded/failed manual
payments (re-processing), so calling it on a submitted payment fails
with "You can not call proceed".

Two subtleties make a plain ``__rpc__`` registration of ``process``
insufficient:

1. ``process`` calls the ``proceed`` button internally, and the
   button wrapper enforces the button's ``invisible`` state whenever
   access checks are on. The wizard works only because its
   ``execute`` RPC is registered with ``check_access=False``
   (``trytond/wizard/wizard.py``).
2. The REST dispatcher never consults the RPC ``check_access`` flag:
   the REST auth wrapper (``trytond/protocols/wrappers.py``) sets the
   request user and enables access checks for the whole request
   unconditionally. So ``check_access=False`` on the RPC entry has no
   effect over REST (verified live: it still failed inside).

Hence this module exposes a thin ``process_rest`` wrapper instead:
it mirrors the Process wizard's ``do_process`` (same eligibility
filter, same grouping, same ``process`` call) with access checks
disabled for the duration of the call -- exactly the scope the
wizard's ``execute`` RPC disables them for. No engine logic is
duplicated: the reconciliation check inside ``proceed`` still runs
(it is method logic, not an access check), as does the journal's
``process_<method>`` hook (a no-op for manual payments, which is
what HesabYar uses).

Two wizard behaviours are deliberately NOT replicated: the overpay
warning (a dismissible user confirmation, meaningless over REST --
the adapter validates amounts itself) and returning the group form
action (a UI concern; the REST action returns the updated payment
record instead).

Why the wrapper must create groups: manual payments require a
``group`` server-side (``Payment.group`` is required when
``process_method`` is manual), and the wizard creates one group per
(company, journal, kind). Calling ``process`` without a group fails
validation with "A value is required for field Group".

See docs/tryton-spikes-502.md §3.8 spike 1 (Path 1b, corrected).
"""
from itertools import groupby

from trytond.exceptions import UserError
from trytond.pool import Pool, PoolMeta
from trytond.rpc import RPC
from trytond.tools import sortable_values
from trytond.transaction import without_check_access


class Payment(metaclass=PoolMeta):
    __name__ = 'account.payment'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__rpc__.update({
            'process_rest': RPC(readonly=False, instantiate=0),
            'process_rest_batch': RPC(readonly=False, instantiate=0),
        })

    @classmethod
    def process_rest(cls, payments):
        """Process payments (submitted/approved -> processing) via REST.

        Mirrors the ``account.payment.process`` wizard's ``do_process``
        (see the module docstring for why a wrapper, and not a plain
        ``__rpc__`` registration of ``process``, is needed).
        """
        cls._process_rest_payments(payments)

    @classmethod
    def process_rest_batch(cls, payment_ids):
        """Process several payments at once via REST (US-522).

        Same core as :meth:`process_rest`, but the payments arrive as
        ids in the no-id REST action body (``{"payment_ids": [...]}``)
        instead of as the single record of the id route. A settlement
        batch's payments are processed in ONE call so the wizard's
        grouping puts them in ONE ``account.payment.group`` (one group
        per (company, journal, kind)) — per-payment calls would give
        each payment its own group. Payments that do not exist are
        refused instead of silently skipped.
        """
        if not payment_ids:
            return
        payments = cls.search([('id', 'in', list(payment_ids))])
        if len(payments) != len(set(payment_ids)):
            found = {payment.id for payment in payments}
            missing = sorted(set(payment_ids) - found)
            raise UserError(
                'process_rest_batch: payments not found: '
                f"{', '.join(str(payment_id) for payment_id in missing)}"
            )
        cls._process_rest_payments(payments)

    @classmethod
    def _process_rest_payments(cls, payments):
        """The shared process core (wizard ``do_process`` mirror)."""
        pool = Pool()
        Group = pool.get('account.payment.group')

        # Same eligibility filter as the wizard: approved payments,
        # or submitted receivable payments. Submitted payables must be
        # approved first; anything else (e.g. already processing) is
        # left untouched, exactly as the wizard leaves it.
        payments = [
            p for p in payments
            if p.state == 'approved'
            or (p.state == 'submitted' and p.kind == 'receivable')]
        if not payments:
            return

        def group_key(payment):
            return (
                ('company', payment.company),
                ('journal', payment.journal),
                ('kind', payment.kind),
                )

        def new_group(values):
            group = Group(**values)
            group.save()
            return group

        process_method_with_group = cls.process_method_with_group()
        with without_check_access():
            for key, grouped in groupby(
                    sorted(payments, key=sortable_values(group_key)),
                    key=group_key):
                key = dict(key)
                process_method = key['journal'].process_method
                if process_method in process_method_with_group:
                    def group(values=key):
                        return new_group(values)
                else:
                    group = None
                cls.process(list(grouped), group)
