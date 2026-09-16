from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_name: str = 'AI Freight Forwarder Multi-Agent Platform'
    environment: str = 'development'
    database_url: str = 'sqlite:///./data/freight.db'
    auth_mode: str = 'dev'
    jwt_secret: str = 'change-me-in-production'
    jwt_issuer: str = 'freight-agent-platform'
    app_api_key: str = 'dev-api-key'
    webhook_secret: str = 'dev-webhook-secret'
    agent_provider: str = 'mock'
    openai_chat_model: str | None = None
    email_provider: str = 'mock'
    smtp_host: str = 'localhost'
    smtp_port: int = 1025
    smtp_from: str = 'ops@example.test'
    schedule_provider: str = 'mock'
    dcsa_schedule_base_url: str | None = None
    dcsa_schedule_token: str | None = None
    booking_provider: str = 'mock'
    dcsa_booking_base_url: str | None = None
    dcsa_booking_token: str | None = None
    tracking_provider: str = 'mock'
    dcsa_tracking_base_url: str | None = None
    dcsa_tracking_token: str | None = None
    # Rate sources. 'mock' = deterministic synthetic (demo/tests). 'contract' and 'index'
    # are licensed/contracted commercial data and require credentials.
    rate_provider: str = 'mock'
    rate_contract_base_url: str | None = None
    rate_contract_token: str | None = None
    rate_index_base_url: str | None = None
    rate_index_token: str | None = None
    default_margin_pct: float = 0.12
    minimum_margin_usd: float = 180.0
    quote_valid_days: int = 7
    rfq_timeout_hours: int = 8
    min_supplier_quotes: int = 2
    worker_poll_seconds: int = 15
    # Prospecting / outreach
    prospecting_provider: str = 'mock'
    prospecting_base_url: str | None = None
    prospecting_token: str | None = None
    prospect_default_limit: int = 20
    # Frequency capping: how many cold outreach messages one prospect may receive in a window.
    outreach_max_per_window: int = 2
    outreach_window_days: int = 30
    # Cold outreach always requires a recorded lawful basis; 'legitimate_interest_reviewed'
    # or 'opt_in'. Anything else is hard-blocked at the service layer.
    outreach_requires_human_approval: bool = True
    # Vessel position (AIS). Licensed data; mock provider is synthetic.
    vessel_provider: str = 'mock'
    ais_base_url: str | None = None
    ais_token: str | None = None
    # A delay beyond this threshold raises a proactive customer notification task.
    vessel_delay_alert_hours: float = 12.0

@lru_cache
def get_settings() -> Settings:
    return Settings()
