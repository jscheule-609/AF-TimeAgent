"""Core deal data models."""
from pydantic import BaseModel, Field
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


class DealInput(BaseModel):
    """User-provided input to kick off the tool."""
    acquirer_ticker: str
    target_ticker: str
    deal_value_usd: Optional[float] = None
    announcement_date: Optional[date] = None


class DealParameters(BaseModel):
    """Validated and enriched deal parameters after Step 0 + Step 1."""
    acquirer_ticker: str
    acquirer_name: str
    acquirer_cik: str
    target_ticker: str
    target_name: str
    target_cik: str
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
