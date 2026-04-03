"""Report generation for scilint lint findings."""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path

from scilint.rules.types import Finding, Severity

_SEVERITY_EMOJI = {
    Severity.CRITICAL: "🔴",
    Severity.MAJOR: "🟠",
    Severity.MINOR: "🟡",
    Severity.ADVISORY: "🔵",
}

_SEVERITY_ORDER = [Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.ADVISORY]

_SEVERITY_COLOR = {
    Severity.CRITICAL: "#c0392b",
    Severity.MAJOR: "#e67e22",
    Severity.MINOR: "#f1c40f",
    Severity.ADVISORY: "#2980b9",
}

_SEVERITY_BG = {
    Severity.CRITICAL: "#fdf0ef",
    Severity.MAJOR: "#fef5ec",
    Severity.MINOR: "#fefce8",
    Severity.ADVISORY: "#eaf4fc",
}

_CSS = """\
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 14px; line-height: 1.6; color: #2c3e50; background: #f8f9fa; padding: 2rem;
}
h1 { font-size: 1.75rem; font-weight: 700; margin-bottom: 0.25rem; color: #1a252f; }
h2 {
  font-size: 1.25rem; font-weight: 600; margin: 2rem 0 0.75rem; color: #1a252f;
  border-bottom: 2px solid #dee2e6; padding-bottom: 0.4rem;
}
h3 { font-size: 1rem; font-weight: 600; margin: 1.25rem 0 0.5rem; color: #495057; }
.meta { color: #6c757d; font-size: 0.875rem; margin-bottom: 1.5rem; }
.summary {
  background: #fff; border: 1px solid #dee2e6; border-radius: 8px;
  padding: 1.25rem; margin-bottom: 2rem;
}
.stats { display: flex; gap: 1.5rem; flex-wrap: wrap; margin-top: 0.75rem; }
.stat { text-align: center; }
.stat-value { font-size: 2rem; font-weight: 700; line-height: 1; }
.stat-label {
  font-size: 0.75rem; color: #6c757d; text-transform: uppercase; letter-spacing: 0.05em;
}
.stat-critical .stat-value { color: #c0392b; }
.stat-major .stat-value { color: #e67e22; }
.stat-minor .stat-value { color: #d4ac0d; }
.stat-advisory .stat-value { color: #2980b9; }
.stat-fixable .stat-value { color: #27ae60; }
table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }
th {
  background: #f1f3f5; text-align: left; padding: 0.5rem 0.75rem; font-size: 0.75rem;
  text-transform: uppercase; letter-spacing: 0.05em; color: #495057;
  border-bottom: 2px solid #dee2e6;
}
td { padding: 0.6rem 0.75rem; border-bottom: 1px solid #f1f3f5; vertical-align: top; }
tr:last-child td { border-bottom: none; }
.file-section {
  background: #fff; border: 1px solid #dee2e6; border-radius: 8px;
  padding: 1.25rem; margin-bottom: 1.5rem;
}
.file-path {
  font-family: 'SF Mono', 'Fira Code', Consolas, monospace; font-size: 0.875rem;
  color: #495057; background: #f1f3f5; padding: 0.2rem 0.5rem; border-radius: 4px;
}
.badge {
  display: inline-block; font-size: 0.7rem; font-weight: 700;
  padding: 0.15rem 0.45rem; border-radius: 12px; text-transform: uppercase;
  letter-spacing: 0.04em; color: #fff; vertical-align: middle;
}
.badge-critical { background: #c0392b; }
.badge-major { background: #e67e22; }
.badge-minor { background: #d4ac0d; color: #1a252f; }
.badge-advisory { background: #2980b9; }
.finding {
  border-left: 3px solid #dee2e6; padding: 0.6rem 0.75rem;
  margin-bottom: 0.75rem; border-radius: 0 4px 4px 0;
}
.finding-critical { border-color: #c0392b; background: #fdf0ef; }
.finding-major { border-color: #e67e22; background: #fef5ec; }
.finding-minor { border-color: #d4ac0d; background: #fefce8; }
.finding-advisory { border-color: #2980b9; background: #eaf4fc; }
.finding-header {
  display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; flex-wrap: wrap;
}
.rule-id {
  font-family: 'SF Mono', 'Fira Code', Consolas, monospace; font-size: 0.75rem; color: #6c757d;
}
.finding-title { font-weight: 600; }
.finding-loc { font-size: 0.75rem; color: #868e96; margin-left: auto; font-family: monospace; }
.finding-message { margin-bottom: 0.3rem; }
.finding-match {
  font-family: 'SF Mono', 'Fira Code', Consolas, monospace;
  background: rgba(0,0,0,0.06); padding: 0.1rem 0.35rem; border-radius: 3px;
  font-size: 0.875em;
}
.finding-suggestion { color: #495057; font-size: 0.875rem; }
.fix-box {
  margin-top: 0.4rem; padding: 0.4rem 0.6rem; background: #e8f5e9;
  border: 1px solid #a5d6a7; border-radius: 4px; font-size: 0.8rem;
}
.fix-label { font-weight: 600; color: #2e7d32; }
.no-issues {
  text-align: center; padding: 3rem; color: #27ae60; font-size: 1.1rem;
  background: #fff; border: 1px solid #dee2e6; border-radius: 8px;
}
"""


