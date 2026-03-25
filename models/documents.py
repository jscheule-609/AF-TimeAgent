"""Parsed document models from 10-K and merger agreement ingestion."""
from pydantic import BaseModel
from typing import Optional
from datetime import date


class GeographicSegment(BaseModel):
    """Revenue or asset breakdown by geography."""
    region: str
    revenue_usd: Optional[float] = None
    revenue_pct: Optional[float] = None
    assets_usd: Optional[float] = None
    assets_pct: Optional[float] = None


class CompetitorInfo(BaseModel):
    """Competitor mentioned in 10-K."""
    name: str
    context: str
    relationship: str  # "direct", "indirect", "potential"


class ParsedTenK(BaseModel):
    """Extracted data from a 10-K filing."""
    company_ticker: str
    company_name: str
    fiscal_year_end: date
    filing_date: date
    geographic_segments: list[GeographicSegment]
    total_revenue_usd: Optional[float] = None
    competitors: list[CompetitorInfo]
    business_description: str
    competition_section: str
    risk_factors_excerpt: str
    products_and_services: str
    full_item1_text: str
    full_item1a_text: str


class MergerAgreementProvision(BaseModel):
    """A specific provision extracted from the merger agreement."""
    provision_type: str
    text: str
    parsed_value: Optional[str] = None


class ParsedMergerAgreement(BaseModel):
    """Extracted data from the merger agreement."""
    efforts_standard: str
    hsr_filing_deadline_days: Optional[int] = None
    ec_filing_deadline_days: Optional[int] = None
    other_filing_deadlines: dict[str, int] = {}
    required_regulatory_approvals: list[str]
    outside_date: Optional[date] = None
    outside_date_extensions: list[str] = []
    extended_outside_date: Optional[date] = None
    target_termination_fee_usd: Optional[float] = None
    reverse_termination_fee_usd: Optional[float] = None
    has_ticking_fee: bool = False
    ticking_fee_details: Optional[str] = None
    divestiture_commitment: Optional[str] = None
    litigation_commitment: bool = False
    provisions: list[MergerAgreementProvision] = []
    regulatory_efforts_section: str = ""
    conditions_to_closing_section: str = ""


class PressReleaseData(BaseModel):
    """Extracted data from the deal press release / 8-K."""
    announcement_date: date
    stated_close_timeline: Optional[str] = None
    stated_close_date: Optional[date] = None
    mentioned_jurisdictions: list[str] = []
    mentioned_conditions: list[str] = []
    deal_rationale_excerpt: str = ""
    stated_synergies: Optional[str] = None
    outside_date_mentioned: Optional[str] = None
    raw_timing_language: str = ""
