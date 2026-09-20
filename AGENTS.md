# HesabYar implementation instructions

Read `docs/ARCHITECTURE.md`, `docs/contracts/domain-contracts.md` and
`docs/IMPLEMENTATION_PLAN.md` before editing. Current design revision:
HY-ARCH-1.2.0-R1. Record the actual checkout commit in each handoff.

Preserve Python, SQLite, floating tafsili, thin MCP and native-Moadian boundaries.
The architecture authorizes foundation implementation, not production release.
Start F0 -> F1 -> F2; do not reopen broad architecture discovery. The first working
slice is an approved local journal and trial balance, not a guessed tax connector.

`docs/archive/`, `docs/research_tax_inflation.md`, old chat answers and the inherited
SKILL/standard summaries are not verified current fiscal rules. Never hardcode their
rates, deadlines, TaxID constants or crime assertions. Use the reviewed rule registry.
The legacy validator has a documented fail-open defect and must not authorize any
financial effect until F1 regression tests pass. Preserve original LICENSE/credits.

Models cannot approve their own changes, post unapproved journals, mutate posted
entries or send real invoices. Implement trusted authorization/approval, exact
money, durable idempotent outbox and per-entity isolation. Default environment is
offline. Do not use production keys, taxpayer data or fiscal endpoints in CI.

Run reference tests with:
`python -m unittest discover -s tests/architecture -p 'test_*.py' -v`.
They test architecture examples only. Add actual application tests by milestone;
report precisely what ran, what remains blocked and which source versions apply.
Do not claim runtime completion from diagrams, mock tests or README badges.
