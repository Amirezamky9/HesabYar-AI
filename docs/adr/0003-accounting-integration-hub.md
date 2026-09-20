# ADR-0003: Accounting Integration Hub (Canonical Model, Connector Ports, Vendor Adapters & Governed Synchronization)

- **Status:** Accepted
- **Date:** 2026-09-20

## Context

Iranian small, medium, and enterprise businesses operate across a fragmented landscape of desktop, client-server, and cloud accounting packages (e.g., Sepidar System / سپیدار سیستم, Holoo / هلو, Parmis / پارمیس, Shayan / شایان, Hamkaran System / همکاران سیستم, Rayvarz / رایورز).
These legacy and commercial systems:
1. **Proprietary & Divergent Schemas:** Use proprietary database schemas (often on Microsoft SQL Server, Access, or Firebird), custom table prefixes, non-standard chart-of-accounts hierarchies, differing floating sub-ledger (Tafsili) semantics, and disparate document numbering rules.
2. **Coupling Risks:** Tightly coupling HesabYar-AI's core domain models (Ledger, Vouchers, Invoices, Payroll) to vendor-specific tables or APIs would destroy domain purity, violate bounded context isolation, and make HesabYar fragile to third-party schema migrations or protocol changes.
3. **Data Integrity & Involuntary Writes:** Writing journal entries, invoices, or master data directly into third-party accounting databases without authorization or validation risks data corruption, audit failure, and accounting period inconsistency.
4. **Synchronization Hazards:** Bidirectional data exchange introduces risks of infinite echo loops, conflicting concurrent updates, duplicated records, lost updates, and mismatched trial balances.
5. **Security & Credential Vulnerabilities:** External connection parameters (SQL connection strings, API tokens, desktop agent keys) require strict tenant isolation, zero credential leakage into logs or AI prompts, and robust access controls.

## Decisions

### 1. Canonical Accounting Model ↕ AccountingConnector Port ↕ Vendor Adapters
HesabYar-AI establishes a dedicated **Accounting Integration Hub** bounded context operating as an Anti-Corruption Layer (ACL):
- **Canonical Accounting Model:** Canonical domain data transfer objects (DTOs) representing the Chart of Accounts (`CanonicalAccount`), Floating Sub-Ledgers (`CanonicalTafsili`), Journal Vouchers (`CanonicalVoucher`, `CanonicalVoucherLine`), Counterparties (`CanonicalParty`), and Invoices (`CanonicalInvoice`).
- **AccountingConnector Port (`AccountingConnector`):** An abstract domain port interface defining lifecycle methods (`connect`, `disconnect`, `ping`, `healthcheck`, `test_credentials`) and standardized synchronization capabilities.
- **Vendor Adapters:** Concrete implementations adapting canonical calls to vendor-specific APIs, direct database drivers (e.g., MS SQL for Sepidar), file-based exchange (CSV/XML/Excel), or intermediate local bridge agents.

### 2. Vendor Isolation in Infrastructure
All vendor-specific logic, protocol decoders, SQL queries, and API clients reside strictly in:
`src/hesabyar/infrastructure/integrations/<vendor>/` (e.g., `sepidar/`, `holoo/`, `parmis/`, `generic_csv/`).
Domain and application layers MUST NOT import vendor packages or reference vendor-specific table/column names.

### 3. Strict Read/Write Governance: Mandatory Preview and Two-Phase Approval
Writing to an external accounting system carries severe business consequences.
- **No Autonomous Writes:** Agents, scheduled jobs, and automatic workflows are strictly forbidden from directly executing mutating writes (`INSERT`, `UPDATE`, `DELETE`) to external systems without explicit human authorization.
- **Mandatory Dry-Run / Preview:** Any proposed mutation generates an `ExternalMutationApproval` artifact detailing the exact external operations, affected records, diff preview, and potential rollback steps.
- **Two-Phase Approval:** Execution requires explicit approval by an authorized tenant actor holding the required operational scope.

### 4. Core Domain Entities and Data Contracts
The integration hub defines eight core domain models:
1. `ExternalSystemConnection`: Tenant-scoped connection credentials, encrypted secret references, endpoint URL, driver type, timeout parameters, and health status.
2. `ConnectorManifest`: Vendor metadata, supported protocol versions, supported sync modes, and credential schema definitions.
3. `ConnectorCapability`: Fine-grained capability negotiation flags (`READ_COA`, `READ_TAFSILI`, `READ_VOUCHERS`, `WRITE_VOUCHERS`, `READ_INVOICES`, `WRITE_INVOICES`, `SUPPORTS_WEBHOOKS`, `SUPPORTS_DELTA_SYNC`, `SUPPORTS_DRY_RUN`).
4. `ExternalObjectMapping`: Bi-directional identity mapping binding canonical entity IDs to external system IDs (with tenant isolation, entity type, content hash, and timestamp).
5. `SyncCheckpoint`: High-water mark state tracker storing sync cursors, transaction log sequence numbers, timestamps, and processed record counts.
6. `SyncConflict`: Immutable record of detected data conflicts between local and external systems, documenting local state, remote state, conflict nature, and resolution audit trail.
7. `ExternalMutationApproval`: Governed approval ticket containing payload preview, validation status, requesting actor, approving actor, expiration, and execution results.
8. `ReconciliationResult`: Financial discrepancy audit report comparing local vs. external balances, line item counts, and account totals.

