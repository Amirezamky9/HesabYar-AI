# This file is part of HesabYar.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
# This module is licensed under the GNU General Public License v3 or later,
# like all Tryton modules.  See LICENSE in this package.
"""Extend ``account.move`` so cheque journals link back to their cheque.

Stock Tryton's ``Move._get_origin`` returns only ``account.fiscalyear``
and ``account.move``; a cheque journal whose ``origin`` is an
``account.check`` would fail selection validation. This module adds
``account.check`` to the origin selection — the same extension
``account_invoice`` makes for ``account.invoice`` — so
``Check.moves`` (One2Many via ``origin``) reads the journal of every
transition, and the move description carries the سند number for
engine-side attribution (a plain ``moves`` read lists ids only).
"""
from trytond.pool import PoolMeta

__all__ = ['Move']


class Move(metaclass=PoolMeta):
    __name__ = 'account.move'

    @classmethod
    def _get_origin(cls):
        return super()._get_origin() + ['account.check']
