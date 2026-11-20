"""Moadian e-invoicing bounded context (design-eng spec §12.3, US-404).

Pure domain models for the Moadian boundary: profiles, documents,
submissions and status events. This package is infrastructure-free —
no SQL, no HTTP, no signing/encryption. The simulator port lives in
:mod:`hesabyar.domain.moadian.ports`; the deterministic fixture
simulator is infrastructure
(:mod:`hesabyar.infrastructure.moadian.simulator`).

Protocol facts recorded here come from the official technical
instruction «دستورالعمل فنی نحوه اتصال به سامانه مودیان» (Iranian
National Tax Administration, INTA), published on the INTA portal
``intamedia.ir`` under «پایانه‌های فروشگاهی و سامانه مودیان» →
«آیین‌نامه‌ها و دستورالعمل‌ها و نرم‌افزارهای مرتبط» (instruction PDF
dated 1401/07/26), as reproduced at
``https://limotx.ir/article/40``. Every field claim in
:func:`validate_invoice_payload` cites that instruction's Table 7
(invoice items) and its error list. See ``docs/moadian.md`` for the
full source table and the documented assumptions.
"""
