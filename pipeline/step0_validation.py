"""
Step 0: Deal Parameter Validation

Resolves tickers, checks MARS for existing deal data, constructs DealParameters.
"""
import logging
from typing import Optional
from models.deal import (
    DealInput, DealParameters, DealStructure,
    BuyerType, ValidationResult, classify_buyer_type,
)
from db.queries_comparables import find_deal_by_tickers

logger = logging.getLogger(__name__)


async def validate_deal(deal_input: DealInput) -> ValidationResult:
    """Validate deal input and construct enriched DealParameters."""
    errors = []
    warnings = []

    # ── Path A: deal_pk provided → load from MARS directly ──
    if deal_input.deal_pk:
        from db.read_autoresearch import load_deal_params_from_mars
        deal_params = await load_deal_params_from_mars(
            deal_input.deal_pk
        )
        if deal_params is None:
            errors.append(
                f"deal_pk={deal_input.deal_pk} not found in MARS"
            )
            return ValidationResult(
                is_valid=False, errors=errors
            )

        # Still resolve CIKs for 10-K fetching
        acq_cik, _ = await _resolve_ticker(
            deal_params.acquirer_ticker
        )
        tgt_cik, _ = await _resolve_ticker(
            deal_params.target_ticker
        )
        if acq_cik:
            deal_params.acquirer_cik = acq_cik
        else:
            warnings.append(
                "Could not resolve acquirer CIK for "
                f"{deal_params.acquirer_ticker}"
            )
        if tgt_cik:
            deal_params.target_cik = tgt_cik
        else:
            warnings.append(
                "Could not resolve target CIK for "
                f"{deal_params.target_ticker}"
            )

        warnings.append(
            "Deal loaded from MARS (autoresearch)"
        )
        return ValidationResult(
            is_valid=True,
            deal_params=deal_params,
            warnings=warnings,
        )

    # ── Path B: tickers provided → SEC API + MARS lookup ──
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

    # Check MARS database by tickers
    mars_deal = None
    try:
        mars_deal = await find_deal_by_tickers(
            deal_input.acquirer_ticker,
            deal_input.target_ticker,
        )
    except Exception as e:
        logger.warning(f"MARS lookup failed: {e}")
        warnings.append(f"MARS database lookup failed: {e}")

    if mars_deal:
        deal_params = _build_from_mars(
            deal_input, mars_deal,
            acquirer_cik, acquirer_name,
            target_cik, target_name,
        )
        if mars_deal.get("deal_outcome") in (
            "Completed", "Terminated",
        ):
            warnings.append(
                f"Deal is {mars_deal['deal_outcome']}. "
                f"Producing forward estimate anyway."
            )
    else:
        deal_params = _build_from_input(
            deal_input, acquirer_cik, acquirer_name,
            target_cik, target_name,
        )
        warnings.append(
            "Deal not found in MARS. Using EDGAR-only data."
        )

    return ValidationResult(
        is_valid=True, deal_params=deal_params,
        warnings=warnings,
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

    buyer_type = classify_buyer_type(
        mars.get("acquirer_name") or acq_name,
        party_type=mars.get("acquirer_party_type"),
    )

    return DealParameters(
        acquirer_ticker=deal_input.acquirer_ticker,
        acquirer_name=mars.get("acquirer_name", acq_name),
        acquirer_cik=acq_cik,
        target_ticker=deal_input.target_ticker,
        target_name=mars.get("target_name", tgt_name),
        target_cik=tgt_cik,
        acquirer_country=mars.get("acquirer_country"),
        target_country=mars.get("target_country"),
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
        buyer_type=classify_buyer_type(acq_name or deal_input.acquirer_ticker, party_type=None),
        announcement_date=deal_input.announcement_date or date.today(),
        sector="",
        industry="",
    )
