"""Sayyad inquiry simulators (US-534, phased design §5 §1.3/§1.4).

Deterministic, fixture-based stand-ins for the Fintech Sayyad API
(legal tier) and the Ehrazino API (natural tier), following the
US-404 Moadian simulator pattern: no network calls, every output a
pure function of the inputs plus the simulator's in-memory registry,
every result clearly marked ``simulated=True`` so nothing here can
be mistaken for an official response.

- :class:`DeterministicFintechSayyadSimulator` answers all 18
  documented Fintech scopes by NAME only — the scope field shapes
  are UNVERIFIED (§9.2 #1–3), so no request/response field is
  guessed: the simulator takes a ``sayad_id`` and a seeded
  :class:`SayyadScenario` and returns the verdict (status / colour
  code / item-match / back-cheques / transfer chain) for that scope.
  An unseeded sayad_id answers ``not_found`` (a fixture must be
  seeded first — the simulator never invents a verdict).
- :class:`DeterministicEhrazinoSimulator` answers the two verified
  Ehrazino shapes: ``cheque-color`` (``nationalCode`` -> colour
  name + code 1–5, 1,700 Toman) and ``sayad-check`` (``sayadId`` ->
  items-match verdict + leaf serial/series, 6,900 Toman). Fixtures
  are keyed by the SHA-256 of the national id, never by the id
  itself, and results never echo it (§3.5).

Live clients (:class:`FintechSayyadLiveClient`,
:class:`EhrazinoLiveClient`) exist as the seam B3 will fill: they
require credentials + a base URL, and until the transport is
verified (Fintech field shapes, Ehrazino endpoint/auth) every call
raises :class:`SayyadLiveNotReadyError` instead of guessing a
request shape. No production call happens from this module.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from hesabyar.domain.cheques.models import (
    EHRAZINO_CHEQUE_COLOR_COST_IRR,
    EHRAZINO_SAYAD_CHECK_COST_IRR,
    InquiryChannel,
    InquiryType,
)
from hesabyar.domain.cheques.sayyad import (
    EHRAZINO_CHEQUE_COLOR_SHAPE,
    EHRAZINO_SAYAD_CHECK_SHAPE,
    FINNOTECH_SAYYAD_SCOPES,
    EhrazinoColorResult,
    EhrazinoSayadCheckResult,
    SayyadInquiryError,
    SayyadInquiryResult,
    SayyadLiveNotReadyError,
    SayyadScope,
    inquiry_type_for_scope,
)

#: Namespace for deterministic simulator references (uuid5).
_REFERENCE_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

#: Colour names on the 1–5 scale (display only; decisions use the code).
COLOR_NAMES: dict[int, str] = {
    1: "white",
    2: "yellow",
    3: "orange",
    4: "brown",
    5: "red",
}


def deterministic_reference(scope: str, key: str) -> str:
    """Derive a stable simulated tracking reference for a scope + key."""
    return f"SIM-{uuid.uuid5(_REFERENCE_NAMESPACE, f'sayyad-sim:{scope}:{key}')}"


def _hash_national_id(national_id: str) -> str:
    """Hash a national id for fixture lookup (never stored raw, §3.5)."""
    return hashlib.sha256(national_id.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SayyadScenario:
    """One seeded fixture for a Fintech simulator inquiry.

    All verdict fields; nothing here is personal data (no names, no
    national ids). ``status`` is the Sayyad leaf status — NOTE the
    two 'in-transit' meanings (§1.3): this is the Sayyad in-transit
    (a leaf issued/transferred but not yet collected), NEVER the
    accounting ``in_clearing`` state; clearing status comes only
    from the bank statement.
    """

    color_code: int = 1
    back_cheques_count: int = 0
    status: str = "registered"  # 'registered' | 'in_transit' | 'transferred' | 'not_found'
    items_matched: bool = True
    transfers: int = 0
    leaves_used: int = 0
    leaves_remaining: int = 25
    last_bounce_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EhrazinoColorScenario:
    """One seeded fixture for an Ehrazino colour inquiry."""

    color_code: int = 1


@dataclass(frozen=True, slots=True)
class EhrazinoSayadCheckScenario:
    """One seeded fixture for an Ehrazino sayad-check inquiry."""

    items_matched: bool = True
    serial_no: str | None = None
    series_no: str | None = None
    branch_code: str | None = None
    check_type: str | None = None
    returned_cheques: bool = False


class DeterministicFintechSayyadSimulator:
    """In-memory, deterministic stand-in for the Fintech Sayyad API.

    No network, no credentials: ``seed()`` a scenario per sayad_id,
    then ``inquire()`` returns that scenario's verdict for the
    requested scope. Unseeded ids answer ``not_found``.
    """

    def __init__(self) -> None:
        self._scenarios: dict[str, SayyadScenario] = {}

    def reset(self) -> None:
        """Clear all seeded scenarios (test isolation)."""
        self._scenarios.clear()

    def seed(self, sayad_id: str, scenario: SayyadScenario) -> None:
        """Register the verdict fixture for one sayad_id."""
        self._scenarios[sayad_id] = scenario

    @property
    def seeded_sayad_ids(self) -> frozenset[str]:
        """Sayad ids with a fixture (introspection for tests)."""
        return frozenset(self._scenarios)

    def available_scopes(self) -> tuple[str, ...]:
        """The 18 documented Fintech Sayyad scope names."""
        return FINNOTECH_SAYYAD_SCOPES

    def inquire(
        self,
        scope: str,
        *,
        sayad_id: str,
        national_id: str | None = None,
        scenario: str | None = None,
    ) -> SayyadInquiryResult:
        """Return the seeded verdict for one scope (simulated).

        ``scenario`` selects a named built-in fixture when no seed
        exists: ``"clean"`` (white, no back-cheques) or ``"red"``
        (red, 12 back-cheques). Anything else unseeded is
        ``not_found`` — the simulator never invents a verdict.
        """
        inquiry_type = inquiry_type_for_scope(scope)
        reference = deterministic_reference(scope, sayad_id)
        fixture = self._scenarios.get(sayad_id)
        if fixture is None and scenario in ("clean", "red"):
            fixture = (
                SayyadScenario()
                if scenario == "clean"
                else SayyadScenario(
                    color_code=5,
                    back_cheques_count=12,
                    status="in_transit",
                    last_bounce_reason="insufficient_funds",
                )
            )
        if fixture is None:
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code="not_found",
                result_summary=(
                    "Simulator: no fixture seeded for this sayad_id "
                    "(seed a scenario first; no verdict was invented)."
                ),
                reference=reference,
                simulated=True,
            )
        return self._verdict(scope, inquiry_type, sayad_id, fixture, reference)

    def _verdict(
        self,
        scope: str,
        inquiry_type: InquiryType,
        sayad_id: str,
        fixture: SayyadScenario,
        reference: str,
    ) -> SayyadInquiryResult:
        """Build the scope's verdict from the fixture (simulated)."""
        color_name = COLOR_NAMES.get(fixture.color_code, "unknown")
        if scope in (
            SayyadScope.CHEQUE_STATUS.value,
            SayyadScope.SMS_CHEQUE_STATUS.value,
        ):
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code=f"status={fixture.status}",
                result_summary=(
                    "Simulator: Sayyad leaf status is "
                    f"{fixture.status} (Sayyad in-transit, NOT accounting in_clearing)."
                ),
                reference=reference,
                simulated=True,
                in_transit_status=fixture.status,
            )
        if scope in (SayyadScope.SAYAD_SERIAL.value, SayyadScope.SMS_SAYAD_CHEQUE.value):
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code=f"items_match={str(fixture.items_matched).lower()}",
                result_summary=(
                    "Simulator: registered Sayyad items "
                    + ("match" if fixture.items_matched else "DO NOT match")
                    + " the leaf."
                ),
                reference=reference,
                simulated=True,
                items_matched=fixture.items_matched,
            )
        if scope in (SayyadScope.TRANSFERS_CHAIN.value, SayyadScope.SMS_TRANSFERS_CHAIN.value):
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code=f"transfers={fixture.transfers}",
                result_summary=f"Simulator: {fixture.transfers} recorded transfer(s) in the chain.",
                reference=reference,
                simulated=True,
            )
        if scope in (
            SayyadScope.CHEQUE_COLOR.value,
            SayyadScope.CHEQUE_COLOR_LEGAL.value,
            SayyadScope.SMS_CHEQUE_COLOR.value,
        ):
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code=f"color={fixture.color_code}",
                result_summary=(
                    f"Simulator: issuer colour is {color_name} (code {fixture.color_code} of 5) — "
                    "issuer history only, not a statement about this leaf."
                ),
                reference=reference,
                simulated=True,
                color_code=fixture.color_code,
            )
        if scope in (SayyadScope.BACK_CHEQUES.value, SayyadScope.BACK_CHEQUES_LEGAL.value):
            reason = (
                f"; last bounce reason: {fixture.last_bounce_reason}"
                if fixture.last_bounce_reason
                else ""
            )
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code=f"back_cheques={fixture.back_cheques_count}",
                result_summary=(
                    f"Simulator: issuer has {fixture.back_cheques_count} bounced cheque(s){reason}."
                ),
                reference=reference,
                simulated=True,
                back_cheques_count=fixture.back_cheques_count,
            )
        if scope == SayyadScope.CHEQUEBOOK.value:
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code=(
                    f"leaves_used={fixture.leaves_used};"
                    f"leaves_remaining={fixture.leaves_remaining}"
                ),
                result_summary=(
                    "Simulator: checkbook has "
                    f"{fixture.leaves_remaining} leaf/leaves remaining "
                    f"({fixture.leaves_used} used)."
                ),
                reference=reference,
                simulated=True,
            )
        if scope in (SayyadScope.AC_ISSUE_CHEQUE.value, SayyadScope.AC_TRANSFER_CHEQUE.value):
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code="registered",
                result_summary="Simulator: Sayyad registration accepted (simulated).",
                reference=reference,
                simulated=True,
            )
        if scope in (
            SayyadScope.SMS_ACCEPT_CHEQUE.value,
            SayyadScope.SMS_TRANSFER_CHEQUE.value,
            SayyadScope.SMS_CANCEL_CHEQUE.value,
            SayyadScope.SMS_ISSUER_INQUIRY.value,
        ):
            return SayyadInquiryResult(
                scope=scope,
                inquiry_type=inquiry_type,
                channel=InquiryChannel.FINNOTECH_API,
                result_code="accepted",
                result_summary="Simulator: SMS Sayyad action accepted (simulated).",
                reference=reference,
                simulated=True,
            )
        raise SayyadInquiryError(
            f"Simulator has no verdict for scope {scope!r}.",
            details={"scope": scope},
        )


