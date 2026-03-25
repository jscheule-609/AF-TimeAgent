"""Markdown rendering for Telegram/display output."""
from models.timeline import DealTimingReport
from output.table_formatter import format_milestone_table, format_scenario_table, format_risk_flags


def render_full_report(report: DealTimingReport) -> str:
    """Render the complete report as formatted text."""
    deal_value_str = _format_value(report.deal_value_usd)

    lines = [
        "═" * 80,
        f" DEAL TIMING ESTIMATE: {report.acquirer} / {report.target} ({deal_value_str})",
        f" Announced: {report.announcement_date} | Enforcement Regime: {report.enforcement_regime}"
        f" | Overlap: {report.overlap_type} ({report.overlap_severity})",
        "═" * 80,
        "",
        " REGULATORY MILESTONES",
        format_milestone_table(report),
        "",
        " SCENARIO PATHS",
        format_scenario_table(report),
        "",
    ]

    if report.risk_flags:
        lines.extend([" RISK FLAGS", " " + "─" * 78])
        lines.append(format_risk_flags(report))
        lines.append("")

    lines.extend([
        "═" * 80,
        f" P50 Close: {report.p50_close_date or 'N/A'}"
        f" | P75: {report.p75_close_date or 'N/A'}"
        f" | P90: {report.p90_close_date or 'N/A'}",
    ])

    if report.outside_date:
        lines.append(
            f" Probability of closing by outside date ({report.outside_date}): "
            f"{report.probability_close_by_outside_date or 0:.0f}%"
        )

    lines.extend([
        f" Critical path: {report.critical_path_jurisdiction}"
        f" | Comparables used: {report.comparable_deals_used}",
        "═" * 80,
    ])

    return "\n".join(lines)


def render_compact_report(report: DealTimingReport) -> str:
    """Render a compact version suitable for Telegram."""
    deal_value_str = _format_value(report.deal_value_usd)

    lines = [
        f"**{report.acquirer} / {report.target}** ({deal_value_str})",
        f"Announced: {report.announcement_date}",
        "",
        f"**P50 Close:** {report.p50_close_date or 'TBD'}",
        f"**P75 Close:** {report.p75_close_date or 'TBD'}",
        f"**P90 Close:** {report.p90_close_date or 'TBD'}",
        f"**Critical Path:** {report.critical_path_jurisdiction}",
        "",
    ]

    if report.outside_date:
        lines.append(
            f"Close by outside date ({report.outside_date}): "
            f"{report.probability_close_by_outside_date or 0:.0f}%"
        )
        lines.append("")

    # Top scenarios
    lines.append("**Scenarios:**")
    for s in report.scenarios[:4]:
        close = s.expected_close_date.strftime("%b %Y") if s.expected_close_date else "N/A"
        lines.append(f"• {s.scenario_name}: {s.probability_pct:.0f}% ({close})")

    # Top risk flags
    if report.risk_flags:
        lines.append("")
        lines.append("**Risks:**")
        for f in report.risk_flags[:3]:
            lines.append(f"• [{f.severity.upper()}] {f.flag}")

    return "\n".join(lines)


def _format_value(value: float) -> str:
    """Format deal value as human-readable string."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    elif value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    elif value > 0:
        return f"${value:,.0f}"
    return "Undisclosed"
