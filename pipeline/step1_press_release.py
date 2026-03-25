"""
Step 1: Press Release Parsing (parallel with Step 2)

Finds 8-K filing, extracts press release exhibit, parses timing data via LLM.
"""
import logging
from datetime import timedelta
from models.deal import DealParameters
from models.documents import PressReleaseData
from parsers.press_release_parser import parse_press_release

logger = logging.getLogger(__name__)


async def parse_deal_press_release(deal_params: DealParameters) -> PressReleaseData:
    """Find and parse the deal announcement press release."""
    try:
        from sec_api_tools import EdgarClient
        async with EdgarClient() as client:
            # Search for 8-K near announcement date
            date_from = deal_params.announcement_date - timedelta(days=5)
            date_to = deal_params.announcement_date + timedelta(days=5)

            # Try acquirer first
            text = await _find_press_release(
                client, deal_params.acquirer_cik, date_from, date_to
            )

            # Fallback to target
            if not text:
                text = await _find_press_release(
                    client, deal_params.target_cik, date_from, date_to
                )

            if text:
                return await parse_press_release(text, deal_params.announcement_date)

    except Exception as e:
        logger.error(f"Press release parsing failed: {e}")

    # Return empty data if nothing found
    logger.warning("No press release found, returning empty PressReleaseData")
    return PressReleaseData(announcement_date=deal_params.announcement_date)


async def _find_press_release(client, cik: str, date_from, date_to) -> str | None:
    """Search for 8-K and extract press release exhibit text."""
    try:
        filings = await client.search_filings(
            form_types=["8-K"], cik=cik,
            date_from=str(date_from), date_to=str(date_to),
            max_results=5,
        )

        if not filings:
            return None

        for filing in filings:
            accession = filing.get("accession_number")
            if not accession:
                continue

            # Get filing index to find EX-99.1 (press release exhibit)
            index = await client.get_filing_index(cik, accession)
            if not index:
                continue

            documents = index.get("documents", [])
            for doc in documents:
                doc_type = (doc.get("type") or "").upper()
                if "EX-99" in doc_type or "PRESS" in doc_type.upper():
                    doc_url = doc.get("url") or doc.get("document_url")
                    if doc_url:
                        content = await client.get(doc_url)
                        if hasattr(content, "text"):
                            return content.text
                        return str(content)

            # Fallback: use primary document
            primary = await client.get_filing_by_accession(cik, accession)
            if primary:
                return str(primary)[:30000]

    except Exception as e:
        logger.warning(f"Failed to find press release for CIK {cik}: {e}")
    return None