class DeterministicEhrazinoSimulator:
    """In-memory, deterministic stand-in for the Ehrazino API.

    Verified shapes only (``cheque-color`` + ``sayad-check``);
    endpoint path + auth are UNVERIFIED, so this is the whole
    Ehrazino transport until B3 verifies it. Colour fixtures are
    keyed by the SHA-256 of the national id — the id itself is
    never kept (§3.5).
    """

    def __init__(self) -> None:
        self._colors: dict[str, EhrazinoColorScenario] = {}
        self._checks: dict[str, EhrazinoSayadCheckScenario] = {}

    def reset(self) -> None:
        """Clear all seeded scenarios (test isolation)."""
        self._colors.clear()
        self._checks.clear()

    def seed_color(self, national_id: str, scenario: EhrazinoColorScenario) -> None:
        """Register a colour fixture (keyed by hash, never the id)."""
        self._colors[_hash_national_id(national_id)] = scenario

    def seed_sayad_check(self, sayad_id: str, scenario: EhrazinoSayadCheckScenario) -> None:
        """Register a sayad-check fixture for one sayad_id."""
        self._checks[sayad_id] = scenario

    def cheque_color(
        self, national_id: str, *, scenario: str | None = None
    ) -> EhrazinoColorResult:
        """Issuer colour by national code (verified shape)."""
        if len(national_id.strip()) not in (10, 11) or not national_id.strip().isdigit():
            raise SayyadInquiryError(
                "Ehrazino cheque-color needs a 10/11-digit national code.",
                details={"shape": EHRAZINO_CHEQUE_COLOR_SHAPE},
            )
        fixture = self._colors.get(_hash_national_id(national_id))
        if fixture is None and scenario == "clean":
            fixture = EhrazinoColorScenario(color_code=1)
        if fixture is None and scenario == "red":
            fixture = EhrazinoColorScenario(color_code=5)
        if fixture is None:
            raise SayyadInquiryError(
                "Simulator: no colour fixture seeded for this issuer "
                "(seed one first; no verdict was invented).",
                details={"shape": EHRAZINO_CHEQUE_COLOR_SHAPE},
            )
        return EhrazinoColorResult(
            color_code=fixture.color_code,
            color_name=COLOR_NAMES.get(fixture.color_code, "unknown"),
            cost_irr=EHRAZINO_CHEQUE_COLOR_COST_IRR,
            simulated=True,
        )

    def sayad_check(
        self, sayad_id: str, *, scenario: str | None = None
    ) -> EhrazinoSayadCheckResult:
        """Leaf identity by 16-digit sayad_id (verified shape)."""
        if len(sayad_id.strip()) != 16 or not sayad_id.strip().isdigit():
            raise SayyadInquiryError(
                "Ehrazino sayad-check needs a 16-digit sayad_id.",
                details={"shape": EHRAZINO_SAYAD_CHECK_SHAPE},
            )
        fixture = self._checks.get(sayad_id)
        if fixture is None and scenario == "clean":
            fixture = EhrazinoSayadCheckScenario()
        if fixture is None:
            raise SayyadInquiryError(
                "Simulator: no sayad-check fixture seeded for this sayad_id "
                "(seed one first; no verdict was invented).",
                details={"shape": EHRAZINO_SAYAD_CHECK_SHAPE},
            )
        return EhrazinoSayadCheckResult(
            sayad_id=sayad_id,
            items_matched=fixture.items_matched,
            serial_no=fixture.serial_no,
            series_no=fixture.series_no,
            branch_code=fixture.branch_code,
            check_type=fixture.check_type,
            returned_cheques=fixture.returned_cheques,
            cost_irr=EHRAZINO_SAYAD_CHECK_COST_IRR,
            simulated=True,
        )


