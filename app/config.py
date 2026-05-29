from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase PostgreSQL — set DATABASE_URL in Railway Variables:
    # postgresql+asyncpg://postgres.[ref]:[password]@aws-0-us-west-2.pooler.supabase.com:6543/postgres
    database_url: str = "sqlite+aiosqlite:///./data/bible.db"  # local fallback only
    api_title: str = "Ethiopian Bible API"
    api_version: str = "1.4.0"
    debug: bool = False

    # Mistral AI — primary provider for on-demand quiz question generation
    # Free tier: 1 B tokens / month — no credit card required
    # Sign up: https://console.mistral.ai
    mistral_api_key: str = ""
    mistral_model: str = "mistral-small-latest"   # Small 3.1 — best quality on free tier
    mistral_base_url: str = "https://api.mistral.ai/v1"

    # Gemini AI — kept as optional fallback (legacy)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"

    class Config:
        env_file = ".env"


settings = Settings()
