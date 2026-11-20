/**
 * Typed API client for the HesabYar FastAPI backend — via the Next.js BFF.
 *
 * The browser NEVER talks to FastAPI (or Tryton/PostgreSQL) directly
 * (spec §19 rule 1, §4 line 252). Every call goes to a same-origin BFF
 * route (`/api/bff/...`) that attaches the operator API key server-side.
 *
 * All Phase 1+2 endpoints under /api/v1 are covered, plus the Phase 4
 * compliance endpoints (standards, tax policy, report validations,
 * Moadian — US-401..US-404), the Phase 5 review-queue endpoints
 * (US-504) and the Phase 5-2 endpoints (manual entries US-513,
 * bank import/reconciliation US-514, packaging US-515, cash box
 * US-516), plus the Phase 5-3 settlement endpoints (US-521–524:
 * gateway payments, tranches, exceptions, fee records, file imports,
 * events, deposits, tie-out) and the US-503 channel/contract
 * registry the settlement center reads, plus the Phase 5-5
 * installment endpoints (US-540–545: plans, receipts, contracts,
 * payoff quotes, early settlements, the overdue report, dunning,
 * plan amendments, SMS reminders) and the US-544 SMS provider
 * registry. Response types mirror the
 * backend's Pydantic schemas
 * (see docs/rest-api.md, docs/standards-registry.md,
 * docs/tax-policy.md, docs/report-validations.md, docs/moadian.md,
 * docs/review-queue.md, docs/event-platform.md, docs/cashbox.md,
 * docs/packaging.md, docs/settlement.md, docs/installments.md,
 * docs/sms-providers.md).
 *
 * Errors: the backend returns RFC 9457 problem-details
 * ({ type, title, status, detail, code, request_id }). Failures throw
 * `ApiError` with the stable `code` — pages map codes to Persian
 * messages, never render raw JSON.
 */

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(status: number, code: string, detail: string, requestId: string | null = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }

  /** True when the session is gone (BFF returns 401) — the page should send the user to /login. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

// ---------------------------------------------------------------------------
// Shared shapes
// ---------------------------------------------------------------------------

export interface Paginated<T> {
  items: T[];
  next_cursor: string | null;
}

export interface PaginationParams {
  limit?: number;
  cursor?: string;
  sort?: string;
  company?: string;
}

export interface ComponentStatus {
  name: string;
  status: string;
  detail: string | null;
}

export interface HealthResponse {
  status?: string;
  service?: string;
  version?: string;
  environment: string;
}

export interface ReadinessResponse {
  status: string;
  checks: ComponentStatus[];
}

export interface MigrationStatus {
  current_revision: string | null;
  head_revision: string | null;
  up_to_date?: boolean;
  status: string;
}

export interface TrytonModuleInfo {
  name: string;
  version: string | null;
}

export interface TrytonStatus {
  status: string;
  release: string | null;
  database: string;
  installed_modules?: TrytonModuleInfo[];
}

export interface InstallationResponse {
  service?: string;
  version?: string;
  environment: string;
  database: ComponentStatus;
  migration: MigrationStatus;
  tryton: TrytonStatus;
}

export interface CompanyResponse {
  id: string;
  engine_id: number;
  name: string;
  currency_code: string;
}

export interface AccountResponse {
  id: number;
  company_id: string;
  code: string;
  name: string;
  type: string | null;
  parent_id: number | null;
  closed: boolean;
  party_required: boolean;
  active: boolean;
  postable: boolean;
}

export interface FiscalYearResponse {
  id: number;
  company_id: string;
  name: string;
  start_date: string;
  end_date: string;
  start_date_jalali: string;
  end_date_jalali: string;
  state: string;
  is_open: boolean;
}

export interface FiscalPeriodResponse {
  id: number;
  company_id: string;
  fiscal_year_id: number;
  name: string;
  start_date: string;
  end_date: string;
  start_date_jalali: string;
  end_date_jalali: string;
  state: string;
  is_open: boolean;
  type: string;
}

export interface SetupStatusResponse {
  company_id: string;
  chart_installed: boolean;
  account_count: number;
  chart_template: string | null;
  fiscal_year_present: boolean;
  fiscal_year_count: number;
  open_fiscal_year: FiscalYearResponse | null;
  open_period_present: boolean;
  open_period_count: number;
  ready: boolean;
}

export interface JournalPeriodResponse {
  name: string;
  start_date: string;
  end_date: string;
  state: string;
}

export interface JournalLineResponse {
  account_id: number;
  debit: number;
  credit: number;
  description: string;
  state: string;
}

export interface JournalDraftResponse {
  id: number;
  company_id: string;
  engine_company_id: number;
  journal_id: number;
  period: JournalPeriodResponse;
  date: string;
  state: string;
  description: string;
  lines: JournalLineResponse[];
  total_debit: number;
  total_credit: number;
  currency?: string;
  unit?: string;
}

export interface PostedJournalResponse {
  id: number;
  company_id: string;
  engine_company_id: number;
  journal_id: number;
  period: JournalPeriodResponse;
  date: string;
  post_date: string | null;
  number: string;
  description: string;
  origin_id: number | null;
  lines: JournalLineResponse[];
  total_debit: number;
  total_credit: number;
  currency?: string;
  unit?: string;
}

export interface JournalDraftLineRequest {
  account_id: number;
  debit?: number;
  credit?: number;
  description?: string;
}

export interface CreateJournalDraftRequest {
  company_id: string;
  date: string;
  description?: string;
  journal_id?: number | null;
  lines: JournalDraftLineRequest[];
}

export interface UpdateJournalDraftRequest {
  date?: string | null;
  description?: string | null;
  lines?: JournalDraftLineRequest[] | null;
}

export interface ReverseJournalRequest {
  description?: string;
}

export interface ValidationIssueResponse {
  code: string;
  message: string;
  field: string | null;
}

export interface ValidationResponse {
  valid: boolean;
  issues?: ValidationIssueResponse[];
}

export interface InvoiceLineResponse {
  account_id: number;
  quantity: string;
  unit_price: number;
  amount: number;
  line_type: string;
  description: string;
}

export interface InvoiceResponse {
  id: number;
  company_id: string;
  engine_company_id: number;
  invoice_type: string;
  party_id: number;
  party_name: string;
  number: string | null;
  invoice_date: string | null;
  accounting_date: string | null;
  state: string;
  description: string;
  reference: string;
  journal_id: number | null;
  account_id: number;
  currency: string;
  unit?: string;
  untaxed_amount: number;
  tax_amount: number;
  total_amount: number;
  lines: InvoiceLineResponse[];
}

export interface InvoiceDraftLineRequest {
  account_id: number;
  unit_price: number;
  quantity?: number | string;
  description?: string;
}

export interface CreateInvoiceDraftRequest {
  company_id: string;
  invoice_type: string;
  party_id: number;
  date: string;
  description?: string;
  reference?: string;
  journal_id?: number | null;
  account_id?: number | null;
  invoice_address_id?: number | null;
  lines: InvoiceDraftLineRequest[];
}

export interface PaymentResponse {
  id: number;
  company_id: string;
  engine_company_id: number;
  number: string;
  kind: string;
  party_id: number;
  party_name: string;
  date: string;
  amount: number;
  state: string;
  reference: string;
  journal_name: string;
  currency: string;
  unit?: string;
}

export interface BankStatementLineResponse {
  id: number;
  date: string;
  amount: number;
  account_id: number;
  description: string;
  number: string | null;
  party_id: number | null;
  party_name: string | null;
  related_invoice_id: number | null;
}

export interface BankStatementResponse {
  id: number;
  company_id: string;
  engine_company_id: number;
  name: string;
  journal_name: string;
  journal_id: number;
  date: string;
  state: string;
  validation: string;
  currency: string;
  unit?: string;
  start_balance: number | null;
  end_balance: number | null;
  balance: number | null;
  total_amount: number | null;
  number_of_lines: number | null;
  lines: BankStatementLineResponse[];
}

export interface FileDetectionResponse {
  file_format: "xlsx" | "csv" | "pdf";
  encoding: string | null;
  columns: string[];
  sample_rows: (string | null)[][];
  suggested_mapping: Record<string, string>;
  missing_fields: string[];
  detection_notes: string[];
}

export interface ColumnMappingResponse {
  id: string;
  bank_key: string;
  name: string;
  file_format: string;
  columns: Record<string, string>;
  date_kind: string;
  currency_unit: string;
  header_row: number;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface BankImportRowResponse {
  id: string;
  row_number: number;
  row_date: string | null;
  amount_irr: number | null;
  description: string;
  bank_ref: string | null;
  balance_irr: number | null;
  is_duplicate: boolean;
  duplicate_reason: string | null;
  parse_errors: string[];
  was_rounded: boolean;
  suggested_account_id: number | null;
  suggested_party_id: number | null;
  suggested_invoice_id: number | null;
  suggestion_source: string | null;
}

export interface BankImportBatchResponse {
  id: string;
  company_id: string;
  bank_key: string;
  statement_journal_id: number;
  filename: string;
  file_format: string;
  currency_unit: string;
  status: string;
  total_rows: number;
  new_rows: number;
  duplicate_rows: number;
  invalid_rows: number;
  total_deposits_irr: number;
  total_withdrawals_irr: number;
  default_account_id: number;
  column_mapping_id: string | null;
  start_balance_irr: number | null;
  end_balance_irr: number | null;
  engine_statement_id: number | null;
  review_item_id: string | null;
  created_at: string | null;
  confirmed_at: string | null;
  cancelled_at: string | null;
  max_rows: number;
}

export interface BankImportBatchDetailResponse extends BankImportBatchResponse {
  rows: BankImportRowResponse[];
}

export interface StatementJournalResponse {
  id: number;
  company_id: string;
  name: string;
  account_id: number;
  validation: string;
  currency_code: string;
  bank_account_number: string | null;
}

export interface MatchSuggestionResponse {
  target_type: string;
  target_id: number;
  target_label: string;
  confidence: number;
  reason: string;
}

export interface ReconcileLineResultResponse {
  line_id: number;
  line_date: string;
  amount_irr: number;
  description: string;
  outcome: string;
  priority: number | null;
  confidence: number | null;
  target_type: string | null;
  target_id: number | string | null;
  target_label: string | null;
  suggestions: MatchSuggestionResponse[];
  review_item_id: string | null;
}

export interface ReconcileResponse {
  statement_id: number;
  company_id: string;
  total_lines: number;
  already_matched_count: number;
  matched_count: number;
  priority1_count: number;
  priority2_count: number;
  suggested_count: number;
  ambiguous_count: number;
  unmatched_count: number;
  matched_total_irr: number;
  unmatched_total_irr: number;
  review_items_filed: number;
  lines: ReconcileLineResultResponse[];
}

export interface BankStatementMatchResponse {
  id: string;
  statement_id: number;
  statement_line_id: number;
  line_date: string;
  line_amount_irr: number;
  line_description: string;
  target_type: string;
  target_id: number | null;
  target_ref: string;
  target_label: string;
  priority: number;
  confidence: number;
  status: string;
  outcome: string | null;
  destination_account_id: number | null;
  destination_shaba: string | null;
  review_item_id: string | null;
  matched_at: string | null;
  matched_by: string | null;
}

export interface MatchRuleResponse {
  id: string;
  bank_key: string;
  pattern: string;
  direction: string;
  target_account_id: number;
  target_party_id: number | null;
  target_kind: string;
  mode: string;
  confirmations: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface ReconciliationRowResponse {
  bank_key: string | null;
  journal_id: number;
  journal_name: string;
  destination_shaba: string | null;
  statements_count: number;
  lines_total: number;
  matched_lines: number;
  suggested_lines: number;
  ambiguous_lines: number;
  unmatched_lines: number;
  match_rate: number | null;
  unmatched_balance_irr: number;
  oldest_unmatched_age_days: number | null;
  oldest_unmatched_line_id: number | null;
}

export interface ReconciliationStatusResponse {
  company_id: string;
  date_from: string;
  date_to: string;
  generated_at: string;
  source: string;
  source_timestamp: string | null;
  report_version: string;
  calculation_version: string;
  freshness: string;
  rows: ReconciliationRowResponse[];
  statements_count: number;
  lines_total: number;
  matched_lines: number;
  suggested_lines: number;
  ambiguous_lines: number;
  unmatched_lines: number;
  match_rate: number | null;
  unmatched_balance_irr: number;
}

export interface StatementImportStatusResponse {
  company_id: string;
  engine_company_id: number;
  total_statements: number;
  draft_count: number;
  validated_count: number;
  posted_count: number;
  cancelled_count: number;
  latest_statement_id: number | null;
  latest_statement_date: string | null;
  total_lines: number;
  matched_lines: number;
  unmatched_lines: number;
}

export interface AuditEventResponse {
  id: string;
  tenant_id: string;
  installation_id: string;
  actor_id: string | null;
  api_client_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  company_external_id: string | null;
  occurred_at: string;
  request_id: string | null;
  source_system: string | null;
  authorization_result: string;
  outcome: string;
  error_code: string | null;
  tryton_reference: string | null;
  payload_hash: string | null;
  metadata_after: Record<string, unknown> | null;
}

export interface StandardRevisionResponse {
  revision_id: string;
  title: string;
  published_at: string | null;
  effective_from: string | null;
  effective_to: string | null;
  status: string;
  supersedes_revision_id: string | null;
  source_authority: string | null;
  source_url: string | null;
  snapshot_uri: string | null;
  sha256: string | null;
  verification_status: string;
  verified_by: string | null;
  verified_at: string | null;
  notes: string | null;
}

export interface StandardRuleResponse {
  rule_id: string;
  category: string;
  description: string;
  dsl_formula: string | null;
  rule_type: string;
  severity: string;
  standard_refs: string[];
  is_executable: boolean;
  ruleset_version: string | null;
  verification_status: string;
}

export interface StandardListItemResponse {
  standard_code: string;
  title: string;
  title_en: string | null;
  ifrs_equivalent: string | null;
  phase: number | null;
  slug: string;
  source_authority: string | null;
  source_url: string | null;
  latest_revision_id: string | null;
  latest_verification_status: string | null;
  latest_effective_from: string | null;
  latest_effective_to: string | null;
  rules_count: number;
}

export interface StandardDetailResponse {
  standard_code: string;
  title: string;
  title_en: string | null;
  ifrs_equivalent: string | null;
  phase: number | null;
  slug: string;
  source_authority: string | null;
  source_url: string | null;
  revisions: StandardRevisionResponse[];
  rules: StandardRuleResponse[];
}

export interface ValidationResultResponse {
  rule_id: string;
  standard_code: string;
  revision_id: string;
  outcome: string;
  severity: string;
  message: string;
  detail: string | null;
}

export interface NotExecutedRuleResponse {
  rule_id: string;
  standard_code: string;
  revision_id: string;
  reason: string;
}

export interface RunCoverageResponse {
  corpus_standards: number;
  standards_with_rules: number;
  rules_implemented: number;
  rules_verified: number;
  rules_executed: number;
}

export interface ValidationRunResponse {
  run_id: string;
  company_id: string | null;
  period_start: string | null;
  period_end: string | null;
  standard_codes: string[];
  ruleset_version: string;
  overall_outcome: string;
  is_valid: boolean;
  rules_executed: number;
  rules_passed: number;
  rules_failed: number;
  rules_not_applicable: number;
  rules_errored: number;
  coverage: RunCoverageResponse;
  results: ValidationResultResponse[];
  not_executed: NotExecutedRuleResponse[];
  created_at: string;
}

export interface TaxPolicyVersionResponse {
  id: string;
  source_key: string;
  rule_key: string;
  rule_name: string;
  version_id: string;
  parameter_value: string;
  value_unit: string;
  rule_year: number;
  performance_year: number | null;
  filing_year: number | null;
  effective_from: string | null;
  effective_to: string | null;
  status: string;
  verification_status: string;
  verified_by: string | null;
  verified_at: string | null;
  source_authority: string | null;
  source_url: string | null;
  snapshot_uri: string | null;
  sha256: string | null;
  corroboration_urls: string[];
  notes: string | null;
}

export interface TrialBalanceLineResponse {
  account_id: number;
  code: string;
  name: string;
  total_debit: number;
  total_credit: number;
  balance: number;
}

/** §11.1 freshness metadata, carried on every financial report (US-550). */
export interface ReportFreshnessResponse {
  source: string;
  source_timestamp: string;
  generated_at: string;
  company_id: string;
  period_start: string | null;
  period_end: string | null;
  report_version: string;
  calculation_version: string;
  freshness_indicator: string;
  reproducibility: Record<string, unknown>;
}

