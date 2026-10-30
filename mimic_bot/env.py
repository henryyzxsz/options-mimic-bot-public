"""Tiny .env loader (stdlib). Existing environment variables win over the file."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

PAPER_BASE_URL = "https://paper-api.alpaca.markets"

def assert_paper_base(env: Mapping[str, str] | None = None) -> None:
    """Refuse any trading URL except Alpaca paper. The error never echoes the supplied URL
    (it can be a live host, or a URL with credentials in the userinfo)."""
    src = os.environ if env is None else env
    raw = (src.get("APCA_API_BASE_URL") or PAPER_BASE_URL).strip().rstrip("/")
    if raw.endswith("/v2"):
        raw = raw[:-3]
    if raw != PAPER_BASE_URL:
        raise RuntimeError(f"refusing APCA_API_BASE_URL: this bot only trades on {PAPER_BASE_URL}")

def load_dotenv(path: Path | str = Path(__file__).resolve().parent.parent / ".env") -> None:
    p = Path(path)
    if not p.exists(): return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
