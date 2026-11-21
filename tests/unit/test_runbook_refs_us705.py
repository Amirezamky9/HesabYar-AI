"""Every monitoring alert's runbook reference resolves (US-705).

US-704's alerts carry a ``runbook_ref`` — a docs path + section,
not a URL. This test pins the contract both ways:

- every ``RUNBOOK_*`` constant in
  ``hesabyar.domain.monitoring.texts`` names a section that EXISTS
  as a ``### `` heading in ``docs/monitoring.md`` (a renamed
  heading must not silently orphan an alert);
- the operator manual (``docs/operator-manual.md``) covers every
  alert rule's incident procedure — each runbook section title
  appears there, so the manual and the alerts cannot drift apart;
- the four US-705 documents exist and are non-trivial.

Docs-as-code precedent: ``tests/unit/test_cheque_events_us530.py``
reads ``docs/adr/`` + ``docs/cheque-decisions.md`` the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

from hesabyar.domain.monitoring import texts

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING_DOC = REPO_ROOT / "docs" / "monitoring.md"
OPERATOR_MANUAL = REPO_ROOT / "docs" / "operator-manual.md"

#: (name, constant) for every runbook reference the alerts can carry.
RUNBOOK_REFS = [
    (name, getattr(texts, name))
    for name in dir(texts)
    if name.startswith("RUNBOOK_")
]

#: The four documents US-705 delivers.
US705_DOCS = (
    "docs/runbook-upgrade-rollback.md",
    "docs/data-migration.md",
    "docs/release.md",
    "docs/operator-manual.md",
)

_SECTION_TITLE = re.compile(r"«(.+)»")


def _section_title(ref: str) -> str:
    match = _SECTION_TITLE.search(ref)
    assert match is not None, f"runbook ref has no «section» title: {ref!r}"
    return match.group(1)


def test_every_runbook_ref_names_an_existing_monitoring_section() -> None:
    """Each RUNBOOK_* constant resolves to a ### heading in docs/monitoring.md."""
    assert RUNBOOK_REFS, "no RUNBOOK_* constants found — the texts module moved?"
    assert len(RUNBOOK_REFS) == 8, f"expected 8 runbook refs, found {len(RUNBOOK_REFS)}"
    doc = MONITORING_DOC.read_text(encoding="utf-8")
    headings = {
        line[4:].strip() for line in doc.splitlines() if line.startswith("### ")
    }
    for name, ref in RUNBOOK_REFS:
        assert ref.startswith("docs/monitoring.md"), f"{name} points outside monitoring.md: {ref!r}"
        title = _section_title(ref)
        assert title in headings, (
            f"{name} points at section «{title}», which has no ### heading "
            "in docs/monitoring.md — rename the ref or the heading."
        )


def test_operator_manual_covers_every_alert_rule() -> None:
    """Each runbook section title appears in the operator manual's incident table."""
    manual = OPERATOR_MANUAL.read_text(encoding="utf-8")
    missing = [
        _section_title(ref) for _, ref in RUNBOOK_REFS if _section_title(ref) not in manual
    ]
    assert not missing, (
        "the operator manual does not cover these alert incidents: "
        + ", ".join(f"«{title}»" for title in missing)
    )


def test_us705_documents_exist_and_are_substantial() -> None:
    for relative in US705_DOCS:
        path = REPO_ROOT / relative
        assert path.is_file(), f"missing US-705 document: {relative}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 50, f"{relative} has only {len(lines)} lines — a stub?"
