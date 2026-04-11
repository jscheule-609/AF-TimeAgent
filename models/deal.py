"""Core deal data models."""
from pydantic import BaseModel, Field, model_validator
from typing import Optional
from datetime import date
from enum import Enum


class DealStructure(str, Enum):
    CASH = "cash"
    STOCK = "stock"
    MIXED = "mixed"
    TENDER = "tender"


class BuyerType(str, Enum):
    STRATEGIC = "strategic"
    FINANCIAL = "financial"
    PE_SPONSOR = "pe_sponsor"


_PE_KEYWORDS = {
    "capital", "partners", "equity", "fund", "investment", "holdings",
    "ventures", "advisors", "management", "acquisition corp", "sponsor",
}


def classify_buyer_type(
    acquirer_name: str | None,
    party_type: str | None = None,
) -> BuyerType:
    """Classify buyer type using party_type from MARS v2 (preferred)
    or name-based keyword heuristic (fallback).

    party_type values from party_entities: corporation, llc, lp,
    government_entity, regulatory_body.  LP and LLC structures are
    strong indicators of PE/sponsor vehicles.
    """
    if party_type:
        pt = party_type.lower().strip()
        if pt in ("lp", "llc"):
            return BuyerType.PE_SPONSOR

    if not acquirer_name:
        return BuyerType.STRATEGIC
    name_lower = acquirer_name.lower()
    if any(kw in name_lower for kw in _PE_KEYWORDS):
        return BuyerType.PE_SPONSOR
    return BuyerType.STRATEGIC


class DealInput(BaseModel):
    """User-provided input to kick off the tool."""
    acquirer_ticker: str = ""
    target_ticker: str = ""
    deal_pk: Optional[int] = None
    deal_value_usd: Optional[float] = None
    announcement_date: Optional[date] = None

    @model_validator(mode="after")
    def check_input_provided(self) -> "DealInput":
        has_pk = self.deal_pk is not None
        has_tickers = bool(self.acquirer_ticker and self.target_ticker)
        if not has_pk and not has_tickers:
            raise ValueError(
                "Either deal_pk or both acquirer_ticker and "
                "target_ticker must be provided"
            )
        return self


class DealParameters(BaseModel):
    """Validated and enriched deal parameters after Step 0 + Step 1."""
    acquirer_ticker: str
    acquirer_name: str
    acquirer_cik: str
    target_ticker: str
    target_name: str
    target_cik: str
    acquirer_country: Optional[str] = None
    target_country: Optional[str] = None
    deal_value_usd: float
    deal_structure: DealStructure
    buyer_type: BuyerType
    announcement_date: date
    sector: str
    industry: str
    gics_sector: Optional[str] = None
    deal_attitude: str = "Friendly"
    mars_deal_pk: Optional[int] = None
    mars_deal_id: Optional[str] = None


class ValidationResult(BaseModel):
    """Result of Step 0 validation."""
    is_valid: bool
    deal_params: Optional[DealParameters] = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
