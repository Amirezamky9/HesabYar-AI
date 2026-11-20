# ruff: noqa: F821  # trytond-console provides pool/transaction as globals
# Seed the US-304 Playwright fixtures in a Tryton database.
#
# VENDORED COPY of hesabyar-reference/seed_us304.py (the dev source of
# truth, which lives outside the repo). CI checks out only the repo, so
# the Playwright job needs this copy in scripts/ci/. When the seed
# changes, update BOTH copies and note it in docs/ci-playwright.md.
# The logic below is identical to the dev copy; only this header differs.
#
# Run by scripts/ci/setup-playwright.sh (which captures the printed
# US304_* lines into apps/admin/.env.local). Standalone use:
#   <tryton-env>/bin/trytond-console -c <trytond.conf> -d <db> \
#       --lock-table account_fiscalyear account_period account_journal account_configuration \
#       -f scripts/ci/seed_us304.py
# Then restart trytond (server cache) and copy the printed US304_* lines
# into apps/admin/.env.local (gitignored, never commit).
#
# Idempotent: safe to re-run; existing records (matched by name) are reused
# and their ids re-printed. The specs need:
#   * party "HesabYar US-304 Test Customer" (+ address)
#   * DRAFT customer invoice dated 2026-11-17 (the CSV spec hardcodes that date)
#   * DRAFT receivable payment, 500,000 IRR, journal "HesabYar US-304 Payments",
#     reference "US-304 e2e payment"
#   * DRAFT statement "US-304 e2e statement", journal "HesabYar US-304 Bank",
#     2 unmatched lines (+1,000,000 "Customer receipt", -250,000 "Supplier
#     payment"), start 0 / end 750,000
#
# This script runs inside trytond-console, which provides `pool` and
# `transaction` and ROLLS BACK on exit -- so it commits explicitly.
# If you see a traceback, the transaction rolled back: NOTHING was saved
# (trytond-console still exits 0 -- check the output, not the exit code).

from datetime import date
from decimal import Decimal

from trytond.transaction import Transaction

COMPANY_NAME = "HesabYar Adapter Test"
CUSTOMER_NAME = "HesabYar US-304 Test Customer"
SUPPLIER_NAME = "HesabYar US-304 Test Supplier"
PAYMENT_JOURNAL_NAME = "HesabYar US-304 Payments"
PAYMENT_REFERENCE = "US-304 e2e payment"
STATEMENT_JOURNAL_NAME = "HesabYar US-304 Bank"
ACCOUNT_JOURNAL_NAME = "HesabYar US-304 Statement Journal"
STATEMENT_NAME = "US-304 e2e statement"
SEED_DATE = date(2026, 11, 17)


def get_company():
    Company = pool.get("company.company")
    companies = Company.search([("party.name", "=", COMPANY_NAME)], limit=1)
    assert companies, f"company {COMPANY_NAME!r} not found -- run restore-dev-env.sh first"
    company, = companies
    print(f"Company {COMPANY_NAME!r} (id={company.id})")
    return company


def get_account(code):
    Account = pool.get("account.account")
    accounts = Account.search([("code", "=", code)], limit=1)
    assert accounts, f"account {code!r} not found in the chart"
    account, = accounts
    return account


def get_or_create_party():
    Party = pool.get("party.party")
    Address = pool.get("party.address")
    parties = Party.search([("name", "=", CUSTOMER_NAME)], limit=1)
    if parties:
        party, = parties
        print(f"Party already exists (id={party.id})")
    else:
        party, = Party.create([{"name": CUSTOMER_NAME}])
        print(f"Created party (id={party.id})")
    addresses = Address.search([("party", "=", party.id)], limit=1)
    if addresses:
        address, = addresses
        print(f"Address already exists (id={address.id})")
    else:
        address, = Address.create([{
            "party": party.id,
            "street": "US-304 Test Street 1",
            "city": "Tehran",
        }])
        print(f"Created address (id={address.id})")
    suppliers = Party.search([("name", "=", SUPPLIER_NAME)], limit=1)
    if suppliers:
        supplier, = suppliers
        print(f"Supplier party already exists (id={supplier.id})")
    else:
        supplier, = Party.create([{"name": SUPPLIER_NAME}])
        print(f"Created supplier party (id={supplier.id})")
    return party, address, supplier


def get_or_create_payment_journal(company, currency):
    Journal = pool.get("account.payment.journal")
    journals = Journal.search([("name", "=", PAYMENT_JOURNAL_NAME)], limit=1)
    if journals:
        journal, = journals
        print(f"Payment journal already exists (id={journal.id})")
        return journal
    journal, = Journal.create([{
        "name": PAYMENT_JOURNAL_NAME,
        "currency": currency.id,
        "company": company.id,
        "process_method": "manual",
    }])
    print(f"Created payment journal (id={journal.id})")
    return journal


def get_or_create_payment(company, party, journal):
    Payment = pool.get("account.payment")
    payments = Payment.search([("reference", "=", PAYMENT_REFERENCE)], limit=1)
    if payments:
        payment, = payments
        print(f"Payment already exists (id={payment.id})")
        return payment
    payment, = Payment.create([{
        "company": company.id,
        "journal": journal.id,
        "kind": "receivable",
        "party": party.id,
        "date": SEED_DATE,
        "amount": Decimal("500000"),
        "reference": PAYMENT_REFERENCE,
    }])
    print(f"Created payment (id={payment.id})")
    return payment


