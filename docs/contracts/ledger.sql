-- HesabYar architecture contract R1: foundation ledger, NOT a production migration.
-- SQLite >= 3.37. Application obligations are in domain-contracts.md.
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;
CREATE TABLE book (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    entity_id TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL CHECK (currency = 'IRR')
) STRICT;
CREATE TRIGGER book_no_update BEFORE UPDATE ON book
BEGIN SELECT RAISE(ABORT,'BOOK_IDENTITY_IMMUTABLE'); END;
CREATE TRIGGER book_no_delete BEFORE DELETE ON book
BEGIN SELECT RAISE(ABORT,'BOOK_IDENTITY_IMMUTABLE'); END;
CREATE TABLE fiscal_years (
    id TEXT PRIMARY KEY,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    CHECK (date(starts_on) IS NOT NULL AND date(starts_on) = starts_on),
    CHECK (date(ends_on) IS NOT NULL AND date(ends_on) = ends_on),
    CHECK (starts_on <= ends_on)
) STRICT;
CREATE TABLE periods (
    id TEXT PRIMARY KEY,
    fiscal_year_id TEXT NOT NULL REFERENCES fiscal_years(id) ON DELETE RESTRICT,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    UNIQUE (id, fiscal_year_id),
    CHECK (date(starts_on) IS NOT NULL AND date(starts_on) = starts_on),
    CHECK (date(ends_on) IS NOT NULL AND date(ends_on) = ends_on),
    CHECK (starts_on <= ends_on)
) STRICT;
CREATE TABLE accounts (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('group', 'kol', 'moein')),
    parent_code TEXT REFERENCES accounts(code) ON DELETE RESTRICT,
    requires_tafsili INTEGER NOT NULL DEFAULT 0 CHECK (requires_tafsili IN (0,1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
) STRICT;
CREATE TABLE tafsili_accounts (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('person','company','bank','project','cost_center','shareholder')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
) STRICT;
CREATE TABLE journal_vouchers (
    id TEXT PRIMARY KEY,
    fiscal_year_id TEXT NOT NULL REFERENCES fiscal_years(id) ON DELETE RESTRICT,
    period_id TEXT NOT NULL,
    accounting_date TEXT NOT NULL,
    voucher_number INTEGER,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    approved_by TEXT,
    posted_at TEXT,
    reversal_of TEXT UNIQUE REFERENCES journal_vouchers(id) ON DELETE RESTRICT,
    UNIQUE (fiscal_year_id, voucher_number),
    FOREIGN KEY (period_id, fiscal_year_id) REFERENCES periods(id, fiscal_year_id) ON DELETE RESTRICT,
    CHECK (date(accounting_date) IS NOT NULL AND date(accounting_date) = accounting_date),
    CHECK (voucher_number IS NULL OR voucher_number > 0),
    CHECK (status != 'posted' OR (voucher_number IS NOT NULL AND approved_by IS NOT NULL AND posted_at IS NOT NULL))
) STRICT;
CREATE TABLE voucher_items (
    id TEXT PRIMARY KEY,
    voucher_id TEXT NOT NULL REFERENCES journal_vouchers(id) ON DELETE RESTRICT,
    row_order INTEGER NOT NULL CHECK (row_order > 0),
    account_code TEXT NOT NULL REFERENCES accounts(code) ON DELETE RESTRICT,
    tafsili_code TEXT REFERENCES tafsili_accounts(code) ON DELETE RESTRICT,
    description TEXT NOT NULL,
    debit_irr INTEGER NOT NULL DEFAULT 0 CHECK (debit_irr BETWEEN 0 AND 9000000000000000),
    credit_irr INTEGER NOT NULL DEFAULT 0 CHECK (credit_irr BETWEEN 0 AND 9000000000000000),
    UNIQUE (voucher_id, row_order),
    CHECK ((debit_irr > 0 AND credit_irr = 0) OR (credit_irr > 0 AND debit_irr = 0))
) STRICT;
-- Extra simultaneous analytical dimensions are separate from the primary tafsili.
CREATE TABLE item_dimensions (
    item_id TEXT NOT NULL REFERENCES voucher_items(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('party','bank','project','cost_center','shareholder')),
    tafsili_code TEXT NOT NULL REFERENCES tafsili_accounts(code) ON DELETE RESTRICT,
    PRIMARY KEY (item_id, role)
) STRICT;
CREATE TABLE audit_events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    previous_sha256 TEXT NOT NULL CHECK (length(previous_sha256) = 64),
    event_sha256 TEXT NOT NULL CHECK (length(event_sha256) = 64)
) STRICT;
CREATE VIEW voucher_totals AS
SELECT v.id, COALESCE(SUM(i.debit_irr),0) AS total_debit_irr,
       COALESCE(SUM(i.credit_irr),0) AS total_credit_irr, COUNT(i.id) AS item_count
