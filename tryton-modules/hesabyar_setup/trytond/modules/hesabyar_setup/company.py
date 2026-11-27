# This file is part of HesabYar.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
# This module is licensed under the GNU General Public License v3 or later,
# like all Tryton modules.  See LICENSE in this package.
"""Chart installation / update for HesabYar, exposed over the REST API.

Tryton's Create Chart / Update Chart wizards have no REST surface, so the
HesabYar setup wizard cannot install a chart through the REST API alone
(see docs/setup-wizard.md, spike US-203). This module adds two ``__rpc__``
methods to ``company.company`` that run the wizards' transitions directly
-- calling Tryton's own template-expansion code, not copying it -- so the
HesabYar adapter can drive them with ordinary REST actions:

    POST /rest/model/company.company/<id>/install_chart
        {"template_id": 1, "account_receivable_code": "1.2.1",
         "account_payable_code": "2.1.1"}
    POST /rest/model/company.company/<id>/update_chart
        {}

Both methods run with access checks on (the ``__rpc__`` default), unlike
wizard execution, and each call is one engine transaction, so a failure
leaves no partial chart behind.
"""
from trytond.exceptions import UserError
from trytond.pool import Pool, PoolMeta
from trytond.rpc import RPC
from trytond.transaction import Transaction, inactive_records


