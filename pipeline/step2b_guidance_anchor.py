"""
Step 2b: Parse AJ / Company Closing Guidance into Date Anchors

Reads ``closing_guidance_companies`` and ``closing_guidance_arbjournal``
from the ``deals`` table and converts free-text strings like "Q1 2027",
"H2 2026", "Mid-2026" into date ranges.

The midpoint of the range is the guidance anchor — a floor below which
the model should not predict close dates.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

from db.connection import get_pool

logger = logging.getLogger(__name__)


async def load_guidance_anchor(
    deal_pk: int,
    announcement_date: date,
) -> Optional[date]:
    """Load and parse closing guidance into a single anchor date.

    Returns the midpoint of the guidance date range, or None
    if guidance is unavailable or unparseable.

    Uses AJ guidance preferentially (more standardized), then
    falls back to company guidance. Takes the later of the two
    if both are available.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT closing_guidance_companies, "
            "closing_guidance_arbjournal "
            "FROM deals WHERE deal_pk = $1",
            deal_pk,
        )

    if not row:
        return None

    aj = row["closing_guidance_arbjournal"]
    co = row["closing_guidance_companies"]

    aj_anchor = _parse_to_midpoint(aj, announcement_date)
    co_anchor = _parse_to_midpoint(co, announcement_date)

    # Use the later of the two (more conservative)
    if aj_anchor and co_anchor:
        anchor = max(aj_anchor, co_anchor)
    else:
        anchor = aj_anchor or co_anchor

    if anchor:
        logger.info(
            f"Guidance anchor: {anchor} "
            f"(AJ={aj!r}, Co={co!r})"
        )

    return anchor


def _parse_to_midpoint(
    text: Optional[str],
    announcement_date: date,
) -> Optional[date]:
    """Parse guidance text into the midpoint of the date range."""
    rng = parse_guidance(text, announcement_date)
    if rng[0] is None or rng[1] is None:
        return None
    delta = (rng[1] - rng[0]).days
    return rng[0] + __import__("datetime").timedelta(
        days=delta // 2
    )


def parse_guidance(
    text: Optional[str],
    announcement_date: date,
) -> tuple[Optional[date], Optional[date]]:
    """Parse guidance text into (earliest, latest) date range.

    Returns (None, None) if unparseable, TBD, or No DMA.

    Supported formats:
      "Q1 2027"        → (2027-01-01, 2027-03-31)
      "Q2 2026"        → (2026-04-01, 2026-06-30)
      "H1 2026"        → (2026-01-01, 2026-06-30)
      "H2 2026"        → (2026-07-01, 2026-12-31)
      "Mid-2026"       → (2026-04-01, 2026-09-30)
      "2026"           → (2026-01-01, 2026-12-31)
      "In 2026"        → (2026-01-01, 2026-12-31)
      "By Late 2026"   → (2026-09-01, 2026-12-31)
      "Early 2026"     → (2026-01-01, 2026-04-30)
      "4/1/2026"       → (2026-04-01, 2026-04-01)
      "12-15 months from the DMA" → announcement + 365..456
      "By Q1 2026"     → same as "Q1 2026"
    """
    if not text:
        return (None, None)

    t = text.strip()

    # Skip non-guidance values
    skip_patterns = [
        "no dma", "tbd", "n/a", "not applicable",
        "pending", "unknown",
    ]
    if any(p in t.lower() for p in skip_patterns):
        return (None, None)

    # Quarter pattern: Q1-Q4 YYYY
    m = re.search(
        r"Q([1-4])\s*(\d{4})", t, re.IGNORECASE,
    )
    if m:
        q = int(m.group(1))
        y = int(m.group(2))
        starts = {
            1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1),
        }
        ends = {
            1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31),
        }
        return (
            date(y, *starts[q]),
            date(y, *ends[q]),
        )

    # Half pattern: H1/H2 YYYY
    m = re.search(
        r"H([12])\s*(\d{4})", t, re.IGNORECASE,
    )
    if m:
        h = int(m.group(1))
        y = int(m.group(2))
        if h == 1:
            return (date(y, 1, 1), date(y, 6, 30))
        return (date(y, 7, 1), date(y, 12, 31))

    # "Mid-YYYY" or "mid YYYY"
    m = re.search(
        r"mid[- ]?(\d{4})", t, re.IGNORECASE,
    )
    if m:
        y = int(m.group(1))
        return (date(y, 4, 1), date(y, 9, 30))

    # "Late YYYY" or "By Late YYYY"
    m = re.search(
        r"late\s+(\d{4})", t, re.IGNORECASE,
    )
    if m:
        y = int(m.group(1))
        return (date(y, 9, 1), date(y, 12, 31))

    # "Early YYYY"
    m = re.search(
        r"early\s+(\d{4})", t, re.IGNORECASE,
    )
    if m:
        y = int(m.group(1))
        return (date(y, 1, 1), date(y, 4, 30))

    # "In YYYY" or just "YYYY"
    m = re.search(r"\b(\d{4})\b", t)
    if m:
        y = int(m.group(1))
        if 2020 <= y <= 2035:
            return (date(y, 1, 1), date(y, 12, 31))

    # "M/D/YYYY" or "MM/DD/YYYY"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        try:
            d = date(
                int(m.group(3)),
                int(m.group(1)),
                int(m.group(2)),
            )
            return (d, d)
        except ValueError:
            pass

    # "X-Y months from the DMA/signing"
    m = re.search(
        r"(\d+)\s*[-–]\s*(\d+)\s*months?\b", t,
        re.IGNORECASE,
    )
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2))
        from dateutil.relativedelta import relativedelta
        return (
            announcement_date + relativedelta(months=lo),
            announcement_date + relativedelta(months=hi),
        )

    return (None, None)
