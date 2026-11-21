"""FastAPI application factory (spec §6.2, §9.1).

``create_app()`` builds the versioned API under ``/api/v1`` with
generated OpenAPI docs (``/docs``, ``/openapi.json``), an ``X-Request-ID``
on every response, and one typed problem-details envelope for all errors.

Run the dev server with::

    uv run uvicorn hesabyar.interfaces.rest.app:app --port 8001

(port 8000 is the Tryton server in the local dev environment).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from hesabyar import __version__
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.dependencies import get_settings_dep
from hesabyar.interfaces.rest.errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from hesabyar.interfaces.rest.middleware import RequestIdMiddleware
from hesabyar.interfaces.rest.routers.accounting import router as accounting_router
from hesabyar.interfaces.rest.routers.audit import router as audit_router
from hesabyar.interfaces.rest.routers.bank import router as bank_router
from hesabyar.interfaces.rest.routers.bank_rules import router as bank_rules_router
from hesabyar.interfaces.rest.routers.cashbox import router as cashbox_router
from hesabyar.interfaces.rest.routers.cheques import router as cheques_router
from hesabyar.interfaces.rest.routers.companies import router as companies_router
from hesabyar.interfaces.rest.routers.gateways import router as gateways_router
from hesabyar.interfaces.rest.routers.health import router as health_router
from hesabyar.interfaces.rest.routers.imports import router as imports_router
from hesabyar.interfaces.rest.routers.installments import router as installments_router
from hesabyar.interfaces.rest.routers.integrations import router as integrations_router
from hesabyar.interfaces.rest.routers.invoices import router as invoices_router
from hesabyar.interfaces.rest.routers.jobs import router as jobs_router
from hesabyar.interfaces.rest.routers.journals import router as journals_router
from hesabyar.interfaces.rest.routers.manual_entries import router as manual_entries_router
from hesabyar.interfaces.rest.routers.mcp import router as mcp_router
from hesabyar.interfaces.rest.routers.moadian import router as moadian_router
from hesabyar.interfaces.rest.routers.packaging import router as packaging_router
from hesabyar.interfaces.rest.routers.payments import router as payments_router
from hesabyar.interfaces.rest.routers.recurring import router as recurring_router
from hesabyar.interfaces.rest.routers.reminders import router as reminders_router
from hesabyar.interfaces.rest.routers.reports import router as reports_router
from hesabyar.interfaces.rest.routers.review_queue import router as review_queue_router
from hesabyar.interfaces.rest.routers.rules import router as rules_router
from hesabyar.interfaces.rest.routers.settlement import router as settlement_router
from hesabyar.interfaces.rest.routers.sms import router as sms_router
from hesabyar.interfaces.rest.routers.standards import router as standards_router
from hesabyar.interfaces.rest.routers.tax_policy import router as tax_policy_router


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Create the HesabYar FastAPI application.

    ``settings`` is an explicit override (used by tests); when omitted,
    settings are loaded from the environment per request via
    :func:`hesabyar.infrastructure.config.get_settings`.
    """
    app = FastAPI(
        title="HesabYar API",
        description=(
            "Deterministic Iranian accounting ledger and tax compliance "
            "engine. Tryton is the canonical ledger; HesabYar adds Iranian "
            "standards, tax policy and Moadian on top."
        ),
        version=__version__,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(companies_router, prefix="/api/v1")
    app.include_router(accounting_router, prefix="/api/v1")
    app.include_router(journals_router, prefix="/api/v1")
    app.include_router(invoices_router, prefix="/api/v1")
    app.include_router(payments_router, prefix="/api/v1")
    app.include_router(bank_router, prefix="/api/v1")
    app.include_router(bank_rules_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(standards_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(tax_policy_router, prefix="/api/v1")
    app.include_router(moadian_router, prefix="/api/v1")
    app.include_router(integrations_router, prefix="/api/v1")
    app.include_router(manual_entries_router, prefix="/api/v1")
    app.include_router(review_queue_router, prefix="/api/v1")
    app.include_router(packaging_router, prefix="/api/v1")
    app.include_router(cashbox_router, prefix="/api/v1")
    app.include_router(settlement_router, prefix="/api/v1")
    app.include_router(imports_router, prefix="/api/v1")
    app.include_router(gateways_router, prefix="/api/v1")
    app.include_router(cheques_router, prefix="/api/v1")
    app.include_router(installments_router, prefix="/api/v1")
    app.include_router(sms_router, prefix="/api/v1")
    app.include_router(reminders_router, prefix="/api/v1")
    app.include_router(jobs_router, prefix="/api/v1")
    app.include_router(rules_router, prefix="/api/v1")
    app.include_router(recurring_router, prefix="/api/v1")
    app.include_router(mcp_router, prefix="/api/v1")
    if settings is not None:
        app.dependency_overrides[get_settings_dep] = lambda: settings
    return app


# Module-level app for uvicorn: `uvicorn hesabyar.interfaces.rest.app:app`.
app = create_app()


def main() -> None:
    """Run the API with uvicorn (``python -m hesabyar.interfaces.rest.app``)."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
