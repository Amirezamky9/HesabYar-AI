# HesabYar Accounting & Compliance Engine

Deterministic double-entry accounting ledger, standards validation, and regulatory Iranian tax compliance system.

## Language

**Tenant**:
An isolated organization or legal entity whose accounting records, vouchers, chart of accounts, and compliance filings are strictly segregated from all other entities.
_Avoid_: Organization, workspace, company, client, account

**Actor**:
An authenticated human user, service account, or automated agent executing actions within a specific tenant context under defined permission scopes.
_Avoid_: User, operator, principal, member

**MoneyIRR**:
An exact monetary amount denominated exclusively in Iranian Rials with integer precision, prohibiting floating-point numbers and implicit Toman conversions.
_Avoid_: Currency, amount, price, Rial, Toman

**FiscalDate**:
An accounting calendar date canonically stored in Gregorian form with exact bidirectional mappings to Solar Hijri (Jalali).
_Avoid_: Date, ShamsiDate, PersianDate, timestamp
