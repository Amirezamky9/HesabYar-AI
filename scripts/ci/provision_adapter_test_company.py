# ruff: noqa: F821  # trytond-console provides pool/transaction as globals
# Provision the dedicated Tryton test company for the HesabYar US-003
# Tryton REST adapter contract tests.
#
# This is the in-repo copy of the original dev provisioning script
# (~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py);
# the logic is identical, only the paths changed so CI can run it from a
# bare checkout. Normally run via scripts/ci/setup-tryton.sh, which
# passes the right --lock-table flags. Run standalone with:
#   <tryton-env>/bin/trytond-console -c <trytond.conf> \
#       -d <database> \
#       --lock-table account_fiscalyear account_period account_journal account_configuration \
#       -f scripts/ci/provision_adapter_test_company.py
#
# The --lock-table flags are REQUIRED: FiscalYear.create calls lock(),
# which raises _TransactionLockError unless the table was pre-declared
# (see docs/adapter.md). Without them the script prints a traceback,
# trytond-console still exits 0, and the transaction rolls back silently.
#
# This script runs inside trytond-console, which provides `pool` and
# `transaction` in the script namespace and ROLLS BACK the transaction when
# it exits -- so the script commits explicitly at the end.
#
# What it creates (idempotent -- safe to re-run; existing records are reused):
#   * currency IRR (only if missing; ISO-4217 defaults, digits=2).
#     The IRR digits decision belongs to US-004 -- this script NEVER modifies
#     an existing IRR record.
#   * party + company "HesabYar Adapter Test" (currency IRR).
#   * the universal chart of accounts from trytond-account templates, via the
#     exact code path of the `account.create_chart` wizard
#     (transition_create_account + transition_create_properties).
#   * a company move sequence, fiscal year 2026 and 12 open monthly periods.
#   * the 4 invoice sequences + fiscalyear.invoice_sequence row (US-502):
#     posting an invoice needs them; Phase 2 never posted, so the gap
#     only surfaced with the US-502 contract tests.
#   * default receivable (1.2.1) / payable (2.1.1) accounts (US-502):
#     the sale flow resolves the invoice account through the party
#     fallback to these defaults.
#   * a general journal "HesabYar Adapter Test General Journal".
#
# Everything is scoped to the "HesabYar Adapter Test" company so it cannot
# interfere with the sibling US-004 story's own test company.

import calendar
from datetime import date
from decimal import Decimal

from trytond.transaction import Transaction

COMPANY_NAME = "HesabYar Adapter Test"
JOURNAL_NAME = "HesabYar Adapter Test General Journal"
FISCAL_YEAR_NAME = "FY 2026"
FISCAL_YEAR_START = date(2026, 1, 1)
FISCAL_YEAR_END = date(2026, 12, 31)


def get_or_create_irr_currency():
    Currency = pool.get("currency.currency")
    currencies = Currency.search([("code", "=", "IRR")], limit=1)
    if currencies:
        currency, = currencies
        print(f"IRR currency already exists (id={currency.id}, "
              f"digits={currency.digits}) -- leaving untouched")
        return currency
    # ISO 4217 defaults for IRR. US-004 owns the digits decision; this
    # create-only step just makes a company possible on a fresh database.
    currency, = Currency.create([{
        "name": "Iranian Rial",
        "symbol": "IRR",
        "code": "IRR",
        "numeric_code": "364",
        "digits": 2,
        "rounding": Decimal("0.01"),
    }])
    print(f"Created IRR currency (id={currency.id}, digits=2 ISO default)")
    return currency


def get_or_create_company(currency):
    Party = pool.get("party.party")
    Company = pool.get("company.company")
    parties = Party.search([("name", "=", COMPANY_NAME)], limit=1)
    if parties:
        party, = parties
        print(f"Party already exists (id={party.id})")
    else:
        party, = Party.create([{"name": COMPANY_NAME}])
        print(f"Created party (id={party.id})")
    companies = Company.search([("party", "=", party.id)], limit=1)
    if companies:
        company, = companies
        print(f"Company already exists (id={company.id})")
        return company
    company, = Company.create([{
        "party": party.id,
        "currency": currency.id,
    }])
    print(f"Created company (id={company.id})")
    return company


