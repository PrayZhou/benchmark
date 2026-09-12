from __future__ import annotations

from ...schemas import Finding

SEVERITY_ORDER: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Drop exact repeats while preserving first-seen order.

    Composite graders flatten several sub-graders; the same violation can be
    reported by more than one, and duplicate evidence would inflate counts.
    """
    seen: set[tuple[str, str, int | None]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.kind, finding.evidence, finding.step)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique
