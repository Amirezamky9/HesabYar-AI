# ADR-0001: correct the architecture before implementation

Date: 2026-09-20. Status: accepted for the implementation baseline under the user's
explicit permission to review, correct and complete the architecture. This is not
approval to deploy, transmit invoices or activate legal rules.

## Preserved decisions from R0

Python, a thin FastMCP surface, deterministic financial logic, SQLite WAL,
floating tafsili, repository/transport/parser seams, an offline simulator, native
Moadian integration, general/currency invoice adapter roadmap, legal-source
provenance, Iranian accounting knowledge and optional document extraction.

## Corrections

* Separate design, source claims and implemented capabilities.
* Replace header-only balance checks and editable posted records with real posting
  invariants, immutable transactions and separately approved reversals.
* Put human approval and an atomic outbox before external transmission.
* Replace fail-open validation with typed outcomes and safe deterministic evaluation.
* Quarantine the R0 fiscal research and all unsupported protocol constants.
* Use typed money/decimal/date policies and per-year identifiers.
* Use stdio first and version-tested Streamable HTTP remotely, not legacy SSE by default.

## New engineering defaults selected in this review

* One legal entity per SQLite database in R1; no implicit multi-tenant SaaS.
* `src/hesabyar` application package; preserve upstream knowledge paths.
* Offline default. No public package name, endpoint or sandbox is assumed available.
* IRR integer storage; decimal-string money at public JSON boundaries.
* Foundation limit: 1,000 lines and 9e15 IRR total per voucher. These are software
  safety limits, not financial law; changing them requires range/overflow tests.
* Primary tafsili plus separately keyed simultaneous analytical dimensions.
* Existing legacy validator stays quarantined until F1; it is not silently replaced
  during a documentation audit and is not allowed to authorize financial effects.

## Alternatives not selected

A distributed services stack or Rust rewrite before the first ledger slice adds
unnecessary implementation surface. A standalone legal chatbot does not satisfy
the requested product. Hardcoding fiscal numbers would be quicker but would leave
no auditable applicability or source-version trail. A live send to test unknown
protocol assumptions risks real fiscal effects and is not an acceptable test.

## Consequences

Foundation implementation can begin now. Real Moadian integration and automated
fiscal decisions remain evidence-gated. The phase boundaries preserve all intended
capabilities without presenting future code or unverified law as already working.