export interface TrialBalanceResponse {
  company_id: string;
  date_from: string;
  date_to: string;
  as_of_date: string;
  lines: TrialBalanceLineResponse[];
  total_debit: number;
  total_credit: number;
  balanced: boolean;
  move_count: number;
  line_count: number;
  source_endpoint: string;
  freshness: ReportFreshnessResponse;
}

export interface GeneralLedgerLineResponse {
  move_id: number;
  move_number: string;
  date: string;
  description: string;
  debit: number;
  credit: number;
  balance: number;
}

export interface GeneralLedgerAccountResponse {
  account_id: number;
  code: string;
  name: string;
  opening_balance: number;
  total_debit: number;
  total_credit: number;
  closing_balance: number;
  lines: GeneralLedgerLineResponse[];
}

export interface GeneralLedgerResponse {
  company_id: string;
  date_from: string;
  date_to: string;
  accounts: GeneralLedgerAccountResponse[];
  total_debit: number;
  total_credit: number;
  move_count: number;
  line_count: number;
  source_endpoint: string;
  freshness: ReportFreshnessResponse;
}

export interface StatementLineResponse {
  kind: "type" | "account";
  ref_id: number;
  label: string;
  code: string | null;
  depth: number;
  amount: number;
  amount_cmp: number | null;
}

export interface StatementSectionResponse {
  key: string;
  title: string;
  lines: StatementLineResponse[];
  total: number;
  total_cmp: number | null;
}

export interface BalanceSheetResponse {
  company_id: string;
  as_of: string;
  comparison_as_of: string | null;
  sections: StatementSectionResponse[];
  total_assets: number;
  total_liabilities: number;
  total_equity: number;
  total_assets_cmp: number | null;
  total_liabilities_cmp: number | null;
  total_equity_cmp: number | null;
  balanced: boolean;
  balanced_cmp: boolean | null;
  source_endpoint: string;
  freshness: ReportFreshnessResponse;
}

export interface MonthlyRevenueExpenseResponse {
  jalali_month: string;
  month_start: string;
  month_end: string;
  revenue: number;
  expense: number;
}

export interface IncomeStatementResponse {
  company_id: string;
  date_from: string;
  date_to: string;
  comparison_from: string | null;
  comparison_to: string | null;
  sections: StatementSectionResponse[];
  total_revenue: number;
  total_expense: number;
  net_income: number;
  total_revenue_cmp: number | null;
  total_expense_cmp: number | null;
  net_income_cmp: number | null;
  monthly: MonthlyRevenueExpenseResponse[];
  monthly_ties_out: boolean;
  source_endpoint: string;
  freshness: ReportFreshnessResponse;
}

export interface CashFlowSectionResponse {
  key: string;
  title: string;
  inflow: number;
  outflow: number;
  net: number;
  line_count: number;
}

export interface CashFlowResponse {
  company_id: string;
  date_from: string;
  date_to: string;
  opening_cash: number;
  closing_cash: number;
  sections: CashFlowSectionResponse[];
  net_change: number;
  ties_out: boolean;
  cash_accounts: string[];
  transfer_count: number;
  unclassified_count: number;
  unclassified_total: number;
  source_endpoint: string;
  freshness: ReportFreshnessResponse;
}

export interface NotesBalanceRowResponse {
  party_id: number | null;
  party_name: string;
  instrument: string;
  account_code: string;
  account_name: string;
  bucket_not_yet_due: number;
  bucket_0_30: number;
  bucket_30_60: number;
  bucket_60_90: number;
  bucket_90_120: number;
  bucket_120_plus: number;
  total: number;
  line_count: number;
}

export interface NotesBalanceResponse {
  company_id: string;
  as_of: string;
  instrument: string;
  rows: NotesBalanceRowResponse[];
  totals: NotesBalanceRowResponse;
  accounts_read: string[];
  source_endpoint: string;
  freshness: ReportFreshnessResponse;
}

/** One expected cash movement on one date (US-553, cash forecast). */
export interface ForecastItemResponse {
  date: string;
  direction: "inflow" | "outflow";
  bucket: "confirmed" | "forecast";
  source:
    | "settlement_tranche"
    | "installment_leg"
    | "cheque_received"
    | "cheque_issued"
    | "invoice_receivable"
    | "invoice_payable"
    | "packaging_purchase"
    | "expected_sale";
  amount_irr: number;
  ref: string;
  label: string;
}

export interface ForecastCalendarDayResponse {
  date: string;
  inflow_confirmed_irr: number;
  outflow_confirmed_irr: number;
  inflow_forecast_irr: number;
  outflow_forecast_irr: number;
  item_count: number;
}

export interface ForecastCalendarMonthResponse {
  month_jalali: string;
  days: ForecastCalendarDayResponse[];
}

export interface ForecastHorizonBucketResponse {
  key: "overdue" | "0_30" | "31_60" | "61_90" | "91_120" | "beyond_120";
  from_days: number | null;
  to_days: number | null;
  inflow_confirmed_irr: number;
  outflow_confirmed_irr: number;
  inflow_forecast_irr: number;
  outflow_forecast_irr: number;
  item_count: number;
}

export interface CashForecastTotalsResponse {
  inflow_confirmed_irr: number;
  outflow_confirmed_irr: number;
  inflow_forecast_irr: number;
  outflow_forecast_irr: number;
  item_count: number;
}

/** Cash forecast: monthly calendar + 30/60/90/120-day horizon (US-553). */
export interface CashForecastResponse {
  company_id: string;
  date_from: string;
  date_to: string;
  as_of: string;
  items: ForecastItemResponse[];
  months: ForecastCalendarMonthResponse[];
  horizon: ForecastHorizonBucketResponse[];
  totals: CashForecastTotalsResponse;
  source_counts: Record<string, number>;
  skipped_undated: number;
  freshness: ReportFreshnessResponse;
}

export interface ReportProvenanceResponse {
  snapshot_id: string;
  source_endpoint: string;
  as_of_date: string;
  payload_hash: string;
  ruleset_version: string;
  total_debit: number;
  total_credit: number;
  balanced: boolean;
}

export interface ReportValidationRunResponse {
  run: ValidationRunResponse;
  provenance: ReportProvenanceResponse;
  note: string | null;
}

export interface CreateReportValidationRunRequest {
  company_id: string;
  period_start: string;
  period_end: string;
  standard_codes: string[] | null;
}

export interface TaxSourceResponse {
  id: string;
  source_key: string;
  title: string;
  authority: string | null;
  source_url: string | null;
  snapshot_uri: string | null;
  sha256: string | null;
  notes: string | null;
  versions_count: number;
}

export interface TaxDecisionResponse {
  id: string;
  company_id: string | null;
  rule_key: string;
  decision_date: string;
  usage_context: string;
  policy_version_id: string | null;
  version_id: string;
  parameter_value: string;
  value_unit: string;
  verification_status: string;
  source_key: string;
  created_at: string;
}

export interface ResolveTaxDecisionRequest {
  company_id?: string | null;
  rule_key: string;
  decision_date: string;
  usage_context?: string;
}

