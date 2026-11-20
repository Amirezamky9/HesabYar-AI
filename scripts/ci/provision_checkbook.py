# ruff: noqa: F821  # trytond-console provides pool/transaction as globals
# Provision one E2E checkbook in the Tryton test DB for the cheque
# Playwright specs (e2e/cheques.spec.ts).
#
# The cheques spec registers an ISSUED cheque through the UI form,
# which requires picking an existing checkbook, and the checkbooks
# page test expects at least one checkbook card — but the checkbooks
# page has no create UI, so a fresh database has none. This script
# creates the bank (+ party) and the checkbook the specs need:
#   * bank party "HesabYar E2E Checkbook Bank"
#   * checkbook "HesabYar E2E Checkbook", leaves 100-199
#
# Normally run via scripts/ci/setup-playwright.sh, right after the
# US-304 seed and before the trytond restart. Run standalone with:
#   <tryton-env>/bin/trytond-console -c <trytond.conf> \
#       -d <database> -f scripts/ci/provision_checkbook.py
# Then restart trytond (server cache).
#
# Idempotent: safe to re-run; existing records (matched by name) are
# reused and their ids re-printed.
#
# This script runs inside trytond-console, which provides `pool` and
# `transaction` and ROLLS BACK on exit -- so it commits explicitly.
# If you see a traceback, the transaction rolled back: NOTHING was saved
# (trytond-console still exits 0 -- check the output, not the exit code).

COMPANY_NAME = "HesabYar Adapter Test"
BANK_PARTY_NAME = "HesabYar E2E Checkbook Bank"
CHECKBOOK_NAME = "HesabYar E2E Checkbook"
FIRST_NUMBER = 100
LAST_NUMBER = 199


def get_company():
    Company = pool.get("company.company")
    companies = Company.search([("party.name", "=", COMPANY_NAME)], limit=1)
    assert companies, f"company {COMPANY_NAME!r} not found -- run setup-tryton.sh first"
    company, = companies
    print(f"Company {COMPANY_NAME!r} (id={company.id})")
    return company


def get_or_create_bank():
    Party = pool.get("party.party")
    Bank = pool.get("bank")
    parties = Party.search([("name", "=", BANK_PARTY_NAME)], limit=1)
    if parties:
        party, = parties
        print(f"Bank party already exists (id={party.id})")
    else:
        party, = Party.create([{"name": BANK_PARTY_NAME}])
        print(f"Created bank party (id={party.id})")
    banks = Bank.search([("party", "=", party.id)], limit=1)
    if banks:
        bank, = banks
        print(f"Bank already exists (id={bank.id})")
        return bank
    bank, = Bank.create([{"party": party.id}])
    print(f"Created bank (id={bank.id})")
    return bank


def get_or_create_checkbook(company, bank):
    Checkbook = pool.get("account.checkbook")
    books = Checkbook.search([("name", "=", CHECKBOOK_NAME)], limit=1)
    if books:
        book, = books
        print(f"Checkbook already exists (id={book.id})")
        return book
    book, = Checkbook.create([{
        "company": company.id,
        "name": CHECKBOOK_NAME,
        "bank": bank.id,
        "first_number": FIRST_NUMBER,
        "last_number": LAST_NUMBER,
    }])
    print(f"Created checkbook (id={book.id})")
    return book


company = get_company()
bank = get_or_create_bank()
book = get_or_create_checkbook(company, bank)

transaction.commit()
print(f"CHECKBOOK_ID={book.id}")
print("CHECKBOOK-PROVISION-COMMITTED")
