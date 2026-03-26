import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    proxy_host: str
    proxy_port: int
    upstream_url: str
    database_url: str
    log_level: str
    hostname: str  # socket.gethostname() at startup


def load_config() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("DATABASE_URL is required but not set")

    return Config(
        proxy_host=os.environ.get("PROXY_HOST", "127.0.0.1"),
        proxy_port=int(os.environ.get("PROXY_PORT", "8888")),
        upstream_url=os.environ.get("UPSTREAM_URL", "https://api.anthropic.com").rstrip("/"),
        database_url=db_url,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        hostname=socket.gethostname(),
    )
