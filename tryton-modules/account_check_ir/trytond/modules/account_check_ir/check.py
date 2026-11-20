# This file is part of HesabYar.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
# This module is licensed under the GNU General Public License v3 or later,
# like all Tryton modules.  See LICENSE in this package.
"""Iranian cheque registry (US-530 scaffold + US-531 transitions,
Phase 5-4 / ۵ث).

``account.checkbook``, ``account.check`` and
``account.check.endorsement`` — the model skeleton (US-530) plus the
received-commercial lifecycle (US-531): ``Check.transition_rest``
runs one transition — state change + its §2.4 journal (سند ۱–۶) —
in a single Tryton transaction. The Sayyad field shapes for the
Fintech scopes are UNVERIFIED (§5 §9.2 #1–3); no Fintech field is
guessed here.

Conventions (phased design §5 §1.1):

* One model for received and issued cheques (``kind``), with
  ``purpose`` (payment | collateral) mandatory from day one —
  collateral cheques never enter receivables/payables or the cash
  forecast.
* ``sayad_id`` is the 16-digit Sayyad identifier — different from
  the leaf (serial) number. It is unique among all live states; the
  exclusion below exempts ``expired_unreceived`` and
  ``rejected_mismatch`` so a never-arrived (or mismatch-rejected)
  leaf can be re-registered with the same id. The old record never
  revives.
* ``sayad_status`` is Sayyad's view of the leaf and is tracked
  separately from the accounting ``state``.
* Amounts are whole Rial (``digits=(16, 0)`` — HesabYar MoneyIRR);
  dates are Gregorian ``Date`` in Tryton and shown as Jalali in the
  HesabYar UI.
* ``installment_leg`` is a plain HesabYar reference until Phase ۵ج
  builds the real installment-leg model; ``secured_obligation`` is
  likewise a reference (installment plan / contract / invoice)
  until a real obligation model exists.
* The leaf scan/photo is the ``attachment`` link to one
  ``ir.attachment`` record; further files use Tryton's standard
  attachment list on the record.
"""
import datetime
import re

from sql.operators import Equal
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import Exclude, ModelSQL, ModelView, fields
from trytond.pool import Pool
from trytond.pyson import Eval
from trytond.rpc import RPC
from trytond.transaction import Transaction, without_check_access

__all__ = ['Checkbook', 'Check', 'CheckEndorsement']

_SAYAD_ID_RE = re.compile(r'\d{16}')

#: States in which a cheque record is dead for uniqueness purposes:
#: a leaf that never arrived, or was rejected on mismatch, may be
#: re-registered under the same sayad_id (phased design §5 §1.1).
SAYAD_ID_EXEMPT_STATES = ('expired_unreceived', 'rejected_mismatch', 'voided')

#: Issued-cheque states that consume a checkbook leaf. A called-in
#: issued collateral cheque consumed its leaf too (the money left
#: the bank) — the count filters kind=issued, so the received
#: ``called_in`` state never leaks in here.
USED_LEAF_STATES = ('issued', 'cleared', 'bounced', 'called_in')

CHECK_KINDS = [
    ('received', 'Received'),
    ('issued', 'Issued'),
    ]

CHECK_PURPOSES = [
    ('payment', 'Payment'),
    ('collateral', 'Collateral'),
    ]

SAYAD_STATUSES = [
    ('unregistered', 'Unregistered'),
    ('registered', 'Registered'),
    ('confirmed', 'Confirmed'),
    ('transferred', 'Transferred'),
    ]

CHECK_STATES = [
    ('registered', 'Registered'),
    ('held', 'Held'),
    ('in_clearing', 'In Clearing'),
    ('cleared', 'Cleared'),
    ('bounced', 'Bounced'),
    ('returned', 'Returned'),
    ('endorsed', 'Endorsed'),
    ('issued', 'Issued'),
    ('voided', 'Voided'),
    ('expired_unreceived', 'Expired — Leaf Never Arrived'),
    ('rejected_mismatch', 'Rejected — Mismatch'),
    ('called_in', 'Called In'),
    ]

STORAGE_LOCATIONS = [
    (None, ''),
    ('cashbox', 'Cashbox'),
    ('vault', 'Vault'),
    ('bank', 'Bank'),
    ('with_endorsee', 'With Endorsee'),
    ('returned_to_issuer', 'Returned to Issuer'),
    ]

ENDORSEE_TYPES = [
    (None, ''),
    ('supplier', 'Supplier'),
    ('other', 'Other'),
    ]

SOURCES = [
    ('site', 'Site'),
    ('manual', 'Manual'),
    ]

#: (source states, target state) per transition name — the engine
#: mirror of the HesabYar state machine
#: (``hesabyar/domain/cheques/state_machine.py``). ``return`` is the
#: only transition with several source states; ``deposit`` from
#: ``bounced`` (re-presentment) is refused explicitly in
#: :meth:`Check.transition_rest`, not by this table. ``clear`` /
#: ``bounce`` run from ``issued`` for issued cheques (سند ۱۱/۱۲) as
#: well as from ``in_clearing`` for received ones (سند ۳/۴);
#: ``deposit`` / ``endorse`` / ``return`` run from ``called_in``
#: for a received collateral cheque that was called in (§2.2 —
#: it follows §2.1 from there); ``return`` from ``issued`` is the
#: issued-collateral return (memorandum سند ۸ in the issued
#: direction).
TRANSITION_STATES = {
    'confirm_receipt': (('registered',), 'held'),
    'deposit': (('held', 'called_in'), 'in_clearing'),
    'clear': (('in_clearing', 'issued', 'called_in'), 'cleared'),
    'bounce': (('in_clearing', 'issued'), 'bounced'),
    'endorse': (('held', 'called_in'), 'endorsed'),
    'return': (('held', 'bounced', 'issued'), 'returned'),
    'expire': (('registered',), 'expired_unreceived'),
    'reject_mismatch': (('registered',), 'rejected_mismatch'),
    'issue': (('registered',), 'issued'),
    'void': (('issued',), 'voided'),
    'call_in': (('held',), 'called_in'),
    'unexpected_call_in': (('issued',), 'called_in'),
    'endorsed_bounce': (('endorsed',), 'bounced'),
    # Site cancellation of an intake before held (US-533): no journal.
    'void_intake': (('registered',), 'voided'),
    }