def install_chart(company):
    """Replicate the `account.create_chart` wizard transitions.

    Source: trytond/modules/account/account.py, class CreateChart,
    transition_create_account / transition_create_properties (Tryton 8.2.2).
    """
    Account = pool.get("account.account")
    AccountTemplate = pool.get("account.account.template")
    TaxTemplate = pool.get("account.tax.template")
    TaxCodeTemplate = pool.get("account.tax.code.template")
    TaxCodeLineTemplate = pool.get("account.tax.code.line.template")
    TaxRuleTemplate = pool.get("account.tax.rule.template")
    TaxRuleLineTemplate = pool.get("account.tax.rule.line.template")
    Config = pool.get("ir.configuration")
    Configuration = pool.get("account.configuration")

    existing = Account.search([("company", "=", company.id)], limit=1)
    if existing:
        count = Account.search_count([("company", "=", company.id)])
        print(f"Company already has a chart ({count} accounts) -- skipping")
        return

    roots = AccountTemplate.search([("parent", "=", None)])
    assert len(roots) == 1, f"expected exactly 1 root chart template, got {len(roots)}"
    account_template, = roots
    print(f"Installing chart template {account_template.rec_name!r} "
          f"(id={account_template.id})")

    with Transaction().set_context(
            language=Config.get_language(), company=company.id):
        template2type = {}
        account_template.type.create_type(
            company.id, template2type=template2type)
        template2account = {}
        account_template.create_account(
            company.id,
            template2account=template2account,
            template2type=template2type)
        template2tax = {}
        TaxTemplate.create_tax(
            account_template.id, company.id,
            template2account=template2account,
            template2tax=template2tax)
        template2tax_code = {}
        TaxCodeTemplate.create_tax_code(
            account_template.id, company.id,
            template2tax_code=template2tax_code)
        template2tax_code_line = {}
        TaxCodeLineTemplate.create_tax_code_line(
            account_template.id,
            template2tax=template2tax,
            template2tax_code=template2tax_code,
            template2tax_code_line=template2tax_code_line)
        account_template.update_account2(template2account, template2tax)
        template2rule = {}
        TaxRuleTemplate.create_rule(
            account_template.id, company.id,
            template2rule=template2rule)
        template2rule_line = {}
        TaxRuleLineTemplate.create_rule_line(
            account_template.id, template2tax, template2rule,
            template2rule_line=template2rule_line)

    count = Account.search_count([("company", "=", company.id)])
    print(f"Chart installed: {count} accounts")

    # Default receivable / payable accounts (wizard properties step).
    # NOTE (US-502): the wizard only pre-fills when exactly one
    # receivable/payable account exists, but the universal chart has
    # three of each -- so the old len==1 condition NEVER set the
    # defaults, and the sale flow (which resolves the invoice account
    # through the party fallback to these defaults) failed with
    # "must define a receivable account for party". Set 1.2.1/2.1.1
    # explicitly, the same defaults install_chart uses.
    receivable = Account.search([
        ("code", "=", "1.2.1"),
        ("company", "=", company.id),
    ], limit=1)
    payable = Account.search([
        ("code", "=", "2.1.1"),
        ("company", "=", company.id),
    ], limit=1)
    with Transaction().set_context(company=company.id):
        config = Configuration(1)
        if receivable:
            config.default_account_receivable = receivable[0]
        if payable:
            config.default_account_payable = payable[0]
        config.save()
    print(f"Default receivable: "
          f"{receivable[0].rec_name if receivable else '(none set)'}; "
          f"default payable: "
          f"{payable[0].rec_name if payable else '(none set)'}")


def get_or_create_move_sequence(company):
    ModelData = pool.get("ir.model.data")
    Sequence = pool.get("ir.sequence.strict")
    seq_type_id = ModelData.get_id("account", "sequence_type_account_move")
    sequences = Sequence.search([
        ("company", "=", company.id),
        ("sequence_type", "=", seq_type_id),
    ], limit=1)
    if sequences:
        sequence, = sequences
        print(f"Move sequence already exists (id={sequence.id})")
        return sequence
    sequence, = Sequence.create([{
        "name": f"{COMPANY_NAME} Move Sequence",
        "sequence_type": seq_type_id,
        "company": company.id,
        "type": "incremental",
        "number_next": 1,
        "number_increment": 1,
        "padding": 4,
    }])
    print(f"Created move sequence (id={sequence.id})")
    return sequence


def get_or_create_fiscal_year(company, move_sequence):
    FiscalYear = pool.get("account.fiscalyear")
    Period = pool.get("account.period")
    years = FiscalYear.search([
        ("company", "=", company.id),
        ("name", "=", FISCAL_YEAR_NAME),
    ], limit=1)
    if years:
        fiscal_year, = years
        print(f"Fiscal year already exists (id={fiscal_year.id})")
    else:
        fiscal_year, = FiscalYear.create([{
            "name": FISCAL_YEAR_NAME,
            "start_date": FISCAL_YEAR_START,
            "end_date": FISCAL_YEAR_END,
            "company": company.id,
            "move_sequence": move_sequence.id,
        }])
        print(f"Created fiscal year (id={fiscal_year.id})")
    created = 0
    for month in range(1, 13):
        name = f"2026-{month:02d}"
        start = date(2026, month, 1)
        end = date(2026, month, calendar.monthrange(2026, month)[1])
        periods = Period.search([
            ("fiscalyear", "=", fiscal_year.id),
            ("name", "=", name),
        ], limit=1)
        if periods:
            continue
        Period.create([{
            "name": name,
            "start_date": start,
            "end_date": end,
            "fiscalyear": fiscal_year.id,
        }])
        created += 1
    print(f"Periods for FY 2026: {12 - created} already present, "
          f"{created} created (all open)")
    return fiscal_year


