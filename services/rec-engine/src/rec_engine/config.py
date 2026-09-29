from functools import cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: int = 8081
    postgres_url: str = "postgresql://rec_svc:rec@postgres:5432/anime"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    model_dir: Path = Path("/data/models")
    als_factors: int = 64
    als_iterations: int = 20
    als_regularization: float = 0.05
    min_users_for_cf: int = 50
    model_reload_seconds: int = 300
    # Run `embed` then `train` inside the server once a day at this UTC time (HH:MM),
    # after the ingest window. Empty disables it (then schedule the CLI yourself).
    nightly_at: str = "04:30"

    @property
    def sqlalchemy_url(self) -> str:
        url = self.postgres_url
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url


@cache
def settings() -> Settings:
    return Settings()
