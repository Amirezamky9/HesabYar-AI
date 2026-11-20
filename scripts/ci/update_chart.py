# ruff: noqa: F821  # trytond-console provides pool/transaction as globals
# Run hesabyar_setup's update_chart on the "HesabYar Adapter Test"
# company, so its chart picks up the HesabYar template accounts
# (cash-box 1.1.5/1.2.6/1.2.7/2.1.6, notes 1.2.4/1.2.5/2.1.5, bounced
# cheques 1.2.9, settlement 1.1.6/1.2.8/5.2.4, memorandum 7.x).
#
# Run via scripts/ci/setup-tryton.sh, or standalone with:
#   <tryton-env>/bin/trytond-console -c <trytond.conf> \
#       -d <database> -f scripts/ci/update_chart.py
#
# trytond-console provides `pool` and `transaction` and ROLLS BACK on
# exit -- so the script commits explicitly at the end. Idempotent:
# update_chart only creates accounts missing from the template, so
# re-running it is a no-op (and the cash-box / settlement contract
# tests skip when these accounts are missing, which is why the step
# exists at all).
#
# Requires the Adapter Test company + its base chart to exist first
# (provision_adapter_test_company.py) and hesabyar_setup to be
# activated (its account_chart_en.xml carries the templates).

COMPANY_NAME = "HesabYar Adapter Test"

Company = pool.get("company.company")
companies = Company.search([("party.name", "=", COMPANY_NAME)], limit=1)
assert companies, f"Company {COMPANY_NAME!r} not found -- run provision_adapter_test_company.py first"
company, = companies
print(f"Company {COMPANY_NAME!r} (id={company.id})")

summary = company.update_chart()
print(f"update_chart summary: {summary}")

# trytond-console rolls the transaction back on exit; commit explicitly.
transaction.commit()
print("COMMITTED: update_chart complete.")
