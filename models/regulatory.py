"""Regulatory jurisdiction requirement models."""
from pydantic import BaseModel, Field, model_validator


class JurisdictionRequirement(BaseModel):
    """A single jurisdiction's filing requirement assessment."""
    jurisdiction: str
    is_required: bool
    confidence: float = Field(ge=0.0, le=1.0)
    applicability: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str  # "merger_agreement", "revenue_threshold", "comparable_precedent", "sector_assessment"
    revenue_data: dict = {}
    notes: str = ""

    @model_validator(mode="after")
    def derive_applicability(self) -> "JurisdictionRequirement":
        """Required filings apply surely; optional confidence is a probability."""
        self.applicability = 1.0 if self.is_required else self.confidence
        return self
