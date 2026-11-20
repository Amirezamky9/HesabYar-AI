"""SMS notification providers + due-date reminders (US-544, Phase 5-5 ۵ج).

ADR 0003 amendment (2026-11-18): outbound SMS goes through a
multi-provider registry with ONE interface every provider must
satisfy. First built-in providers: Kavenegar + sms.ir; the
simulator is the default in dev/test (and the fail-safe default
everywhere until a live provider is configured). Other providers
are addable per installation behind the same interface.

Provider specifics (price per SMS, rate limits, line type,
contract/eNamad requirements) are USER-ENTERED editable fields —
never hard-coded. Only each provider's API integration shape is
code (see ``hesabyar.infrastructure.sms`` + ``docs/sms-providers.md``).
"""

from __future__ import annotations
