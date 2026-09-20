# HesabYar-AI | Iranian accounting assistant foundation

**Architecture: HY-ARCH-1.2.0-R1. Foundation implementation ready; production blocked.**

HesabYar-AI is building deterministic Iranian accounting validation, an approved
local double-entry ledger with floating tafsili, a thin MCP interface and a
versioned native Moadian adapter. These are product goals, not claims that all
features or a certified accountant service already exist.

## Start here

[Canonical architecture](docs/ARCHITECTURE.md) explains scope and source precedence.
[Implementation plan](docs/IMPLEMENTATION_PLAN.md) gives the coding sequence and
acceptance gates. [Domain contracts](docs/contracts/domain-contracts.md) specify
posting, approval, idempotency and invoice delivery. [Review findings](docs/review/ARCHITECTURE_REVIEW.md)
trace the corrections to the reviewed commit. [ADR-0001](docs/adr/0001-implementation-readiness.md)
distinguishes inherited decisions from newly chosen engineering defaults.

The earlier Persian architecture, research and README are preserved unchanged in
[the archive](docs/archive/README-R0.md). They are historical evidence, not the
current implementation contract. The [research status](docs/research_tax_inflation.md)
and [regulatory registry](policies/regulatory-rules.json) prohibit treating unverified
FACT labels as executable law. No current Iranian tax rule is activated by this review.

## What exists and what does not

| Component | Actual state |
|---|---|
| Accounting knowledge, templates, mappings, source-processing scripts | Inherited assets; not newly certified as current law |
| Legacy financial validator and tests | Present; fail-open defects documented; not safe as an effect-authorization gate |
| Foundation SQL contract and architecture tests | Executable reference examples, not the finished ledger application |
| Approved ledger, MCP server, parser service, invoice/outbox runtime | To implement through F0-F4 |
| Verified native Moadian profile and tax decision modules | Evidence-gated F5-F6; no production sending |
| Published installable MCP package | Not established by this repository review; no `uvx` release claim |

## Run the reference contract tests

Use Python 3.10+ with SQLite >=3.37 and JSON support. No external dependencies,
credentials or network calls are needed for these reference tests:

```sh
python -m unittest discover -s tests/architecture -p 'test_*.py' -v
```

These tests cover selected SQL invariants only. The application still needs trusted
approval, atomic post/audit, migrations, exact reversals, worker crash recovery,
backup/restore and full integration tests. The inherited diagnostic CLI remains
`python scripts/validator.py tests/sample-data.json --format markdown` after its
legacy dependencies are installed, but its output must not be treated as a
reliable financial authorization until F1 is completed.

## Coding sequence

F0 reproducible package/source boundaries -> F1 fail-closed validator -> F2 approved
local ledger -> F3 thin MCP -> F4 offline invoice/outbox -> F5 verified native protocol
-> F6 reviewed fiscal features and separately authorized production release.

## Lineage, acknowledgments and license

HesabYar-AI continues the work of **Vohuman (`seiahposh`)**, original author of
[seiahposh/accounting-iran-standards](https://github.com/seiahposh/accounting-iran-standards).
The inherited standards corpus, YAML templates and validation rules, IFRS mappings,
and processing scripts remain credited to that upstream work. Its **MIT License,
Copyright (c) 2026 Vohuman**, is retained without alteration in [LICENSE](LICENSE).
The original full acknowledgment text is also preserved in the archived README.

The repository's knowledge materials are inputs requiring applicability/version
review; their presence is not a certification of the product or of current law.