class FintechSayyadLiveClient:
    """Live Fintech Sayyad client — NOT wired until B3 verifies the transport.

    Constructed only with explicit credentials + base URL (no
    defaults, nothing guessed). Every inquiry raises
    :class:`SayyadLiveNotReadyError`: the scope field shapes
    (§9.2 #1), the ``ac-sayad-*`` auth model (#2) and the tariff
    (#3) are UNVERIFIED, and this client will not guess a request
    shape. The ۵ت remainder (B3) fills this in from the Fintech
    service-detail pages / console.
    """

    def __init__(self, *, base_url: str, client_id: str, client_secret: str) -> None:
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret

    def available_scopes(self) -> tuple[str, ...]:
        return FINNOTECH_SAYYAD_SCOPES

    def inquire(
        self,
        scope: str,
        *,
        sayad_id: str,
        national_id: str | None = None,
        scenario: str | None = None,
    ) -> SayyadInquiryResult:
        raise SayyadLiveNotReadyError(
            "Live Fintech Sayyad calls are not wired: the scope field "
            "shapes, the ac-sayad-* auth model and the tariff are "
            "UNVERIFIED (§9.2 #1–3). Use the simulator + manual "
            "fallback until the ۵ت remainder (B3) verifies them.",
            details={"scope": scope},
        )


