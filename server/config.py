"""Server-side configuration loaded from .env (per docs/server-api-spec.md §9.1)."""

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR: Path = Path(__file__).resolve().parent
load_dotenv(SERVER_DIR / ".env")

logger = logging.getLogger(__name__)


def _resolve_path(value: str) -> Path:
    """Return value as Path, anchored to SERVER_DIR if relative."""
    p = Path(value)
    return p if p.is_absolute() else SERVER_DIR / p


_secret = os.getenv("FLASK_SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    logger.warning(
        "FLASK_SECRET_KEY not set in .env — using a random ephemeral key. "
        "Sessions and CSRF tokens will not survive a server restart."
    )
FLASK_SECRET_KEY: str = _secret

MCU_SERIAL_PORT: str = os.getenv("MCU_SERIAL_PORT", "/dev/ttyACM0")
MCU_BAUD_RATE: int = int(os.getenv("MCU_BAUD_RATE", "115200"))
DB_PATH: Path = _resolve_path(os.getenv("DB_PATH", "data/cabinet.db"))
IFTTT_WEBHOOK_KEY: str | None = os.getenv("IFTTT_WEBHOOK_KEY") or None
