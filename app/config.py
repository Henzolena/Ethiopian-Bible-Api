from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./data/bible.db"
    api_title: str = "Ethiopian Bible API"
    api_version: str = "1.2.0"
    debug: bool = False

    # Gemini AI — for on-demand quiz question generation
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"

    class Config:
        env_file = ".env"


settings = Settings()