class EhrazinoLiveClient:
    """Live Ehrazino client — NOT wired until B3 verifies the transport.

    The field shapes are verified (``nationalCode`` /
    ``cheque_color`` + ``cheque_color_code``; ``sayadId`` + the
    sayad-check outputs) but the endpoint URL path and the auth
    header are UNVERIFIED, and business-account access for natural
    persons is UNVERIFIED (§9.2 #9). Every inquiry raises
    :class:`SayyadLiveNotReadyError` instead of guessing the
    transport. Constructed only with an explicit API key + base URL.
    """

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key

    def cheque_color(
        self, national_id: str, *, scenario: str | None = None
    ) -> EhrazinoColorResult:
        raise SayyadLiveNotReadyError(
            "Live Ehrazino calls are not wired: the endpoint path and "
            "auth header are UNVERIFIED (and business-account access "
            "for natural persons, §9.2 #9). Use the simulator + manual "
            "fallback until the ۵ت remainder (B3) verifies them.",
            details={"shape": EHRAZINO_CHEQUE_COLOR_SHAPE},
        )

    def sayad_check(
        self, sayad_id: str, *, scenario: str | None = None
    ) -> EhrazinoSayadCheckResult:
        raise SayyadLiveNotReadyError(
            "Live Ehrazino calls are not wired: the endpoint path and "
            "auth header are UNVERIFIED. Use the simulator + manual "
            "fallback until the ۵ت remainder (B3) verifies them.",
            details={"shape": EHRAZINO_SAYAD_CHECK_SHAPE},
        )
