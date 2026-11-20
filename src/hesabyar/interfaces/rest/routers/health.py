"""Health, readiness and installation-status routes (spec §7.6, §9.2).

Mounted under ``/api/v1`` by the app factory. Routes stay thin: they
return the health service's results and nothing else.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.dependencies import (
    get_health_service,
    get_settings_dep,
    require_admin,
)
from hesabyar.interfaces.rest.schemas.health import (
    HealthResponse,
    InstallationResponse,
    ReadinessResponse,
)
from hesabyar.interfaces.rest.services import HealthService

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health(settings: AppSettings = Depends(get_settings_dep)) -> HealthResponse:
    """Return service status. The process is up; no dependency is checked."""
    return HealthResponse(environment=settings.env.value)


@router.get("/readiness", response_model=ReadinessResponse, summary="Readiness probe")
def readiness(service: HealthService = Depends(get_health_service)) -> JSONResponse:
    """Check the HesabYar DB and the Tryton adapter; 503 when not ready."""
    result = service.readiness()
    status_code = 200 if result.status == "ready" else 503
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))


@router.get(
    "/installation",
    response_model=InstallationResponse,
    summary="Installation and schema status (admin only)",
)
def installation(
    service: HealthService = Depends(get_health_service),
    _admin: AuthenticatedClient = Depends(require_admin),
) -> InstallationResponse:
    """Report Alembic revision, Tryton release and modules (read-only)."""
    return service.installation_status()
