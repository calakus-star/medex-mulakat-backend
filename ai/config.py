import os
from dataclasses import dataclass

@dataclass(frozen=True)
class AIConfig:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    jwt_secret: str = os.getenv("JWT_SECRET", "medex-secret-key-2024")
    admin_email: str = os.getenv("ADMIN_EMAIL", "admin@medex-smo.com")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "medex2024")
    report_emails: str = os.getenv("REPORT_EMAILS", "hr@medex-smo.com")
    from_email: str = os.getenv("FROM_EMAIL", "onboarding@resend.dev")
    base_url: str = os.getenv("BASE_URL", "http://localhost:3000")
    db_path: str = os.getenv("DB_PATH", "medex_mulakat.db")

    # v2 optional knobs. Defaults are safe; absence will not break v1.
    openai_realtime_model: str = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
    openai_text_model: str = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
    claude_text_model: str = os.getenv("CLAUDE_TEXT_MODEL", "claude-3-5-sonnet-20241022")
    medex_level_strategy: str = os.getenv("MEDEX_LEVEL_STRATEGY", "L1_CLAUDE_L2_OPENAI_L3_DUAL")

config = AIConfig()
