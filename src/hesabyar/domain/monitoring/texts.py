"""Persian alert texts (US-704).

Fixed Persian text, built by pure functions — NOT user-editable
templates (the same rule as the reminder messages in
``hesabyar.domain.reminders.messages``): an alert's wording is an
operational fact, and a mistyped template must never silence one.

Rendering rules (the US-544 rules, reused):
- counts, day counts and rates render in Persian digits;
- money renders in Rial (never Toman — no silent conversion);
- identifiers (token labels, dedup keys) render verbatim.
"""

from __future__ import annotations

from hesabyar.domain.sms.templates import to_persian_digits

#: Documentation reference carried on every alert that has a runbook.
#: A docs path + section, not a URL — there is no deployed docs site.
RUNBOOK_INBOX_STALE = "docs/monitoring.md — بخش «صندوق ورودی گیرکرده»"
RUNBOOK_WORKER_LAG = "docs/monitoring.md — بخش «کارگر (Worker) پاسخ نمی‌دهد»"
RUNBOOK_FINTECH_ROTATION = "docs/monitoring.md — بخش «چرخش توکن فینوتک»"
RUNBOOK_API_KEY_ROTATION = "docs/monitoring.md — بخش «چرخش کلید API»"
RUNBOOK_SETTLEMENT_DRIFT = "docs/monitoring.md — بخش «مغایرت تسویه»"
RUNBOOK_WEBHOOK_FAILED = "docs/monitoring.md — بخش «وب‌هوک‌های ناموفق»"
RUNBOOK_BACKUP_FAILED = "docs/monitoring.md — بخش «پشتیبان‌گیری ناموفق»"
RUNBOOK_ERROR_RATE = "docs/monitoring.md — بخش «نرخ خطای ۵xx»"


def inbox_stale_text(*, count: int, hours: int) -> tuple[str, str]:
    """Title + message for the inbox-staleness alert."""
    return (
        "صندوق ورودی گیرکرده است",
        f"{to_persian_digits(count)} رویداد بیش از {to_persian_digits(hours)} ساعت "
        "در صندوق ورودی پردازش نشده‌اند. صف بازبینی را بررسی کنید.",
    )


def worker_lag_text(*, lag_minutes: int) -> tuple[str, str]:
    """Title + message for the worker-lag alert (a sweep ran, but long ago)."""
    return (
        "کارگر عقب مانده است",
        f"آخرین اجرای جاروی رویدادها {to_persian_digits(lag_minutes)} دقیقه پیش بوده است. فرایند کارگر را بررسی کنید.",
    )


def worker_never_ran_text() -> tuple[str, str]:
    """Title + message when no sweep run was ever recorded."""
    return (
        "کارگر هنوز اجرا نشده است",
        "هیچ اجرای ثبت‌شده‌ای برای جاروی رویدادها وجود ندارد؛ فرایند کارگر را راه‌اندازی کنید.",
    )


def token_expiring_text(*, label: str, days_left: int, kind: str) -> tuple[str, str]:
    """Title + message for a token-expiry alert.

    ``days_left`` is negative past expiry; ``kind`` is the token kind
    (``fintech`` / ``api_key`` / ``other``) used only for the runbook
    choice, not rendered.
    """
    if days_left < 0:
        return (
            "توکن منقضی شده است",
            f"توکن «{label}» {to_persian_digits(abs(days_left))} روز پیش منقضی شده است. فوراً آن را بچرخانید.",
        )
    if days_left == 0:
        return (
            "توکن امروز منقضی می‌شود",
            f"توکن «{label}» امروز منقضی می‌شود. آن را بچرخانید.",
        )
    return (
        "توکن نزدیک به انقضاست",
        f"توکن «{label}» تا {to_persian_digits(days_left)} روز دیگر منقضی می‌شود. چرخش آن را برنامه‌ریزی کنید.",
    )


def settlement_drift_text(*, count: int, total_irr: int) -> tuple[str, str]:
    """Title + message for the settlement-drift alert (Rial, digits=0)."""
    return (
        "مغایرت تسویه",
        f"{to_persian_digits(count)} مغایرت باز تسویه با مجموع اختلاف "
        f"{to_persian_digits(f'{total_irr:,}')} ریال وجود دارد. "
        "مرکز تسویه را بررسی کنید.",
    )


def webhook_failed_text(*, count: int) -> tuple[str, str]:
    """Title + message for the failed-webhooks alert."""
    return (
        "وب‌هوک‌های ناموفق",
        f"{to_persian_digits(count)} تلاش ناموفق وب‌هوک در ۲۴ ساعت گذشته ثبت شده است. گزارش یکپارچه‌سازی را بررسی کنید.",
    )


def backup_failed_text(*, database: str, error: str | None) -> tuple[str, str]:
    """Title + message for a backup-failure alert."""
    label = "حسابیار" if database == "hesabyar" else "ترایتون"
    detail = f" خطا: {error}" if error else ""
    return (
        "پشتیبان‌گیری ناموفق بود",
        f"آخرین پشتیبان‌گیری پایگاه دادهٔ {label} ناموفق بود.{detail}",
    )


def backup_stale_text(*, database: str, hours: int) -> tuple[str, str]:
    """Title + message when a database has no recent successful backup."""
    label = "حسابیار" if database == "hesabyar" else "ترایتون"
    return (
        "پشتیبان‌گیری قدیمی است",
        f"پایگاه دادهٔ {label} بیش از {to_persian_digits(hours)} ساعت است پشتیبان موفق نداشته است.",
    )


def error_rate_text(*, rate_percent: str, total: int) -> tuple[str, str]:
    """Title + message for the 5xx-rate alert.

    ``rate_percent`` arrives pre-formatted (Persian digits, e.g.
    «۷٫۵٪») — formatting lives in the rule, not here.
    """
    return (
        "نرخ خطای سرور بالاست",
        f"نرخ خطای ۵xx در {to_persian_digits(total)} درخواست اخیر {rate_percent} است. گزارش‌های سرور را بررسی کنید.",
    )
