from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_database_url: str
    migration_database_url: str

    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "permission-rag"
    access_token_minutes: int = Field(default=15, ge=1, le=1440)

    # --- LLM (one wrapper, three interchangeable providers) ---
    llm_provider: Literal["anthropic", "ollama", "gemini"] = "anthropic"
    llm_max_tokens: int = Field(default=800, ge=64, le=4000)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)

    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"  # Anthropic model name

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"  # pick a free model in Google AI Studio


settings = Settings()