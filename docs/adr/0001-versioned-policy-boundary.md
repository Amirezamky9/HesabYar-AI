# ADR-0001: Separate deterministic accounting from versioned legal policy

- **Status:** Accepted
- **Date:** 2026-09-20

## Context

HesabYar combines accounting standards, bookkeeping, Iranian tax rules, Moadian integration and LLM/MCP orchestration. Accounting invariants are comparatively stable; tax rules and government protocols are date-sensitive and can change. The existing v1.1 architecture treated several research claims as deterministic facts.

## Decision

1. Use a modular monolith with ports/adapters for R0.
2. Keep the ledger deterministic and independent from tax-policy and MCP frameworks.
3. Store legal rules as source-backed, effective-dated versions.
4. Require an `as_of_date` for policy decisions.
5. Treat external Moadian transmission as an explicitly approved side effect.
6. Treat document extraction and advisory analytics as untrusted/non-posting inputs.
7. Fail closed when a critical rule cannot be evaluated or verified.

## Consequences

- More schema/provenance work at the start.
- Historical calculations become reproducible.
- Legal updates do not require rewriting core accounting code.
- LLM orchestration cannot silently create accounting or tax side effects.
