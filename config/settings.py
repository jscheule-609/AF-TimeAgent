"""
Pydantic Settings for all configuration.
Load from .env or environment variables.
"""
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # MARS Database
    mars_db_host: str = "localhost"
    mars_db_port: int = 5434
    mars_db_name: str = "mars"
    mars_db_user: str = ""
    mars_db_password: str = ""

    # AF-SECAPI
    sec_user_agent: str = ""

    # Brave Search API
    brave_api_key: str = ""

    # OpenRouter (for LLM extraction)
    openrouter_api_key: str = ""
    extraction_model: str = "google/gemini-2.5-flash"
    reasoning_model: str = "google/gemini-2.5-flash"

    # Tool behavior
    max_comparables_per_group: int = 15
    comparable_lookback_years: int = 5
    time_weight_half_life_months: int = 24
    default_confidence_threshold: float = 0.6

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"
