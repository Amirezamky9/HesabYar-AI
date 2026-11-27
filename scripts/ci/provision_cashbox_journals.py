# ruff: noqa: F821  # trytond-console provides pool/transaction as globals
# Provision the US-516 cash-box statement journals in the Tryton test DB.
#
# One Statement Journal per cash box on the Adapter Test company:
#   * "HesabYar US-516 Store Cash Box"  -> cash account 1.1.1
#   * "HesabYar US-516 Petty Cash Box"  -> petty-cash account 1.1.5
# Each has its own statement-type account.journal (validation='balance').
#
# This is the in-repo copy of the original dev provisioning script
# (~/workspace/coord-logs/us516-provision-cashboxes.py); the logic is
# identical, only the paths changed so CI can run it from a bare
# checkout. Normally run via scripts/ci/setup-tryton.sh, which passes
# the right --lock-table flags. Run standalone with:
#   <tryton-env>/bin/trytond-console -c <trytond.conf> \
#       -d <database> \
#       --lock-table account_journal account_statement_journal \
#       -f scripts/ci/provision_cashbox_journals.py
# Then restart trytond (server cache).
#
# Idempotent: safe to re-run; existing records (matched by name) are
# reused and their ids re-printed. Prints US516_* lines for the e2e
# specs and the cash-box registry.
#
# This script runs inside trytond-console, which provides `pool` and
# `transaction` and ROLLS BACK on exit -- so it commits explicitly.
# If you see a traceback, the transaction rolled back: NOTHING was saved
# (trytond-console still exits 0 -- check the output, not the exit code).

COMPANY_NAME = "HesabYar Adapter Test"

BOXES = [
    {
        "key": "STORE",
        "statement_journal_name": "HesabYar US-516 Store Cash Box",
        "account_journal_name": "HesabYar US-516 Store Cash Journal",
        "account_journal_code": "STMT-US516-STORE",
        "account_code": "1.1.1",
    },
    {
        "key": "PETTY",
        "statement_journal_name": "HesabYar US-516 Petty Cash Box",
        "account_journal_name": "HesabYar US-516 Petty Cash Journal",
        "account_journal_code": "STMT-US516-PETTY",
        "account_code": "1.1.5",
    },
]


def get_company():
    Company = pool.get("company.company")
    companies = Company.search([("party.name", "=", COMPANY_NAME)], limit=1)
    assert companies, f"company {COMPANY_NAME!r} not found"
    company, = companies
    print(f"Company {COMPANY_NAME!r} (id={company.id}, currency={company.currency.code})")
    return company


def get_account(code, company):
    Account = pool.get("account.account")
    accounts = Account.search([
        ("code", "=", code),
        ("company", "=", company.id),
    ], limit=1)
    assert accounts, f"account {code!r} not found for company {company.id} -- run update_chart first"
    account, = accounts
    print(f"Account {code} {account.name!r} (id={account.id})")
    return account


def get_or_create_account_journal(box):
    Journal = pool.get("account.journal")
    journals = Journal.search([("name", "=", box["account_journal_name"])], limit=1)
    if journals:
        journal, = journals
        print(f"Account journal already exists (id={journal.id})")
        return journal
    journal, = Journal.create([{
        "name": box["account_journal_name"],
        "code": box["account_journal_code"],
        "type": "statement",
    }])
    print(f"Created account journal (id={journal.id})")
    return journal


def get_or_create_statement_journal(box, company, account_journal, account):
    Journal = pool.get("account.statement.journal")
    journals = Journal.search([("name", "=", box["statement_journal_name"])], limit=1)
    if journals:
        journal, = journals
        print(f"Statement journal already exists (id={journal.id})")
        return journal
    journal, = Journal.create([{
        "name": box["statement_journal_name"],
        "journal": account_journal.id,
        "currency": company.currency.id,
        "company": company.id,
        "validation": "balance",
        "account": account.id,
    }])
    print(f"Created statement journal (id={journal.id})")
    return journal


company = get_company()
for box in BOXES:
    account = get_account(box["account_code"], company)
    account_journal = get_or_create_account_journal(box)
    statement_journal = get_or_create_statement_journal(
        box, company, account_journal, account)
    print(f"US516_{box['key']}_STATEMENT_JOURNAL_ID={statement_journal.id}")
    print(f"US516_{box['key']}_ACCOUNT_JOURNAL_ID={account_journal.id}")
    print(f"US516_{box['key']}_ACCOUNT_ID={account.id}")

transaction.commit()
print("US516-PROVISION-COMMITTED")
