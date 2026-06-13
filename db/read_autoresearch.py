"""
Read autoresearch-populated data from MARS v2.

These functions query tables that AF-ARB_AUTORESEARCH writes to
and return AF-TimeAgent Pydantic models. TimeAgent NEVER writes
to these tables — it only reads.

v2 migration (2026-04-12):
  - break_fees → deal_break_fees  (fee_direction replaces party+fee_type)
  - deal_regulatory_efforts → deal_protections + deal_conditions
  - 5 antitrust tables → regulatory_reviews (jurisdiction_code filter)
  - deal_dma_terms unchanged (still exists in v2)
"""
import logging
from datetime import date
from dateutil.relativedelta import relativedelta
from typing import Optional

from db.connection import get_pool
from models.deal import (
    DealParameters, DealStructure, classify_buyer_type,
)
from models.documents import ParsedMergerAgreement, PressReleaseData

logger = logging.getLogger(__name__)

# Map autoresearch approval names to TimeAgent jurisdiction codes
_APPROVAL_MAP = {
    "hsr": "HSR", "hsr act": "HSR",
    "hart-scott-rodino": "HSR",
    "doj": "HSR", "ftc": "HSR",  # same regulatory process
    "european commission": "EC", "ec": "EC",
    "eu merger regulation": "EC",
    "cma": "CMA", "competition and markets authority": "CMA",
    "samr": "SAMR",
    "state administration for market regulation": "SAMR",
    "cfius": "CFIUS", "accc": "ACCC",
}

# Map v2 jurisdiction_code → TimeAgent jurisdiction name
_JURISDICTION_CODE_MAP = {
    "US": "HSR",
    "EU": "EC",
    "GB": "CMA",
    "CN": "SAMR",
    "CFIUS": "CFIUS",
}


# ------------------------------------------------------------------
# DealParameters from MARS
# ------------------------------------------------------------------

async def load_deal_params_from_mars(
    deal_pk: int,
) -> Optional[DealParameters]:
    """Load DealParameters from deals + parties tables.

    Returns None if deal_pk not found.
    CIK fields are left empty — caller must resolve via SEC API
    if 10-K fetching is needed.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                d.deal_pk, d.deal_id,
                d.deal_value_usd, d.type_of_consideration,
                d.deal_structure_type,
                d.date_announced, d.date_expected_close_parsed,
                d.industry, d.gics_sector, d.deal_attitude,
                pe_acq.ticker AS acquirer_ticker,
                COALESCE(pe_acq.short_name, pe_acq.legal_name) AS acquirer_name,
                pe_tgt.ticker AS target_ticker,
                COALESCE(pe_tgt.short_name, pe_tgt.legal_name) AS target_name,
                pe_acq.party_type AS acquirer_party_type,
                pe_acq.domicile_country AS acquirer_country,
                pe_tgt.domicile_country AS target_country
            FROM deals d
            -- OQ-N20: v1 parties pa/pt removed; names/tickers now from the v2
            -- deal_parties + party_entities joins already present below.
            LEFT JOIN deal_parties dp_acq
                ON d.deal_pk = dp_acq.deal_pk
                AND dp_acq.role_type = 'acquirer'
            LEFT JOIN party_entities pe_acq
                ON dp_acq.party_id = pe_acq.party_id
            LEFT JOIN deal_parties dp_tgt
                ON d.deal_pk = dp_tgt.deal_pk
                AND dp_tgt.role_type = 'target'
            LEFT JOIN party_entities pe_tgt
                ON dp_tgt.party_id = pe_tgt.party_id
            WHERE d.deal_pk = $1
            """,
            deal_pk,
        )

    if not row:
        return None

    return DealParameters(
        acquirer_ticker=row["acquirer_ticker"] or "",
        acquirer_name=row["acquirer_name"] or "",
        acquirer_cik="",
        target_ticker=row["target_ticker"] or "",
        target_name=row["target_name"] or "",
        target_cik="",
        acquirer_country=row["acquirer_country"],
        target_country=row["target_country"],
        deal_value_usd=float(row["deal_value_usd"] or 0),
        deal_structure=_map_consideration(
            row["type_of_consideration"],
            row["deal_structure_type"],
        ),
        buyer_type=classify_buyer_type(
            row["acquirer_name"],
            party_type=row["acquirer_party_type"],
        ),
        announcement_date=(
            row["date_announced"] or date.today()
        ),
        sector=row["gics_sector"] or "",
        industry=row["industry"] or "",
        gics_sector=row["gics_sector"],
        deal_attitude=row["deal_attitude"] or "Friendly",
        mars_deal_pk=row["deal_pk"],
        mars_deal_id=row["deal_id"],
    )


