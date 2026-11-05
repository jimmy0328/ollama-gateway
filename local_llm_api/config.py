from urllib.parse import urlsplit
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str
    model_id: str = Field(default="qwen3.8:27b-mlx", min_length=1)
    api_key: SecretStr
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    max_tokens: int = Field(default=4096, ge=1)
    max_prompt_chars: int = Field(default=32000, ge=1)
    max_body_bytes: int = Field(default=1048576, ge=1)
    request_timeout: float = Field(default=300, gt=0)
    max_inflight: int = Field(default=2, ge=1)
    memory_db_path: Path = Path("data/conversations.sqlite3")
    memory_context_turns: int = Field(default=20, ge=1, le=1000)
    summary_chunk_chars: int = Field(default=12000, ge=256)
    summary_max_calls: int = Field(default=64, ge=1, le=1000)

    @model_validator(mode="after")
    def check_settings(self):
        key = self.api_key.get_secret_value()
        if len(key) < 16 or key == "replace-with-your-random-api-key":
            raise ValueError("API_KEY must be a random secret of at least 16 characters")
        url = urlsplit(self.ollama_base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or url.path not in {"", "/"}
                or url.query or url.fragment):
            raise ValueError("OLLAMA_BASE_URL must be an HTTP(S) origin, e.g. http://192.168.0.105:11434")
        if url.port is not None and url.port < 1:
            raise ValueError("OLLAMA_BASE_URL needs a valid port")
        self.ollama_base_url = self.ollama_base_url.rstrip("/")
        return self