def get_or_create_invoice_sequences(company, fiscal_year):
    """Create the 4 invoice sequences + the fiscalyear row (US-502).

    Posting an invoice assigns its number from the fiscal year's
    invoice sequences; without them post fails with "you must define a
    sequence on fiscal year". Phase 2 never posted invoices, so this
    gap only surfaced with the US-502 sale/payment contract tests.
    """
    ModelData = pool.get("ir.model.data")
    Sequence = pool.get("ir.sequence.strict")
    InvoiceSequence = pool.get("account.fiscalyear.invoice_sequence")
    seq_type_id = ModelData.get_id(
        "account_invoice", "sequence_type_account_invoice")
    rows = InvoiceSequence.search([
        ("fiscalyear", "=", fiscal_year.id),
    ], limit=1)
    if rows:
        print(f"Invoice sequences already exist "
              f"(row id={rows[0].id})")
        return
    seqs = {}
    for key, name in [
            ("out_invoice", "Customer Invoice"),
            ("out_credit_note", "Customer Credit Note"),
            ("in_invoice", "Supplier Invoice"),
            ("in_credit_note", "Supplier Credit Note")]:
        existing = Sequence.search([
            ("company", "=", company.id),
            ("sequence_type", "=", seq_type_id),
            ("name", "=", f"{COMPANY_NAME} {name} Sequence"),
        ], limit=1)
        if existing:
            seqs[key], = existing
            continue
        seqs[key], = Sequence.create([{
            "name": f"{COMPANY_NAME} {name} Sequence",
            "sequence_type": seq_type_id,
            "company": company.id,
            "type": "incremental",
            "number_next": 1,
            "number_increment": 1,
            "padding": 4,
        }])
    row, = InvoiceSequence.create([{
        "company": company.id,
        "fiscalyear": fiscal_year.id,
        "out_invoice_sequence": seqs["out_invoice"].id,
        "out_credit_note_sequence": seqs["out_credit_note"].id,
        "in_invoice_sequence": seqs["in_invoice"].id,
        "in_credit_note_sequence": seqs["in_credit_note"].id,
    }])
    print(f"Created invoice sequences (row id={row.id})")


def get_or_create_journal():
    Journal = pool.get("account.journal")
    journals = Journal.search([("name", "=", JOURNAL_NAME)], limit=1)
    if journals:
        journal, = journals
        print(f"Journal already exists (id={journal.id})")
        return journal
    journal, = Journal.create([{
        "name": JOURNAL_NAME,
        "code": "HYAT-GEN",
        "type": "general",
    }])
    print(f"Created journal (id={journal.id})")
    return journal


def grant_admin_access(company):
    """Add the company to the admin user's allowed companies.

    Every accounting model carries a global ir.rule
    `[('company', 'in', Eval('companies', []))]`, so records are invisible
    to a user whose res.user.companies does not include the company -- even
    for the admin. Append merge-safely: never replace the list, so the
    sibling story's company grant is preserved.

    Also sets company_filter='all': with the default 'one' and no current
    company, User.get_companies() returns [] and every company-scoped
    read comes back empty (and writes fail with 400 "User in
    companies"). The dev DBs got 'all' implicitly from the US-004 IRR
    fixture (tests/infrastructure/test_tryton_irr_jalali.py sets it);
    a fresh database must set it explicitly here, or the contract
    tests see an empty chart and empty fiscal years.
    """
    User = pool.get("res.user")
    users = User.search([("login", "=", "admin")], limit=1)
    assert len(users) == 1, "admin user not found"
    admin, = users
    current = [c.id for c in admin.companies]
    if company.id in current:
        print(f"Admin already has access to company {company.id}")
    else:
        admin.companies = list(admin.companies) + [company]
        admin.save()
        print(f"Granted admin access to company {company.id} "
              f"(companies now: {current + [company.id]})")
    if admin.company_filter != "all":
        admin.company_filter = "all"
        admin.save()
        print("Admin company_filter set to 'all'")
    else:
        print("Admin company_filter already 'all'")


currency = get_or_create_irr_currency()
company = get_or_create_company(currency)
grant_admin_access(company)
install_chart(company)
move_sequence = get_or_create_move_sequence(company)
fiscal_year = get_or_create_fiscal_year(company, move_sequence)
get_or_create_invoice_sequences(company, fiscal_year)
get_or_create_journal()

# trytond-console rolls the transaction back on exit; commit explicitly.
transaction.commit()
print("COMMITTED: test company provisioning complete.")
print(f"Company: {COMPANY_NAME!r} (id={company.id})")
