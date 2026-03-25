"""
Statutory timelines, jurisdiction parameters, and regulatory constants.
These are LEGAL FACTS, not estimates — they define the state machine clocks.
"""

# ═══════════════════════════════════════════════════════════
# HSR (Hart-Scott-Rodino) — US Antitrust
# ═══════════════════════════════════════════════════════════
HSR_INITIAL_WAITING_PERIOD_DAYS = 30
HSR_CASH_TENDER_WAITING_PERIOD_DAYS = 15
HSR_SECOND_REQUEST_CLOCK_DAYS = None
HSR_TYPICAL_SECOND_REQUEST_COMPLIANCE_DAYS_RANGE = (90, 180)
HSR_EARLY_TERMINATION_TYPICAL_DAYS = (12, 20)

# ═══════════════════════════════════════════════════════════
# European Commission
# ═══════════════════════════════════════════════════════════
EC_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE = (20, 90)
EC_PHASE_1_WORKING_DAYS = 25
EC_PHASE_1_EXTENSION_WORKING_DAYS = 10
EC_PHASE_2_WORKING_DAYS = 90
EC_PHASE_2_EXTENSION_WORKING_DAYS = 20
EC_PHASE_2_MAX_TOTAL_WORKING_DAYS = 125
EC_STOP_CLOCK_MAX_DAYS = None

# ═══════════════════════════════════════════════════════════
# CMA (Competition and Markets Authority) — UK
# ═══════════════════════════════════════════════════════════
CMA_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE = (15, 60)
CMA_PHASE_1_STATUTORY_DAYS = 40
CMA_PHASE_2_STATUTORY_WEEKS = 24
CMA_PHASE_2_EXTENSION_WEEKS = 8

# ═══════════════════════════════════════════════════════════
# SAMR (State Administration for Market Regulation) — China
# ═══════════════════════════════════════════════════════════
SAMR_PHASE_1_DAYS = 30
SAMR_PHASE_2_DAYS = 90
SAMR_PHASE_3_DAYS = 60
SAMR_SIMPLIFIED_REVIEW_DAYS = 30
SAMR_PRE_ACCEPTANCE_TYPICAL_DAYS_RANGE = (15, 45)

# ═══════════════════════════════════════════════════════════
# CFIUS
# ═══════════════════════════════════════════════════════════
CFIUS_INITIAL_REVIEW_DAYS = 45
CFIUS_INVESTIGATION_DAYS = 45
CFIUS_PRESIDENTIAL_REVIEW_DAYS = 15

# ═══════════════════════════════════════════════════════════
# ACCC (Australia) — simplified
# ═══════════════════════════════════════════════════════════
ACCC_INFORMAL_REVIEW_TYPICAL_WEEKS = (6, 12)
ACCC_FORMAL_REVIEW_WEEKS = 12

# ═══════════════════════════════════════════════════════════
# SEC / Shareholder Approval
# ═══════════════════════════════════════════════════════════
SEC_S4_REVIEW_TYPICAL_DAYS_RANGE = (30, 60)
SEC_S4_COMMENT_RESPONSE_DAYS = (10, 30)
SEC_DEFM14A_REVIEW_TYPICAL_DAYS = (10, 20)
SHAREHOLDER_MEETING_AFTER_MAIL_DAYS = (20, 30)

# ═══════════════════════════════════════════════════════════
# Regulatory Climate Regime Labels
# ═══════════════════════════════════════════════════════════
ENFORCEMENT_REGIMES = {
    "aggressive": {
        "label": "Aggressive Antitrust Environment",
        "second_request_multiplier": 1.4,
        "phase_2_multiplier": 1.3,
        "pre_notification_multiplier": 1.5,
    },
    "normal": {
        "label": "Normal Enforcement",
        "second_request_multiplier": 1.0,
        "phase_2_multiplier": 1.0,
        "pre_notification_multiplier": 1.0,
    },
    "lenient": {
        "label": "Lenient Enforcement",
        "second_request_multiplier": 0.7,
        "phase_2_multiplier": 0.8,
        "pre_notification_multiplier": 0.8,
    },
}

# ═══════════════════════════════════════════════════════════
# Deal Size Buckets (match MARS schema)
# ═══════════════════════════════════════════════════════════
DEAL_SIZE_BUCKETS = {
    "mega": 10_000_000_000,
    "large": 1_000_000_000,
    "mid": 100_000_000,
    "small": 0,
}

# ═══════════════════════════════════════════════════════════
# Geographic Revenue Thresholds for Jurisdictional Triggers
# ═══════════════════════════════════════════════════════════
JURISDICTION_REVENUE_THRESHOLDS = {
    "HSR": {
        "combined_assets_or_revenue_usd": 111_400_000,
        "size_of_person_usd": 222_700_000,
    },
    "EC": {
        "combined_worldwide_turnover_eur": 5_000_000_000,
        "eu_turnover_each_party_eur": 250_000_000,
    },
    "CMA": {
        "uk_turnover_gbp": 70_000_000,
        "share_of_supply_pct": 25,
    },
    "SAMR": {
        "combined_worldwide_turnover_cny": 10_000_000_000,
        "china_turnover_each_party_cny": 400_000_000,
    },
}