export interface MoadianProfileResponse {
  profile_id: string;
  name: string;
  slug: string;
  environment: string;
  sender_mode: string;
  fiscal_id: string | null;
  economic_code: string | null;
  packet_type: string;
  api_base_url: string;
  protocol_version: string;
  source_authority: string | null;
  source_url: string | null;
  snapshot_uri: string | null;
  verification_status: string;
  key_reference: string | null;
  key_fingerprint: string | null;
  is_active: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateMoadianProfileRequest {
  name: string;
  slug: string;
  environment: string;
  sender_mode?: string;
  fiscal_id?: string | null;
  economic_code?: string | null;
  packet_type?: string;
  key_reference?: string | null;
  notes?: string | null;
}

export interface UpdateMoadianProfileRequest {
  name?: string | null;
  is_active?: boolean | null;
  fiscal_id?: string | null;
  economic_code?: string | null;
  key_reference?: string | null;
  notes?: string | null;
}

export interface MoadianDocumentResponse {
  document_id: string;
  profile_id: string;
  invoice_id: string | null;
  taxid: string | null;
  serial: string | null;
  invoice_type: number;
  invoice_pattern: number;
  invoice_subject: number;
  payload: Record<string, unknown>;
  payload_hash: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface CreateMoadianDocumentRequest {
  profile_id: string;
  invoice_id?: string | null;
  payload: Record<string, unknown>;
}

export interface MoadianSubmissionResponse {
  submission_id: string;
  document_id: string;
  profile_id: string;
  uid: string;
  reference_number: string | null;
  correlation_id: string;
  environment: string;
  scenario: string;
  status: string;
  retry: boolean;
  approved_by: string | null;
  approved_at: string | null;
  approval_note: string | null;
  request_hash: string | null;
  response_hash: string | null;
  request_payload: Record<string, unknown> | null;
  response_payload: Record<string, unknown> | null;
  outbox_event_id: string | null;
  last_error_code: string | null;
  last_error_detail: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateMoadianSubmissionRequest {
  document_id: string;
  scenario?: string;
}

export interface MoadianStatusEventResponse {
  event_id: string;
  submission_id: string;
  status: string;
  source: string;
  evidence: Record<string, unknown> | null;
  correlation_id: string | null;
  occurred_at: string;
  created_at: string;
}

export interface ReconciliationReportResponse {
  dispatched: number;
  submit_failed: number;
  skipped: number;
  polled: number;
  accepted: number;
  rejected: number;
  still_pending: number;
}

// -- review queue (US-504, Phase 5 F4) ----------------------------------------

export interface ReviewActionResponse {
  code: string;
  kind: string;
  resolution: string | null;
  requires_manager: boolean;
}

export interface ReviewAuditEntryResponse {
  id: string;
  actor: string;
  action: string;
  decision: string | null;
  note: string | null;
  policy_version: string | null;
  mapping_version: string | null;
  created_at: string | null;
}

export interface ReviewItemResponse {
  id: string;
  item_type: string;
  subtype: string | null;
  severity: string;
  status: string;
  channel: string | null;
  company_id: string | null;
  source_event_ref: string | null;
  reason: string;
  suggested_action: string;
  suggested_action_code: string;
  suggested_reason_key: string;
  amount_irr: number | null;
  assignee: string | null;
  resolution: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string | null;
  age_hours: number;
  sla_deadline: string;
  sla_breached: boolean;
  sla_warn: boolean;
  actions: ReviewActionResponse[];
  audit: ReviewAuditEntryResponse[];
}

/** The completed mapping a reviewer supplies with complete_mapping_retry (US-552). */
export interface MappingLearningRequest {
  mapping_kind: string;
  source_key: string;
  target_type: string;
  target_ref: string;
  channel_key?: string;
}

/** One mapping-center mapping version (US-503; confirmations + auto_eligible added US-552). */
export interface IntegrationMappingResponse {
  id: string;
  mapping_kind: string;
  channel_key: string;
  source_key: string;
  target_type: string;
  target_ref: string;
  version: number;
  status: string;
  suggestion_mode: string;
  source: string;
  confidence: number | null;
  notes: string | null;
  created_by: string | null;
  confirmations: number;
  auto_eligible: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface ReviewItemTypeResponse {
  item_type: string;
  actions: ReviewActionResponse[];
}

export interface ReviewTypeCounterResponse {
  item_type: string;
  open_count: number;
  in_review_count: number;
  resolved_count: number;
  overdue_count: number;
  oldest_open_age_hours: number | null;
  oldest_open_item_id: string | null;
}

export interface AdminNotificationResponse {
  item_id: string;
  item_type: string;
  severity: string;
  message: string;
  overdue_hours: number;
  created_at: string;
}

export interface ReviewQueueSummaryResponse {
  counters: ReviewTypeCounterResponse[];
  total_open: number;
  total_in_review: number;
  total_overdue: number;
  critical_overdue_count: number;
  oldest_open_age_hours: number | null;
  oldest_open_item_id: string | null;
  admin_notifications: AdminNotificationResponse[];
}

// ---------------------------------------------------------------------------
// Packaging (US-515) — docs/packaging.md
//
// Decimals arrive as JSON strings (Pydantic serializes Decimal as a
// string); money is whole IRR ints. Dates are Gregorian ISO; the UI
// renders them Jalali via lib/jalali.
// ---------------------------------------------------------------------------

export interface PackagingLocationResponse {
  engine_id: number;
  name: string;
  location_type: string;
  code: string | null;
}

export interface PackagingSettingsResponse {
  company_id: string;
  cost_method: string;
  shipment_cost_method: string;
  days_of_cover_window_days: number;
  low_stock_alert_days: number;
  packaging_location_id: number | null;
  production_location_id: number | null;
  warehouse_id: number | null;
  packaging_category_id: number | null;
  analytic_root_id: number | null;
  updated_at: string | null;
}

export interface PackagingMaterialResponse {
  id: string;
  company_id: string;
  tryton_product_id: number;
  tryton_template_id: number;
  name_fa: string;
  cost_method: string;
  code: string | null;
  uom_name: string | null;
  is_active: boolean;
  created_at: string | null;
}

export interface MaterialStockResponse {
  material_id: string;
  quantity: string;
  unit_cost_irr: string | null;
  location_engine_id: number;
  location_name: string;
  stock_value_irr: string | null;
  is_negative: boolean;
}

export interface ReorderPointResponse {
  material_id: string;
  warehouse_engine_id: number;
  min_quantity: string;
  target_quantity: string;
  tryton_order_point_id: number | null;
  max_quantity: string | null;
}

export interface DaysOfCoverResponse {
  material_id: string;
  quantity_on_hand: string;
  window_days: number;
  consumed_in_window: string;
  avg_daily_consumption: string;
  days_of_cover: number | null;
  last_consumption_date: string | null;
  is_out_of_stock: boolean;
}

export interface MaterialDetailResponse {
  material: PackagingMaterialResponse;
  stock: MaterialStockResponse | null;
  reorder_point: ReorderPointResponse | null;
  coverage: DaysOfCoverResponse | null;
}

export interface ConsumptionResponse {
  id: string;
  company_id: string;
  material_id: string;
  order_ref: string;
  shipment_ref: string;
  quantity: string;
  unit_cost_irr: string;
  total_cost_irr: number;
  rounding_delta_irr: number;
  idempotency_key: string;
  source: string;
  analytic_account_engine_id: number | null;
  tryton_move_id: number | null;
  consumed_on: string | null;
  created_at: string | null;
}

export interface ShipmentCostResponse {
  id: string;
  company_id: string;
  order_ref: string;
  method: string;
  invoiced_irr: number;
  actual_irr: number;
  absorbed_irr: number;
  carrier_name: string | null;
  tryton_sale_id: number | null;
  tryton_shipment_id: number | null;
  analytic_account_engine_id: number | null;
  created_at: string | null;
}

export interface CarrierRateResponse {
  id: string;
  company_id: string;
  carrier_name: string;
  rate_irr: number;
  basis: string;
  tryton_carrier_id: number | null;
  effective_from: string | null;
  is_active: boolean;
  created_at: string | null;
}

export interface OrderCostCenterResponse {
  id: string;
  company_id: string;
  order_ref: string;
  analytic_account_engine_id: number;
  created_at: string | null;
}

export interface OrderCostReportResponse {
  order_ref: string;
  analytic_account_engine_id: number | null;
  goods_cost_irr: number;
  packaging_cost_irr: number;
  shipment_invoiced_irr: number;
  shipment_actual_irr: number;
  shipment_absorbed_irr: number;
  revenue_irr: number;
  total_cost_irr: number;
  margin_irr: number;
  analytic_line_count: number;
  consumption_count: number;
}

export interface PriceQuoteLineResponse {
  material_id: string;
  material_name: string;
  quantity: string;
  unit_cost_irr: string;
  line_cost_irr: number;
  stale_price: boolean;
}

export interface PriceQuoteResponse {
  goods_cost_irr: number;
  packaging_cost_irr: number;
  shipment_cost_irr: number;
  margin_irr: number;
  total_irr: number;
  approximate: boolean;
  lines: PriceQuoteLineResponse[];
  warnings: string[];
  margin_percent: string | null;
  carrier_name: string | null;
}

export interface CountLineResponse {
  material_id: string;
  material_name: string;
  expected_quantity: string;
  counted_quantity: string | null;
  variance: string | null;
  tryton_line_id: number | null;
}

export interface InventoryCountResponse {
  id: string;
  company_id: string;
  tryton_inventory_id: number;
  location_engine_id: number;
  location_name: string;
  count_date: string;
  state: string;
  counted_by: string | null;
  lines: CountLineResponse[];
  created_at: string | null;
}

export interface SupplyCheckReportResponse {
  company_id: string;
  checked_at: string;
  materials_checked: number;
  purchase_requests_seen: number;
  purchase_requests_new: number;
  items_filed: number;
  items_skipped_open: number;
  low_stock_materials: string[];
  errors: string[];
}

export interface SlaPolicyResponse {
  item_type: string;
  sla_hours: number;
  warn_hours: number;
  updated_at: string | null;
}

export interface CreateReviewItemRequest {
  item_type: string;
  severity: string;
  reason: string;
  suggested_action: string;
  company_id?: string | null;
  subtype?: string | null;
  channel?: string | null;
  source_event_ref?: string | null;
  amount_irr?: number | null;
  assignee?: string | null;
}

// ---------------------------------------------------------------------------
// Manual entries (US-513, dual entry) — money is whole Toman (int)
// ---------------------------------------------------------------------------

export interface ManualOrderLineRequest {
  description: string;
  /** Exact quantity: a JSON number, or a decimal string for fractions ("1.5"). */
  quantity: number | string;
  unit_price_toman: number;
  discount_toman?: number;
}

export interface CreateManualOrderRequest {
  order_number: string;
  store_id?: string;
  customer_name?: string | null;
  customer_ref?: string | null;
  lines: ManualOrderLineRequest[];
  discount_toman?: number;
  shipping_toman?: number;
  tax_toman?: number;
  occurred_at?: string | null;
  note?: string | null;
}

export interface ManualRefundLineRequest {
  description: string;
  line_total_toman: number;
}

export interface CreateManualRefundRequest {
  order_number: string;
  refund_amount_toman: number;
  refund_reference?: string | null;
  refund_payment?: boolean;
  lines?: ManualRefundLineRequest[];
  reason?: string | null;
  reason_code?: string | null;
  store_id?: string;
  occurred_at?: string | null;
  note?: string | null;
}

export interface CreateManualCancellationRequest {
  order_number: string;
  total_amount_toman?: number | null;
  reason?: string | null;
  store_id?: string;
  occurred_at?: string | null;
  note?: string | null;
}

export interface ManualEntryEventResponse {
  event_type: string;
  inbox_event_id: string;
  source_event_id: string;
  external_entity_type: string;
  external_entity_id: string;
  status: string | null;
  last_error: string | null;
  processed_at: string | null;
  engine_record_id: string | null;
  decision: string | null;
}

export interface ManualEntryResponse {
  entry_id: string;
  entry_type: string;
  source_store_id: string;
  order_number: string;
  total_amount_toman: number;
  entered_by: string;
  entered_by_name: string | null;
  events: ManualEntryEventResponse[];
  payload: Record<string, unknown>;
  note: string | null;
  created_at: string;
}

// Cash box (US-516)
// ---------------------------------------------------------------------------

export interface CashBoxResponse {
  id: string;
  company_id: string;
  name_fa: string;
  kind: string;
  statement_journal_id: number;
  cash_account_id: number;
  cash_account_code: string;
  ceiling_irr: number | null;
  active: boolean;
  book_balance_irr: number | null;
  last_count_at: string | null;
  days_since_last_count: number | null;
  uncounted_alert: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateCashBoxRequest {
  company_id: string;
  name_fa: string;
  kind: string;
  statement_journal_id: number;
  ceiling_irr?: number | null;
}

export interface UpdateCashBoxRequest {
  name_fa: string;
  kind: string;
  ceiling_irr?: number | null;
  active: boolean;
}

export interface CashBoxStatementJournalResponse {
  id: number;
  name: string;
  account_id: number;
  account_code: string;
  account_name: string;
  currency_code: string;
  validation: string;
  active: boolean;
}

export interface DenominationResponse {
  denomination_irr: number;
  count: number;
  total_irr: number;
}

export interface CountSessionResponse {
  id: string;
  company_id: string;
  cash_box_id: string;
  cashier: string;
  supervisor: string;
  status: string;
  book_balance_irr: number;
  counted_amount_irr: number | null;
  variance_irr: number | null;
  denominations: DenominationResponse[];
  minutes_text: string | null;
  minutes_required: boolean;
  minutes_signed: boolean;
  witnessed_by: string | null;
  witnessed_at: string | null;
  minutes_cashier_signed_by: string | null;
  minutes_cashier_signed_at: string | null;
  minutes_supervisor_signed_by: string | null;
  minutes_supervisor_signed_at: string | null;
  minutes_manager_signed_by: string | null;
  minutes_manager_signed_at: string | null;
  review_item_id: string | null;
  draft_journal_id: number | null;
  posted_journal_id: number | null;
  notes: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface CashBoxSettingsResponse {
  variance_policy: string;
  material_variance_threshold_irr: number | null;
  count_reminder_days: number;
  count_schedule: string;
  supervisor_required: boolean;
  shortage_settlement_hours: number;
  updated_at: string | null;
}

export interface UpdateCashBoxSettingsRequest {
  variance_policy: string;
  material_variance_threshold_irr?: number | null;
  count_reminder_days?: number;
  count_schedule?: string;
  supervisor_required?: boolean;
  shortage_settlement_hours?: number;
}

export interface CashBoxReportLineResponse {
  cash_box_id: string;
  cash_box_name_fa: string;
  cash_account_code: string;
  opening_book_balance_irr: number;
  closing_book_balance_irr: number;
  receipts_irr: number;
  payments_irr: number;
  session_count: number;
  shortage_irr: number;
  overage_irr: number;
  net_variance_irr: number;
  recorded_sales_irr: number;
  possible_unrecorded_sales_irr: number;
}

export interface CashBoxReportResponse {
  company_id: string;
  date_from: string;
  date_to: string;
  lines: CashBoxReportLineResponse[];
  total_shortage_irr: number;
  total_overage_irr: number;
  total_possible_unrecorded_sales_irr: number;
  generated_at: string;
  source: string;
  sales_source: string;
  report_version: string;
  calculation_version: string;
}

// Settlement center (US-525, Phase 5-3 Wave C)
// ---------------------------------------------------------------------------
// Mirrors src/hesabyar/interfaces/rest/schemas/settlement.py +
// schemas/integrations.py (channels/contracts). All amounts are integer
// IRR; fee percents are decimal strings per the §9.3 contract (never a
// binary float — "10" means 10%).

export interface PaymentChannelResponse {
  id: string;
  channel_key: string;
  display_name_fa: string;
  channel_type: string;
  is_builtin: boolean;
  enabled: boolean;
  status: string;
  merchant_ref: string | null;
  settlement_iban: string | null;
  environment: string;
  notes: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface UpdatePaymentChannelRequest {
  display_name_fa?: string | null;
  channel_type?: string | null;
  enabled?: boolean | null;
  merchant_ref?: string | null;
  settlement_iban?: string | null;
  environment?: string | null;
  notes?: string | null;
}

export interface TrancheLegSchema {
  month_offset: number;
  payout_day: number;
  /** Decimal string ("25" means 25%). Legs must sum to 100. */
  percent: string;
}

export interface GatewayContractResponse {
  id: string;
  channel_key: string;
  version: number;
  valid_from: string;
  valid_to: string | null;
  /** Decimal string, user-entered — never hard-coded ("10" means 10%). */
  fee_percent: string;
  fee_vat_rate: string | null;
  fee_vat_claimable: boolean;
  fee_timing: string;
  fee_accrual_policy: string;
  settlement_model: string;
  tranche_legs: TrancheLegSchema[];
  payout_window_start_day: number | null;
  payout_window_end_day: number | null;
  payout_calendar: string;
  working_day_adjustment: boolean;
  day_lag: number | null;
  grace_days_after_due: number;
  match_tolerance_irr: number;
  tieout_tolerance_irr: number;
  auto_post_policy: string;
  auto_post_max_amount_irr: number | null;
  source_note: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateGatewayContractRequest {
  valid_from: string;
  fee_percent: string;
  fee_vat_rate?: string | null;
  fee_vat_claimable?: boolean;
  fee_timing?: string;
  fee_accrual_policy?: string;
  settlement_model?: string;
  tranche_legs: TrancheLegSchema[];
  payout_window_start_day?: number | null;
  payout_window_end_day?: number | null;
  payout_calendar?: string;
  working_day_adjustment?: boolean;
  day_lag?: number | null;
  grace_days_after_due?: number;
  match_tolerance_irr?: number;
  tieout_tolerance_irr?: number;
  auto_post_policy?: string;
  auto_post_max_amount_irr?: number | null;
  source_note?: string | null;
}

export interface GatewayPaymentResponse {
  id: string;
  payment_channel_id: string;
  gateway_transaction_id: string;
  store_order_id: string | null;
  store_payment_id: string | null;
  tryton_invoice_id: number | null;
  tryton_journal_id: number | null;
  customer_party_id: number | null;
  gross_irr: number;
  fee_irr: number;
  fee_vat_irr: number;
  net_irr: number;
  contract_version_id: string;
  fee_rate_snapshot: string;
  fee_vat_rate_snapshot: string | null;
  sales_period_jalali: string;
  gateway_confirmed_at: string;
  source: string;
  status: string;
  refunds_deducted_irr: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateGatewayPaymentRequest {
  channel_key: string;
  gateway_transaction_id: string;
  gross_irr: number;
  gateway_confirmed_at: string;
  source: string;
  store_order_id?: string | null;
  store_payment_id?: string | null;
  tryton_invoice_id?: number | null;
  tryton_journal_id?: number | null;
  customer_party_id?: number | null;
  canonical_event_id?: string | null;
  fee_vat_rate_override?: string | null;
}

export interface RecordPaymentResponse {
  payment: GatewayPaymentResponse;
  tranches: SettlementTrancheResponse[];
  fee_record_id: string;
}

export interface PaymentScheduleResponse {
  payment: GatewayPaymentResponse;
  tranches: SettlementTrancheResponse[];
}

export interface CancelGatewayPaymentResponse {
  payment: GatewayPaymentResponse;
  cancelled_tranche_ids: string[];
  refunds_deducted_irr: number;
  exception_id: string | null;
}

export interface ReassignSalesPeriodResponse {
  payment: GatewayPaymentResponse;
  rederived_tranche_ids: string[];
  untouched_tranche_ids: string[];
  exception_id: string | null;
}

export interface BulkRederiveResponse {
  rederived_payment_ids: string[];
  skipped: { payment_id: string; reason: string }[];
}

export interface SettlementTrancheResponse {
  id: string;
  gateway_payment_id: string;
  tranche_index: number;
  tranche_count: number;
  sales_period_jalali: string;
  expected_window_start: string;
  expected_date: string;
  gross_irr: number;
  fee_irr: number;
  fee_vat_irr: number;
  net_irr: number;
  confirmed_net_irr: number;
  received_net_irr: number;
  outstanding_net_irr: number;
  state: string;
  tryton_receivable_line_id: number | null;
  variance_irr: number | null;
  variance_reason: string | null;
  match_method: string | null;
  match_confidence: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SettlementExceptionResponse {
  id: string;
  exception_type: string;
  severity: string;
  status: string;
  tranche_id: string | null;
  settlement_event_id: string | null;
  bank_deposit_id: string | null;
  gateway_payment_id: string | null;
  expected_amount_irr: number | null;
  actual_amount_irr: number | null;
  /** Overdue escalation level (0=none, 1=7d, 2=30d, 3=60d). */
  escalation_level: number;
  resolution: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  note: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SettlementFeeRecordResponse {
  id: string;
  gateway_payment_id: string;
  tranche_id: string | null;
  contract_version_id: string;
  fee_percent_applied: string;
  fee_vat_rate_applied: string | null;
  expected_fee_irr: number;
  actual_fee_irr: number | null;
  fee_variance_irr: number | null;
  expected_vat_irr: number;
  actual_vat_irr: number | null;
  vat_variance_irr: number | null;
  tryton_fee_journal_id: number | null;
  source: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface TickResponse {
  moved_to_due: number;
  moved_to_overdue: number;
  exceptions_created: number;
  exceptions_escalated: number;
}

export interface SettlementPostingResponse {
  id: string;
  gateway_payment_id: string;
  tranche_id: string | null;
  kind: string;
  status: string;
  amount_irr: number;
  tryton_move_id: number | null;
  tryton_payment_id: number | null;
  tryton_reconciliation_id: number | null;
  reason: string | null;
  event_ref: string | null;
  details: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SettlementFileRowResponse {
  id: string;
  row_no: number;
  raw: Record<string, unknown>;
  gateway_transaction_id: string | null;
  tran_type: string | null;
  amount_irr: number | null;
  fee_irr: number | null;
  net_irr: number | null;
  transaction_date: string | null;
  tracking_ref: string | null;
  matched_tranche_id: string | null;
  match_status: string;
  note: string | null;
  created_at: string | null;
}

export interface SettlementFileImportResponse {
  id: string;
  payment_channel_id: string;
  file_type: string;
  period_jalali: string;
  file_sha256: string;
  import_batch_id: string;
  status: string;
  uploaded_by: string | null;
  uploaded_at: string | null;
  row_count: number;
  matched_count: number;
  new_count: number;
  error_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface SettlementFileImportDetailResponse {
  file_import: SettlementFileImportResponse;
  rows: SettlementFileRowResponse[];
}

export interface ImportSettlementFileRequest {
  filename: string;
  content_base64: string;
  channel_key: string;
  file_type: string;
  period_jalali: string;
  currency_unit?: string;
  columns?: Record<string, string> | null;
  date_kind?: string;
  header_row?: number;
  actor?: string | null;
}

export interface ImportPreviewResponse {
  gross_irr: number;
  fee_irr: number;
  vat_irr: number;
  net_irr: number;
  refunds_irr: number;
  deductions_irr: number;
  expected_deposit_irr: number;
  receivable_before_irr: number;
  receivable_after_irr: number;
  matched_rows: number;
  new_payment_rows: number;
  unknown_rows: number;
  duplicate_rows: number;
  invalid_rows: number;
  dropped_rows: number;
}

export interface DroppedRowResponse {
  row_number: number;
  reason: string;
}

export interface ImportSettlementFileResponse {
  file_import: SettlementFileImportResponse;
  preview: ImportPreviewResponse;
  review_item_id: string | null;
  is_duplicate: boolean;
  dropped_rows: DroppedRowResponse[];
  rows: SettlementFileRowResponse[];
}

export interface ApproveImportResponse {
  import_batch_id: string;
  settlement_event_id: string;
  canonical_event_id: string;
  confirmed_tranche_ids: string[];
  created_payment_ids: string[];
  exception_ids: string[];
  review_item_ids: string[];
  net_irr: number;
  deductions_irr: number;
}

export interface SettlementEventResponse {
  id: string;
  payment_channel_id: string;
  payout_date: string;
  net_irr: number;
  source: string;
  deductions_irr: number;
  tracking_ref: string | null;
  destination_iban: string | null;
  gateway_batch_ref: string | null;
  import_batch_id: string | null;
  canonical_event_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface BankDepositResponse {
  id: string;
  deposit_date: string;
  amount_irr: number;
  bank_account_ref: string | null;
  description: string | null;
  tracking_ref: string | null;
  bank_import_row_id: string | null;
  source: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface TieOutResultResponse {
  holds: boolean;
  confirmed_total_irr: number;
  deductions_irr: number;
  expected_deposits_irr: number;
  matched_deposits_irr: number;
  difference_irr: number;
  tolerance_irr: number;
}

// -- unified manual-import batches (US-554) -----------------------------------

export interface ImportBatchStageResponse {
  stage: string;
  at: string;
  note: string | null;
}

export interface ImportBatchPreviewResponse {
  gross_irr: number;
  fee_irr: number;
  tax_irr: number;
  refunds_irr: number;
  net_irr: number;
  deductions_irr: number;
  receivable_before_irr: number | null;
  receivable_after_irr: number | null;
  expected_deposit_irr: number;
  matched_rows: number;
  new_rows: number;
  duplicate_rows: number;
  invalid_rows: number;
  dropped_rows: number;
}

export interface ManualImportBatchResponse {
  id: string;
  kind: "statement" | "settlement";
  source_key: string;
  company_id: string;
  filename: string;
  file_format: string;
  currency_unit: string;
  status: string;
  attempt: number;
  source_import_id: string | null;
  source_import_batch_id: string | null;
  column_mapping_id: string | null;
  mapping_columns: Record<string, string>;
  file_type: string | null;
  period_jalali: string | null;
  preview: ImportBatchPreviewResponse | null;
  review_item_id: string | null;
  rejection_reason: string | null;
  auto_post_opt_in: boolean;
  posting_status: string | null;
  posting_detail: string | null;
  engine_ref_id: number | null;
  stages: ImportBatchStageResponse[];
  created_at: string | null;
  updated_at: string | null;
  approved_at: string | null;
  posted_at: string | null;
  rejected_at: string | null;
  archived_at: string | null;
}

export interface UploadImportBatchResponse {
  batch: ManualImportBatchResponse;
  preview: ImportBatchPreviewResponse;
  duplicate: boolean;
}

export interface ImportBatchUploadRequest {
  kind: "statement" | "settlement";
  source_key: string;
  company: string;
  filename: string;
  content_base64: string;
  currency_unit?: string;
  date_kind?: string;
  header_row?: number;
  mapping_id?: string | null;
  columns?: Record<string, string> | null;
  save_mapping?: boolean;
  mapping_name?: string | null;
  statement_journal_id?: number | null;
  default_account_id?: number | null;
  file_type?: string;
  period_jalali?: string | null;
}

export interface DetectImportFileRequest {
  kind: "statement" | "settlement";
  filename: string;
  content_base64: string;
  file_type?: string;
}

export interface ApproveImportBatchResponse {
  batch: ManualImportBatchResponse;
  posted: boolean;
  tryton_journal_id: number | null;
  settlement_event_id: string | null;
  tieout: TieOutResultResponse | null;
  gate_reason: string | null;
  confirmed_tranche_ids: string[];
  created_payment_ids: string[];
}

export interface RetryBatchPostingResponse {
  batch: ManualImportBatchResponse;
  posted: boolean;
  tryton_journal_id: number | null;
  tieout: TieOutResultResponse | null;
  gate_reason: string | null;
  already_settled: boolean;
}

export interface BatchVarianceReportResponse {
  expected_net_irr: number;
  imported_net_irr: number;
  posted_net_irr: number;
  difference_irr: number;
  tieout_holds: boolean | null;
  tieout_difference_irr: number | null;
  tieout_tolerance_irr: number | null;
  gate_allowed: boolean | null;
  gate_reason: string | null;
  posting_status: string | null;
  generated_at: string;
}

export interface SettlementColumnMappingResponse {
  id: string;
  channel_key: string;
  name: string;
  file_format: string;
  file_type: string;
  columns: Record<string, string>;
  date_kind: string;
  currency_unit: string;
  header_row: number;
  company_id: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface SettlementColumnMappingCreateRequest {
  channel_key: string;
  name: string;
  file_format: string;
  file_type: string;
  columns: Record<string, string>;
  date_kind?: string;
  currency_unit?: string;
  header_row?: number;
  company?: string | null;
}

export interface RecordDepositRequest {
  amount_irr: number;
  deposit_date: string;
  bank_account_ref?: string | null;
  description?: string | null;
  tracking_ref?: string | null;
  bank_import_row_id?: string | null;
  source?: string;
  channel_key?: string | null;
  company_id?: string | null;
  actor?: string | null;
}

export interface DepositMatchSuggestionResponse {
  target_kind: string;
  target_id: string;
  confidence: string;
  reason: string;
}

export interface RecordDepositResponse {
  deposit: BankDepositResponse;
  decision_method: string;
  decision_confidence: string;
  matched_tranche_ids: string[];
  suggestions: DepositMatchSuggestionResponse[];
  exception_ids: string[];
  tieout: TieOutResultResponse | null;
  posted_journal_id: number | null;
}

export interface SettlementMatchResponse {
  id: string;
  tranche_id: string;
  allocated_amount_irr: number;
  method: string;
  bank_deposit_id: string | null;
  settlement_event_id: string | null;
  confidence: string | null;
  matched_by: string | null;
  is_automatic: boolean;
  matched_at: string | null;
  created_at: string | null;
}

export interface RunTieOutRequest {
  company_id?: string | null;
  actor?: string | null;
}

export interface TieOutRunResponse {
  import_batch_id: string;
  status: string;
  tieout: TieOutResultResponse;
  gate_allowed: boolean | null;
  gate_reason: string | null;
  posted: boolean;
  tryton_journal_id: number | null;
  settled_tranche_ids: string[];
  exception_ids: string[];
  review_item_ids: string[];
}

// ---------------------------------------------------------------------------
// Cheques (US-535, Phase 5-4) — docs/cheques.md
//
// Mirrors src/hesabyar/interfaces/rest/schemas/cheques.py. Dates travel
// as Jalali "YYYY-MM-DD" strings; amounts in responses are integer IRR
// with amount_toman alongside for display. The create body carries the
// same 24 fields as the site payload, amounts in Toman.
// ---------------------------------------------------------------------------

export interface ChequeCustodyEntryResponse {
  id: string;
  from_location: string | null;
  to_location: string;
  actor: string;
  reason: string;
  at: string;
}

export interface InquiryReceiptResponse {
  id: string;
  channel: string;
  inquiry_type: string;
  result_code: string;
  result_summary: string;
  reference: string;
  recorded_by: string;
  cost_irr: number;
  requested_at: string | null;
  responded_at: string | null;
}

export interface ChequeResponse {
  id: string;
  state: string;
  kind: string;
  purpose: string;
  sayad_id: string;
  sayad_status: string;
  sayad_unconfirmed: boolean;
  amount_irr: number;
  amount_toman: number;
  issue_date: string;
  due_date: string;
  issuer_name: string;
  issuer_national_id: string;
  issuer_bank: string;
  payee_name: string;
  payee_national_id: string;
  order_id: string;
  customer_ref: string;
  leaf_number: string | null;
  issuer_branch: string | null;
  issuer_account_number: string | null;
  reason: string | null;
  invoice_id: string | null;
  installment_leg_id: string | null;
  installment_leg_status: string | null;
  scan_url: string | null;
  scan_ref: string | null;
  scan_filestore_ref: string | null;
  source_event_id: string | null;
  age_days: number;
  storage_location: string | null;
  engine_id: number | null;
  move_ids: Record<string, number>;
  destination_bank_account_id: number | null;
  destination_bank_name: string | null;
  deposit_date: string | null;
  clearing_confidence: string | null;
  clearing_statement_ref: string | null;
  clearing_reconciliation_id: number | null;
  issuer_color_code: number | null;
  issuer_back_cheques: number | null;
  bounce_reason: string | null;
  bounce_date: string | null;
  bounce_certificate_reference: string | null;
  endorsee_name: string | null;
  endorsee_type: string | null;
  sayad_transfer_reference: string | null;
  checkbook_engine_id: number | null;
  secured_obligation: string | null;
  void_reason: string | null;
  call_in_approved_by: string | null;
  call_in_approved_at: string | null;
  unexpected_call_in: boolean;
  call_in_statement_reference: string | null;
  journal_pending_review: boolean;
  receipt_missing_warning: boolean;
  mismatch_detail: string | null;
  warnings: string[];
  created_at: string;
  updated_at: string;
}

export interface ChequeDetailResponse extends ChequeResponse {
  custody_entries: ChequeCustodyEntryResponse[];
  inquiry_receipts: InquiryReceiptResponse[];
}

export interface ChequeSettingsResponse {
  installation_id: string;
  leaf_arrival_deadline_days: number;
  clearing_window_days: number | null;
  bounce_destination: string;
  red_brown_policy: string;
  max_in_transit_commitments_irr: number | null;
  default_storage_location: string;
}

export interface CheckbookResponse {
  checkbook_engine_id: number;
  name: string;
  bank_account_id: number | null;
  total_leaves: number;
  used_leaves: number;
  voided_leaves: number;
  remaining_leaves: number;
  leaves_registered_in_sayad: number | null;
  inquiry_simulated: boolean;
  inquiry_notes: string[];
}

export interface CollateralSummaryResponse {
  cheques: ChequeResponse[];
  received_total_irr: number;
  issued_total_irr: number;
  by_obligation: Record<string, ChequeResponse[]>;
}

export interface EndorsementSummaryResponse {
  supplier: ChequeResponse[];
  other: ChequeResponse[];
  supplier_total_irr: number;
  other_total_irr: number;
}

export interface ClearingAgingResponse {
  cheque_id: string;
  sayad_id: string;
  days_in_clearing: number;
  stale: boolean;
  window_days: number | null;
  window_unset: boolean;
}

export interface InquiryLedgerResponse {
  total_receipts: number;
  by_channel: Record<string, number>;
  color_distribution: Record<string, number>;
  total_cost_irr: number;
}

/** One stored month of clearing-match history (US-553, report 12). */
export interface MonthlyMatchTrendMonthResponse {
  month_jalali: string;
  exact_count: number;
  probable_count: number;
  ambiguous_count: number;
  unmatched_count: number;
  total_lines: number;
  match_rate: string;
}

/** Cheque report 12: monthly match-trend from stored history (US-553). */
export interface MonthlyMatchTrendResponse {
  months: MonthlyMatchTrendMonthResponse[];
  total_exact: number;
  total_probable: number;
  total_ambiguous: number;
  total_unmatched: number;
  total_lines: number;
  overall_match_rate: string;
  freshness: ReportFreshnessResponse;
}

export interface ReceiptRuleAuditResponse {
  checked: number;
  missing: number;
  items_filed: number;
  cheque_ids: string[];
}

export interface ClearingLineDecisionResponse {
  line_id: number;
  confidence: string;
  cheque_id: string | null;
  sayad_id: string | null;
  action: string;
  reconciliation_id: number | null;
  review_item_id: string | null;
  reasons: string[];
}

export interface ChequeClearingResponse {
  statement_id: number;
  total_lines: number;
  exact_count: number;
  probable_count: number;
  ambiguous_count: number;
  unmatched_count: number;
  cleared_count: number;
  review_items_filed: number;
  lines: ClearingLineDecisionResponse[];
}

export interface CreateChequeRequest {
  company_id: string;
  sayad_id: string;
  amount_toman: number;
  amount_currency?: string;
  issue_date: string;
  due_date: string;
  issuer_name: string;
  issuer_national_id: string;
  issuer_bank: string;
  payee_name: string;
  payee_national_id: string;
  purpose?: string;
  kind?: string;
  checkbook_id?: number | null;
  secured_obligation?: string | null;
  order_id: string;
  customer_ref: string;
  sayad_registered: boolean;
  leaf_number?: string | null;
  issuer_branch?: string | null;
  issuer_account_number?: string | null;
  reason?: string | null;
  invoice_id?: string | null;
  installment_leg_id?: string | null;
  scan_url?: string | null;
  scan_ref?: string | null;
  registered_by?: string | null;
  notes?: string | null;
}

export interface ConfirmReceiptRequest {
  items_matched: boolean;
  sayad_accepted: boolean;
  color_code?: number | null;
  back_cheques_count?: number | null;
  in_transit_commitments_irr?: number | null;
  manager_approval?: string | null;
  storage_location?: string | null;
}

export interface InquiryReceiptRequest {
  channel: string;
  inquiry_type: string;
  result_code: string;
  result_summary: string;
  reference: string;
  cost_irr?: number;
  requested_at?: string | null;
  responded_at?: string | null;
}

// ---------------------------------------------------------------------------
// Installments (US-540–545, Phase 5-5 ۵ج) + SMS reminders (US-544)
//
// Response types mirror the backend's Pydantic schemas
// (src/hesabyar/interfaces/rest/schemas/installments.py and
// schemas/sms.py). Amounts in requests are Toman; responses carry
// Rial (*_irr) with Toman alongside. Dates are Jalali YYYY-MM-DD.
// Leg statuses are computed at read time, never stored:
// expected | due | partially_paid | paid | overdue |
// pending_clearance (internal plans) and expected |
// gateway_confirmed | settled_per_tranche | paid (BNPL plans).
// ---------------------------------------------------------------------------

export interface InstallmentLegAllocationResponse {
  leg_id: string;
  amount_irr: number;
  amount_toman: number;
}

/** The cheque linked to a leg (US-542) — sayad_id, due date, state. */
export interface LinkedChequeResponse {
  cheque_id: string;
  sayad_id: string;
  due_date: string;
  state: string;
  amount_irr: number;
  amount_toman: number;
}

export interface InstallmentLegResponse {
  id: string;
  plan_id: string;
  sequence: number;
  due_date: string;
  principal_irr: number;
  principal_toman: number;
  received_irr: number;
  received_toman: number;
  balance_irr: number;
  balance_toman: number;
  status: string;
  pending_clearance_irr: number;
  pending_clearance_toman: number;
  linked_cheque: LinkedChequeResponse | null;
}

export interface InstallmentReceiptResponse {
  id: string;
  plan_id: string;
  invoice_id: string;
  journal_move_id: number;
  amount_irr: number;
  amount_toman: number;
  received_date: string;
  payment_method: string;
  cheque_id: string | null;
  collected_at: string | null;
  allocations: InstallmentLegAllocationResponse[];
  allocated_irr: number;
  advance_irr: number;
  advance_toman: number;
  finance_income_irr: number;
  finance_income_toman: number;
  late_fee_irr: number;
  late_fee_toman: number;
  applied_advance_irr: number;
  applied_advance_toman: number;
  is_early_settlement: boolean;
  reference: string | null;
  notes: string | null;
  /** US-543 (BNPL): the gateway link — null for non-BNPL receipts. */
  gateway: string | null;
  /** Same value as `gateway`, under the plan-response name. */
  gateway_channel_key: string | null;
  gateway_reference: string | null;
  gateway_payment_id: string | null;
  bnpl_status: string | null;
  created_at: string;
}

export interface InstallmentPlanResponse {
  id: string;
  invoice_id: string;
  contract_id: string | null;
  customer_ref: string | null;
  company_engine_id: number;
  total_principal_irr: number;
  total_principal_toman: number;
  leg_count: number;
  interval_days: number | null;
  first_due_date: string | null;
  auto_post: boolean;
  applied_advance_irr: number;
  applied_advance_toman: number;
  source: string;
  kind: string;
  gateway_channel_key: string | null;
  status: string;
  total_received_irr: number;
  total_received_toman: number;
  outstanding_irr: number;
  outstanding_toman: number;
  pending_clearance_irr: number;
  pending_clearance_toman: number;
  created_at: string;
  updated_at: string;
}

export interface InstallmentPlanDetailResponse {
  plan: InstallmentPlanResponse;
  legs: InstallmentLegResponse[];
  receipts: InstallmentReceiptResponse[];
  status: string;
  total_received_irr: number;
  total_received_toman: number;
  total_advance_irr: number;
  total_advance_toman: number;
  outstanding_irr: number;
  outstanding_toman: number;
  total_pending_clearance_irr: number;
  total_pending_clearance_toman: number;
  /** US-543 (BNPL): gateway-side totals, never summed with the customer side. */
  gateway_receivable_irr: number;
  gateway_receivable_toman: number;
  gateway_settled_irr: number;
  gateway_settled_toman: number;
}

export interface InstallmentContractResponse {
  id: string;
  code: string;
  name: string;
  version: number;
  effective_from: string;
  company_engine_id: number;
  late_fee_basis: string;
  late_fee_rate: number;
  late_fee_grace_days: number;
  penalty_account_engine_id: number | null;
  penalty_account_code: string | null;
  finance_income_rate: number;
  dunning_offsets: number[];
  receivable_account_engine_id: number | null;
  receivable_account_code: string | null;
  early_settlement_fee_policy: string;
  early_settlement_income_policy: string;
  early_settlement_discount_rate: number;
  supersedes_contract_id: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface PayoffQuoteResponse {
  plan_id: string;
  as_of: string;
  contract_id: string | null;
  remaining_principal_irr: number;
  remaining_principal_toman: number;
  income_realised_irr: number;
  income_realised_toman: number;
  income_realised_unpaid_irr: number;
  income_realised_unpaid_toman: number;
  income_unrealised_irr: number;
  income_unrealised_toman: number;
  income_due_irr: number;
  income_due_toman: number;
  income_policy: string;
  late_fees_accrued_irr: number;
  late_fees_accrued_toman: number;
  late_fees_due_irr: number;
  late_fees_due_toman: number;
  fee_policy: string;
  advance_held_irr: number;
  advance_held_toman: number;
  payoff_total_irr: number;
  payoff_total_toman: number;
  open_leg_count: number;
}

export interface LegPrincipalSnapshotResponse {
  leg_id: string;
  sequence: number;
  principal_irr: number;
  principal_toman: number;
}

/** One schedule amendment (US-541 rows, US-545 read): the plan-change audit record. */
export interface ScheduleAmendmentResponse {
  id: string;
  plan_id: string;
  kind: string;
  applied_advance_irr: number;
  applied_advance_toman: number;
  legs_before: LegPrincipalSnapshotResponse[];
  legs_after: LegPrincipalSnapshotResponse[];
  receipt_id: string | null;
  created_at: string;
}

export interface EarlySettlementResponse {
  receipt: InstallmentReceiptResponse;
  quote: PayoffQuoteResponse;
  settlement_type: string;
  journal_move_ids: number[];
  amendment: ScheduleAmendmentResponse | null;
}

export interface OverdueLegResponse {
  plan_id: string;
  invoice_id: string;
  customer_ref: string | null;
  contract_code: string | null;
  leg_id: string;
  sequence: number;
  due_date: string;
  days_overdue: number;
  principal_irr: number;
  principal_toman: number;
  received_irr: number;
  received_toman: number;
  balance_irr: number;
  balance_toman: number;
  dunning_stage: number;
  next_reminder_date: string | null;
  late_fee_accrued_irr: number;
  late_fee_accrued_toman: number;
}

export interface OverdueReportResponse {
  rows: OverdueLegResponse[];
  total_rows: number;
  total_balance_irr: number;
  total_balance_toman: number;
  total_late_fees_irr: number;
  total_late_fees_toman: number;
}

export interface DunningRunResponse {
  as_of: string;
  plans_scanned: number;
  overdue_legs: number;
  items_filed: string[];
  fees_assessed_irr: number;
  fees_assessed_toman: number;
  journals_created: number[];
  legs_skipped_no_account: string[];
}

/** One SMS reminder-log row (US-544; the DLQ + audit trail). */
export interface ReminderLogResponse {
  id: string;
  tenant_id: string;
  installation_id: string;
  plan_id: string;
  leg_id: string;
  provider_id: string | null;
  provider_key: string;
  recipient_phone: string;
  message_text: string;
  offset_days: number;
  trigger: string;
  status: string;
  provider_message_id: string | null;
  provider_status_raw: string | null;
  /** Cost in Toman when the provider's unit is known — null otherwise, never guessed. */
  cost_toman: number | null;
  provider_cost_raw: string | null;
  provider_cost_unit: string | null;
  error_code: string | null;
  error_detail: string | null;
  sent_at: string | null;
  created_at: string | null;
}

/** One SMS provider row (credentials never included — only `has_credentials`). */
export interface SmsProviderResponse {
  id: string;
  installation_id: string;
  provider_key: string;
  display_name_fa: string;
  provider_kind: string;
  is_builtin: boolean;
  enabled: boolean;
  is_default: boolean;
  status: string;
  line_number: string | null;
  price_per_sms_toman: number | null;
  rate_limit_per_minute: number | null;
  line_type: string | null;
  contract_required: boolean;
  enmad_required: boolean;
  notes: string | null;
  has_credentials: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateInstallmentPlanRequest {
  company_id: string;
  invoice_id: string;
  contract_id?: string | null;
  contract_code?: string | null;
  customer_ref?: string | null;
  total_principal_toman?: number | null;
  leg_count?: number | null;
  interval_days?: number | null;
  first_due_date?: string | null;
  legs?: { due_date: string; principal_toman: number }[] | null;
  auto_post?: boolean;
  kind?: string;
  gateway_channel_key?: string | null;
}

export interface CreateInstallmentReceiptRequest {
  invoice_id: string;
  journal_move_id: number;
  amount_toman: number;
  received_date?: string | null;
  payment_method?: string;
  cheque_id?: string | null;
  reference?: string | null;
  notes?: string | null;
  gateway?: string | null;
  gateway_reference?: string | null;
}

export interface CreateInstallmentContractRequest {
  company_id: string;
  code: string;
  name: string;
  version?: number;
  effective_from: string;
  late_fee_basis?: string;
  late_fee_rate?: string | number;
  late_fee_grace_days?: number;
  penalty_account_engine_id?: number | null;
  penalty_account_code?: string | null;
  finance_income_rate?: string | number;
  dunning_offsets?: number[] | null;
  receivable_account_engine_id?: number | null;
  receivable_account_code?: string | null;
  early_settlement_fee_policy?: string;
  early_settlement_income_policy?: string;
  early_settlement_discount_rate?: string | number;
  supersedes_contract_id?: string | null;
}

export interface EarlySettlementRequestBody {
  invoice_id: string;
  journal_move_id: number;
  amount_toman: number;
  settlement_type?: string;
  settlement_date?: string | null;
  payment_method?: string;
  reference?: string | null;
  notes?: string | null;
}

export interface TriggerReminderRequestBody {
  leg_id?: string | null;
  provider_id?: string | null;
  recipient_phone?: string | null;
  dry_run?: boolean;
}

// ---------------------------------------------------------------------------
// Inbox staleness (US-551 dashboard alerts) — mirrors the backend's
// StaleInboxResponse / InboxEventResponse
// (src/hesabyar/interfaces/rest/schemas/integrations.py). Only the
// fields the dashboard alert renders are declared; the backend sends
// the full event and extra JSON fields are ignored.
// ---------------------------------------------------------------------------

export interface StaleInboxEventResponse {
  inbox_event_id: string;
  source_system: string;
  event_type: string;
  received_at: string;
  status: string;
}

export interface StaleInboxResponse {
  stale_after_hours: number;
  stale_count: number;
  events: StaleInboxEventResponse[];
}

// ---------------------------------------------------------------------------
// Transport over the BFF
// ---------------------------------------------------------------------------

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT";
  /** JSON body for POST/PATCH. */
  body?: unknown;
  /** Idempotency-Key for mutations (required by the backend on creates/posts). */
  idempotencyKey?: string;
  /** Extra fetch init (e.g. Next.js `next: { revalidate }` from server components). */
  init?: RequestInit;
}

/** Fresh idempotency key for one mutation attempt. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

/**
 * Call one BFF route. `path` is the path after /api/v1, e.g. "companies"
 * or "accounting/accounts/12". Same-origin fetch; the session cookie is
 * sent automatically and the BFF attaches the API key server-side.
 */
export async function bffRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;

  const res = await fetch(`/api/bff/${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: "same-origin",
    ...options.init,
  });

  // Empty success body (e.g. 204) — none of the endpoints use it, but stay safe.
  if (res.status === 204) return undefined as T;

  const data: unknown = await res.json().catch(() => null);

  if (!res.ok) {
    const problem = (data ?? {}) as {
      code?: string;
      detail?: string;
      title?: string;
      request_id?: string;
    };
    throw new ApiError(
      res.status,
      typeof problem.code === "string" ? problem.code : `HTTP_${res.status}`,
      typeof problem.detail === "string" && problem.detail
        ? problem.detail
        : typeof problem.title === "string"
          ? problem.title
          : `Request failed (${res.status})`,
      typeof problem.request_id === "string" ? problem.request_id : null,
    );
  }

  return data as T;
}

// ---------------------------------------------------------------------------
// Endpoint methods (207) — one per backend route
// ---------------------------------------------------------------------------

export const api = {
  // -- health / installation (no auth on the backend, but still via the BFF
  //    so the browser never learns the backend origin) ----------------------
  getHealth: () => bffRequest<HealthResponse>("health"),
  getReadiness: () => bffRequest<ReadinessResponse>("readiness"),
  getInstallation: () => bffRequest<InstallationResponse>("installation"),

  // -- companies ------------------------------------------------------------
  listCompanies: (params: PaginationParams & { name?: string; currency_code?: string } = {}) =>
    bffRequest<Paginated<CompanyResponse>>(`companies${buildQuery(params as Record<string, string | number | undefined>)}`),
  getCompany: (companyId: string) => bffRequest<CompanyResponse>(`companies/${encodeURIComponent(companyId)}`),

  // -- chart of accounts ----------------------------------------------------
  listAccounts: (
    params: PaginationParams & { code?: string; name?: string; type?: string; active?: boolean; closed?: boolean },
  ) =>
    bffRequest<Paginated<AccountResponse>>(
      `accounting/accounts${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getAccount: (accountId: number, company?: string) =>
    bffRequest<AccountResponse>(
      `accounting/accounts/${accountId}${buildQuery({ company })}`,
    ),

  // -- fiscal years / periods / setup ---------------------------------------
  listFiscalYears: (params: PaginationParams & { state?: string } = {}) =>
    bffRequest<Paginated<FiscalYearResponse>>(
      `accounting/fiscal-years${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getFiscalYear: (fiscalYearId: number, company?: string) =>
    bffRequest<FiscalYearResponse>(
      `accounting/fiscal-years/${fiscalYearId}${buildQuery({ company })}`,
    ),
  listFiscalPeriods: (params: PaginationParams & { fiscal_year?: string; state?: string; type?: string } = {}) =>
    bffRequest<Paginated<FiscalPeriodResponse>>(
      `accounting/fiscal-periods${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getFiscalPeriod: (periodId: number, company?: string) =>
    bffRequest<FiscalPeriodResponse>(
      `accounting/fiscal-periods/${periodId}${buildQuery({ company })}`,
    ),
  getSetupStatus: (company?: string) =>
    bffRequest<SetupStatusResponse>(`accounting/setup-status${buildQuery({ company })}`),

  // -- journal drafts / journals --------------------------------------------
  listJournalDrafts: (params: PaginationParams = {}) =>
    bffRequest<Paginated<JournalDraftResponse>>(
      `accounting/journal-drafts${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createJournalDraft: (body: CreateJournalDraftRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<JournalDraftResponse>("accounting/journal-drafts", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  getJournalDraft: (draftId: number) =>
    bffRequest<JournalDraftResponse>(`accounting/journal-drafts/${draftId}`),
  updateJournalDraft: (draftId: number, body: UpdateJournalDraftRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<JournalDraftResponse>(`accounting/journal-drafts/${draftId}`, {
      method: "PATCH",
      body,
      idempotencyKey,
    }),
  validateJournalDraft: (draftId: number) =>
    bffRequest<ValidationResponse>(`accounting/journal-drafts/${draftId}/validate`, { method: "POST" }),
  postJournalDraft: (draftId: number, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<PostedJournalResponse>(`accounting/journal-drafts/${draftId}/post`, {
      method: "POST",
      idempotencyKey,
    }),
  reverseJournal: (journalId: number, body: ReverseJournalRequest = {}, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<PostedJournalResponse>(`accounting/journals/${journalId}/reverse`, {
      method: "POST",
      body,
      idempotencyKey,
    }),

  // -- invoices ---------------------------------------------------------------
  listInvoices: (params: PaginationParams & { type?: string; state?: string } = {}) =>
    bffRequest<Paginated<InvoiceResponse>>(
      `invoices${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getInvoice: (invoiceId: number) => bffRequest<InvoiceResponse>(`invoices/${invoiceId}`),
  createInvoiceDraft: (body: CreateInvoiceDraftRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<InvoiceResponse>("invoices/drafts", { method: "POST", body, idempotencyKey }),
  validateInvoice: (invoiceId: number) =>
    bffRequest<ValidationResponse>(`invoices/${invoiceId}/validate`, { method: "POST" }),

  // -- payments ---------------------------------------------------------------
  listPayments: (params: PaginationParams & { kind?: string; state?: string } = {}) =>
    bffRequest<Paginated<PaymentResponse>>(
      `payments${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getPayment: (paymentId: number) => bffRequest<PaymentResponse>(`payments/${paymentId}`),

  // -- bank statements --------------------------------------------------------
  listStatements: (params: PaginationParams & { state?: string } = {}) =>
    bffRequest<Paginated<BankStatementResponse>>(
      `bank/statements${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getStatementImportStatus: (company?: string) =>
    bffRequest<StatementImportStatusResponse>(
      `bank/statements/import-status${buildQuery({ company })}`,
    ),
  getStatement: (statementId: number) =>
    bffRequest<BankStatementResponse>(`bank/statements/${statementId}`),
  /** Reconcile a draft statement (US-514): 3-priority matching, human approval for suggestions. */
  reconcileStatement: (
    statementId: number,
    options: { windowDays?: number; idempotencyKey?: string } = {},
  ) =>
    bffRequest<ReconcileResponse>(`bank/statements/${statementId}/reconcile`, {
      method: "POST",
      body: options.windowDays === undefined ? {} : { window_days: options.windowDays },
      idempotencyKey: options.idempotencyKey ?? newIdempotencyKey(),
    }),
  listStatementMatches: (statementId: number) =>
    bffRequest<BankStatementMatchResponse[]>(`bank/statements/${statementId}/matches`),
  applyManualMatch: (
    statementId: number,
    lineId: number,
    body: {
      target_type: string;
      target_id: string;
      account_id?: number;
      party_id?: number;
      save_as_rule?: boolean;
      review_item_id?: string;
    },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<BankStatementMatchResponse>(`bank/statements/${statementId}/lines/${lineId}/match`, {
      method: "POST",
      body,
      idempotencyKey,
    }),

  // -- bank statement import (US-514, B1) -------------------------------------
  detectStatementFile: (body: { filename: string; content_base64: string }) =>
    bffRequest<FileDetectionResponse>("bank/statements/imports/detect", {
      method: "POST",
      body,
    }),
  listColumnMappings: (params: { bank_key?: string } = {}) =>
    bffRequest<ColumnMappingResponse[]>(
      `bank/column-mappings${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  createColumnMapping: (
    body: {
      bank_key: string;
      name: string;
      file_format: string;
      columns: Record<string, string>;
      date_kind?: string;
      currency_unit?: string;
      header_row?: number;
    },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ColumnMappingResponse>("bank/column-mappings", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  supersedeColumnMapping: (mappingId: string, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ColumnMappingResponse>(`bank/column-mappings/${mappingId}/supersede`, {
      method: "POST",
      idempotencyKey,
    }),
  createStatementImport: (
    body: {
      company: string;
      bank_key: string;
      statement_journal_id: number;
      filename: string;
      content_base64: string;
      currency_unit?: string;
      default_account_id: number;
      mapping_id?: string;
      columns?: Record<string, string>;
      date_kind?: string;
      header_row?: number;
    },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<BankImportBatchDetailResponse>("bank/statements/imports", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listStatementImports: (params: { company: string; status?: string }) =>
    bffRequest<BankImportBatchResponse[]>(
      `bank/statements/imports${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  getStatementImport: (batchId: string) =>
    bffRequest<BankImportBatchDetailResponse>(`bank/statements/imports/${batchId}`),
  confirmStatementImport: (batchId: string, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<BankImportBatchResponse>(`bank/statements/imports/${batchId}/confirm`, {
      method: "POST",
      idempotencyKey,
    }),
  cancelStatementImport: (batchId: string, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<BankImportBatchResponse>(`bank/statements/imports/${batchId}/cancel`, {
      method: "POST",
      idempotencyKey,
    }),
  listStatementJournals: (company: string) =>
    bffRequest<StatementJournalResponse[]>(
      `bank/statement-journals${buildQuery({ company })}`,
    ),
  getReconciliationStatus: (params: { company: string; date_from: string; date_to: string }) =>
    bffRequest<ReconciliationStatusResponse>(
      `bank/reconciliation-status${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  listMatchRules: (params: { company: string; bank_key?: string }) =>
    bffRequest<MatchRuleResponse[]>(
      `bank/match-rules${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  setMatchRuleMode: (
    ruleId: string,
    mode: "suggest" | "auto",
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<MatchRuleResponse>(`bank/match-rules/${ruleId}/mode`, {
      method: "POST",
      body: { mode },
      idempotencyKey,
    }),

  // -- audit ------------------------------------------------------------------
  listAuditEvents: (
    params: PaginationParams & { actor?: string; action?: string; from?: string; to?: string } = {},
  ) =>
    bffRequest<Paginated<AuditEventResponse>>(
      `audit/events${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getAuditEvent: (eventId: string) =>
    bffRequest<AuditEventResponse>(`audit/events/${encodeURIComponent(eventId)}`),

  // -- standards registry + validation runs (US-401) ------------------------
  listStandards: (
    params: PaginationParams & { verification_status?: string; phase?: number; has_rules?: boolean } = {},
  ) =>
    bffRequest<Paginated<StandardListItemResponse>>(
      `standards${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getStandard: (standardCode: string) =>
    bffRequest<StandardDetailResponse>(`standards/${encodeURIComponent(standardCode)}`),
  getValidationRun: (runId: string) =>
    bffRequest<ValidationRunResponse>(`validation-runs/${encodeURIComponent(runId)}`),

  // -- tax-policy registry + decisions (US-402) ------------------------------
  listTaxSources: (params: PaginationParams = {}) =>
    bffRequest<Paginated<TaxSourceResponse>>(
      `tax-policy/sources${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  listTaxVersions: (
    params: PaginationParams & {
      rule_key?: string;
      verification_status?: string;
      effective_on?: string;
      rule_year?: number;
      performance_year?: number;
      filing_year?: number;
    } = {},
  ) =>
    bffRequest<Paginated<TaxPolicyVersionResponse>>(
      `tax-policy/versions${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getTaxVersion: (versionId: string) =>
    bffRequest<TaxPolicyVersionResponse>(`tax-policy/versions/${encodeURIComponent(versionId)}`),
  resolveTaxDecision: (body: ResolveTaxDecisionRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<TaxDecisionResponse>("tax-policy/decisions", {
      method: "POST",
      body,
      idempotencyKey,
    }),

  // -- reports: trial balance + report validation runs (US-403) ---------------
  getTrialBalance: (params: { company?: string; date_from: string; date_to: string }) =>
    bffRequest<TrialBalanceResponse>(
      `reports/trial-balance${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  // -- reports: financial statements (US-550) --------------------------------
  getGeneralLedger: (params: { company?: string; date_from: string; date_to: string; account_id?: number }) =>
    bffRequest<GeneralLedgerResponse>(
      `reports/general-ledger${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getBalanceSheet: (params: { company?: string; as_of: string; comparison_as_of?: string }) =>
    bffRequest<BalanceSheetResponse>(
      `reports/balance-sheet${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getIncomeStatement: (params: {
    company?: string;
    date_from: string;
    date_to: string;
    comparison_from?: string;
    comparison_to?: string;
  }) =>
    bffRequest<IncomeStatementResponse>(
      `reports/income-statement${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getCashFlow: (params: { company?: string; date_from: string; date_to: string }) =>
    bffRequest<CashFlowResponse>(
      `reports/cash-flow${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getNotesBalance: (params: { company?: string; as_of: string; instrument?: string; party_id?: number }) =>
    bffRequest<NotesBalanceResponse>(
      `reports/notes-balance${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  // -- reports: cash forecast (US-553) ---------------------------------------
  getCashForecast: (params: { company?: string; date_from: string; date_to: string }) =>
    bffRequest<CashForecastResponse>(
      `reports/cash-forecast${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createReportValidationRun: (body: CreateReportValidationRunRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ReportValidationRunResponse>("report-validation-runs", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  getReportValidationRun: (runId: string) =>
    bffRequest<ReportValidationRunResponse>(`report-validation-runs/${encodeURIComponent(runId)}`),

  // -- Moadian profiles / documents / submissions (US-404) --------------------
  listMoadianProfiles: (params: PaginationParams & { environment?: string; is_active?: boolean } = {}) =>
    bffRequest<Paginated<MoadianProfileResponse>>(
      `moadian/profiles${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createMoadianProfile: (body: CreateMoadianProfileRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<MoadianProfileResponse>("moadian/profiles", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  updateMoadianProfile: (profileId: string, body: UpdateMoadianProfileRequest) =>
    bffRequest<MoadianProfileResponse>(`moadian/profiles/${encodeURIComponent(profileId)}`, {
      method: "PATCH",
      body,
    }),
  listMoadianDocuments: (params: PaginationParams & { profile_id?: string; status?: string } = {}) =>
    bffRequest<Paginated<MoadianDocumentResponse>>(
      `moadian/documents${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createMoadianDocument: (body: CreateMoadianDocumentRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<MoadianDocumentResponse>("moadian/documents", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listMoadianSubmissions: (
    params: PaginationParams & { document_id?: string; profile_id?: string; status?: string } = {},
  ) =>
    bffRequest<Paginated<MoadianSubmissionResponse>>(
      `moadian/submissions${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createMoadianSubmission: (body: CreateMoadianSubmissionRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<MoadianSubmissionResponse>("moadian/submissions", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  getMoadianSubmission: (submissionId: string) =>
    bffRequest<MoadianSubmissionResponse>(`moadian/submissions/${encodeURIComponent(submissionId)}`),
  approveMoadianSubmission: (submissionId: string, body: { note?: string | null } = {}) =>
    bffRequest<MoadianSubmissionResponse>(`moadian/submissions/${encodeURIComponent(submissionId)}/approve`, {
      method: "POST",
      body,
    }),
  queueMoadianSubmission: (submissionId: string) =>
    bffRequest<MoadianSubmissionResponse>(`moadian/submissions/${encodeURIComponent(submissionId)}/queue`, {
      method: "POST",
    }),
  listMoadianStatusEvents: (submissionId: string, params: PaginationParams = {}) =>
    bffRequest<Paginated<MoadianStatusEventResponse>>(
      `moadian/submissions/${encodeURIComponent(submissionId)}/status-events${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  runMoadianReconcile: () =>
    bffRequest<ReconciliationReportResponse>("moadian/reconcile", { method: "POST" }),

  // -- review queue (US-504) --------------------------------------------------
  listReviewItemTypes: () => bffRequest<ReviewItemTypeResponse[]>("review-queue/types"),
  listReviewItems: (
    params: PaginationParams & {
      type?: string;
      severity?: string;
      status?: string;
      channel?: string;
      assignee?: string;
      min_amount?: number;
      max_amount?: number;
      min_age_hours?: number;
      max_age_hours?: number;
      overdue?: boolean;
    } = {},
  ) =>
    bffRequest<Paginated<ReviewItemResponse>>(
      `review-queue/items${buildQuery({
        ...params,
        overdue: params.overdue ? "true" : undefined,
      } as Record<string, string | number | undefined>)}`,
    ),
  createReviewItem: (body: CreateReviewItemRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ReviewItemResponse>("review-queue/items", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  getReviewItem: (itemId: string) =>
    bffRequest<ReviewItemResponse>(`review-queue/items/${encodeURIComponent(itemId)}`),
  performReviewAction: (
    itemId: string,
    body: { action: string; note?: string | null; mapping?: MappingLearningRequest | null },
  ) =>
    bffRequest<ReviewItemResponse>(`review-queue/items/${encodeURIComponent(itemId)}/actions`, {
      method: "POST",
      body,
    }),
  assignReviewItem: (itemId: string, body: { assignee: string | null }) =>
    bffRequest<ReviewItemResponse>(`review-queue/items/${encodeURIComponent(itemId)}`, {
      method: "PATCH",
      body,
    }),
  getReviewQueueSummary: (params: { company?: string } = {}) =>
    bffRequest<ReviewQueueSummaryResponse>(
      `review-queue/summary${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  listSlaPolicies: () => bffRequest<SlaPolicyResponse[]>("review-queue/sla-policies"),
  updateSlaPolicy: (itemType: string, body: { sla_hours: number; warn_hours: number }) =>
    bffRequest<SlaPolicyResponse>(`review-queue/sla-policies/${encodeURIComponent(itemType)}`, {
      method: "PATCH",
      body,
    }),

  // -- integrations: mappings (US-552, Suggest → Auto promotion) --------------
  promoteIntegrationMapping: (mappingId: string) =>
    bffRequest<IntegrationMappingResponse>(
      `integrations/mappings/${encodeURIComponent(mappingId)}/promote`,
      { method: "POST" },
    ),

  // -- manual entries (US-513, dual entry) ------------------------------------
  createManualOrder: (body: CreateManualOrderRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ManualEntryResponse>("manual-entries/orders", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  createManualRefund: (body: CreateManualRefundRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ManualEntryResponse>("manual-entries/refunds", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  createManualCancellation: (
    body: CreateManualCancellationRequest,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ManualEntryResponse>("manual-entries/cancellations", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listManualEntries: (params: PaginationParams & { entry_type?: string } = {}) =>
    bffRequest<Paginated<ManualEntryResponse>>(
      `manual-entries${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getManualEntry: (entryId: string) =>
    bffRequest<ManualEntryResponse>(`manual-entries/${encodeURIComponent(entryId)}`),
  // -- packaging (US-515) -----------------------------------------------------
  listPackagingLocations: (companyId: string) =>
    bffRequest<PackagingLocationResponse[]>(
      `packaging/locations${buildQuery({ company_id: companyId })}`,
    ),
  getPackagingSettings: (companyId: string) =>
    bffRequest<PackagingSettingsResponse>(
      `packaging/settings${buildQuery({ company_id: companyId })}`,
    ),
  savePackagingSettings: (
    body: {
      company_id: string;
      cost_method?: string;
      shipment_cost_method?: string;
      days_of_cover_window_days?: number;
      low_stock_alert_days?: number;
      packaging_location_id?: number | null;
      production_location_id?: number | null;
      warehouse_id?: number | null;
      packaging_category_id?: number | null;
      analytic_root_id?: number | null;
    },
  ) => bffRequest<PackagingSettingsResponse>("packaging/settings", { method: "PUT", body }),
  listPackagingMaterials: (
    companyId: string,
    params: PaginationParams & { sort?: string } = {},
  ) =>
    bffRequest<Paginated<MaterialDetailResponse>>(
      `packaging/materials${buildQuery({ company_id: companyId, ...params } as Record<string, string | number | undefined>)}`,
    ),
  createPackagingMaterial: (body: {
    company_id: string;
    name_fa: string;
    cost_method?: string;
    code?: string | null;
  }) => bffRequest<MaterialDetailResponse>("packaging/materials", { method: "POST", body }),
  getPackagingMaterial: (materialId: string, companyId: string) =>
    bffRequest<MaterialDetailResponse>(
      `packaging/materials/${encodeURIComponent(materialId)}${buildQuery({ company_id: companyId })}`,
    ),
  updatePackagingMaterial: (
    materialId: string,
    body: { name_fa?: string | null; code?: string | null; is_active?: boolean | null },
  ) =>
    bffRequest<PackagingMaterialResponse>(
      `packaging/materials/${encodeURIComponent(materialId)}`,
      { method: "PATCH", body },
    ),
  setReorderPoint: (
    materialId: string,
    body: { company_id: string; min_quantity: string; target_quantity: string },
  ) =>
    bffRequest<ReorderPointResponse>(
      `packaging/materials/${encodeURIComponent(materialId)}/reorder-point`,
      { method: "PUT", body },
    ),
  getMaterialCoverage: (materialId: string, companyId: string) =>
    bffRequest<DaysOfCoverResponse>(
      `packaging/materials/${encodeURIComponent(materialId)}/coverage${buildQuery({ company_id: companyId })}`,
    ),
  recordConsumption: (body: {
    company_id: string;
    material_id: string;
    order_ref: string;
    shipment_ref?: string | null;
    quantity: string;
    consumed_on?: string | null;
  }) => bffRequest<ConsumptionResponse>("packaging/consumptions", { method: "POST", body }),
  listConsumptions: (
    companyId: string,
    params: PaginationParams & { order_ref?: string; material_id?: string } = {},
  ) =>
    bffRequest<Paginated<ConsumptionResponse>>(
      `packaging/consumptions${buildQuery({ company_id: companyId, ...params } as Record<string, string | number | undefined>)}`,
    ),
  createOrderCostCenter: (orderRef: string, body: { company_id: string; order_ref: string }) =>
    bffRequest<OrderCostCenterResponse>(
      `packaging/orders/${encodeURIComponent(orderRef)}/cost-center`,
      { method: "POST", body },
    ),
  getOrderCost: (orderRef: string, companyId: string) =>
    bffRequest<OrderCostReportResponse>(
      `packaging/orders/${encodeURIComponent(orderRef)}/cost${buildQuery({ company_id: companyId })}`,
    ),
  listShipmentCosts: (
    companyId: string,
    params: PaginationParams & { order_ref?: string } = {},
  ) =>
    bffRequest<Paginated<ShipmentCostResponse>>(
      `packaging/shipment-costs${buildQuery({ company_id: companyId, ...params } as Record<string, string | number | undefined>)}`,
    ),
  recordShipmentCost: (body: {
    company_id: string;
    order_ref: string;
    method: string;
    actual_irr: number;
    invoiced_irr?: number | null;
    carrier_name?: string | null;
    tryton_sale_id?: number | null;
    tryton_shipment_id?: number | null;
  }) => bffRequest<ShipmentCostResponse>("packaging/shipment-costs", { method: "POST", body }),
  computePriceQuote: (body: {
    company_id: string;
    goods_cost_irr: number;
    packaging: { material_id: string; quantity: string }[];
    shipment_cost_irr?: number | null;
    carrier_rate_id?: string | null;
    margin_percent?: string | null;
    margin_fixed_irr?: number | null;
  }) => bffRequest<PriceQuoteResponse>("packaging/price-calculator/quote", { method: "POST", body }),
  listCarrierRates: (
    companyId: string,
    params: PaginationParams & { active_only?: boolean; sort?: string } = {},
  ) =>
    bffRequest<Paginated<CarrierRateResponse>>(
      `packaging/carrier-rates${buildQuery({ company_id: companyId, ...params, active_only: params.active_only ? "true" : undefined } as Record<string, string | number | undefined>)}`,
    ),
  createCarrierRate: (body: {
    company_id: string;
    carrier_name: string;
    rate_irr: number;
    basis?: string;
    tryton_carrier_id?: number | null;
    effective_from?: string | null;
  }) => bffRequest<CarrierRateResponse>("packaging/carrier-rates", { method: "POST", body }),
  updateCarrierRate: (
    rateId: string,
    body: { rate_irr?: number | null; is_active?: boolean | null },
  ) =>
    bffRequest<CarrierRateResponse>(`packaging/carrier-rates/${encodeURIComponent(rateId)}`, {
      method: "PATCH",
      body,
    }),
  createCount: (body: { company_id: string; count_date?: string | null; counted_by?: string | null }) =>
    bffRequest<InventoryCountResponse>("packaging/counts", { method: "POST", body }),
  listCounts: (companyId: string, params: PaginationParams = {}) =>
    bffRequest<Paginated<InventoryCountResponse>>(
      `packaging/counts${buildQuery({ company_id: companyId, ...params } as Record<string, string | number | undefined>)}`,
    ),
  getCount: (countId: string, companyId: string) =>
    bffRequest<InventoryCountResponse>(
      `packaging/counts/${encodeURIComponent(countId)}${buildQuery({ company_id: companyId })}`,
    ),
  setCountLine: (countId: string, body: { line_id: number; quantity: string }) =>
    bffRequest<InventoryCountResponse>(
      `packaging/counts/${encodeURIComponent(countId)}/lines`,
      { method: "POST", body },
    ),
  confirmCount: (countId: string) =>
    bffRequest<InventoryCountResponse>(`packaging/counts/${encodeURIComponent(countId)}/confirm`, {
      method: "POST",
    }),
  cancelCount: (countId: string) =>
    bffRequest<InventoryCountResponse>(`packaging/counts/${encodeURIComponent(countId)}/cancel`, {
      method: "POST",
    }),
  runSupplyCheck: (body: { company_id: string }) =>
    bffRequest<SupplyCheckReportResponse>("packaging/supply-check", { method: "POST", body }),
  // -- cash box (US-516) ------------------------------------------------------
  getCashBoxSettings: () => bffRequest<CashBoxSettingsResponse>("cash-boxes/settings"),
  updateCashBoxSettings: (body: UpdateCashBoxSettingsRequest) =>
    bffRequest<CashBoxSettingsResponse>("cash-boxes/settings", { method: "PATCH", body }),
  listCashBoxStatementJournals: (params: { company?: string } = {}) =>
    bffRequest<CashBoxStatementJournalResponse[]>(
      `cash-boxes/statement-journals${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getCashBoxReport: (params: {
    company?: string;
    date_from: string;
    date_to: string;
    cash_box_id?: string;
  }) =>
    bffRequest<CashBoxReportResponse>(
      `cash-boxes/report${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  listCashBoxes: (params: PaginationParams & { company?: string } = {}) =>
    bffRequest<Paginated<CashBoxResponse>>(
      `cash-boxes${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createCashBox: (body: CreateCashBoxRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<CashBoxResponse>("cash-boxes", { method: "POST", body, idempotencyKey }),
  getCashBox: (boxId: string) =>
    bffRequest<CashBoxResponse>(`cash-boxes/${encodeURIComponent(boxId)}`),
  updateCashBox: (boxId: string, body: UpdateCashBoxRequest) =>
    bffRequest<CashBoxResponse>(`cash-boxes/${encodeURIComponent(boxId)}`, {
      method: "PATCH",
      body,
    }),
  startCountSession: (
    boxId: string,
    body: { company_id: string; cashier: string; supervisor: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<CountSessionResponse>(`cash-boxes/${encodeURIComponent(boxId)}/count-sessions`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listCountSessions: (
    params: PaginationParams & {
      company?: string;
      cash_box_id?: string;
      status?: string;
      date_from?: string;
      date_to?: string;
    } = {},
  ) =>
    bffRequest<Paginated<CountSessionResponse>>(
      `count-sessions${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getCountSession: (sessionId: string) =>
    bffRequest<CountSessionResponse>(`count-sessions/${encodeURIComponent(sessionId)}`),
  enterCount: (
    sessionId: string,
    body: {
      counted_amount_irr: number;
      denominations?: { denomination_irr: number; count: number }[];
      actor: string;
    },
  ) =>
    bffRequest<CountSessionResponse>(`count-sessions/${encodeURIComponent(sessionId)}/count`, {
      method: "POST",
      body,
    }),
  signSession: (
    sessionId: string,
    body: { role: string; actor: string; minutes_text?: string | null },
  ) =>
    bffRequest<CountSessionResponse>(`count-sessions/${encodeURIComponent(sessionId)}/sign`, {
      method: "POST",
      body,
    }),
  postVarianceJournal: (sessionId: string, body: { actor: string }) =>
    bffRequest<CountSessionResponse>(
      `count-sessions/${encodeURIComponent(sessionId)}/post-variance-journal`,
      { method: "POST", body },
    ),
  recountSession: (sessionId: string, body: { actor: string }) =>
    bffRequest<CountSessionResponse>(`count-sessions/${encodeURIComponent(sessionId)}/recount`, {
      method: "POST",
      body,
    }),

  // -- settlement center (US-525, Phase 5-3 Wave C) --------------------------
  // Payment channels + gateway contracts (US-503 registry, /integrations).
  /** Stuck inbox events (US-501 staleness alert): received/processing longer than N hours, oldest first. */
  listStaleInboxEvents: (params: { hours?: number; limit?: number } = {}) =>
    bffRequest<StaleInboxResponse>(
      `integrations/inbox/stale${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  listPaymentChannels: (params: PaginationParams & { enabled?: boolean; channel_type?: string; is_builtin?: boolean } = {}) =>
    bffRequest<Paginated<PaymentChannelResponse>>(
      `integrations/channels${buildQuery({
        ...params,
        enabled: params.enabled === undefined ? undefined : String(params.enabled),
        is_builtin: params.is_builtin === undefined ? undefined : String(params.is_builtin),
      } as Record<string, string | number | undefined>)}`,
    ),
  getPaymentChannel: (channelKey: string) =>
    bffRequest<PaymentChannelResponse>(`integrations/channels/${encodeURIComponent(channelKey)}`),
  seedBuiltinChannels: (idempotencyKey = newIdempotencyKey()) =>
    bffRequest<Paginated<PaymentChannelResponse>>("integrations/channels/seed", {
      method: "POST",
      idempotencyKey,
    }),
  updatePaymentChannel: (channelKey: string, body: UpdatePaymentChannelRequest) =>
    bffRequest<PaymentChannelResponse>(`integrations/channels/${encodeURIComponent(channelKey)}`, {
      method: "PATCH",
      body,
    }),
  listGatewayContracts: (channelKey: string, params: PaginationParams & { sort?: string } = {}) =>
    bffRequest<Paginated<GatewayContractResponse>>(
      `integrations/channels/${encodeURIComponent(channelKey)}/contracts${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createGatewayContract: (channelKey: string, body: CreateGatewayContractRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<GatewayContractResponse>(`integrations/channels/${encodeURIComponent(channelKey)}/contracts`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  getActiveGatewayContract: (channelKey: string) =>
    bffRequest<GatewayContractResponse>(
      `integrations/channels/${encodeURIComponent(channelKey)}/contracts/active`,
    ),

  // Gateway payments + tranche schedules (US-521).
  listGatewayPayments: (
    params: PaginationParams & { channel_key?: string; status?: string; sales_period?: string; sort?: string } = {},
  ) =>
    bffRequest<Paginated<GatewayPaymentResponse>>(
      `settlements/payments${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  recordGatewayPayment: (body: CreateGatewayPaymentRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<RecordPaymentResponse>("settlements/payments", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  getGatewayPayment: (paymentId: string) =>
    bffRequest<PaymentScheduleResponse>(`settlements/payments/${encodeURIComponent(paymentId)}`),
  cancelGatewayPayment: (paymentId: string, body: { reason: string; actor?: string | null }) =>
    bffRequest<CancelGatewayPaymentResponse>(
      `settlements/payments/${encodeURIComponent(paymentId)}/cancel`,
      { method: "POST", body },
    ),
  reassignSalesPeriod: (
    paymentId: string,
    body: { new_sales_period_jalali: string; reason: string; actor?: string | null },
  ) =>
    bffRequest<ReassignSalesPeriodResponse>(
      `settlements/payments/${encodeURIComponent(paymentId)}/reassign-period`,
      { method: "POST", body },
    ),
  bulkRederivePayments: (body: {
    channel_key?: string | null;
    sales_period_jalali?: string | null;
    actor: string;
    reason: string;
  }) =>
    bffRequest<BulkRederiveResponse>("settlements/payments/bulk-rederive", {
      method: "POST",
      body,
    }),

  // Settlement tranches (US-521). The due-range list IS the schedule API
  // the 5ح cash calendar consumes: expected tranches in a window, per
  // channel, with state + amounts.
  listSettlementTranches: (
    params: PaginationParams & {
      channel_key?: string;
      state?: string;
      sales_period?: string;
      due_from?: string;
      due_to?: string;
      sort?: string;
    } = {},
  ) =>
    bffRequest<Paginated<SettlementTrancheResponse>>(
      `settlements/tranches${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getSettlementTranche: (trancheId: string) =>
    bffRequest<SettlementTrancheResponse>(`settlements/tranches/${encodeURIComponent(trancheId)}`),
  writeOffTranche: (trancheId: string, body: { approved_by: string; reason: string }) =>
    bffRequest<SettlementTrancheResponse>(
      `settlements/tranches/${encodeURIComponent(trancheId)}/write-off`,
      { method: "POST", body },
    ),
  listTrancheMatches: (trancheId: string) =>
    bffRequest<SettlementMatchResponse[]>(`settlements/tranches/${encodeURIComponent(trancheId)}/matches`),

  // Settlement exceptions + fee records (US-521).
  listSettlementExceptions: (
    params: PaginationParams & { exception_type?: string; status?: string; tranche_id?: string; sort?: string } = {},
  ) =>
    bffRequest<Paginated<SettlementExceptionResponse>>(
      `settlements/exceptions${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getSettlementException: (exceptionId: string) =>
    bffRequest<SettlementExceptionResponse>(
      `settlements/exceptions/${encodeURIComponent(exceptionId)}`,
    ),
  resolveSettlementException: (
    exceptionId: string,
    body: { resolution: string; resolved_by: string; note?: string | null },
  ) =>
    bffRequest<SettlementExceptionResponse>(
      `settlements/exceptions/${encodeURIComponent(exceptionId)}/resolve`,
      { method: "POST", body },
    ),
  listSettlementFeeRecords: (
    params: PaginationParams & { gateway_payment_id?: string; channel_key?: string; sort?: string } = {},
  ) =>
    bffRequest<Paginated<SettlementFeeRecordResponse>>(
      `settlements/fee-records${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),

  // Daily tick (US-521 W1): expected → due → overdue + escalations.
  runSettlementTick: () =>
    bffRequest<TickResponse>("settlements/tick", { method: "POST" }),

  // Settlement postings (US-522 audit trail).
  listPaymentPostings: (paymentId: string) =>
    bffRequest<SettlementPostingResponse[]>(
      `settlements/payments/${encodeURIComponent(paymentId)}/postings`,
    ),

  // Settlement file imports (US-523).
  importSettlementFile: (body: ImportSettlementFileRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ImportSettlementFileResponse>("settlements/imports", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listSettlementImports: (
    params: PaginationParams & { channel_key?: string; status?: string; sort?: string } = {},
  ) =>
    bffRequest<Paginated<SettlementFileImportResponse>>(
      `settlements/imports${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getSettlementImport: (importId: string) =>
    bffRequest<SettlementFileImportDetailResponse>(
      `settlements/imports/${encodeURIComponent(importId)}`,
    ),
  approveSettlementImport: (importId: string, body: { actor?: string | null } = {}) =>
    bffRequest<ApproveImportResponse>(`settlements/imports/${encodeURIComponent(importId)}/approve`, {
      method: "POST",
      body,
    }),
  rejectSettlementImport: (importId: string, body: { reason?: string | null; actor?: string | null }) =>
    bffRequest<SettlementFileImportResponse>(
      `settlements/imports/${encodeURIComponent(importId)}/reject`,
      { method: "POST", body },
    ),
  runImportTieOut: (importId: string, body: RunTieOutRequest) =>
    bffRequest<TieOutRunResponse>(`settlements/imports/${encodeURIComponent(importId)}/tie-out`, {
      method: "POST",
      body,
    }),

  // Unified manual-import batches (US-554): one cycle for statement
  // (B1) + settlement (P2) imports. Scopes are per kind — banking:import
  // for statements, settlement:manage for settlements.
  detectImportFile: (body: DetectImportFileRequest) =>
    bffRequest<FileDetectionResponse>("imports/batches/detect", {
      method: "POST",
      body,
    }),
  uploadImportBatch: (body: ImportBatchUploadRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<UploadImportBatchResponse>("imports/batches", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listImportBatches: (
    params: { kind?: string; status?: string; company?: string; source_import_id?: string } = {},
  ) =>
    bffRequest<ManualImportBatchResponse[]>(
      `imports/batches${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  getImportBatch: (batchId: string) =>
    bffRequest<ManualImportBatchResponse>(`imports/batches/${encodeURIComponent(batchId)}`),
  getImportBatchPreview: (batchId: string) =>
    bffRequest<ImportBatchPreviewResponse>(
      `imports/batches/${encodeURIComponent(batchId)}/preview`,
    ),
  getImportBatchVariance: (batchId: string) =>
    bffRequest<BatchVarianceReportResponse>(
      `imports/batches/${encodeURIComponent(batchId)}/variance`,
    ),
  approveImportBatch: (
    batchId: string,
    body: { auto_post?: boolean } = {},
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ApproveImportBatchResponse>(
      `imports/batches/${encodeURIComponent(batchId)}/approve`,
      { method: "POST", body, idempotencyKey },
    ),
  rejectImportBatch: (
    batchId: string,
    body: { reason: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ManualImportBatchResponse>(
      `imports/batches/${encodeURIComponent(batchId)}/reject`,
      { method: "POST", body, idempotencyKey },
    ),
  rerunImportBatch: (batchId: string, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ManualImportBatchResponse>(
      `imports/batches/${encodeURIComponent(batchId)}/re-run`,
      { method: "POST", idempotencyKey },
    ),
  retryBatchPosting: (batchId: string, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<RetryBatchPostingResponse>(
      `imports/batches/${encodeURIComponent(batchId)}/retry-posting`,
      { method: "POST", idempotencyKey },
    ),
  listSettlementColumnMappings: (
    params: { channel_key?: string; company?: string } = {},
  ) =>
    bffRequest<SettlementColumnMappingResponse[]>(
      `imports/settlement-column-mappings${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  getSettlementColumnMapping: (mappingId: string) =>
    bffRequest<SettlementColumnMappingResponse>(
      `imports/settlement-column-mappings/${encodeURIComponent(mappingId)}`,
    ),
  createSettlementColumnMapping: (
    body: SettlementColumnMappingCreateRequest,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<SettlementColumnMappingResponse>("imports/settlement-column-mappings", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  supersedeSettlementColumnMapping: (mappingId: string, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<SettlementColumnMappingResponse>(
      `imports/settlement-column-mappings/${encodeURIComponent(mappingId)}/supersede`,
      { method: "POST", idempotencyKey },
    ),

  // Settlement events + bank deposits (US-523).
  listSettlementEvents: (
    params: PaginationParams & { channel_key?: string; import_batch_id?: string; sort?: string } = {},
  ) =>
    bffRequest<Paginated<SettlementEventResponse>>(
      `settlements/events${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getSettlementEvent: (eventId: string) =>
    bffRequest<SettlementEventResponse>(`settlements/events/${encodeURIComponent(eventId)}`),
  listEventMatches: (eventId: string) =>
    bffRequest<SettlementMatchResponse[]>(`settlements/events/${encodeURIComponent(eventId)}/matches`),
  recordBankDeposit: (body: RecordDepositRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<RecordDepositResponse>("settlements/deposits", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listBankDeposits: (params: PaginationParams & { unmatched_only?: boolean; sort?: string } = {}) =>
    bffRequest<Paginated<BankDepositResponse>>(
      `settlements/deposits${buildQuery({
        ...params,
        unmatched_only: params.unmatched_only ? "true" : undefined,
      } as Record<string, string | number | undefined>)}`,
    ),
  getBankDeposit: (depositId: string) =>
    bffRequest<BankDepositResponse>(`settlements/deposits/${encodeURIComponent(depositId)}`),
  listDepositMatches: (depositId: string) =>
    bffRequest<SettlementMatchResponse[]>(`settlements/deposits/${encodeURIComponent(depositId)}/matches`),
  matchDepositManually: (
    depositId: string,
    body: { tranche_id: string; allocated_amount_irr: number; company_id?: string | null; actor?: string | null },
  ) =>
    bffRequest<RecordDepositResponse>(`settlements/deposits/${encodeURIComponent(depositId)}/match`, {
      method: "POST",
      body,
    }),

  // -- cheque center (US-535, Phase 5-4) ------------------------------------
  // The BFF catch-all proxies these to /api/v1/cheques/* — no dedicated
  // BFF route is needed. Transitions post through cheques:transition;
  // reads through cheques:read; manual registration through
  // cheques:write. Amounts in the create body are Toman (x10 to Rial
  // at the backend boundary); dates are Jalali YYYY-MM-DD.
  // The list + widget reads accept an optional `company` filter
  // (US-551, API company id); absent, the backend answers tenant-wide.
  listCheques: (
    params: {
      state?: string;
      issuer_national_id?: string;
      due_before?: string;
      kind?: string;
      purpose?: string;
      endorsee_type?: string;
      storage_location?: string;
      company?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    bffRequest<ChequeResponse[]>(
      `cheques${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getCheque: (chequeId: string) =>
    bffRequest<ChequeDetailResponse>(`cheques/${encodeURIComponent(chequeId)}`),
  createCheque: (body: CreateChequeRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ChequeResponse>("cheques", { method: "POST", body, idempotencyKey }),
  getChequeSettings: () => bffRequest<ChequeSettingsResponse>("cheques/settings"),
  listCheckbooks: (companyEngineId: number) =>
    bffRequest<CheckbookResponse[]>(
      `cheques/checkbooks${buildQuery({ company_id: companyEngineId })}`,
    ),
  getCheckbook: (checkbookId: number, companyEngineId: number) =>
    bffRequest<CheckbookResponse>(
      `cheques/checkbooks/${checkbookId}${buildQuery({ company_id: companyEngineId })}`,
    ),
  getCollateralSummary: (params: { company?: string } = {}) =>
    bffRequest<CollateralSummaryResponse>(
      `cheques/collateral-summary${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  getEndorsementSummary: () =>
    bffRequest<EndorsementSummaryResponse>("cheques/endorsement-summary"),
  listChequeIntake: (params: { limit?: number; offset?: number } = {}) =>
    bffRequest<ChequeResponse[]>(
      `cheques/intake${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  confirmChequeReceipt: (
    chequeId: string,
    body: ConfirmReceiptRequest,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/confirm-receipt`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  depositCheque: (
    chequeId: string,
    body: { destination_account_id: number; destination_bank_name?: string | null },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/deposit`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  clearCheque: (
    chequeId: string,
    body: { statement_reference?: string | null } = {},
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/clear`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  bounceCheque: (
    chequeId: string,
    body: {
      bounce_reason: string;
      bounce_date: string;
      certificate_reference?: string | null;
      bounce_destination?: string | null;
    },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/bounce`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  endorseCheque: (
    chequeId: string,
    body: { endorsee_name: string; endorsee_type: string; sayad_transfer_reference: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/endorse`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  // The return transition takes no body (held -> returned, سند ۶;
  // from bounced it returns the leaf with no journal).
  returnCheque: (chequeId: string, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/return`, {
      method: "POST",
      idempotencyKey,
    }),
  rejectChequeMismatch: (
    chequeId: string,
    body: { mismatch_detail: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/reject-mismatch`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  issueCheque: (
    chequeId: string,
    body: { destination_account_id: number; destination_bank_name?: string | null },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/issue`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  voidIssuedCheque: (
    chequeId: string,
    body: { void_reason: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/void`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  callInCheque: (
    chequeId: string,
    body: { accountant_approval: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/call-in`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  unexpectedCallInCheque: (
    chequeId: string,
    body: { statement_reference: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/unexpected-call-in`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  endorsedBounceCheque: (
    chequeId: string,
    body: { bounce_reason: string; bounce_date: string; certificate_reference: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeResponse>(`cheques/${encodeURIComponent(chequeId)}/endorsed-bounce`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  recordInquiryReceipt: (
    chequeId: string,
    body: InquiryReceiptRequest,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<InquiryReceiptResponse>(`cheques/${encodeURIComponent(chequeId)}/receipts`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  runSayyadInquiry: (
    chequeId: string,
    body: { scope: string; scenario?: string | null },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<InquiryReceiptResponse>(
      `cheques/${encodeURIComponent(chequeId)}/sayyad-inquiries`,
      { method: "POST", body, idempotencyKey },
    ),
  runEhrazinoInquiry: (
    chequeId: string,
    body: { kind: string; scenario?: string | null },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<InquiryReceiptResponse>(
      `cheques/${encodeURIComponent(chequeId)}/ehrazino-inquiries`,
      { method: "POST", body, idempotencyKey },
    ),
  listChequeInquiries: (chequeId: string) =>
    bffRequest<InquiryReceiptResponse[]>(`cheques/${encodeURIComponent(chequeId)}/inquiries`),
  confirmClearingMatch: (
    chequeId: string,
    body: { statement_id: number; line_id: number },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ClearingLineDecisionResponse>(
      `cheques/${encodeURIComponent(chequeId)}/clearing/confirm`,
      { method: "POST", body, idempotencyKey },
    ),
  listInClearingAging: (params: { company?: string } = {}) =>
    bffRequest<ClearingAgingResponse[]>(
      `cheques/reports/in-clearing-aging${buildQuery(params as Record<string, string | undefined>)}`,
    ),
  getInquiryLedger: () => bffRequest<InquiryLedgerResponse>("cheques/reports/inquiry-ledger"),
  getMonthlyMatchTrend: (params: { months?: number } = {}) =>
    bffRequest<MonthlyMatchTrendResponse>(
      `cheques/reports/monthly-match-trend${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  runReceiptRuleAudit: (idempotencyKey = newIdempotencyKey()) =>
    bffRequest<ReceiptRuleAuditResponse>("cheques/reports/receipt-rule-audit", {
      method: "POST",
      idempotencyKey,
    }),
  runChequeClearing: (
    statementId: number,
    body: { bank_key?: string | null } = {},
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ChequeClearingResponse>(`bank/statements/${statementId}/cheque-clearing`, {
      method: "POST",
      body,
      idempotencyKey,
    }),

  // -- installments center (US-545, Phase 5-5 ۵ج) ---------------------------
  // The BFF catch-all proxies these to /api/v1/installments/* and
  // /api/v1/integrations/sms/* — no dedicated BFF route is needed.
  // Reads need installments:read; writes need installments:write;
  // the manual reminder trigger and the SMS registry need
  // sms:manage. Amounts in the create bodies are Toman (x10 to Rial
  // at the backend boundary); dates are Jalali YYYY-MM-DD.
  listInstallmentPlans: (
    params: {
      invoice_id?: string;
      kind?: string;
      status?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    bffRequest<InstallmentPlanResponse[]>(
      `installments/plans${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getInstallmentPlan: (planId: string) =>
    bffRequest<InstallmentPlanDetailResponse>(`installments/plans/${encodeURIComponent(planId)}`),
  /** Overdue legs report (US-541): most overdue first, with dunning stages + accrued late fees. */
  listOverdueInstallments: (
    params: { plan_id?: string; limit?: number; offset?: number } = {},
  ) =>
    bffRequest<OverdueReportResponse>(
      `installments/overdue${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  createInstallmentPlan: (body: CreateInstallmentPlanRequest, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<InstallmentPlanResponse>("installments/plans", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  createInstallmentReceipt: (
    planId: string,
    body: CreateInstallmentReceiptRequest,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<InstallmentReceiptResponse>(`installments/plans/${encodeURIComponent(planId)}/receipts`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  rejectBnplReceipt: (
    planId: string,
    receiptId: string,
    body: { reason: string },
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<InstallmentReceiptResponse>(
      `installments/plans/${encodeURIComponent(planId)}/receipts/${encodeURIComponent(receiptId)}/reject-bnpl`,
      { method: "POST", body, idempotencyKey },
    ),
  listInstallmentContracts: (params: { code?: string; limit?: number; offset?: number } = {}) =>
    bffRequest<InstallmentContractResponse[]>(
      `installments/contracts${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  getInstallmentContract: (contractId: string) =>
    bffRequest<InstallmentContractResponse>(
      `installments/contracts/${encodeURIComponent(contractId)}`,
    ),
  createInstallmentContract: (
    body: CreateInstallmentContractRequest,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<InstallmentContractResponse>("installments/contracts", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  getPayoffQuote: (planId: string, asOf?: string) =>
    bffRequest<PayoffQuoteResponse>(
      `installments/plans/${encodeURIComponent(planId)}/payoff-quote${buildQuery({ as_of: asOf })}`,
    ),
  createEarlySettlement: (
    planId: string,
    body: EarlySettlementRequestBody,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<EarlySettlementResponse>(
      `installments/plans/${encodeURIComponent(planId)}/early-settlement`,
      { method: "POST", body, idempotencyKey },
    ),
  listOverdueLegs: (params: { plan_id?: string; limit?: number; offset?: number } = {}) =>
    bffRequest<OverdueReportResponse>(
      `installments/overdue${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  runDunning: (body: { as_of?: string } = {}, idempotencyKey = newIdempotencyKey()) =>
    bffRequest<DunningRunResponse>("installments/dunning/run", {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listPlanAmendments: (planId: string) =>
    bffRequest<ScheduleAmendmentResponse[]>(
      `installments/plans/${encodeURIComponent(planId)}/amendments`,
    ),
  triggerPlanReminder: (
    planId: string,
    body: TriggerReminderRequestBody,
    idempotencyKey = newIdempotencyKey(),
  ) =>
    bffRequest<ReminderLogResponse>(`installments/plans/${encodeURIComponent(planId)}/remind`, {
      method: "POST",
      body,
      idempotencyKey,
    }),
  listPlanReminders: (
    planId: string,
    params: { status?: string; leg_id?: string; limit?: number; offset?: number } = {},
  ) =>
    bffRequest<ReminderLogResponse[]>(
      `installments/plans/${encodeURIComponent(planId)}/reminders${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  listSmsReminders: (
    params: {
      plan_id?: string;
      leg_id?: string;
      provider_key?: string;
      status?: string;
      trigger?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    bffRequest<ReminderLogResponse[]>(
      `integrations/sms/reminders${buildQuery(params as Record<string, string | number | undefined>)}`,
    ),
  listSmsProviders: () => bffRequest<SmsProviderResponse[]>("integrations/sms/providers"),
};
