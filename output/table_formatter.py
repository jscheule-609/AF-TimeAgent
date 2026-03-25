"""Structured table output for deal timing reports."""
from models.timeline import DealTimingReport, MilestoneRow


def format_milestone_table(report: DealTimingReport) -> str:
    """Format milestone rows as a fixed-width table."""
    lines = []
    header = (
        f" {'Milestone':<25}│ {'Jurisdiction':<13}│ {'Contractual':<12}│ "
        f"{'P50':<10}│ {'P75':<10}│ {'P90':<10}│ Risk Flags"
    )
    separator = "─" * 25 + "┼" + "─" * 14 + "┼" + "─" * 13 + "┼" + "─" * 11 + "┼" + "─" * 11 + "┼" + "─" * 11 + "┼" + "─" * 20

    lines.append(separator)
    lines.append(header)
    lines.append(separator)

    for m in report.milestones:
        contractual = m.contractual_deadline.strftime("%b %d") if m.contractual_deadline else ""
        p50 = m.base_case_date.strftime("%b %d") if m.base_case_date else ""
        p75 = m.extended_case_date.strftime("%b %d") if m.extended_case_date else ""
        p90 = m.stress_case_date.strftime("%b %d") if m.stress_case_date else ""
        flags = ", ".join(m.risk_flags[:2]) if m.risk_flags else ""

        lines.append(
            f" {m.milestone:<25}│ {m.jurisdiction:<13}│ {contractual:<12}│ "
            f"{p50:<10}│ {p75:<10}│ {p90:<10}│ {flags}"
        )

    lines.append(separator)
    return "\n".join(lines)


def format_scenario_table(report: DealTimingReport) -> str:
    """Format scenario paths as a fixed-width table."""
    lines = []
    header = (
        f" {'Scenario':<35}│ {'Probability':<12}│ {'Close Date':<11}│ "
        f"{'Duration':<9}│ Description"
    )
    separator = "─" * 35 + "┼" + "─" * 13 + "┼" + "─" * 12 + "┼" + "─" * 10 + "┼" + "─" * 25

    lines.append(separator)
    lines.append(header)
    lines.append(separator)

    for s in report.scenarios:
        close = s.expected_close_date.strftime("%b %Y") if s.expected_close_date else "N/A"
        duration = f"{s.duration_days}d" if s.duration_days else "N/A"

        lines.append(
            f" {s.scenario_name:<35}│ {s.probability_pct:>6.0f}%     │ {close:<11}│ "
            f"{duration:<9}│ {s.description[:25]}"
        )

    lines.append(separator)
    return "\n".join(lines)


def format_risk_flags(report: DealTimingReport) -> str:
    """Format risk flags."""
    lines = []
    for f in report.risk_flags:
        severity = f.severity.upper()
        jur = f" ({f.jurisdiction})" if f.jurisdiction else ""
        lines.append(f" [{severity}] {f.flag}{jur} — {f.detail}")
    return "\n".join(lines)
