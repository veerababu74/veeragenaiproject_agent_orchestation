import os
from functools import lru_cache
from pathlib import Path
from tempfile import gettempdir
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_LOCAL_FILE = PROJECT_ROOT / ".env.local"


class Settings(BaseSettings):
    jwt_secret: str
    frontend_url: str = "http://localhost:3000"
    frontend_urls: str = ""
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    data_dir: str = ""
    port: int = 8003
    cleanup_interval_seconds: int = 3600

    # Shared with veeragenai_projects_be: same Hugging Face bucket (documents are
    # namespaced under an "agent-orchestrator/" prefix) and the same Pinecone
    # account, but a dedicated index — the OpenAI embeddings used here are a
    # different dimension (1536) than veeragenai's Gemini embeddings (768), so
    # the vectors cannot share an index.
    huggingface_token: str = ""
    huggingface_bucket: str = "veera20/veeragenaiproject"
    pinecone_api_key: str = ""
    pinecone_index: str = "agent-orchestrator-rag"

    model_config = SettingsConfigDict(env_file=(ENV_FILE, ENV_LOCAL_FILE), extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_values(cls, values):
        # Hosting dashboards often define a variable with an empty value; treat that as unset.
        if isinstance(values, dict):
            return {key: value for key, value in values.items() if value != ""}
        return values

    @property
    def frontend_url_set(self):
        return {
            url.strip().rstrip("/")
            for url in f"{self.frontend_url},{self.frontend_urls}".split(",")
            if url.strip()
        }

    def sqlite_path(self, filename: str, default_directory: Path):
        if os.getenv("VERCEL"):
            directory = Path(gettempdir()) / "agent_orchestrator"
        else:
            directory = Path(self.data_dir) if self.data_dir else default_directory
        directory.mkdir(parents=True, exist_ok=True)
        return directory / filename


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
