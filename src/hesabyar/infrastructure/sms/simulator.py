"""The SMS simulator (US-544).

The default provider in dev/test — and the fail-safe default for
every installation until a live provider is configured. It sends
nothing: it logs the message (recipient, sender line, text) and
returns a fake delivery id (``sim-<12 hex>``) with status SENT.

The log row a simulated send produces is indistinguishable in
shape from a live send's row — provider, cost, delivery status —
except its ``provider_key`` is ``simulator`` and its cost is
None (no money moved). That is the point: every reminder flow
(scheduled job, manual trigger, dry-run, failure filing) is
exercised end-to-end without a live provider, and an installation
that never configures a live provider fails SAFE (logged
simulations) rather than failing SILENT or sending unexpectedly.
"""

from __future__ import annotations

import logging
import uuid

from hesabyar.domain.sms.models import PROVIDER_KIND_SIMULATOR, SmsDeliveryStatus
from hesabyar.domain.sms.ports import SmsDeliveryUpdate, SmsSendResult

logger = logging.getLogger(__name__)


class SimulatorSmsClient:
    """Logs the message and returns a fake delivery id. Sends nothing."""

    @property
    def provider_kind(self) -> str:
        return PROVIDER_KIND_SIMULATOR

    def send(
        self, *, to: str, message: str, sender_line: str | None
    ) -> SmsSendResult:
        """Log the simulated send and return a fake delivery id."""
        delivery_id = f"sim-{uuid.uuid4().hex[:12]}"
        logger.info(
            "SIMULATOR SMS %s to=%s sender=%s message=%r",
            delivery_id,
            to,
            sender_line or "(default)",
            message,
        )
        return SmsSendResult(
            provider_message_id=delivery_id,
            status=SmsDeliveryStatus.SENT,
            provider_status_raw="simulated",
        )

    def get_status(self, provider_message_id: str) -> SmsDeliveryUpdate:
        """Simulated messages are always SENT (there is no real delivery)."""
        return SmsDeliveryUpdate(
            provider_message_id=provider_message_id,
            status=SmsDeliveryStatus.SENT,
            provider_status_raw="simulated",
        )

    def close(self) -> None:
        """No resources to release."""
        return None