#: §2.4 journal (سند) number per transition — only for the
#: transitions whose number never varies. ``clear`` / ``bounce`` /
#: ``return`` / ``confirm_receipt`` vary by kind/purpose, so their
#: number comes from :meth:`Check._journal_number` instead (see
#: below); ``call_in`` posts TWO moves (۹الف + ۹ب) and
#: ``unexpected_call_in`` posts the memorandum reversal whose
#: number is the mirror of سند ۷, not a §2.4 row.
TRANSITION_JOURNAL = {
    'deposit': '۲',
    'endorse': '۵',
    'issue': '۱۰',
    'void': '۱۳',
    }

#: Chart account codes of the §2.4 journal designs (resolved per
#: company at transition time — never hard-coded ids).
#: ``2.1.1`` is Trade Payables (حساب‌های پرداختنی — the §2.4 debit
#: of سند ۵/۱۰ and credit of سند ۱۲/۱۳); ``2.1.5`` is Notes Payable
#: (اسناد پرداختنی — the credit of سند ۱۰ and debit of سند ۱۱/۱۲/۱۳).
#: US-531 labelled 2.1.5 "payables" and debited it for سند ۵; that
#: was the wrong account (Notes Payable, not Trade Payables) and is
#: corrected here (US-532).
ACCOUNT_HELD = '1.2.4'
ACCOUNT_IN_CLEARING = '1.2.5'
ACCOUNT_RECEIVABLES = '1.2.1'
ACCOUNT_BOUNCED = '1.2.9'
ACCOUNT_PAYABLES = '2.1.1'
ACCOUNT_NOTES_PAYABLE = '2.1.5'
#: Memorandum (off-balance-sheet) accounts — collateral only (§2.2):
#: received collateral uses 7.1.1 / 7.2.1, issued collateral 7.1.2 /
#: 7.2.2. A collateral journal NEVER touches a commercial account.
ACCOUNT_COLLATERAL_RECEIVED = '7.1.1'
ACCOUNT_CONTRA_COLLATERAL_RECEIVED = '7.2.1'
ACCOUNT_COLLATERAL_HELD_BY_OTHERS = '7.1.2'
ACCOUNT_CONTRA_COLLATERAL_HELD = '7.2.2'

#: Bounce-destination values (HesabYar ``BounceDestination``) and
#: the chart account each maps to (§9.1 #2).
BOUNCE_DESTINATION_ACCOUNTS = {
    'receivables': ACCOUNT_RECEIVABLES,
    'bounced_receivables': ACCOUNT_BOUNCED,
    }

#: Journal code of the general journal cheque moves post to. Journals
#: are global in Tryton (``account.journal`` has no company field);
#: the chart install creates one MISC journal per database.
CHEQUE_JOURNAL_CODE = 'MISC'


class Checkbook(ModelSQL, ModelView):
    """A chequebook (دسته‌چک): bank, bank account, leaf range.

    Used / voided / remaining leaves are computed from the issued
    cheques linked to this checkbook; a leaf is used once an issued
    cheque exists for it in a non-voided state.
    """
    __name__ = 'account.checkbook'

    company = fields.Many2One('company.company', 'Company',
        required=True, ondelete='CASCADE')
    name = fields.Char('Name', required=True)
    bank = fields.Many2One('bank', 'Bank', required=True,
        ondelete='RESTRICT')
    bank_account = fields.Many2One('bank.account', 'Bank Account',
        domain=[('bank', '=', Eval('bank'))],
        depends=['bank'], ondelete='RESTRICT')
    first_number = fields.Integer('First Leaf Number', required=True)
    last_number = fields.Integer('Last Leaf Number', required=True)
    checks = fields.One2Many('account.check', 'checkbook', 'Cheques',
        readonly=True)
    total_leaves = fields.Function(fields.Integer('Total Leaves'),
        'get_leaf_counts')
    used_leaves = fields.Function(fields.Integer('Used Leaves'),
        'get_leaf_counts')
    voided_leaves = fields.Function(fields.Integer('Voided Leaves'),
        'get_leaf_counts')
    remaining_leaves = fields.Function(fields.Integer('Remaining Leaves'),
        'get_leaf_counts')

    def get_rec_name(self, name):
        return self.name

    @classmethod
    def validate(cls, checkbooks):
        super().validate(checkbooks)
        for checkbook in checkbooks:
            checkbook.check_leaf_range()

    def check_leaf_range(self):
        if self.first_number is None or self.last_number is None:
            return
        if self.first_number <= 0 or self.last_number <= 0:
            raise UserError(gettext(
                'Checkbook leaf numbers must be positive.'))
        if self.first_number > self.last_number:
            raise UserError(gettext(
                'The first leaf number must not be greater than the '
                'last leaf number.'))

    @classmethod
    def get_leaf_counts(cls, checkbooks, names):
        """Multi-name Function getter: {field name: {record id: value}}."""
        pool = Pool()
        Check = pool.get('account.check')
        result = {name: {} for name in names}
        for checkbook in checkbooks:
            total = (checkbook.last_number - checkbook.first_number + 1
                if checkbook.first_number is not None
                and checkbook.last_number is not None else 0)
            used = Check.search_count([
                ('checkbook', '=', checkbook.id),
                ('kind', '=', 'issued'),
                ('state', 'in', USED_LEAF_STATES),
                ])
            voided = Check.search_count([
                ('checkbook', '=', checkbook.id),
                ('kind', '=', 'issued'),
                ('state', '=', 'voided'),
                ])
            values = {
                'total_leaves': total,
                'used_leaves': used,
                'voided_leaves': voided,
                'remaining_leaves': total - used - voided,
                }
            for name in names:
                result[name][checkbook.id] = values[name]
        return result


