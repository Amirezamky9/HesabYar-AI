"""Registry-backed webhook-secret lookup for the receiver (US-511).

The F3 store registry owns webhook secrets (encrypted in the
database, shown to the operator exactly once at creation/rotation);
this :class:`WebhookSecretPort` implementation decrypts them through
the credential vault so the receiver can verify HMAC signatures for
real instead of skipping the check (the F1
:class:`NoWebhookSecretStore` default).

Lookup is installation-scoped: store keys are unique per
installation, so the store is found by ``(installation_id,
source_system)`` — the store key IS the connector name. One secret
per connector: a connector with several ``source_store_id`` values
shares its secret (the registry models one store row per connector;
per-store-id secrets are a follow-up if multi-store connectors need
them).

Fail-closed: a store WITH a secret whose ciphertext is missing or
undecryptable (no vault key configured, wrong key, tampered data)
raises :class:`WebhookVaultUnavailableError` — the ingest fails
instead of silently skipping verification. A store with no secret
configured returns None (skip + record, as before).
"""

from __future__ import annotations

from hesabyar.domain.events.errors import WebhookVaultUnavailableError
from hesabyar.domain.integrations.ports import CredentialVaultPort, IntegrationsRegistryPort


class RegistryWebhookSecretStore:
    """Decrypt store webhook secrets from the registry (F3 + vault)."""

    def __init__(
        self,
        registry: IntegrationsRegistryPort,
        vault: CredentialVaultPort | None,
    ) -> None:
        self._registry = registry
        self._vault = vault

    def get_secret(
        self,
        source_system: str,
        source_store_id: str,
        installation_id: str | None = None,
    ) -> str | None:
        """Return the store's webhook secret, or None when none is configured.

        ``installation_id`` scopes the lookup; without it no store
        can be identified, so the lookup returns None (the ingest
        always passes the installation from the API key).
        """
        if installation_id is None:
            return None
        store = self._registry.get_store(installation_id, source_system)
        if store is None or not store.has_webhook_secret:
            return None
        ciphertext = self._registry.get_store_ciphertext(store.id, "webhook_secret")
        if ciphertext is None:
            raise WebhookVaultUnavailableError(
                f"Store {source_system!r} reports a webhook secret, but no "
                "ciphertext is stored for it; signature verification is impossible.",
                details={"source_system": source_system},
            )
        if self._vault is None:
            raise WebhookVaultUnavailableError(
                f"Store {source_system!r} has a webhook secret, but no credential "
                "vault is configured to decrypt it; signature verification is impossible.",
                details={"source_system": source_system},
            )
        try:
            return self._vault.decrypt(ciphertext)
        except ValueError as exc:
            raise WebhookVaultUnavailableError(
                f"Store {source_system!r} webhook secret cannot be decrypted "
                "(wrong key or tampered data); signature verification is impossible.",
                details={"source_system": source_system},
            ) from exc
