"""SMS provider clients (US-544, Phase 5-5 ۵ج).

One client per provider kind behind the
:class:`hesabyar.domain.sms.ports.SmsProviderClient` interface:

- :mod:`hesabyar.infrastructure.sms.kavenegar` — Kavenegar REST API
  (shape verified from the official docs, kavenegar.com/rest.html);
- :mod:`hesabyar.infrastructure.sms.sms_ir` — sms.ir v1 REST API
  (shape verified from the official OpenAPI document,
  api.sms.ir/swagger/v1/swagger.json);
- :mod:`hesabyar.infrastructure.sms.simulator` — the simulator
  (logs the message, returns a fake delivery id; the default in
  dev/test and the fail-safe default everywhere).

No live provider call is ever made from a test (``httpx.MockTransport``
stands in), and no live call happens without stored credentials —
the factory fails closed first.
"""

from __future__ import annotations