FROM journal_vouchers v LEFT JOIN voucher_items i ON i.voucher_id=v.id GROUP BY v.id;
CREATE TRIGGER no_direct_post BEFORE INSERT ON journal_vouchers
WHEN NEW.status != 'draft' BEGIN SELECT RAISE(ABORT,'CREATE_DRAFT_FIRST'); END;
CREATE TRIGGER no_posted_voucher_update BEFORE UPDATE ON journal_vouchers
WHEN OLD.status='posted' BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER no_posted_voucher_delete BEFORE DELETE ON journal_vouchers
WHEN OLD.status='posted' BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER posting_guard BEFORE UPDATE OF status ON journal_vouchers
WHEN NEW.status='posted' BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM book WHERE id=1)
        THEN RAISE(ABORT,'BOOK_REQUIRED') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM periods p JOIN fiscal_years y ON y.id=p.fiscal_year_id
        WHERE p.id=NEW.period_id AND p.fiscal_year_id=NEW.fiscal_year_id AND p.state='open'
          AND p.starts_on>=y.starts_on AND p.ends_on<=y.ends_on
          AND NEW.accounting_date BETWEEN p.starts_on AND p.ends_on)
        THEN RAISE(ABORT,'PERIOD_NOT_OPEN_OR_DATE_INVALID') END;
    SELECT CASE WHEN (SELECT COUNT(*) FROM voucher_items WHERE voucher_id=NEW.id) NOT BETWEEN 2 AND 1000
        THEN RAISE(ABORT,'INVALID_LINE_COUNT') END;
    SELECT CASE WHEN (SELECT SUM(debit_irr) != SUM(credit_irr) OR SUM(debit_irr)>9000000000000000
        FROM voucher_items WHERE voucher_id=NEW.id)
        THEN RAISE(ABORT,'UNBALANCED_OR_OUT_OF_RANGE') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM voucher_items i JOIN accounts a ON a.code=i.account_code
        LEFT JOIN tafsili_accounts t ON t.code=i.tafsili_code
        WHERE i.voucher_id=NEW.id AND (a.level!='moein' OR a.is_active!=1
            OR (a.requires_tafsili=1 AND i.tafsili_code IS NULL)
            OR (i.tafsili_code IS NOT NULL AND COALESCE(t.is_active,0)!=1)))
        THEN RAISE(ABORT,'INVALID_POSTING_ACCOUNT_OR_TAFSILI') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM item_dimensions d JOIN voucher_items i ON i.id=d.item_id
        JOIN tafsili_accounts t ON t.code=d.tafsili_code
        WHERE i.voucher_id=NEW.id AND (t.is_active!=1 OR
          NOT ((d.role='party' AND t.kind IN ('person','company')) OR d.role=t.kind)))
        THEN RAISE(ABORT,'INVALID_DIMENSION') END;
    SELECT CASE WHEN NEW.reversal_of IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM journal_vouchers WHERE id=NEW.reversal_of AND status='posted')
        THEN RAISE(ABORT,'REVERSAL_REQUIRES_POSTED_ORIGINAL') END;
END;
CREATE TRIGGER no_posted_item_insert BEFORE INSERT ON voucher_items
WHEN (SELECT status FROM journal_vouchers WHERE id=NEW.voucher_id)='posted'
BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER no_posted_item_update BEFORE UPDATE ON voucher_items
WHEN EXISTS (SELECT 1 FROM journal_vouchers WHERE id IN (OLD.voucher_id,NEW.voucher_id) AND status='posted')
BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER no_posted_item_delete BEFORE DELETE ON voucher_items
WHEN (SELECT status FROM journal_vouchers WHERE id=OLD.voucher_id)='posted'
BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER no_posted_dimension_insert BEFORE INSERT ON item_dimensions
WHEN EXISTS (SELECT 1 FROM voucher_items i JOIN journal_vouchers v ON v.id=i.voucher_id
             WHERE i.id=NEW.item_id AND v.status='posted')
BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER no_posted_dimension_update BEFORE UPDATE ON item_dimensions
WHEN EXISTS (SELECT 1 FROM voucher_items i JOIN journal_vouchers v ON v.id=i.voucher_id
             WHERE i.id IN (OLD.item_id,NEW.item_id) AND v.status='posted')
BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER no_posted_dimension_delete BEFORE DELETE ON item_dimensions
WHEN EXISTS (SELECT 1 FROM voucher_items i JOIN journal_vouchers v ON v.id=i.voucher_id
             WHERE i.id=OLD.item_id AND v.status='posted')
BEGIN SELECT RAISE(ABORT,'POSTED_IMMUTABLE'); END;
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT,'AUDIT_APPEND_ONLY'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT,'AUDIT_APPEND_ONLY'); END;
CREATE TRIGGER audit_chain BEFORE INSERT ON audit_events BEGIN
    SELECT CASE WHEN NEW.seq != COALESCE((SELECT MAX(seq)+1 FROM audit_events),1)
        THEN RAISE(ABORT,'AUDIT_SEQUENCE') END;
    SELECT CASE WHEN NEW.previous_sha256 != COALESCE((SELECT event_sha256 FROM audit_events ORDER BY seq DESC LIMIT 1),printf('%064d',0))
        THEN RAISE(ABORT,'AUDIT_CHAIN') END;
END;
-- The DB owner can remove triggers or rewrite the file. Hashes are tamper evidence,
-- NOT tamper prevention or proof of receipt by a tax authority.
-- Application MUST add: atomic audit/posting, actor authorization and approval hash,
-- exact reversal matching incl. dimensions, number allocation, date/year overlap
-- checks, immutable account snapshots, idempotency payload conflict detection,
-- backup/restore verification and migration tests before this becomes runtime code.