### 5. Connector Modes
Connections operate under four mutually exclusive operational modes per entity type:
- `READ_ONLY`: Connects solely for telemetry, health checking, or read verification; zero state is imported into HesabYar or written externally.
- `IMPORT`: One-way data ingestion from the external system into HesabYar (external system is authoritative source).
- `EXPORT`: One-way data publishing from HesabYar to the external system (HesabYar is authoritative source, requiring `ExternalMutationApproval`).
- `BIDIRECTIONAL`: Two-way synchronization with strict conflict resolution and echo-suppression.

### 6. Configurable Source of Truth (SoT) per Tenant and Entity Type
Source of truth is explicitly configured per tenant and per entity type:
- E.g., Tenant A: Chart of Accounts = `EXTERNAL_SYSTEM`, Journal Vouchers = `HESABYAR_AI`, Customers/Suppliers = `EXTERNAL_SYSTEM`.
- Sync operations enforce SoT rules; an inbound change to an entity where HesabYar is SoT is rejected or flagged as a conflict.

### 7. The 18 Architectural Concerns for Integration Hub
The design comprehensively addresses 18 core concerns:
1. **Canonical vs. Vendor Model Abstraction:** Strict ACL shielding domain logic from third-party structures.
2. **Connector Port Contracts:** Formal lifecycle interfaces (`connect`, `disconnect`, `healthcheck`, `fetch_delta`, `push_batch`).
3. **Vendor Isolation:** Segregated directory structures (`src/hesabyar/infrastructure/integrations/<vendor>/`).
4. **Capability Negotiation:** Runtime feature discovery via `ConnectorCapability` flags.
5. **Connector Manifest:** Versioned vendor capabilities, supported protocol levels, and authentication requirements.
6. **Connection Management & Secret Isolation:** Tenant-isolated storage of credentials; secrets encrypted at rest; zero credentials exposed in logs or MCP tool arguments.
7. **Operational Modes Enforcement:** Strict runtime guards enforcing `READ_ONLY`, `IMPORT`, `EXPORT`, or `BIDIRECTIONAL` policies.
8. **Source of Truth (SoT) Matrix:** Declarative per-entity ownership preventing dual-master split-brain.
9. **Identity Mapping & FK Abstraction:** Persistent cross-system ID translation via `ExternalObjectMapping`.
10. **Bi-temporal & Delta Sync Tracking:** Cursor-based high-water mark synchronization with `SyncCheckpoint`.
11. **Conflict Resolution Policies:** Configurable strategies (`LOCAL_WINS`, `REMOTE_WINS`, `MANUAL_REVIEW`, `MERGE_IMMUTABLE`).
12. **Mutation Governance & Dual Control:** Mandatory preview, dry-run, and cryptographic approval tickets for all writes.
13. **Idempotency & Replay Resistance:** Deterministic mutation keys and deduplication tokens for external writes.
14. **Echo Suppression & Sync Loop Prevention:** Change-set origin tracking with cryptographic fingerprinting to discard self-originated updates.
15. **Schema & Semantic Translation:** Canonical normalization for currencies (IRR / Toman), dates (Jalali / Gregorian), and floating Tafsili levels.
16. **Transactional Outbox & Resilient Async Execution:** Integration events and outbound sync jobs dispatched through the transactional outbox with exponential backoff and dead-letter queues.
17. **Financial Reconciliation & Parity Auditing:** Automated `ReconciliationResult` jobs validating trial balances, debit/credit parity, and line-item integrity across systems.
18. **Observability, Metrics & Telemetry:** Prometheus metrics for sync throughput, latency, conflict rates, connector health, and structured audit logs.

## Consequences

### Positive
- **Vendor Independence:** Supporting a new accounting vendor (e.g., Parmis or Rayvarz) requires only implementing an adapter in `infrastructure/integrations/`; core domain code remains untouched.
- **Safety and Auditability:** The mandatory preview and `ExternalMutationApproval` workflow eliminates accidental writes to client production accounting databases.
- **Deterministic Reconciliation:** Continuous financial parity checking guarantees that drift between HesabYar and legacy desktop systems is detected immediately.
- **Robust Multi-Tenancy:** Each connection is strictly scoped to a single `tenant_id`, guaranteeing cross-tenant data leakage is impossible at the driver and repository levels.

### Negative / Trade-offs
- **Adapter Maintenance Overhead:** Vendor-specific adapters require continuous maintenance as vendors release new desktop updates or database patches.
- **Asynchronous Latency:** Governed approvals and outbox-driven synchronization introduce eventual consistency across systems compared to direct synchronous writes.
- **Conflict Management Complexity:** Bidirectional synchronization requires administrative oversight for edge-case conflicts flagged for `MANUAL_REVIEW`.
