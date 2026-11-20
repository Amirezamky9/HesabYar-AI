# This file is part of HesabYar.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
# This module is licensed under the GNU General Public License v3 or later,
# like all Tryton modules.  See LICENSE in this package.
"""Physical custody log for cheque leaves (US-530, phased design §5 §1.1).

Every move of a leaf between cashbox / vault / bank / endorsee /
returned-to-issuer is one append-only, read-only row: origin,
destination, time, actor, reason. A custody move is a custody event,
never a journal — it is the basis of the cash-box count (§5 §6) and
of report 16 (cheques by storage location).
"""
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import ModelSQL, ModelView, fields

from .check import STORAGE_LOCATIONS

__all__ = ['CustodyLog']


class CustodyLog(ModelSQL, ModelView):
    """Append-only, read-only log of physical leaf moves."""
    __name__ = 'account.check.custody_log'

    check = fields.Many2One('account.check', 'Cheque', required=True,
        ondelete='CASCADE')
    origin = fields.Selection(STORAGE_LOCATIONS, 'Origin', required=True)
    destination = fields.Selection(STORAGE_LOCATIONS, 'Destination',
        required=True)
    moved_at = fields.DateTime('Moved At', required=True)
    actor = fields.Many2One('res.user', 'Actor', ondelete='SET NULL')
    reason = fields.Text('Reason')

    def get_rec_name(self, name):
        return (
            f'{self.check.rec_name if self.check else "?"}: '
            f'{self.origin} -> {self.destination}')

    @classmethod
    def write(cls, *args):
        raise UserError(gettext(
            'The custody log is append-only: a recorded move cannot '
            'be changed. Record a correcting move instead.'))

    @classmethod
    def delete(cls, records):
        """A log row is deleted only with its cheque, never on its own.

        Tryton runs the ORM cascade (parent cheque deleted) with
        access checks off (``_check_access=False`` in the
        transaction context); a direct delete — REST, SAO, or any
        other caller — runs with them on. A row whose cheque is being
        deleted goes away with it (its lifetime is owned by the
        cheque); a direct delete is refused, so the log stays
        append-only from every outside caller.
        """
        from trytond.transaction import Transaction
        if Transaction().context.get('_check_access', True):
            raise UserError(gettext(
                'The custody log is append-only: a recorded move '
                'cannot be deleted. Record a correcting move instead.'))
        super().delete(records)
