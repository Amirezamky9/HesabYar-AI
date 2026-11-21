"""Scheduled jobs (US-560, Phase 5-7 ۵خ W1).

The scheduler is the time trigger for the whole platform, on the SAME
Procrastinate setup the event platform (F1/US-501) already runs — never
a second scheduler. This package owns the job registry's domain shape:

- :mod:`hesabyar.domain.scheduler.schedules` — Jalali-aware schedules
  and the in-app ``next_run`` computation (no Jalali cron exists);
- :mod:`hesabyar.domain.scheduler.models` — job definitions, run
  records, and the seeded registry of the platform's periodic jobs;
- :mod:`hesabyar.domain.scheduler.ports` — the registry + inventory
  ports the application layer drives.

Friday is the weekend default: a job scheduled "every day" does not
fire on Friday unless its schedule says otherwise (phased design §6.5
/ §1.3 ۵خ). Jalali month lengths vary (Esfand 29/30 days) — a
day-31 monthly schedule clamps to the month's last day, honestly
recorded, never silently skipped.
"""

from __future__ import annotations
