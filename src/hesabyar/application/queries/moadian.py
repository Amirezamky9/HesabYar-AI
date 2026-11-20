"""Moadian read use cases (US-404).

REST, MCP and workers all call this service instead of querying the
Moadian tables directly, so Moadian reads have one implementation
and one error contract (design-eng spec §6.2/§6.3, §12.3).

Every read is tenant-scoped: a profile, document or submission of
another tenant looks exactly like one that does not exist.

Reads go through the repository port
(:class:`hesabyar.domain.moadian.ports.MoadianRepositoryPort`) —
never SQLAlchemy or ORM rows (spec §6.3).
"""

from __future__ import annotations

from collections.abc import Sequence

from hesabyar.application.errors import (
    MoadianDocumentNotFoundError,
    MoadianProfileNotFoundError,
    MoadianSubmissionNotFoundError,
)
from hesabyar.domain.moadian.models import (
    DocumentStatus,
    MoadianDocument,
    MoadianEnvironment,
    MoadianProfile,
    MoadianStatusEvent,
    MoadianSubmission,
    SubmissionStatus,
)
from hesabyar.domain.moadian.ports import MoadianRepositoryPort


class MoadianQueryService:
    """Read-only Moadian queries over the HesabYar database."""

    def __init__(self, repository: MoadianRepositoryPort) -> None:
        self._repository = repository

    # -- profiles ----------------------------------------------------------

    def list_profiles(
        self,
        tenant_id: str,
        *,
        environment: MoadianEnvironment | None = None,
        is_active: bool | None = None,
    ) -> Sequence[MoadianProfile]:
        """Return the tenant's profiles, optionally filtered."""
        return self._repository.list_profiles(
            tenant_id,
            environment=environment,
            is_active=is_active,
        )

    def get_profile(self, profile_id: str, tenant_id: str) -> MoadianProfile:
        """Return one profile, or raise MoadianProfileNotFoundError."""
        profile = self._repository.get_profile(profile_id, tenant_id)
        if profile is None:
            raise MoadianProfileNotFoundError(
                f"Moadian profile {profile_id!r} does not exist.",
                details={"moadian_profile_id": profile_id},
            )
        return profile

    # -- documents ---------------------------------------------------------

    def list_documents(
        self,
        tenant_id: str,
        *,
        profile_id: str | None = None,
        status: DocumentStatus | None = None,
    ) -> Sequence[MoadianDocument]:
        """Return the tenant's documents, optionally filtered."""
        return self._repository.list_documents(
            tenant_id,
            profile_id=profile_id,
            status=status,
        )

    def get_document(self, document_id: str, tenant_id: str) -> MoadianDocument:
        """Return one document, or raise MoadianDocumentNotFoundError."""
        document = self._repository.get_document(document_id, tenant_id)
        if document is None:
            raise MoadianDocumentNotFoundError(
                f"Moadian document {document_id!r} does not exist.",
                details={"moadian_document_id": document_id},
            )
        return document

    # -- submissions -------------------------------------------------------

    def list_submissions(
        self,
        tenant_id: str,
        *,
        document_id: str | None = None,
        profile_id: str | None = None,
        status: SubmissionStatus | None = None,
    ) -> Sequence[MoadianSubmission]:
        """Return the tenant's submissions, optionally filtered."""
        return self._repository.list_submissions(
            tenant_id,
            document_id=document_id,
            profile_id=profile_id,
            status=status,
        )

    def get_submission(self, submission_id: str, tenant_id: str) -> MoadianSubmission:
        """Return one submission, or raise MoadianSubmissionNotFoundError."""
        submission = self._repository.get_submission(submission_id, tenant_id)
        if submission is None:
            raise MoadianSubmissionNotFoundError(
                f"Moadian submission {submission_id!r} does not exist.",
                details={"moadian_submission_id": submission_id},
            )
        return submission

    def list_status_events(
        self,
        submission_id: str,
        tenant_id: str,
    ) -> Sequence[MoadianStatusEvent]:
        """Return a submission's status events, oldest first.

        Raises MoadianSubmissionNotFoundError when the submission does
        not exist (or belongs to another tenant) — the events of
        another tenant's submission are never visible.
        """
        self.get_submission(submission_id, tenant_id)
        return self._repository.list_status_events(submission_id)
