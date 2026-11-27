# ruff: noqa: F821  # trytond-console provides pool/transaction as globals
# Provision fiscal year 2027 (+ 12 open monthly periods) for the
# "HesabYar Adapter Test" company on the Tryton test DB.
#
# The settlement contract tests (tests/contract/test_settlement_posting.py)
# clear and settle Jan/Feb 2027 tranches, so they SKIP unless FY 2027
# exists with all 12 periods open. The dev databases got FY 2027 by a
# one-time manual REST setup (lane 1 only); this script makes it
# reproducible for CI and fresh databases.
#
# Run via scripts/ci/setup-tryton.sh, or standalone with:
#   <tryton-env>/bin/trytond-console -c <trytond.conf> \
#       -d <database> \
#       --lock-table account_fiscalyear account_period account_journal account_configuration \
#       -f scripts/ci/provision_fy2027.py
#
# The --lock-table flags are REQUIRED: FiscalYear.create calls lock(),
# which raises _TransactionLockError unless the table was pre-declared
# (see provision_adapter_test_company.py). trytond-console provides
# `pool` and `transaction` and ROLLS BACK on exit -- so the script
# commits explicitly at the end.
#
# What it creates (idempotent -- safe to re-run; existing records are
# reused):
#   * a dedicated move sequence for FY 2027 (Tryton enforces
#     move-sequence uniqueness per fiscal year).
#   * fiscal year "FY 2027" (2027-01-01 .. 2027-12-31) + 12 open
#     monthly periods ("2027-01" .. "2027-12").
#   * the fiscalyear.invoice_sequence row for FY 2027, REUSING the
#     company's 4 invoice sequences (sequences are shared across
#     fiscal years; only the linking row is per-year).
#
# Requires the Adapter Test company + its FY 2026 provisioning to
# exist first (provision_adapter_test_company.py).

import calendar
from datetime import date

COMPANY_NAME = "HesabYar Adapter Test"
FISCAL_YEAR_NAME = "FY 2027"
FISCAL_YEAR_START = date(2027, 1, 1)
FISCAL_YEAR_END = date(2027, 12, 31)
MOVE_SEQUENCE_NAME = f"{COMPANY_NAME} FY 2027 Move Sequence"


def get_company():
    Company = pool.get("company.company")
    companies = Company.search([("party.name", "=", COMPANY_NAME)], limit=1)
    assert companies, f"Company {COMPANY_NAME!r} not found -- run provision_adapter_test_company.py first"
    company, = companies
    print(f"Company {COMPANY_NAME!r} (id={company.id})")
    return company


def get_or_create_move_sequence(company):
    ModelData = pool.get("ir.model.data")
    Sequence = pool.get("ir.sequence.strict")
    seq_type_id = ModelData.get_id("account", "sequence_type_account_move")
    sequences = Sequence.search([
        ("company", "=", company.id),
        ("sequence_type", "=", seq_type_id),
        ("name", "=", MOVE_SEQUENCE_NAME),
    ], limit=1)
    if sequences:
        sequence, = sequences
        print(f"FY 2027 move sequence already exists (id={sequence.id})")
        return sequence
    sequence, = Sequence.create([{
        "name": MOVE_SEQUENCE_NAME,
        "sequence_type": seq_type_id,
        "company": company.id,
        "type": "incremental",
        "number_next": 1,
        "number_increment": 1,
        "padding": 4,
    }])
    print(f"Created FY 2027 move sequence (id={sequence.id})")
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
        name = f"2027-{month:02d}"
        start = date(2027, month, 1)
        end = date(2027, month, calendar.monthrange(2027, month)[1])
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
    print(f"Periods for FY 2027: {12 - created} already present, "
          f"{created} created (all open)")
    return fiscal_year


def get_or_create_invoice_sequences(company, fiscal_year):
    """Link FY 2027 to the company's 4 invoice sequences.

    The sequences themselves are shared across fiscal years (created
    by provision_adapter_test_company.py); only the
    account.fiscalyear.invoice_sequence row is per-year.
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
        print(f"FY 2027 invoice-sequence row already exists "
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
        assert existing, (
            f"Invoice sequence {name!r} not found -- run "
            "provision_adapter_test_company.py first")
        seqs[key], = existing
    row, = InvoiceSequence.create([{
        "company": company.id,
        "fiscalyear": fiscal_year.id,
        "out_invoice_sequence": seqs["out_invoice"].id,
        "out_credit_note_sequence": seqs["out_credit_note"].id,
        "in_invoice_sequence": seqs["in_invoice"].id,
        "in_credit_note_sequence": seqs["in_credit_note"].id,
    }])
    print(f"Created FY 2027 invoice-sequence row (id={row.id})")


company = get_company()
move_sequence = get_or_create_move_sequence(company)
fiscal_year = get_or_create_fiscal_year(company, move_sequence)
get_or_create_invoice_sequences(company, fiscal_year)

# trytond-console rolls the transaction back on exit; commit explicitly.
transaction.commit()
print("COMMITTED: FY 2027 provisioning complete.")
print(f"Fiscal year: {FISCAL_YEAR_NAME!r} (id={fiscal_year.id})")
