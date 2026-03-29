"""
Step 1: Press Release Parsing (parallel with Step 2)

Finds 8-K filing, extracts press release exhibit, parses timing data.
"""
import logging
from datetime import timedelta
from models.deal import DealParameters
from models.documents import PressReleaseData
from parsers.press_release_parser import parse_press_release

logger = logging.getLogger(__name__)


async def parse_deal_press_release(
    deal_params: DealParameters,
) -> PressReleaseData:
    """Find and parse the deal announcement press release."""

    # Try MARS first if deal is known
    if deal_params.mars_deal_pk:
        try:
            from db.read_autoresearch import (
                load_press_release_data_from_mars,
            )
            mars_data = await load_press_release_data_from_mars(
                deal_params.mars_deal_pk
            )
            if mars_data and mars_data.mentioned_jurisdictions:
                logger.info(
                    "Press release data loaded from MARS "
                    "(autoresearch)"
                )
                return mars_data
        except Exception as e:
            logger.warning(
                "MARS press release load failed, "
                f"falling back to EDGAR: {e}"
            )

    # Fallback: fetch from EDGAR
    try:
        from sec_api_tools import EdgarClient
        async with EdgarClient() as client:
            date_from = (
                deal_params.announcement_date - timedelta(days=5)
            )
            date_to = (
                deal_params.announcement_date + timedelta(days=5)
            )

            # Try acquirer first, then target
            for cik in [
                deal_params.acquirer_cik,
                deal_params.target_cik,
            ]:
                text = await _find_press_release(
                    client, cik, date_from, date_to,
                )
                if text:
                    return await parse_press_release(
                        text, deal_params.announcement_date,
                    )

    except Exception as e:
        logger.error(f"Press release parsing failed: {e}")

    logger.warning(
        "No press release found, returning empty PressReleaseData"
    )
    return PressReleaseData(
        announcement_date=deal_params.announcement_date
    )


async def _find_press_release(
    client, cik: str, date_from, date_to,
) -> str | None:
    """Search for 8-K and extract press release exhibit."""
    from sec_api_tools import (
        search_filings, get_filing_index,
        get_filing_document,
    )
    try:
        result = await search_filings(
            client, form_types=["8-K"], cik=cik,
            date_from=date_from, date_to=date_to,
            max_results=5,
        )

        for filing in result.filings:
            accession = filing.accession_number
            if not accession:
                continue

            # Get filing index to find EX-99.1
            index = await get_filing_index(
                cik, accession, client,
            )
            if not index:
                continue

            for doc in index:
                doc_type = (
                    doc.get("type") or ""
                ).upper()
                if "EX-99" in doc_type or "PRESS" in doc_type:
                    doc_url = (
                        doc.get("url")
                        or doc.get("document_url")
                    )
                    if doc_url:
                        resp = await client.get(doc_url)
                        if hasattr(resp, "text"):
                            return resp.text
                        return str(resp)

            # Fallback: use primary document
            doc = await get_filing_document(filing, client)
            if doc and doc.clean_text:
                return doc.clean_text[:30000]

    except Exception as e:
        logger.warning(
            f"Failed to find press release for CIK {cik}: {e}"
        )
    return None