class Company(metaclass=PoolMeta):
    __name__ = 'company.company'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__rpc__.update({
                'install_chart': RPC(readonly=False, instantiate=0),
                'update_chart': RPC(readonly=False, instantiate=0),
                })

    def install_chart(
            self, template_id, account_receivable_code='1.2.1',
            account_payable_code='2.1.1'):
        """Install a chart of accounts from a root template.

        Mirrors ``account.create_chart`` (``transition_create_account`` +
        ``transition_create_properties``), with two deliberate differences:

        * a company that already has accounts is refused with a plain
          ``UserError`` instead of the wizard's dismissable
          ``ChartWarning`` -- there is no warning-dismissal dance over
          REST;
        * the receivable / payable defaults are given as account CODES
          (``'1.2.1'`` / ``'2.1.1'`` by default), resolved against the
          freshly installed chart. The spike sketch (docs/setup-wizard.md
          §2) passed account ids, but ids are unknowable before the
          install -- codes are stable, listable from the template
          beforehand, and what the dashboard wizard shows the user.

        Returns a summary dict for the dashboard wizard to display.
        """
        pool = Pool()
        Account = pool.get('account.account')
        AccountTemplate = pool.get('account.account.template')
        AccountType = pool.get('account.account.type')
        TaxCodeTemplate = pool.get('account.tax.code.template')
        TaxCodeLineTemplate = pool.get('account.tax.code.line.template')
        TaxTemplate = pool.get('account.tax.template')
        TaxRuleTemplate = pool.get('account.tax.rule.template')
        TaxRuleLineTemplate = \
            pool.get('account.tax.rule.line.template')
        Config = pool.get('ir.configuration')
        Configuration = pool.get('account.configuration')
        transaction = Transaction()

        existing = Account.search([('company', '=', self.id)], limit=1)
        if existing:
            raise UserError(
                f'Company "{self.rec_name}" already has a chart of accounts.')

        templates = AccountTemplate.search([('id', '=', template_id)])
        if not templates:
            raise UserError(
                f'Account template {template_id} does not exist.')
        account_template, = templates
        if account_template.parent is not None:
            raise UserError(
                f'Account template "{account_template.rec_name}" is not a root template.')

        with transaction.set_context(language=Config.get_language(),
                company=self.id):
            # Create account types
            template2type = {}
            account_template.type.create_type(
                self.id,
                template2type=template2type)

            # Create accounts
            template2account = {}
            account_template.create_account(
                self.id,
                template2account=template2account,
                template2type=template2type)

            # Create taxes
            template2tax = {}
            TaxTemplate.create_tax(
                account_template.id, self.id,
                template2account=template2account,
                template2tax=template2tax)

            # Create tax codes
            template2tax_code = {}
            TaxCodeTemplate.create_tax_code(
                account_template.id, self.id,
                template2tax_code=template2tax_code)

            # Create tax code lines
            template2tax_code_line = {}
            TaxCodeLineTemplate.create_tax_code_line(
                account_template.id,
                template2tax=template2tax,
                template2tax_code=template2tax_code,
                template2tax_code_line=template2tax_code_line)

            # Update taxes and replaced_by on accounts
            account_template.update_account2(template2account, template2tax)

            # Create tax rules
            template2rule = {}
            TaxRuleTemplate.create_rule(
                account_template.id, self.id,
                template2rule=template2rule)

            # Create tax rule lines
            template2rule_line = {}
            TaxRuleLineTemplate.create_rule_line(
                account_template.id, template2tax, template2rule,
                template2rule_line=template2rule_line)

            # Validate the receivable / payable defaults against the
            # wizard's properties domain, then write them to the
            # company configuration (transition_create_properties).
            # Codes are resolved against the freshly installed chart.
            receivable = self._validate_default_account(
                account_receivable_code, 'receivable')
            payable = self._validate_default_account(
                account_payable_code, 'payable')
            if receivable is not None or payable is not None:
                config = Configuration(1)
                if receivable is not None:
                    config.default_account_receivable = receivable
                if payable is not None:
                    config.default_account_payable = payable
                config.save()

            accounts = Account.search_count([('company', '=', self.id)])
            types = AccountType.search_count([('company', '=', self.id)])

        return {
            'company': self.id,
            'company_name': self.rec_name,
            'template': account_template.rec_name,
            'accounts': accounts,
            'account_types': types,
            'default_account_receivable': (
                receivable.id if receivable is not None else None),
            'default_account_payable': (
                payable.id if payable is not None else None),
            }

    def _validate_default_account(self, code, kind):
        """Validate one default account against the wizard's domain.

        The domain is the one on ``account.create_chart.properties``:
        not closed, the right type flag, party required, same company.
        The account is found by code within this company. Returns the
        account record, or None when no code was given (pass ``None``
        to leave that default unset).
        """
        if code is None:
            return None
        pool = Pool()
        Account = pool.get('account.account')
        accounts = Account.search([
                ('code', '=', code),
                ('closed', '!=', True),
                (f'type.{kind}', '=', True),
                ('party_required', '=', True),
                ('company', '=', self.id),
                ], limit=1)
        if not accounts:
            raise UserError(
                f'Account {code} is not a valid default {kind} account for '
                f'company "{self.rec_name}" (must exist, be open, {kind}, party '
                'required, same company).')
        return accounts[0]

    @inactive_records
    def update_chart(self):
        """Update the company's chart from its template.

        Mirrors ``account.update_chart.transition_update``: existing
        accounts / types / taxes are updated from their templates and
        missing ones (added to the template since the install) are
        created. This is how an existing company picks up the
        HesabYar template accounts (notes receivable/payable,
        memorandum accounts) after this module is activated.

        Returns a summary dict with the account counts before/after.
        """
        pool = Pool()
        Account = pool.get('account.account')
        TaxCode = pool.get('account.tax.code')
        TaxCodeTemplate = pool.get('account.tax.code.template')
        TaxCodeLine = pool.get('account.tax.code.line')
        TaxCodeLineTemplate = pool.get('account.tax.code.line.template')
        Tax = pool.get('account.tax')
        TaxTemplate = pool.get('account.tax.template')
        TaxRule = pool.get('account.tax.rule')
        TaxRuleTemplate = pool.get('account.tax.rule.template')
        TaxRuleLine = pool.get('account.tax.rule.line')
        TaxRuleLineTemplate = \
            pool.get('account.tax.rule.line.template')

        roots = Account.search([
                ('company', '=', self.id),
                ('parent', '=', None),
                ('template', '!=', None),
                ])
        if not roots:
            raise UserError(
                f'Company "{self.rec_name}" has no chart of accounts to update '
                '(install one first with install_chart).')
        if len(roots) > 1:
            raise UserError(
                f'Company "{self.rec_name}" has {len(roots)} chart roots; '
                'update_chart needs exactly one.')

        # Re-browse to have the inactive context (as the wizard does).
        account = Account(roots[0].id)
        company = account.company
        accounts_before = Account.search_count(
            [('company', '=', company.id)])

        template2type = {}
        # Update account types
        account.type.update_type(template2type=template2type)
        # Create missing account types
        if account.type.template:
            account.type.template.create_type(
                company.id,
                template2type=template2type)
            # Update again to set new parent
            account.type.update_type(template2type=template2type)

        # Update accounts
        template2account = {}
        account.update_account(template2account=template2account,
            template2type=template2type)
        # Create missing accounts
        if account.template:
            account.template.create_account(
                company.id,
                template2account=template2account,
                template2type=template2type)

        # Update taxes
        template2tax = {}
        Tax.update_tax(
            company.id,
            template2account=template2account,
            template2tax=template2tax)
        # Create missing taxes
        if account.template:
            TaxTemplate.create_tax(
                account.template.id, account.company.id,
                template2account=template2account,
                template2tax=template2tax)
            # Update again to set new parent
            Tax.update_tax(
                company.id,
                template2account=template2account,
                template2tax=template2tax)

        # Update tax codes
        template2tax_code = {}
        TaxCode.update_tax_code(
            company.id,
            template2tax_code=template2tax_code)
        # Create missing tax codes
        if account.template:
            TaxCodeTemplate.create_tax_code(
                account.template.id, company.id,
                template2tax_code=template2tax_code)
            # Update again to set new parent
            TaxCode.update_tax_code(
                company.id,
                template2tax_code=template2tax_code)

        # Update tax code lines
        template2tax_code_line = {}
        TaxCodeLine.update_tax_code_line(
            company.id,
            template2tax=template2tax,
            template2tax_code=template2tax_code,
            template2tax_code_line=template2tax_code_line)
        # Create missing tax code lines
        if account.template:
            TaxCodeLineTemplate.create_tax_code_line(
                account.template.id,
                template2tax=template2tax,
                template2tax_code=template2tax_code,
                template2tax_code_line=template2tax_code_line)

        # Update parent, taxes and replaced_by on accounts
        account.update_account2(template2account, template2tax)

        # Update tax rules
        template2rule = {}
        TaxRule.update_rule(company.id, template2rule=template2rule)
        # Create missing tax rules
        if account.template:
            TaxRuleTemplate.create_rule(
                account.template.id, account.company.id,
                template2rule=template2rule)

        # Update tax rule lines
        template2rule_line = {}
        TaxRuleLine.update_rule_line(
            company.id, template2tax, template2rule,
            template2rule_line=template2rule_line)
        # Create missing tax rules lines
        if account.template:
            TaxRuleLineTemplate.create_rule_line(
                account.template.id, template2tax, template2rule,
                template2rule_line=template2rule_line)

        accounts_after = Account.search_count(
            [('company', '=', company.id)])
        return {
            'company': company.id,
            'company_name': company.rec_name,
            'template': account.template.rec_name,
            'accounts_before': accounts_before,
            'accounts_after': accounts_after,
            'accounts_added': accounts_after - accounts_before,
            }
