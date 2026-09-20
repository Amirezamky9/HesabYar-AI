# Independent architecture review and change record

Date: 2026-09-20. Source commit: `06c99b58e01f3341f981eb16b334d3f1ed84a10c`.
Output revision: HY-ARCH-1.2.0-R1. User-authorized architecture correction;
no production deployment, credentials, authority requests or default-branch merge.

## Evidence and method

Read the original architecture in full, repository tree, README, validator code,
validation rules, existing tests and workflow. The repository contains a knowledge
base/validator, not the whole runtime depicted in the architecture. Review findings
below distinguish repository observations from new engineering decisions and from
unverified fiscal assertions. Original files are archived using their original Git
blob identities, not rewritten as if the earlier design never existed.

| ID | Priority | Evidence in the source baseline | Correction / remaining implementation |
|---|---|---|---|
| A01 | P0 | Architecture sections 1/5/11 call planned features complete and zero-error | Truthful capability states; canonical architecture sections 1-2 |
| A02 | P0 | `scripts/validator.py`: evaluation failure returns `passed=True` | F1 fail-closed results; legacy runtime remains blocked for effects |
| A03 | P0 | Same file: missing rules return `{}`; empty report is valid | Explicit configuration/incomplete-report failure in F1 |
| A04 | P1 | String substitution, `eval`, float result, missing aggregates -> zero | Allowlisted evaluator, exact arithmetic and missing-data semantics |
| A05 | P1 | Existing tests count skips as passes and one test only checks list type | Required regression matrix; no false pass coverage |
| A06 | P0 | R0 section 10 checks cached header totals, not actual journal items | Real line sums, posting transaction and executable SQL contract |
| A07 | P0 | Posted vouchers/items are mutable; FK cascades undermine audit claims | Immutable posted records/dimensions; restricted deletion; append-only events |
| A08 | P1 | Voucher number globally unique; no fiscal-year/period/entity model | Single-entity DB, per-year numbering and period controls |
| A09 | P1 | One tafsili column despite matrix requirements | Primary tafsili plus simultaneous typed dimensions |
| A10 | P0 | R0 section 12 uses float money-related values; SQLite DECIMAL assumed exact | Decimal strings/integers; strict bounds and versioned rounding |
| A11 | P0 | Defaulted fields rely on field validators for required cross-field checks | Model-level invariants; missing-reference/default-line negative tests |
| A12 | P0 | Model tools can post/send without trusted approval binding | Actor/role/approval service; draft/approve/post separation |
| A13 | P0 | R0 section 6 sends before recording; no durable outbox | Atomic enqueue before network; stable IDs and crash reconciliation |
| A14 | P0 | HTTP, remote acceptance, buyer response and local states conflated | Separate state machines; UNKNOWN_OUTCOME not blind resend |
| A15 | P0 | TaxID epoch/preprocessing, crypto profile and endpoints unsourced | No enabled native profile until authoritative spec and independent vectors |
| A16 | P1 | Subject 1-4 called invoice type; FX mixed with exports; universal buyer constraints | Versioned kind/pattern/subject/buyer schemas, no guessed codes |
| A17 | P0 | R0 section 7 treats a local cap as authority balance and auto-corrects invoices | Evidence-dated observations, estimates, reservations and human review |
| A18 | P0 | R0 sections 7-9 turn tax research assertions into fixed rate/deadline/crime rules | Research quarantine; zero active fiscal rules; reviewed version registry |
| A19 | P0 | Cash contributions become expenses; R&D spend becomes credit by default | Evidence-dependent tax-position lifecycle and no double allocation |
| A20 | P1 | Land-only revaluation and bank-credit guilt assumptions become software guards | Remove unsupported categorical restrictions; advisory not adjudication |
| A21 | P1 | SSE default, SDK ambiguity, universal timeout, assumed published package | Pin SDK/protocol, stdio first, tested Streamable HTTP, truthful packaging |
| A22 | P1 | Simulator repeats producer logic and claims official errors/conformance | Independent vectors and labelled synthetic profile; production gate |
| A23 | P1 | Parsing time/Rust speed guarantees without measurements; no document trust boundary | Remove promises; bounded parsing, provenance, isolation and threat tests |
| A24 | P1 | Workflow runs only `tests/test_validator.py` | Separate architecture-contract CI; future full application collection in F0 |
| A25 | P1 | Unversioned rules, numeric YAML paragraph refs, no precedence/change contract | Immutable rules/source hashes and explicit source precedence |
| A26 | P1 | No restore/migration/account snapshot or audit trust model | F2 application obligations and F6 operational acceptance |

P0 means an implementation blocker for the affected financial-effect path; it does
not mean all work must stop. P1 means required before its dependent feature/release.
These are engineering priorities, not legal or political evaluations.

## Numerical correction, not a legal finding

The research's example has 130 million IRR remaining and 1.5 million IRR replacement
cost per unit: 86.6667 units versus 100 is a 13.3333% physical decline, not 20%.
The 20-million-IRR funding gap includes the assumed 45-million dividend. Without
that dividend, its own cash figures cover the 150-million replacement need.
The architecture now separates financing/cash assumptions from tax effects.

## External technical verification

Primary documentation consulted on 2026-09-20:

* SQLite type affinity: https://www.sqlite.org/datatype3.html
* SQLite constraints: https://www.sqlite.org/lang_createtable.html
* Pydantic validator/default behavior: https://docs.pydantic.dev/latest/concepts/validators/
* MCP transport specification (dated revision, not asserted latest):
  https://modelcontextprotocol.io/specification/2025-11-25/basic/transports

The MCP source confirms the move away from the old HTTP+SSE transport; implementers
must pin and test the actual SDK/protocol revision rather than assume all revisions
have identical transport behavior. The Pydantic source explains why omitted
defaulted fields are not safely guarded merely by field-level validators.

No authoritative current Moadian specification or complete legal rule pack was
established in this review. In particular, the previous chat response is not proof
of newly claimed 1405 rates, ceilings or circulars. Those claims are not activated.

## Verification performed and limits

Executed the new reference-schema suite locally on SQLite 3.46.1:

```sh
python -m unittest discover -s tests/architecture -p 'test_*.py' -v
```

26 tests passed. Coverage includes real balance checking, period/number constraints,
posted-record and dimension immutability, tafsili checks, restricted entity identity,
idempotency uniqueness, foreign keys and audit chain/append-only behavior including
`INSERT OR REPLACE` attempts. See the committed tests for exact assertions.

This is NOT a run of the legacy repository's full CI, production application,
real crypto conformance, authenticated MCP integration or a fiscal submission.
The reference schema does not implement application approval/authentication,
atomic post+audit, exact reversal matching, idempotency payload comparison or
backups; these remain mandatory F2 tasks. `scripts/validator.py` was not silently
rewritten by a documentation patch; its fail-open behavior remains the first F1
implementation blocker. The whole knowledge corpus was not re-certified.

## Preservation and delivery boundaries

The R0 architecture, research and README retain their original blobs under
`docs/archive/`. LICENSE and all inherited source/standards assets remain intact.
New engineering defaults are in ADR-0001. The resulting state is foundation
implementation readiness, not completion of all software or production approval.