def _normalize_approvals(raw: list[str]) -> list[str]:
    """Normalize autoresearch approval names to jurisdiction codes.
    Deduplicates (e.g. DOJ + FTC + HSR all map to HSR)."""
    seen: set[str] = set()
    result: list[str] = []
    for name in raw:
        mapped = _APPROVAL_MAP.get(name.lower().strip())
        if mapped and mapped not in seen:
            seen.add(mapped)
            result.append(mapped)
        elif not mapped and name.upper() not in seen:
            # Keep unknown approvals as-is (e.g. "SEC")
            seen.add(name.upper())
            result.append(name.upper())
    return result


def _map_consideration(
    consideration: str | None,
    structure_type: str | None,
) -> DealStructure:
    """Map MARS consideration/structure fields to DealStructure enum."""
    raw = (consideration or structure_type or "").lower()
    if "tender" in raw:
        return DealStructure.TENDER
    if "cash" in raw and "stock" in raw:
        return DealStructure.MIXED
    if "stock" in raw or "share" in raw:
        return DealStructure.STOCK
    return DealStructure.CASH


# ------------------------------------------------------------------
# ParsedMergerAgreement from MARS
# ------------------------------------------------------------------

async def load_merger_terms_from_mars(
    deal_pk: int,
) -> Optional[ParsedMergerAgreement]:
    """Load merger agreement data from v2 deal_outside_date_mechanics +
    deal_break_fees + deal_protections + deal_conditions.

    Returns None if no deal_dma_terms row exists (autoresearch
    has not yet profiled this deal).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # OQ-N20: v1 deal_dma_terms -> v2 6-way split. The fields this loader uses
        # (outside date + ticking fee) live in deal_outside_date_mechanics +
        # deal_protections. "Not profiled" maps to "no outside-date-mechanics row".
        # long_stop_extensions (v1 count) has no v2 equivalent — extended date is
        # taken directly from odm.extended_outside_date instead.
        dma = await conn.fetchrow(
            """
            SELECT
                odm.outside_date AS long_stop_date,
                odm.extended_outside_date AS extended_long_stop_date,
                odm.extension_length_days,
                odm.extension_available,
                p.ticking_fee_present,
                p.ticking_fee_details
            FROM deal_outside_date_mechanics odm
            LEFT JOIN deal_protections p ON p.deal_pk = odm.deal_pk
            WHERE odm.deal_pk = $1
            """,
            deal_pk,
        )
        if not dma:
            return None

        # v2: break_fees → deal_break_fees
        fees = await conn.fetch(
            "SELECT * FROM deal_break_fees WHERE deal_pk = $1",
            deal_pk,
        )

        # v2: deal_regulatory_efforts → deal_protections
        prot = await conn.fetchrow(
            """SELECT efforts_standard, divestiture_cap
               FROM deal_protections WHERE deal_pk = $1""",
            deal_pk,
        )

        # v2: required approvals from deal_conditions
        cond_rows = await conn.fetch(
            """SELECT condition_name
               FROM deal_conditions
               WHERE deal_pk = $1
                 AND condition_family IN ('antitrust', 'foreign_investment')""",
            deal_pk,
        )

    # Parse fees — v2 uses fee_direction instead of party + fee_type
    target_fee = None
    reverse_fee = None
    for fee in fees:
        direction = (fee.get("fee_direction") or "").lower()
        amount = float(fee.get("amount") or 0)
        if not amount and fee.get("pct_of_equity_value"):
            # Fallback: percentage-based fee, skip dollar amount
            continue
        if "target_to_acquirer" in direction or "target" in direction:
            target_fee = amount
        elif "acquirer_to_target" in direction or "reverse" in direction:
            reverse_fee = amount

    # Parse outside date from deal_dma_terms
    outside_date = dma["long_stop_date"]
    extended_outside_date = dma.get("extended_long_stop_date")
    extensions = dma.get("long_stop_extensions") or 0
    extension_desc = []
    if outside_date and extensions > 0:
        if not extended_outside_date:
            extended_outside_date = (
                outside_date
                + relativedelta(months=extensions)
            )
        extension_desc = [
            f"{extensions}-month extension available"
        ]

    # Parse regulatory efforts from deal_protections + deal_conditions
    efforts_standard = "unknown"
    required_approvals: list[str] = []
    divestiture_commitment: str | None = None
    litigation_commitment = False

    if prot:
        efforts_standard = (
            prot["efforts_standard"] or "unknown"
        )
        if prot["divestiture_cap"]:
            divestiture_commitment = prot["divestiture_cap"]
        else:
            divestiture_commitment = "no"
        litigation_commitment = (
            "hell" in (prot["efforts_standard"] or "").lower()
        )

    if cond_rows:
        raw_approvals = [r["condition_name"] for r in cond_rows]
        required_approvals = _normalize_approvals(raw_approvals)

    return ParsedMergerAgreement(
        efforts_standard=efforts_standard,
        required_regulatory_approvals=required_approvals,
        outside_date=outside_date,
        outside_date_extensions=extension_desc,
        extended_outside_date=extended_outside_date,
        target_termination_fee_usd=target_fee,
        reverse_termination_fee_usd=reverse_fee,
        has_ticking_fee=bool(
            dma.get("ticking_fee") or dma.get("ticking_fee_present")
        ),
        ticking_fee_details=dma.get("ticking_fee_details"),
        divestiture_commitment=divestiture_commitment,
        litigation_commitment=litigation_commitment,
    )


# ------------------------------------------------------------------
# PressReleaseData from MARS
# ------------------------------------------------------------------

async def load_press_release_data_from_mars(
    deal_pk: int,
) -> Optional[PressReleaseData]:
    """Synthesize PressReleaseData from deals + regulatory_reviews.

    Returns None if deal has no date_announced.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                d.date_announced,
                d.date_expected_close,
                d.date_expected_close_parsed,
                dma.outside_date AS outside_date
            FROM deals d
            -- OQ-N20: v1 deal_dma_terms.long_stop_date -> v2 odm.outside_date
            LEFT JOIN deal_outside_date_mechanics dma ON d.deal_pk = dma.deal_pk
            WHERE d.deal_pk = $1
            """,
            deal_pk,
        )

        if not row or not row["date_announced"]:
            return None

        # Get jurisdictions from regulatory_reviews
        jur_rows = await conn.fetch(
            "SELECT jurisdiction_code "
            "FROM regulatory_reviews "
            "WHERE deal_pk = $1 "
            "AND review_status != 'not_filed'",
            deal_pk,
        )

    # Map jurisdiction_code → TimeAgent names
    jurisdictions = []
    for jr in jur_rows:
        code = jr["jurisdiction_code"]
        mapped = _JURISDICTION_CODE_MAP.get(code)
        if mapped:
            jurisdictions.append(mapped)

    outside_str = None
    if row["outside_date"]:
        outside_str = str(row["outside_date"])

    return PressReleaseData(
        announcement_date=row["date_announced"],
        stated_close_timeline=row["date_expected_close"],
        stated_close_date=row["date_expected_close_parsed"],
        mentioned_jurisdictions=jurisdictions,
        outside_date_mentioned=outside_str,
    )


# ------------------------------------------------------------------
# Regulatory jurisdiction flags from MARS
# ------------------------------------------------------------------

async def load_regulatory_flags_from_mars(
    deal_pk: int,
) -> dict[str, bool]:
    """Load jurisdiction applicability flags from MARS v2.

    Queries regulatory_reviews — if a row exists for a jurisdiction,
    the jurisdiction is applicable.

    Returns dict like {"HSR": True, "EC": True}.
    Empty dict if no regulatory reviews populated.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT jurisdiction_code FROM regulatory_reviews "
            "WHERE deal_pk = $1 AND review_status != 'not_filed'",
            deal_pk,
        )

    flags: dict[str, bool] = {}
    for r in rows:
        code = r["jurisdiction_code"]
        mapped = _JURISDICTION_CODE_MAP.get(code)
        if mapped:
            flags[mapped] = True

    return flags