def get_or_create_account_journal(company):
    Journal = pool.get("account.journal")
    journals = Journal.search([("name", "=", ACCOUNT_JOURNAL_NAME)], limit=1)
    if journals:
        journal, = journals
        print(f"Account journal already exists (id={journal.id})")
        return journal
    journal, = Journal.create([{
        "name": ACCOUNT_JOURNAL_NAME,
        "code": "STMT-US304",
        "type": "statement",
    }])
    print(f"Created account journal (id={journal.id})")
    return journal


def get_or_create_statement_journal(company, currency, account_journal, bank_account):
    Journal = pool.get("account.statement.journal")
    journals = Journal.search([("name", "=", STATEMENT_JOURNAL_NAME)], limit=1)
    if journals:
        journal, = journals
        print(f"Statement journal already exists (id={journal.id})")
        return journal
    journal, = Journal.create([{
        "name": STATEMENT_JOURNAL_NAME,
        "journal": account_journal.id,
        "currency": currency.id,
        "company": company.id,
        "validation": "balance",
        "account": bank_account.id,
    }])
    print(f"Created statement journal (id={journal.id})")
    return journal


def get_or_create_statement(company, journal, party, supplier, receivable, payable):
    Statement = pool.get("account.statement")
    statements = Statement.search([("name", "=", STATEMENT_NAME)], limit=1)
    if statements:
        statement, = statements
        print(f"Statement already exists (id={statement.id})")
        return statement
    statement, = Statement.create([{
        "name": STATEMENT_NAME,
        "journal": journal.id,
        "company": company.id,
        "date": SEED_DATE,
        "start_balance": Decimal("0"),
        "end_balance": Decimal("750000"),
        "lines": [
            ("create", [{
                "date": SEED_DATE,
                "description": "Customer receipt",
                "amount": Decimal("1000000"),
                "party": party.id,
                "account": receivable.id,
            }, {
                "date": SEED_DATE,
                "description": "Supplier payment",
                "amount": Decimal("-250000"),
                "party": supplier.id,
                "account": payable.id,
            }]),
        ],
    }])
    print(f"Created statement (id={statement.id})")
    return statement


def get_or_create_invoice(company, currency, party, address, receivable, revenue):
    Invoice = pool.get("account.invoice")
    invoices = Invoice.search([
        ("party", "=", party.id),
        ("type", "=", "out"),
        ("state", "=", "draft"),
        ("invoice_date", "=", SEED_DATE),
    ], limit=1)
    if invoices:
        invoice, = invoices
        print(f"Invoice already exists (id={invoice.id})")
        return invoice
    invoice, = Invoice.create([{
        "company": company.id,
        "currency": currency.id,
        "party": party.id,
        "invoice_address": address.id,
        "type": "out",
        "invoice_date": SEED_DATE,
        "account": receivable.id,
        "description": "US-304 seed invoice",
        "lines": [
            ("create", [{
                "account": revenue.id,
                "quantity": 1,
                "unit_price": Decimal("5000000"),
                "description": "کالای آزمایشی",
            }]),
        ],
    }])
    print(f"Created invoice (id={invoice.id})")
    return invoice


def main():
    company = get_company()
    with Transaction().set_context(company=company.id):
        _seed(company)


def _seed(company):
    Currency = pool.get("currency.currency")
    currency, = Currency.search([("code", "=", "IRR")], limit=1)
    receivable = get_account("1.2.1")
    revenue = get_account("4.1.1")
    # Bank/cash account for the statement journal: first postable 1.1.* account.
    Account = pool.get("account.account")
    bank_accounts = Account.search([
        ("code", "like", "1.1.%"),
        ("type", "!=", None),
        ("closed", "!=", True),
    ], limit=1)
    assert bank_accounts, "no postable 1.1.* bank/cash account found"
    bank_account, = bank_accounts
    # Payable counterpart for the supplier-payment line: first postable 2.* account.
    payable_accounts = Account.search([
        ("code", "like", "2.%"),
        ("type", "!=", None),
        ("closed", "!=", True),
    ], limit=1)
    assert payable_accounts, "no postable 2.* payable account found"
    payable, = payable_accounts

    party, address, supplier = get_or_create_party()
    payment_journal = get_or_create_payment_journal(company, currency)
    payment = get_or_create_payment(company, party, payment_journal)
    account_journal = get_or_create_account_journal(company)
    statement_journal = get_or_create_statement_journal(
        company, currency, account_journal, bank_account)
    statement = get_or_create_statement(
        company, statement_journal, party, supplier, receivable, payable)
    invoice = get_or_create_invoice(
        company, currency, party, address, receivable, revenue)

    transaction.commit()
    print()
    print("Seed complete -- copy these lines into apps/admin/.env.local:")
    print()
    print(f"US304_PARTY_ID={party.id}")
    print(f"US304_REVENUE_ACCOUNT_ID={revenue.id}")
    print(f"US304_PAYMENT_ID={payment.id}")
    print(f"US304_STATEMENT_ID={statement.id}")
    print(f"(seed invoice id={invoice.id}, state draft)")


main()
