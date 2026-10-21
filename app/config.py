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
    # Quality floor for a cold send. This is an *operating policy*, not a model output: the
    # agent proposes a fit_score, but a business decides how low it will go before writing to
    # a stranger. Below the floor the message is still drafted (drafting harms nobody) and is
    # refused at send time, next to the frequency cap -- both are anti-spam, not legality.
    outreach_min_score: int = 70
    # Vessel position (AIS). Licensed data; mock provider is synthetic.
    vessel_provider: str = 'mock'
    ais_base_url: str | None = None
    ais_token: str | None = None
    # A delay beyond this threshold raises a proactive customer notification task.
    vessel_delay_alert_hours: float = 12.0
    # Demo-only shortcuts (synthetic supplier quotes, auto-approve at the human gate).
    # Kept as an explicit setting so production can refuse them outright instead of trusting
    # every caller to pass the right flag.
    allow_demo_shortcuts: bool = False
    # Knowledge base / RAG. 'hashing' is deterministic and offline; 'openai' needs a key.
    embedding_provider: str = 'hashing'
    embedding_model: str = 'text-embedding-3-small'
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    knowledge_top_k: int = 5
    # Bounded final retrieval score below which the agent refuses instead of guessing.
    #
    # Responsibilities are split deliberately: questions the corpus does not speak about are
    # refused upstream by the character-coverage gate in retrieval.py (they return no
    # candidates at all), so this threshold only decides "in-domain but weakly matched".
    # Calibrated on the bundled corpus: short questions score 0.59-0.85, long natural
    # sentences 0.41-0.59, out-of-domain 0.00 (gated). Set below the long-sentence band so a
    # verbose but answerable question is not refused.
    knowledge_min_score: float = 0.38
    # LLM call discipline. Without a timeout a single hung provider stalls the worker forever.
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    # Hard ceiling on tool-calling rounds, so a model that keeps asking cannot loop forever.
    agent_max_tool_steps: int = 4
    # Per-1k token prices. Left at 0 = "not configured", in which case cost is recorded as
    # NULL rather than 0.0 (a free-looking dashboard is worse than no dashboard).
    llm_prompt_price_per_1k: float = 0.0
    llm_completion_price_per_1k: float = 0.0

@lru_cache
def get_settings() -> Settings:
    return Settings()
