# This file is part of HesabYar.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
# This module is licensed under the GNU General Public License v3 or later,
# like all Tryton modules.  See LICENSE in this package.
"""Assign + complete a draft stock move over the REST API.

A standalone ``stock.move`` out of a storage location cannot be
completed through stock Tryton over REST:

- ``Move.do`` is a button, but its ``readonly`` state blocks it on a
  draft move whenever ``assignation_required`` is true — which it is
  for every move out of a storage (or view) location. The call fails
  with ``You can not call "do"``.
- ``Move.assign`` (draft -> assigned) is a plain workflow transition,
  not a button and not in ``__rpc__``, so the REST dispatcher refuses
  it with 403. ``Move.assign_try`` is equally unreachable.

The Sao UI never hits this: it assigns moves through shipments, and
the move form has no Assign button. HesabYar's packaging consumption
moves are standalone (packaging storage -> production location, no
shipment), so they need a wrapper — the same pattern as
``payment.process_rest`` in this module.

``assign_and_do_rest`` calls stock Tryton's own ``assign`` then
``do`` in one engine transaction. No engine logic is duplicated:
``assign`` checks the origin and transitions the state (it does NOT
check stock availability — a consumption larger than stock goes
negative, which is the behaviour HesabYar wants: a warning signal,
not an error), and ``do`` computes the cost price and posts the
anglo-saxon accounting move.

Access checks are disabled for the duration of the call — the same
scope as ``process_rest`` in this module, and for the same reason:
``do``'s body reads ``res.user`` internally (warning checks), and
the HesabYar service account (the REST application user, admin with
no groups) cannot read that record with checks on — the call fails
with ``You are trying to read records "1" of "User"``. The REST
user is a service account, not an end user: HesabYar enforces its
own authorisation (API key + scopes + tenant/company checks) before
it ever calls this wrapper, so the engine-side check adds nothing
here. Verified live against Tryton 8.2.3.
"""
from trytond.pool import PoolMeta
from trytond.rpc import RPC
from trytond.transaction import without_check_access


class Move(metaclass=PoolMeta):
    __name__ = 'stock.move'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__rpc__.update({
            'assign_and_do_rest': RPC(readonly=False, instantiate=0),
        })

    @classmethod
    def assign_and_do_rest(cls, moves):
        """Assign then complete draft moves (draft -> assigned -> done).

        Only draft moves are touched; anything else (already done,
        cancelled, staged) is left alone, so a repeated call is a
        no-op for completed moves.
        """
        drafts = [m for m in moves if m.state == 'draft']
        if not drafts:
            return
        with without_check_access():
            cls.assign(drafts)
            cls.do(drafts)
