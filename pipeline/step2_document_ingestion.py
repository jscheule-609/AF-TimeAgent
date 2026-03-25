"""
Step 2: Document Ingestion (parallel with Step 1)

Ingests 10-K (acquirer + target) and merger agreement.
"""
import logging
from models.deal import DealParameters
from models.documents import ParsedTenK, ParsedMergerAgreement
from parsers.tenk_parser import parse_tenk
from parsers.merger_agreement_parser import parse_merger_agreement

logger = logging.getLogger(__name__)


async def ingest_documents(
    deal_params: DealParameters,
) -> tuple[ParsedTenK | None, ParsedTenK | None, ParsedMergerAgreement | None]:
    """Ingest 10-K filings for both parties and the merger agreement."""
    import asyncio

    tenk_acquirer_task = _ingest_tenk(deal_params.acquirer_cik, deal_params.acquirer_ticker, deal_params.acquirer_name)
    tenk_target_task = _ingest_tenk(deal_params.target_cik, deal_params.target_ticker, deal_params.target_name)
    merger_task = _ingest_merger_agreement(deal_params)

    tenk_acquirer, tenk_target, merger = await asyncio.gather(
        tenk_acquirer_task, tenk_target_task, merger_task,
        return_exceptions=True,
    )

    # Handle exceptions gracefully
    if isinstance(tenk_acquirer, Exception):
        logger.error(f"Acquirer 10-K ingestion failed: {tenk_acquirer}")
        tenk_acquirer = None
    if isinstance(tenk_target, Exception):
        logger.error(f"Target 10-K ingestion failed: {tenk_target}")
        tenk_target = None
    if isinstance(merger, Exception):
        logger.error(f"Merger agreement ingestion failed: {merger}")
        merger = None

    return tenk_acquirer, tenk_target, merger


async def _ingest_tenk(cik: str, ticker: str, company_name: str) -> ParsedTenK | None:
    """Fetch and parse the most recent 10-K for a company."""
    try:
        from sec_api_tools import EdgarClient
        async with EdgarClient() as client:
            filings = await client.search_filings(
                form_types=["10-K"], cik=cik, max_results=3
            )
            if not filings:
                logger.warning(f"No 10-K found for {ticker}")
                return None

            filing = filings[0]
            accession = filing.get("accession_number")
            if not accession:
                return None

            content = await client.get_filing_by_accession(cik, accession)
            if not content:
                return None

            text = str(content)
            filing_date_str = filing.get("filing_date", "")

            from datetime import date
            try:
                filing_date = date.fromisoformat(filing_date_str) if filing_date_str else date.today()
            except ValueError:
                filing_date = date.today()

            return await parse_tenk(
                text=text,
                company_ticker=ticker,
                company_name=company_name,
                fiscal_year_end=filing_date,
                filing_date=filing_date,
            )
    except Exception as e:
        logger.error(f"10-K ingestion failed for {ticker}: {e}")
        return None


async def _ingest_merger_agreement(deal_params: DealParameters) -> ParsedMergerAgreement | None:
    """Find and parse the merger agreement."""
    try:
        from sec_api_tools import EdgarClient
        async with EdgarClient() as client:
            # Priority: DEFM14A → S-4 → PREM14A → 8-K
            for form_type in ["DEFM14A", "S-4", "PREM14A"]:
                for cik in [deal_params.target_cik, deal_params.acquirer_cik]:
                    text = await _find_merger_agreement_in_filing(client, cik, form_type)
                    if text:
                        return await parse_merger_agreement(text)

            # Fallback: check 8-K exhibits
            for cik in [deal_params.acquirer_cik, deal_params.target_cik]:
                text = await _find_merger_agreement_in_8k(client, cik, deal_params)
                if text:
                    return await parse_merger_agreement(text)

    except Exception as e:
        logger.error(f"Merger agreement ingestion failed: {e}")
    return None


async def _find_merger_agreement_in_filing(client, cik: str, form_type: str) -> str | None:
    """Search for merger agreement exhibit (EX-2.1) in a specific filing type."""
    try:
        filings = await client.search_filings(
            form_types=[form_type], cik=cik, max_results=3
        )
        if not filings:
            return None

        for filing in filings:
            accession = filing.get("accession_number")
            if not accession:
                continue

            index = await client.get_filing_index(cik, accession)
            if not index:
                continue

            documents = index.get("documents", [])
            for doc in documents:
                doc_type = (doc.get("type") or "").upper()
                if "EX-2" in doc_type or "EX-10" in doc_type:
                    doc_url = doc.get("url") or doc.get("document_url")
                    if doc_url:
                        content = await client.get(doc_url)
                        if hasattr(content, "text"):
                            return content.text
                        return str(content)

            # Fallback: use the primary document
            content = await client.get_filing_by_accession(cik, accession)
            if content:
                return str(content)[:50000]

    except Exception as e:
        logger.warning(f"Failed searching {form_type} for CIK {cik}: {e}")
    return None


async def _find_merger_agreement_in_8k(client, cik: str, deal_params: DealParameters) -> str | None:
    """Check 8-K near announcement for merger agreement exhibit."""
    from datetime import timedelta
    try:
        date_from = deal_params.announcement_date - timedelta(days=5)
        date_to = deal_params.announcement_date + timedelta(days=5)

        filings = await client.search_filings(
            form_types=["8-K"], cik=cik,
            date_from=str(date_from), date_to=str(date_to),
            max_results=3,
        )
        if not filings:
            return None

        for filing in filings:
            accession = filing.get("accession_number")
            if not accession:
                continue

            index = await client.get_filing_index(cik, accession)
            if not index:
                continue

            for doc in index.get("documents", []):
                doc_type = (doc.get("type") or "").upper()
                if "EX-2" in doc_type or "EX-10" in doc_type:
                    doc_url = doc.get("url") or doc.get("document_url")
                    if doc_url:
                        content = await client.get(doc_url)
                        if hasattr(content, "text"):
                            return content.text
                        return str(content)

    except Exception as e:
        logger.warning(f"Failed searching 8-K exhibits for CIK {cik}: {e}")
    return None
