"""
Step 0: Deal Parameter Validation

Resolves tickers, checks MARS for existing deal data, constructs DealParameters.
"""
import logging
from typing import Optional
from models.deal import (
    DealInput, DealParameters, DealStructure,
    BuyerType, ValidationResult,
)
from db.queries_comparables import find_deal_by_tickers

logger = logging.getLogger(__name__)


async def validate_deal(deal_input: DealInput) -> ValidationResult:
    """Validate deal input and construct enriched DealParameters."""
    errors = []
    warnings = []

    # Step 1: Resolve CIKs via AF-SECAPI
    acquirer_cik, acquirer_name = await _resolve_ticker(
        deal_input.acquirer_ticker
    )
    if not acquirer_cik:
        errors.append(
            f"Could not resolve acquirer ticker: "
            f"{deal_input.acquirer_ticker}"
        )

    target_cik, target_name = await _resolve_ticker(
        deal_input.target_ticker
    )
    if not target_cik:
        errors.append(
            f"Could not resolve target ticker: "
            f"{deal_input.target_ticker}"
        )

    if errors:
        return ValidationResult(is_valid=False, errors=errors)

    # Step 2: Check MARS database
    mars_deal = None
    try:
        mars_deal = await find_deal_by_tickers(
            deal_input.acquirer_ticker, deal_input.target_ticker
        )
    except Exception as e:
        logger.warning(f"MARS lookup failed: {e}")
        warnings.append(f"MARS database lookup failed: {e}")

    # Step 3: Build DealParameters
    if mars_deal:
        deal_params = _build_from_mars(
            deal_input, mars_deal,
            acquirer_cik, acquirer_name,
            target_cik, target_name,
        )
        if mars_deal.get("deal_outcome") in ("Completed", "Terminated"):
            warnings.append(
                f"Deal is {mars_deal['deal_outcome']}. "
                f"Producing forward estimate anyway."
            )
    else:
        deal_params = _build_from_input(
            deal_input, acquirer_cik, acquirer_name,
            target_cik, target_name,
        )
        warnings.append("Deal not found in MARS. Using EDGAR-only data.")

    return ValidationResult(
        is_valid=True, deal_params=deal_params, warnings=warnings
    )


async def _resolve_ticker(
    ticker: str,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a ticker to CIK and company name via AF-SECAPI."""
    try:
        from sec_api_tools import (
            EdgarClient, resolve_cik, get_company_info,
        )
        async with EdgarClient() as client:
            cik = await resolve_cik(ticker, client)
            if cik:
                info = await get_company_info(cik, client)
                name = info.name if info else ticker
                return cik, name
    except Exception as e:
        logger.error(f"Failed to resolve ticker {ticker}: {e}")
    return None, None


def _build_from_mars(
    deal_input: DealInput, mars: dict,
    acq_cik: str, acq_name: str, tgt_cik: str, tgt_name: str,
) -> DealParameters:
    """Build DealParameters from MARS deal data."""
    consideration = (mars.get("type_of_consideration") or "").lower()
    if "cash" in consideration and "stock" in consideration:
        structure = DealStructure.MIXED
    elif "tender" in consideration:
        structure = DealStructure.TENDER
    elif "stock" in consideration:
        structure = DealStructure.STOCK
    else:
        structure = DealStructure.CASH

    acq_type = (mars.get("acquirer_type") or "").lower()
    if "pe" in acq_type or "sponsor" in acq_type:
        buyer_type = BuyerType.PE_SPONSOR
    elif "financial" in acq_type:
        buyer_type = BuyerType.FINANCIAL
    else:
        buyer_type = BuyerType.STRATEGIC

    return DealParameters(
        acquirer_ticker=deal_input.acquirer_ticker,
        acquirer_name=mars.get("acquirer_name", acq_name),
        acquirer_cik=acq_cik,
        target_ticker=deal_input.target_ticker,
        target_name=mars.get("target_name", tgt_name),
        target_cik=tgt_cik,
        deal_value_usd=(
            deal_input.deal_value_usd
            or mars.get("deal_value_usd", 0)
        ),
        deal_structure=structure,
        buyer_type=buyer_type,
        announcement_date=(
            deal_input.announcement_date
            or mars.get("date_announced")
        ),
        sector=mars.get("gics_sector", ""),
        industry=mars.get("industry", ""),
        gics_sector=mars.get("gics_sector"),
        deal_attitude=mars.get("deal_attitude", "Friendly"),
        mars_deal_pk=mars.get("deal_pk"),
        mars_deal_id=mars.get("deal_id"),
    )


def _build_from_input(
    deal_input: DealInput,
    acq_cik: str, acq_name: str, tgt_cik: str, tgt_name: str,
) -> DealParameters:
    """Build DealParameters from user input only (no MARS data)."""
    from datetime import date
    return DealParameters(
        acquirer_ticker=deal_input.acquirer_ticker,
        acquirer_name=acq_name or deal_input.acquirer_ticker,
        acquirer_cik=acq_cik,
        target_ticker=deal_input.target_ticker,
        target_name=tgt_name or deal_input.target_ticker,
        target_cik=tgt_cik,
        deal_value_usd=deal_input.deal_value_usd or 0.0,
        deal_structure=DealStructure.CASH,
        buyer_type=BuyerType.STRATEGIC,
        announcement_date=deal_input.announcement_date or date.today(),
        sector="",
        industry="",
    )
