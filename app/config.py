import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("KANBAN_DB_PATH", "./kanban.db")
    default_lease_seconds: int = int(os.environ.get("DEFAULT_LEASE_SECONDS", "60"))
    sweep_interval_seconds: int = int(os.environ.get("SWEEP_INTERVAL_SECONDS", "10"))
    http_host: str = os.environ.get("HTTP_HOST", "127.0.0.1")
    http_port: int = int(os.environ.get("HTTP_PORT", "8000"))


settings = Settings()