class Check(ModelSQL, ModelView):
    """One cheque (received or issued, commercial or collateral).

    US-531 adds the received-commercial lifecycle:
    :meth:`transition_rest` runs one transition — state change +
    its §2.4 journal (سند ۱–۶) — in a single Tryton transaction.
    US-532 adds the issued / collateral transitions the same way
    (سند ۷–۱۳ + the two-move call-in ۹الف/۹ب), the
    accountant-gated paths (call-in approval, unexpected call-in,
    bounced endorsed cheque) and the checkbook-leaf accounting.
    """
    __name__ = 'account.check'

    company = fields.Many2One('company.company', 'Company',
        required=True, ondelete='CASCADE')
    kind = fields.Selection(CHECK_KINDS, 'Kind', required=True)
    purpose = fields.Selection(CHECK_PURPOSES, 'Purpose', required=True,
        help='Collateral cheques are off-balance-sheet (memorandum '
        'accounts only) and never enter receivables, payables or the '
        'cash forecast.')
    number = fields.Char('Leaf Number',
        help='The serial number printed on the leaf — different from '
        'the 16-digit Sayyad id.')
    sayad_id = fields.Char('Sayyad ID', size=16, required=True,
        help='The 16-digit Sayyad identifier of the leaf.')
    sayad_status = fields.Selection(SAYAD_STATUSES, 'Sayyad Status',
        required=True,
        help='Sayyad\u2019s view of the leaf — separate from the '
        'accounting state.')
    amount = fields.Numeric('Amount', digits=(16, 0), required=True,
        help='Whole Rial (HesabYar MoneyIRR, digits=0).')
    issue_date = fields.Date('Issue Date')
    due_date = fields.Date('Due Date',
        help='Stored as Gregorian in Tryton; shown as Jalali in the '
        'HesabYar UI.')
    party = fields.Many2One('party.party', 'Party',
        help='Issuer for a received cheque, beneficiary for an issued '
        'cheque.',
        ondelete='RESTRICT')
    bank = fields.Many2One('bank', 'Bank',
        help='Issuing bank of a received cheque.',
        ondelete='RESTRICT')
    checkbook = fields.Many2One('account.checkbook', 'Checkbook',
        help='The chequebook an issued cheque was drawn from.',
        ondelete='RESTRICT')
    state = fields.Selection(CHECK_STATES, 'State', required=True)
    source = fields.Selection(SOURCES, 'Source', required=True,
        help='Whether the cheque was registered on the store site or '
        'entered manually. Both paths write to this one registry.')
    storage_location = fields.Selection(STORAGE_LOCATIONS,
        'Storage Location',
        help='Physical location of the leaf. A move is a custody '
        'event, never a journal.')
    branch = fields.Char('Branch',
        help='Branch of the issuing bank.')
    issuer_account_number = fields.Char('Issuer Account Number',
        help='Current-account number of the issuer, when known.')
    invoice = fields.Many2One('account.invoice', 'Invoice',
        ondelete='SET NULL')
    sale = fields.Many2One('sale.sale', 'Sale / Order',
        ondelete='SET NULL')
    installment_leg = fields.Char('Installment Leg',
        help='HesabYar installment-leg reference for a cheque paying '
        'a specific installment. A real model link lands in Phase 5ج.')
    applied_amount_irr = fields.Numeric('Applied Amount (IRR)',
        digits=(16, 0),
        help='Amount allocated to the linked installment leg, when it '
        'differs from the cheque amount.')
    endorsee = fields.Many2One('party.party', 'Endorsee',
        ondelete='SET NULL')
    endorsee_type = fields.Selection(ENDORSEE_TYPES, 'Endorsee Type')
    destination_account = fields.Many2One('account.account',
        'Destination Account',
        help='Bank/cash account the leaf is deposited to — chosen at '
        'deposit, used by clear for سند ۳.',
        ondelete='SET NULL')
    secured_obligation = fields.Char('Secured Obligation',
        help='For a collateral cheque: the obligation it secures '
        '(installment plan / contract / invoice reference).')
    attachment = fields.Many2One('ir.attachment', 'Attachment',
        help='Primary scan/photo of the leaf (optional); further '
        'files use the standard attachment list.',
        ondelete='SET NULL')
    endorsement_history = fields.One2Many(
        'account.check.endorsement', 'check', 'Endorsement History')
    moves = fields.One2Many('account.move', 'origin', 'Moves',
        readonly=True,
        help='The journal move of every transition (full audit trail).')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('sayad_id_unique_active',
                Exclude(table, (table.sayad_id, Equal),
                    where=~table.state.in_(SAYAD_ID_EXEMPT_STATES)),
                'account_check_ir.msg_sayad_id_unique'),
            ]
        cls.__rpc__.update({
            'transition_rest': RPC(readonly=False, instantiate=0),
            })

    @staticmethod
    def default_sayad_status():
        return 'unregistered'

    @classmethod
    def transition_rest(cls, checks, transition=None, auto_post=False,
            defer_journal=False, destination_account=None,
            bounce_destination=None, bounce_reason=None, bounce_date=None,
            certificate_reference=None, endorsee_name=None,
            endorsee_type=None, sayad_transfer_reference=None,
            storage_location=None, statement_reference=None,
            mismatch_detail=None, accountant_approval=None,
            void_reason=None):
        """Run one lifecycle transition over REST (US-531 + US-532).

        One call, one Tryton transaction: the state change AND its
        §2.4 journal (سند ۱–۱۳) commit together, or neither does —
        the REST dispatcher rolls the transaction back on any error.
        The call shape is the contract in
        ``docs/cheque-engine-contract.md``: the JSON body keys arrive
        as kwargs (``transition`` + the parameters that transition
        needs; the rest are omitted). Returns ``{"id", "state",
        "move_id", "move_ids"}`` — ``move_id`` is the journal this
        transition created (the commercial recognition move for
        ``call_in``), or null when it has no journal (expiry,
        reject-mismatch, bounced→returned, endorsed bounce) or when
        it is deferred; ``move_ids`` maps every move this
        transition created, keyed by its role (``call_in`` posts two
        moves — ``call_in_reversal`` + ``call_in`` — in one
        transaction).

        Access checks are disabled for the duration of the call —
        the same scope as ``process_rest`` / ``reconcile_rest`` /
        ``assign_and_do_rest`` in ``hesabyar_setup``, and for the
        same reason: the stock engine calls below read records the
        REST service account (admin with no groups) cannot read with
        checks on. The REST user is a service account, not an end
        user: HesabYar enforces its own authorisation (API key +
        scopes + tenant/company checks) before it ever calls this
        wrapper.

        Rules enforced here (mirroring the HesabYar state machine):

        * unknown transition name, or a transition from the wrong
          state, is refused — including re-presentment
          (``bounced → in_clearing``, §9.1 #5), which has its own
          error naming the rule;
        * every transition is scoped by (kind, purpose, from-state)
          — a commercial transition on a collateral cheque, or an
          issued transition on a received cheque, is refused
          (``_check_scope``); a collateral cheque that was called
          in has purpose ``payment`` from then on, so the §2.1
          transitions apply to it. The no-journal terminal
          transitions (``expire``, ``reject_mismatch``,
          ``void_intake`` — US-533) are purpose-agnostic: voiding
          or expiring an intake posts nothing, so refusing them
          for a collateral intake would strand the record
          forever;
        * every journal line on a ``party_required`` account
          carries the cheque's party (issuer for a received
          cheque, beneficiary for an issued one); a cheque without
          one fails closed (party resolution happens at intake, in
          the adapter — see ``docs/cheque-engine-contract.md``);
        * the bounce journal is dated today, so it lands in the
          CURRENT period, never by reopening a closed one (§9.1 #2);
        * endorsement to a non-supplier never guesses a journal
          (§9.1 #12): ``endorsee_type='other'`` defers even when
          ``defer_journal`` was not sent;
        * ``call_in`` needs the accountant's per-case approval
          (§9.1 #4, §5 §4) and posts TWO moves in one transaction
          (۹الف memorandum reversal + ۹ب commercial recognition);
        * ``unexpected_call_in`` posts ONLY the memorandum
          reversal — the commercial journal for a called-in issued
          collateral cheque is accountant-gated (§9.1 #4) and is
          never guessed here;
        * ``endorsed_bounce`` posts NO journal at all (§9.1 #3):
          the state moves to ``bounced`` and the journal is
          deferred to the accountant;
        * a collateral journal NEVER touches a commercial account
          (memorandum 7.x accounts only — segregation, §2.2).
        """
        check, = checks
        if transition not in TRANSITION_STATES:
            raise UserError(gettext(
                'Unknown cheque transition "%s".', transition))
        sources, target = TRANSITION_STATES[transition]
        if check.state not in sources:
            if check.state == 'bounced' and transition == 'deposit':
                raise UserError(gettext(
                    'A bounced cheque cannot be re-presented '
                    '(§9.1 #5): return it and take a replacement '
                    'payment instead.'))
            raise UserError(gettext(
                'Cheque in state "%s" cannot run "%s".',
                check.state, transition))
        cls._check_scope(check, transition)
        context_company = Transaction().context.get('company')
        if (context_company is not None
                and int(context_company) != check.company.id):
            raise UserError(gettext(
                'Cheque %s belongs to another company.',
                check.sayad_id))
        params = {
            'auto_post': bool(auto_post),
            'defer_journal': bool(defer_journal),
            'destination_account': destination_account,
            'bounce_destination': bounce_destination,
            'bounce_reason': bounce_reason,
            'bounce_date': bounce_date,
            'certificate_reference': certificate_reference,
            'endorsee_name': endorsee_name,
            'endorsee_type': endorsee_type,
            'sayad_transfer_reference': sayad_transfer_reference,
            'storage_location': storage_location,
            'statement_reference': statement_reference,
            'mismatch_detail': mismatch_detail,
            'accountant_approval': accountant_approval,
            'void_reason': void_reason,
            }
        with without_check_access():
            return cls._transition_rest(check, transition, target, params)

    @classmethod
    def _check_scope(cls, check, transition):
        """Refuse a transition outside its (kind, purpose) scope.

        The state table alone cannot express scope: ``clear`` runs
        from ``issued`` for issued cheques and from ``in_clearing``
        for received ones, ``return`` has a memorandum form for
        collateral, and the accountant-gated transitions belong to
        exactly one direction. A called-in received collateral
        cheque has purpose ``payment`` by then, so it passes the
        commercial scope — that is the point of the flip.
        """
        kind = check.kind
        purpose = check.purpose
        state = check.state
        allowed = True
        if transition == 'confirm_receipt':
            allowed = kind == 'received'
        elif transition in ('deposit', 'endorse'):
            allowed = kind == 'received' and purpose == 'payment'
        elif transition == 'clear':
            allowed = (kind == 'received' and purpose == 'payment'
                and state == 'in_clearing') or (
                kind == 'issued' and purpose == 'payment'
                and state in ('issued', 'called_in'))
        elif transition == 'bounce':
            allowed = (kind == 'received' and purpose == 'payment'
                and state == 'in_clearing') or (
                kind == 'issued' and purpose == 'payment'
                and state == 'issued')
        elif transition == 'return':
            allowed = (kind == 'received' and state in ('held', 'bounced')) or (
                kind == 'issued' and purpose == 'collateral'
                and state == 'issued')
        elif transition == 'issue':
            allowed = kind == 'issued'
        elif transition == 'void':
            allowed = kind == 'issued' and purpose == 'payment'
        elif transition == 'call_in':
            allowed = (kind == 'received' and purpose == 'collateral'
                and state == 'held')
        elif transition == 'unexpected_call_in':
            allowed = (kind == 'issued' and purpose == 'collateral'
                and state == 'issued')
        elif transition == 'endorsed_bounce':
            allowed = (kind == 'received' and purpose == 'payment'
                and state == 'endorsed')
        elif transition in ('expire', 'reject_mismatch', 'void_intake'):
            allowed = kind == 'received'
        if not allowed:
            raise UserError(gettext(
                'transition_rest: "%s" does not apply to a %s / %s '
                'cheque in state "%s".',
                transition, kind, purpose, state))

    @classmethod
    def _journal_number(cls, check, transition):
        """The §2.4 سند number of one transition for this cheque.

        ``clear`` / ``bounce`` / ``return`` / ``confirm_receipt``
        vary by kind/purpose; the fixed numbers live in
        ``TRANSITION_JOURNAL``. ``call_in`` has two numbers (۹الف /
        ۹ب) and is handled by the caller; ``unexpected_call_in``
        posts the mirror reversal of the issued memorandum (سند ۷
        in the issued direction), labelled as such — it is not a
        §2.4 row of its own.
        """
        if transition in TRANSITION_JOURNAL:
            return TRANSITION_JOURNAL[transition]
        if transition == 'confirm_receipt':
            return '۷' if check.purpose == 'collateral' else '۱'
        if transition == 'clear':
            return '۱۱' if check.kind == 'issued' else '۳'
        if transition == 'bounce':
            return '۱۲' if check.kind == 'issued' else '۴'
        if transition == 'return':
            if check.kind == 'issued':
                return '۸'
            if check.purpose == 'collateral':
                return '۸'
            return '۶'
        if transition == 'unexpected_call_in':
            return '۷-معکوس'
        return None

    @classmethod
    def _transition_rest(cls, check, transition, target, params):
        """The transition core (access checks off, one transaction).

        ``moves`` accumulates every move this transition creates,
        keyed by role (``call_in`` creates two: the memorandum
        reversal + the commercial recognition). The caller returns
        the map; ``move_id`` is the single move for one-move
        transitions, the commercial recognition move for
        ``call_in``, and null when no journal was created.
        """
        pool = Pool()
        Move = pool.get('account.move')
        previous_state = check.state
        previous_storage = check.storage_location
        values = {'state': target}
        moves = {}

        if transition == 'confirm_receipt':
            values['storage_location'] = (
                params['storage_location'] or 'vault')
            if check.purpose == 'collateral':
                # سند انتظامی ۷ — memorandum ONLY (7.x accounts;
                # never a commercial account, §2.2 segregation).
                moves['confirm_receipt'] = cls._post_cheque_move(
                    check, transition, [
                        (ACCOUNT_COLLATERAL_RECEIVED, check.amount, 0,
                            check.party),
                        (ACCOUNT_CONTRA_COLLATERAL_RECEIVED, 0,
                            check.amount, check.party),
                        ])
            else:
                moves['confirm_receipt'] = cls._post_cheque_move(
                    check, transition, [
                        (ACCOUNT_HELD, check.amount, 0, check.party),
                        (ACCOUNT_RECEIVABLES, 0, check.amount, check.party),
                        ])
        elif transition == 'deposit':
            destination = cls._destination_account(check, params)
            values['destination_account'] = destination.id
            values['storage_location'] = 'bank'
            moves['deposit'] = cls._post_cheque_move(check, transition, [
                (ACCOUNT_IN_CLEARING, check.amount, 0, check.party),
                (ACCOUNT_HELD, 0, check.amount, check.party),
                ])
        elif transition == 'clear':
            if check.kind == 'issued':
                # سند ۱۱ — the bank account is the one recorded at
                # issue, or the checkbook's bank account when the
                # issue did not record one.
                bank_account = cls._issued_bank_account(check)
                moves['clear'] = cls._post_cheque_move(
                    check, transition, [
                        (ACCOUNT_NOTES_PAYABLE, check.amount, 0,
                            check.party),
                        (bank_account, 0, check.amount, check.party),
                        ])
            else:
                if not check.destination_account:
                    raise UserError(gettext(
                        'Cheque %s has no destination account — it was '
                        'never deposited with one.', check.sayad_id))
                destination = check.destination_account
                moves['clear'] = cls._post_cheque_move(check, transition, [
                    (destination, check.amount, 0, check.party),
                    (ACCOUNT_IN_CLEARING, 0, check.amount, check.party),
                    ])
        elif transition == 'bounce':
            if check.kind == 'issued':
                # سند ۱۲ — the obligation returns (signature
                # mismatch and the like). No bank certificate exists
                # for our own bounced cheque: reason + date only.
                cls._require_issued_bounce_params(params)
                moves['bounce'] = cls._post_cheque_move(
                    check, transition, [
                        (ACCOUNT_NOTES_PAYABLE, check.amount, 0,
                            check.party),
                        (ACCOUNT_PAYABLES, 0, check.amount, check.party),
                        ])
            else:
                cls._require_bounce_params(params)
                code = BOUNCE_DESTINATION_ACCOUNTS.get(
                    params['bounce_destination'] or 'receivables')
                if code is None:
                    raise UserError(gettext(
                        'Unknown bounce destination "%s".',
                        params['bounce_destination']))
                moves['bounce'] = cls._post_cheque_move(check, transition, [
                    (code, check.amount, 0, check.party),
                    (ACCOUNT_IN_CLEARING, 0, check.amount, check.party),
                    ])
        elif transition == 'endorse':
            endorsee, endorsee_type = cls._prepare_endorsement(params)
            values['endorsee'] = endorsee.id
            values['endorsee_type'] = endorsee_type
            values['sayad_status'] = 'transferred'
            values['storage_location'] = 'with_endorsee'
            if not (params['defer_journal'] or endorsee_type == 'other'):
                moves['endorse'] = cls._post_cheque_move(check, transition, [
                    (ACCOUNT_PAYABLES, check.amount, 0, endorsee),
                    (ACCOUNT_HELD, 0, check.amount, check.party),
                    ])
        elif transition == 'return':
            if check.kind == 'issued':
                # Issued collateral returned by the counterparty:
                # memorandum سند ۸ in the issued direction, and the
                # leaf comes back into our vault.
                values['storage_location'] = 'vault'
                moves['return'] = cls._post_cheque_move(check, transition, [
                    (ACCOUNT_CONTRA_COLLATERAL_HELD, check.amount, 0,
                        check.party),
                    (ACCOUNT_COLLATERAL_HELD_BY_OTHERS, 0, check.amount,
                        check.party),
                    ])
            else:
                values['storage_location'] = 'returned_to_issuer'
                if previous_state == 'held':
                    if check.purpose == 'collateral':
                        # سند انتظامی ۸ — memorandum reversal.
                        moves['return'] = cls._post_cheque_move(
                            check, transition, [
                                (ACCOUNT_CONTRA_COLLATERAL_RECEIVED,
                                    check.amount, 0, check.party),
                                (ACCOUNT_COLLATERAL_RECEIVED, 0,
                                    check.amount, check.party),
                                ])
                    else:
                        moves['return'] = cls._post_cheque_move(
                            check, transition, [
                                (ACCOUNT_RECEIVABLES, check.amount, 0,
                                    check.party),
                                (ACCOUNT_HELD, 0, check.amount, check.party),
                                ])
                # From bounced: no journal — the claim was
                # re-established by سند ۴ (§9.1 #2).
        elif transition == 'issue':
            # — → issued: the leaf leaves our checkbook for the
            # beneficiary. The bank account the cheque will clear
            # against is recorded now (used by سند ۱۱).
            if not check.checkbook:
                raise UserError(gettext(
                    'Cheque %s has no checkbook — an issued cheque '
                    'must come from one of the company\u2019s '
                    'checkbooks.', check.sayad_id))
            destination = cls._destination_account(check, params)
            values['destination_account'] = destination.id
            values['sayad_status'] = 'registered'
            if check.purpose == 'collateral':
                # Memorandum in the issued direction (mirror of
                # سند ۷): اسناد تضمینی نزد دیگران / طرف حساب‌های
                # انتظامی — never a commercial account.
                moves['issue'] = cls._post_cheque_move(check, transition, [
                    (ACCOUNT_COLLATERAL_HELD_BY_OTHERS, check.amount, 0,
                        check.party),
                    (ACCOUNT_CONTRA_COLLATERAL_HELD, 0, check.amount,
                        check.party),
                    ])
            else:
                # سند ۱۰: Dr Trade Payables / Cr Notes Payable.
                moves['issue'] = cls._post_cheque_move(check, transition, [
                    (ACCOUNT_PAYABLES, check.amount, 0, check.party),
                    (ACCOUNT_NOTES_PAYABLE, 0, check.amount, check.party),
                    ])
        elif transition == 'void':
            # سند ۱۳: the obligation returns; the leaf is marked
            # voided in its checkbook (state ``voided`` is what the
            # checkbook counts) and appears in report 8.
            cls._require_void_params(params)
            moves['void'] = cls._post_cheque_move(check, transition, [
                (ACCOUNT_NOTES_PAYABLE, check.amount, 0, check.party),
                (ACCOUNT_PAYABLES, 0, check.amount, check.party),
                ])
        elif transition == 'call_in':
            # §2.2 held → called_in: TWO simultaneous journals in
            # ONE transaction — سند ۹الف (memorandum reversal) +
            # سند ۹ب (commercial recognition, identical to سند ۱) —
            # then the cheque follows §2.1. The design is the §2.4
            # proposal and the transition is accountant-gated (§9.1
            # #4): the per-case approval travels in the params and
            # is required here. The cheque becomes commercial:
            # its purpose flips to ``payment``.
            cls._require_call_in_params(params)
            moves['call_in_reversal'] = cls._post_cheque_move(
                check, transition, [
                    (ACCOUNT_CONTRA_COLLATERAL_RECEIVED, check.amount, 0,
                        check.party),
                    (ACCOUNT_COLLATERAL_RECEIVED, 0, check.amount,
                        check.party),
                    ], number='۹الف')
            moves['call_in'] = cls._post_cheque_move(
                check, transition, [
                    (ACCOUNT_HELD, check.amount, 0, check.party),
                    (ACCOUNT_RECEIVABLES, 0, check.amount, check.party),
                    ], number='۹ب')
            values['purpose'] = 'payment'
        elif transition == 'unexpected_call_in':
            # The counterparty took our issued collateral cheque to
            # the bank without notice and the money left our
            # account (withdrawal matched by leaf number — the
            # statement reference travels in the params). The
            # memorandum is reversed (mirror of سند ۷ in the issued
            # direction); the COMMERCIAL journal is accountant-gated
            # (§9.1 #4) and is NEVER guessed here — the cheque
            # becomes commercial (purpose flips) with its journal
            # pending the accountant's review. The leaf is at the
            # bank now.
            cls._require_unexpected_call_in_params(params)
            values['storage_location'] = 'bank'
            moves['unexpected_call_in'] = cls._post_cheque_move(
                check, transition, [
                    (ACCOUNT_CONTRA_COLLATERAL_HELD, check.amount, 0,
                        check.party),
                    (ACCOUNT_COLLATERAL_HELD_BY_OTHERS, 0, check.amount,
                        check.party),
                    ])
            values['purpose'] = 'payment'
        elif transition == 'endorsed_bounce':
            # A cheque we endorsed away bounced: it is no longer
            # our asset, and its bounce means we owe the next
            # beneficiary — a separate flow with a reverse-of-
            # endorsement journal. The journal design is
            # accountant-gated (§9.1 #3): NO journal is guessed
            # here; the state moves to ``bounced`` and the journal
            # is deferred to the accountant.
            cls._require_endorsed_bounce_params(params)
            # No journal, no storage change (the leaf is with the
            # next beneficiary, not with us).
        # expire / reject_mismatch / void_intake: terminal, no journal.

        cls.write([check], values)
        if transition == 'deposit':
            cls._custody_entry(check, previous_storage, 'bank',
                'deposit — leaf delivered to the bank for clearing')
        elif transition == 'endorse':
            cls._custody_entry(check, previous_storage, 'with_endorsee',
                'endorse — leaf handed to the endorsee')
            cls._endorsement_entry(check, params)
        elif transition == 'return':
            destination = values.get('storage_location')
            cls._custody_entry(check, previous_storage, destination,
                'return — leaf returned to the issuer'
                if check.kind == 'received' else
                'return — issued collateral leaf returned by the '
                'counterparty')
        elif transition == 'unexpected_call_in':
            cls._custody_entry(check, previous_storage, 'bank',
                'unexpected call-in — leaf presented at the bank '
                'without notice')
        move_ids = {key: move.id for key, move in moves.items()}
        if params['auto_post']:
            for move in moves.values():
                Move.post([move])
        return {
            'id': check.id,
            'state': target,
            'move_id': (
                move_ids.get('call_in')
                or move_ids.get(transition)
                or None),
            'move_ids': move_ids,
            }

    @classmethod
    def _require_issued_bounce_params(cls, params):
        """An issued bounce needs its reason and date (§5 §2.3).

        No bank non-payment certificate exists for our own bounced
        cheque — the bank notifies us of the signature mismatch or
        other cause, and that notification is the evidence.
        """
        for name in ('bounce_reason', 'bounce_date'):
            value = params.get(name)
            if value is None or (isinstance(value, str)
                    and not value.strip()):
                raise UserError(gettext(
                    'an issued bounce needs %s (reason + date).', name))

    @classmethod
    def _require_void_params(cls, params):
        """Voiding an issued cheque needs its reason (audit trail)."""
        value = params.get('void_reason')
        if value is None or (isinstance(value, str)
                and not value.strip()):
            raise UserError(gettext(
                'void needs void_reason (why the issued leaf was '
                'voided).'))

    @classmethod
    def _require_call_in_params(cls, params):
        """A call-in needs the accountant's per-case approval (§5 §4)."""
        value = params.get('accountant_approval')
        if value is None or (isinstance(value, str)
                and not value.strip()):
            raise UserError(gettext(
                'call_in needs accountant_approval: a collateral '
                'cheque is called in only with the accountant\u2019s '
                'approval for this case (§9.1 #4).'))

    @classmethod
    def _require_unexpected_call_in_params(cls, params):
        """An unexpected call-in needs the statement reference.

        The withdrawal matched from the bank statement (exact
        amount + leaf number) is the evidence that the counterparty
        presented the cheque — without it there is no call-in.
        """
        value = params.get('statement_reference')
        if value is None or (isinstance(value, str)
                and not value.strip()):
            raise UserError(gettext(
                'unexpected_call_in needs statement_reference (the '
                'bank-statement withdrawal matched by leaf number).'))

    @classmethod
    def _require_endorsed_bounce_params(cls, params):
        """A bounced endorsed cheque needs its evidence (§9.1 #3).

        Reason + date + certificate: the accountant needs all three
        to design the reverse-of-endorsement journal.
        """
        for name in ('bounce_reason', 'bounce_date',
                'certificate_reference'):
            value = params.get(name)
            if value is None or (isinstance(value, str)
                    and not value.strip()):
                raise UserError(gettext(
                    'endorsed_bounce needs %s (reason + date + bank '
                    'certificate).', name))

    @classmethod
    def _issued_bank_account(cls, check):
        """The bank account an issued cheque clears against (سند ۱۱).

        The account recorded at issue wins; otherwise the
        checkbook's bank account. A cheque with neither fails
        closed — سند ۱۱ cannot credit an unknown account.
        """
        if check.destination_account:
            return check.destination_account
        checkbook = check.checkbook
        if checkbook is not None and checkbook.bank_account is not None:
            account = checkbook.bank_account.account
            if account is not None:
                return account
        raise UserError(gettext(
            'Cheque %s has no bank account — record one at issue, '
            'or set one on its checkbook.', check.sayad_id))

    @classmethod
    def _require_bounce_params(cls, params):
        """A bounce needs its reason, date and certificate (§5 §2.1)."""
        for name in ('bounce_reason', 'bounce_date',
                'certificate_reference'):
            value = params.get(name)
            if value is None or (isinstance(value, str)
                    and not value.strip()):
                raise UserError(gettext(
                    'bounce needs %s (reason + date + bank '
                    'non-payment certificate).', name))

    @classmethod
    def _destination_account(cls, check, params):
        """Resolve the bank/cash account (must be ours, by id).

        Used by ``deposit`` (where the leaf is deposited) and by
        ``issue`` (the account the issued cheque will clear
        against, used by سند ۱۱).
        """
        pool = Pool()
        Account = pool.get('account.account')
        destination_id = params.get('destination_account')
        if not destination_id:
            raise UserError(gettext(
                'This transition needs a destination_account (the '
                'bank/cash account).'))
        accounts = Account.search([
            ('id', '=', int(destination_id)),
            ('company', '=', check.company.id),
            ], limit=1)
        if not accounts:
            raise UserError(gettext(
                'Destination account %s does not exist in this '
                'company.', destination_id))
        return accounts[0]

    @classmethod
    def _prepare_endorsement(cls, params):
        """Validate the endorsement params + resolve the endorsee.

        Returns ``(endorsee party, endorsee_type)``. The endorsee is
        found by exact name, or created when unknown — the HesabYar
        side passes names only, so a same-name party is taken as the
        same endorsee; resolving by party id is a follow-up.
        """
        pool = Pool()
        Party = pool.get('party.party')
        name = (params.get('endorsee_name') or '').strip()
        endorsee_type = params.get('endorsee_type')
        transfer_ref = (params.get('sayad_transfer_reference') or ''
            ).strip()
        if not name or endorsee_type not in ('supplier', 'other') \
                or not transfer_ref:
            raise UserError(gettext(
                'endorse needs endorsee_name, endorsee_type '
                '(supplier | other) and sayad_transfer_reference.'))
        parties = Party.search([('name', '=', name)], limit=1)
        if parties:
            return parties[0], endorsee_type
        party, = Party.create([{'name': name}])
        return party, endorsee_type

    @classmethod
    def _endorsement_entry(cls, check, params):
        """One row of the endorsement chain (§5 §1.1).

        The endorser is the company itself (``company.party`` when
        the company has one); the Sayyad transfer receipt + the
        endorsee are the audit trail.
        """
        pool = Pool()
        Endorsement = pool.get('account.check.endorsement')
        Date = pool.get('ir.date')
        company = check.company
        endorser = company.party.id if company.party else None
        Endorsement.create([{
            'check': check.id,
            'endorser': endorser,
            'date': Date.today(),
            'sayad_transfer_receipt':
                (params.get('sayad_transfer_reference') or '').strip(),
            'reason': (
                f"Endorsed to {(params.get('endorsee_name') or '').strip()} "
                f"({params.get('endorsee_type')})"),
            }])

    @classmethod
    def _custody_entry(cls, check, origin, destination, reason):
        """One append-only custody row (a move, never a journal)."""
        if not origin:
            return
        pool = Pool()
        CustodyLog = pool.get('account.check.custody_log')
        CustodyLog.create([{
            'check': check.id,
            'origin': origin,
            'destination': destination,
            'moved_at': datetime.datetime.now(),
            'actor': Transaction().user,
            'reason': reason,
            }])

    @classmethod
    def _account_by_code(cls, company, code):
        """One chart account by code (never a hard-coded id)."""
        pool = Pool()
        Account = pool.get('account.account')
        accounts = Account.search([
            ('code', '=', code),
            ('company', '=', company.id),
            ], limit=1)
        if not accounts:
            raise UserError(gettext(
                'Cheque journal needs account %s in company %s, but '
                'it is not in the chart.', code, company.rec_name))
        return accounts[0]

    @classmethod
    def _misc_journal(cls):
        """The general journal cheque moves post to.

        Journals are global in Tryton (``account.journal`` has no
        company field) — the chart install creates one MISC journal
        per database, so the lookup is by code alone.
        """
        pool = Pool()
        Journal = pool.get('account.journal')
        journals = Journal.search([
            ('code', '=', CHEQUE_JOURNAL_CODE),
            ], limit=1)
        if not journals:
            raise UserError(gettext(
                'Cheque journal needs the %s journal, but it is not '
                'in the chart.', CHEQUE_JOURNAL_CODE))
        return journals[0]

    @classmethod
    def _journal_line(cls, account, debit, credit, party, date):
        """One move line: a party only when the account requires one.

        The cheque accounts are ``party_required``; a missing issuer
        party fails closed here instead of posting a party-less line.
        A non-party account never carries one (the engine rejects a
        party on such lines, and the adapter pre-validates the same
        rule).
        """
        if account.party_required and party is None:
            raise UserError(gettext(
                'Account %s requires a party, but the cheque has no '
                'issuer party.', account.rec_name))
        return {
            'account': account.id,
            'debit': debit,
            'credit': credit,
            'party': party.id if account.party_required else None,
            'date': date,
            }

    @classmethod
    def _post_cheque_move(cls, check, transition, specs, number=None):
        """Create the §2.4 journal for one transition (draft).

        ``specs`` is a list of ``(account, debit, credit, party)``
        where ``account`` is a code or an account record. The move is
        dated today — so a bounce lands in the CURRENT period — in
        the open period covering today, on the MISC journal, with
        ``origin`` the cheque (see ``move.py``) and the سند number in
        the description. Posting is the caller's choice (``auto_post``).
        ``number`` overrides the سند number (``call_in`` posts two
        moves, ۹الف + ۹ب, under one transition name).
        """
        pool = Pool()
        Move = pool.get('account.move')
        Period = pool.get('account.period')
        Date = pool.get('ir.date')
        today = Date.today()
        period = Period.find(check.company, date=today)
        journal = cls._misc_journal()
        lines = []
        for account, debit, credit, party in specs:
            if isinstance(account, str):
                account = cls._account_by_code(check.company, account)
            lines.append(cls._journal_line(
                account, debit, credit, party, today))
        number = number or cls._journal_number(check, transition)
        move, = Move.create([{
            'company': check.company.id,
            'journal': journal.id,
            'date': today,
            'period': period.id,
            'description': (
                f'Cheque {check.sayad_id} — سند {number} ({transition})'),
            'origin': check,
            'lines': [('create', lines)],
            }])
        return move

    @staticmethod
    def default_state():
        return 'registered'

    @staticmethod
    def default_source():
        return 'manual'

    def get_rec_name(self, name):
        if self.number:
            return f'{self.sayad_id} ({self.number})'
        return self.sayad_id

    @classmethod
    def validate(cls, checks):
        super().validate(checks)
        for check in checks:
            check.check_sayad_id()
            check.check_amount()
            check.check_dates()
            check.check_checkbook_range()

    def check_sayad_id(self):
        if not self.sayad_id or not _SAYAD_ID_RE.fullmatch(self.sayad_id):
            raise UserError(gettext(
                'The Sayyad ID must be exactly 16 digits.'))

    def check_amount(self):
        if self.amount is not None and self.amount <= 0:
            raise UserError(gettext(
                'The cheque amount must be greater than zero.'))

    def check_dates(self):
        if (self.issue_date and self.due_date
                and self.due_date < self.issue_date):
            raise UserError(gettext(
                'The due date must not be before the issue date.'))

    def check_checkbook_range(self):
        if (not self.checkbook or not self.number
                or not self.number.isdigit()):
            return
        leaf = int(self.number)
        if not (self.checkbook.first_number <= leaf
                <= self.checkbook.last_number):
            raise UserError(gettext(
                'The leaf number is outside the checkbook range.'))


class CheckEndorsement(ModelSQL, ModelView):
    """One entry of a cheque's endorsement chain (§5 §1.1)."""
    __name__ = 'account.check.endorsement'

    check = fields.Many2One('account.check', 'Cheque', required=True,
        ondelete='CASCADE')
    endorser = fields.Many2One('party.party', 'Endorser',
        ondelete='RESTRICT')
    date = fields.Date('Date')
    sayad_transfer_receipt = fields.Char('Sayyad Transfer Receipt')
    reason = fields.Char('Reason')