class ReportGenerator:
    """Generate Markdown and HTML reports from scilint findings."""

    def __init__(
        self,
        findings: list[tuple[Path, list[Finding]]],
        source_texts: dict[Path, str] | None = None,
    ) -> None:
        self._findings = findings
        self._source_texts = source_texts or {}

    def summary_stats(self) -> dict:
        """Return aggregate counts across all files."""
        by_severity: dict[str, int] = {s.value: 0 for s in Severity}
        by_rule: dict[str, int] = defaultdict(int)
        fixable = 0
        total = 0

        for _path, file_findings in self._findings:
            for f in file_findings:
                total += 1
                by_severity[f.severity.value] += 1
                by_rule[f.rule_id] += 1
                if f.fix is not None:
                    fixable += 1

        return {
            "total": total,
            "by_severity": by_severity,
            "by_rule": dict(by_rule),
            "fixable": fixable,
        }

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        stats = self.summary_stats()
        lines: list[str] = []

        lines.append("# Scilint Report\n")

        # Summary table
        lines.append("## Summary\n")
        lines.append("| Metric | Count |")
        lines.append("|--------|------:|")
        lines.append(f"| Total findings | {stats['total']} |")
        for sev in _SEVERITY_ORDER:
            emoji = _SEVERITY_EMOJI[sev]
            count = stats["by_severity"].get(sev.value, 0)
            lines.append(f"| {emoji} {sev.value.capitalize()} | {count} |")
        lines.append(f"| ✅ Fixable | {stats['fixable']} |")
        lines.append("")

        if not stats["total"]:
            lines.append("_No issues found._\n")
            return "\n".join(lines)

        # Per-file findings
        lines.append("## Findings\n")
        for file_path, file_findings in self._findings:
            if not file_findings:
                continue
            lines.append(f"### `{file_path}`\n")

            # Group by severity
            by_sev: dict[Severity, list[Finding]] = defaultdict(list)
            for f in file_findings:
                by_sev[f.severity].append(f)

            for sev in _SEVERITY_ORDER:
                group = by_sev.get(sev, [])
                if not group:
                    continue
                emoji = _SEVERITY_EMOJI[sev]
                lines.append(f"#### {emoji} {sev.value.capitalize()}\n")
                for f in group:
                    loc = f"L{f.source_line}"
                    fix_note = " ✅" if f.fix else ""
                    lines.append(f"- **[{f.rule_id}] {f.title}**{fix_note} ({loc})")
                    lines.append(f"  {f.message}")
                    if f.match:
                        lines.append(f"  - Match: `{f.match}`")
                    if f.suggestion:
                        lines.append(f"  - Suggestion: {f.suggestion}")
                    if f.fix:
                        lines.append(
                            f"  - Fix: `{f.fix.original}` → `{f.fix.replacement}`"
                            f" _(confidence: {f.fix.confidence:.0%})_"
                        )
                    lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def to_html(self) -> str:
        stats = self.summary_stats()
        parts: list[str] = []

        parts.append("<!DOCTYPE html>")
        parts.append('<html lang="en">')
        parts.append("<head>")
        parts.append('<meta charset="UTF-8">')
        parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        parts.append("<title>Scilint Report</title>")
        parts.append(f"<style>{_CSS}</style>")
        parts.append("</head>")
        parts.append("<body>")
        parts.append("<h1>Scilint Report</h1>")

        file_count = len(self._findings)
        parts.append(
            f'<p class="meta">{file_count} file{"s" if file_count != 1 else ""} analyzed</p>'
        )

        # Summary
        parts.append('<div class="summary">')
        parts.append("<h2>Summary</h2>")
        parts.append('<div class="stats">')

        def _stat(label: str, value: int, css_class: str) -> str:
            return (
                f'<div class="stat {css_class}">'
                f'<div class="stat-value">{value}</div>'
                f'<div class="stat-label">{label}</div>'
                f"</div>"
            )

        parts.append(_stat("Total", stats["total"], "stat-total"))
        for sev in _SEVERITY_ORDER:
            count = stats["by_severity"].get(sev.value, 0)
            parts.append(_stat(sev.value.capitalize(), count, f"stat-{sev.value}"))
        parts.append(_stat("Fixable", stats["fixable"], "stat-fixable"))
        parts.append("</div>")  # stats
        parts.append("</div>")  # summary

        if not stats["total"]:
            parts.append('<div class="no-issues">✅ No issues found. Clear as a bell!</div>')
            parts.append("</body></html>")
            return "\n".join(parts)

        # Per-file findings
        parts.append("<h2>Findings</h2>")

        for file_path, file_findings in self._findings:
            if not file_findings:
                continue
            parts.append('<div class="file-section">')
            parts.append(
                f'<h3>📄 <span class="file-path">{html.escape(str(file_path))}</span></h3>'
            )

            by_sev: dict[Severity, list[Finding]] = defaultdict(list)
            for f in file_findings:
                by_sev[f.severity].append(f)

            for sev in _SEVERITY_ORDER:
                group = by_sev.get(sev, [])
                if not group:
                    continue
                for f in group:
                    sev_class = f"finding-{sev.value}"
                    parts.append(f'<div class="finding {sev_class}">')
                    parts.append('<div class="finding-header">')
                    parts.append(f'<span class="badge badge-{sev.value}">{sev.value}</span>')
                    parts.append(f'<span class="rule-id">{html.escape(f.rule_id)}</span>')
                    parts.append(f'<span class="finding-title">{html.escape(f.title)}</span>')
                    parts.append(f'<span class="finding-loc">L{f.source_line}</span>')
                    parts.append("</div>")  # finding-header
                    parts.append(f'<div class="finding-message">{html.escape(f.message)}</div>')
                    if f.match:
                        parts.append(
                            f'Match: <code class="finding-match">{html.escape(f.match)}</code>'
                        )
                    if f.suggestion:
                        parts.append(
                            f'<div class="finding-suggestion">💡 {html.escape(f.suggestion)}</div>'
                        )
                    if f.fix:
                        fix = f.fix
                        parts.append('<div class="fix-box">')
                        label = f'<span class="fix-label">✅ Fix ({fix.confidence:.0%}):</span>'
                        orig = f"<code>{html.escape(fix.original)}</code>"
                        repl = f"<code>{html.escape(fix.replacement)}</code>"
                        parts.append(f"{label} {orig} → {repl}")
                        if fix.explanation:
                            parts.append(f"<br>{html.escape(fix.explanation)}")
                        parts.append("</div>")  # fix-box
                    parts.append("</div>")  # finding

            parts.append("</div>")  # file-section

        parts.append("</body></html>")
        return "\n".join(parts)
