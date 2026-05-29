from urllib.parse import quote_plus
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Option 1 — full URL (must URL-encode special chars in password, e.g. $ → %24)
    # Leave blank and use DB_* vars below to avoid encoding issues.
    database_url: str = ""

    # Option 2 — individual Supabase connection params (preferred: no encoding needed)
    # Set these in Railway Variables instead of DATABASE_URL when password has special chars.
    db_host: str = ""
    db_user: str = ""
    db_password: str = ""
    db_name: str = "postgres"
    db_port: int = 5432

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

    def get_database_url(self) -> str:
        """
        Return a SQLAlchemy-safe async database URL.
        Priority: DB_HOST params > DATABASE_URL > local SQLite fallback.

        Handles:
        - DB_HOST/USER/PASSWORD params (Supabase, avoids $ encoding issues)
        - DATABASE_URL with postgresql:// scheme (Railway PostgreSQL plugin)
          → auto-converted to postgresql+asyncpg:// for SQLAlchemy
        - Fallback to local SQLite for development
        """
        if self.db_host:
            user = quote_plus(self.db_user)
            password = quote_plus(self.db_password)
            return (
                f"postgresql+asyncpg://{user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        if self.database_url:
            url = self.database_url
            # Railway PostgreSQL plugin provides postgresql:// — convert for asyncpg
            if url.startswith("postgresql://") or url.startswith("postgres://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
                url = url.replace("postgres://", "postgresql+asyncpg://", 1)
            return url
        return "sqlite+aiosqlite:///./data/bible.db"

    class Config:
        env_file = ".env"


settings = Settings()
