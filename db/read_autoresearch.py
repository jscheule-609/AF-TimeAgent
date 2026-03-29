"""
Read autoresearch-populated data from MARS.

These functions query tables that AF-ARB_AUTORESEARCH writes to
and return AF-TimeAgent Pydantic models. TimeAgent NEVER writes
to these tables — it only reads.
"""
import json
import logging
from datetime import date
from dateutil.relativedelta import relativedelta
from typing import Optional

from db.connection import get_pool
from models.deal import (
    DealParameters, DealStructure, BuyerType,
)
from models.documents import ParsedMergerAgreement, PressReleaseData

logger = logging.getLogger(__name__)


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
                pa.ticker  AS acquirer_ticker,
                pa.company_name AS acquirer_name,
                pt.ticker  AS target_ticker,
                pt.company_name AS target_name
            FROM deals d
            LEFT JOIN parties pa
                ON d.deal_pk = pa.deal_pk AND pa.role = 'acquirer'
            LEFT JOIN parties pt
                ON d.deal_pk = pt.deal_pk AND pt.role = 'target'
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
        deal_value_usd=float(row["deal_value_usd"] or 0),
        deal_structure=_map_consideration(
            row["type_of_consideration"],
            row["deal_structure_type"],
        ),
        buyer_type=BuyerType.STRATEGIC,
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
    """Load merger agreement data from deal_dma_terms + break_fees +
    deal_regulatory_efforts.

    Returns None if no deal_dma_terms row exists (autoresearch
    has not yet extracted the merger agreement for this deal).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        dma = await conn.fetchrow(
            "SELECT * FROM deal_dma_terms WHERE deal_pk = $1",
            deal_pk,
        )
        if not dma:
            return None

        fees = await conn.fetch(
            "SELECT * FROM break_fees WHERE deal_pk = $1",
            deal_pk,
        )
        efforts = await conn.fetchrow(
            "SELECT * FROM deal_regulatory_efforts "
            "WHERE deal_pk = $1",
            deal_pk,
        )

    # Parse fees
    target_fee = None
    reverse_fee = None
    for fee in fees:
        party = (fee["party"] or "").lower()
        fee_type = (fee["fee_type"] or "").lower()
        amount = float(fee["amount_usd"] or fee["amount"] or 0)
        if "target" in party and "termination" in fee_type:
            target_fee = amount
        elif "acquirer" in party or "reverse" in fee_type:
            reverse_fee = amount

    # Parse outside date + extensions
    outside_date = dma["long_stop_date"]
    extensions = dma["long_stop_extensions"] or 0
    extended_outside_date = None
    extension_desc = []
    if outside_date and extensions > 0:
        extended_outside_date = (
            outside_date + relativedelta(months=extensions)
        )
        extension_desc = [
            f"{extensions}-month extension available"
        ]

    # Parse regulatory efforts
    efforts_standard = "unknown"
    required_approvals: list[str] = []
    divestiture_commitment: str | None = None
    litigation_commitment = False

    if efforts:
        efforts_standard = (
            efforts["efforts_standard"] or "unknown"
        )
        raw_approvals = efforts["required_approvals"]
        if isinstance(raw_approvals, list):
            required_approvals = raw_approvals
        elif isinstance(raw_approvals, str):
            try:
                parsed = json.loads(raw_approvals)
                if isinstance(parsed, list):
                    required_approvals = parsed
            except (json.JSONDecodeError, TypeError):
                required_approvals = [raw_approvals]
        elif raw_approvals:
            required_approvals = list(raw_approvals)
        if efforts["divestiture_commitment"]:
            divestiture_commitment = (
                efforts["divestiture_cap"]
                or "yes (no cap specified)"
            )
        else:
            divestiture_commitment = "no"
        litigation_commitment = bool(
            efforts["litigation_commitment"]
        )

    return ParsedMergerAgreement(
        efforts_standard=efforts_standard,
        required_regulatory_approvals=required_approvals,
        outside_date=outside_date,
        outside_date_extensions=extension_desc,
        extended_outside_date=extended_outside_date,
        target_termination_fee_usd=target_fee,
        reverse_termination_fee_usd=reverse_fee,
        has_ticking_fee=bool(dma["ticking_fee_present"]),
        ticking_fee_details=dma["ticking_fee_details"],
        divestiture_commitment=divestiture_commitment,
        litigation_commitment=litigation_commitment,
    )


# ------------------------------------------------------------------
# PressReleaseData from MARS
# ------------------------------------------------------------------

async def load_press_release_data_from_mars(
    deal_pk: int,
) -> Optional[PressReleaseData]:
    """Synthesize PressReleaseData from deals + regulatory tables.

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
                dma.long_stop_date,
                da.is_hsr_applicable,
                ec.is_ec_approval_required,
                cma.is_cma_approval_required,
                cfius.is_cfius_review_required,
                samr.is_samr_approval_required
            FROM deals d
            LEFT JOIN deal_dma_terms dma
                ON d.deal_pk = dma.deal_pk
            LEFT JOIN deal_antitrust da
                ON d.deal_pk = da.deal_pk
            LEFT JOIN deal_ec_antitrust ec
                ON d.deal_pk = ec.deal_pk
            LEFT JOIN deal_cma_antitrust cma
                ON d.deal_pk = cma.deal_pk
            LEFT JOIN deal_cfius cfius
                ON d.deal_pk = cfius.deal_pk
            LEFT JOIN deal_samr_antitrust samr
                ON d.deal_pk = samr.deal_pk
            WHERE d.deal_pk = $1
            """,
            deal_pk,
        )

    if not row or not row["date_announced"]:
        return None

    # Derive mentioned jurisdictions from regulatory flags
    jurisdictions = []
    if row["is_hsr_applicable"]:
        jurisdictions.append("HSR")
    if row["is_ec_approval_required"]:
        jurisdictions.append("EC")
    if row["is_cma_approval_required"]:
        jurisdictions.append("CMA")
    if row["is_cfius_review_required"]:
        jurisdictions.append("CFIUS")
    if row["is_samr_approval_required"]:
        jurisdictions.append("SAMR")

    outside_str = None
    if row["long_stop_date"]:
        outside_str = str(row["long_stop_date"])

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
    """Load jurisdiction applicability flags from MARS.

    Returns dict like {"HSR": True, "EC": True, "CMA": False}.
    Only includes jurisdictions where autoresearch has data.
    Empty dict if no regulatory tables populated.
    """
    pool = await get_pool()
    flags: dict[str, bool] = {}

    async with pool.acquire() as conn:
        da = await conn.fetchrow(
            "SELECT is_hsr_applicable FROM deal_antitrust "
            "WHERE deal_pk = $1",
            deal_pk,
        )
        if da and da["is_hsr_applicable"] is not None:
            flags["HSR"] = bool(da["is_hsr_applicable"])

        ec = await conn.fetchrow(
            "SELECT is_ec_approval_required "
            "FROM deal_ec_antitrust WHERE deal_pk = $1",
            deal_pk,
        )
        if ec and ec["is_ec_approval_required"] is not None:
            flags["EC"] = bool(ec["is_ec_approval_required"])

        cma = await conn.fetchrow(
            "SELECT is_cma_approval_required "
            "FROM deal_cma_antitrust WHERE deal_pk = $1",
            deal_pk,
        )
        if cma and cma["is_cma_approval_required"] is not None:
            flags["CMA"] = bool(cma["is_cma_approval_required"])

        cfius = await conn.fetchrow(
            "SELECT is_cfius_review_required "
            "FROM deal_cfius WHERE deal_pk = $1",
            deal_pk,
        )
        if cfius and cfius["is_cfius_review_required"] is not None:
            flags["CFIUS"] = bool(
                cfius["is_cfius_review_required"]
            )

        samr = await conn.fetchrow(
            "SELECT is_samr_approval_required "
            "FROM deal_samr_antitrust WHERE deal_pk = $1",
            deal_pk,
        )
        if samr and samr["is_samr_approval_required"] is not None:
            flags["SAMR"] = bool(
                samr["is_samr_approval_required"]
            )

    return flags
