import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"usdm_noaa": {"usdm", "noaa"}, "nldas_usgs": {"nldas", "usgs"}, "all": {"usdm", "noaa", "nldas", "usgs"}}


@dataclass(frozen=True)
class Config:
    client_id: str
    role: str
    base_url: str
    api_key: str = field(repr=False)
    timeout: float = 120
    max_bytes: int = 20 * 1024 * 1024
    retries: int = 3
    root: Path = ROOT

    @classmethod
    def load(cls, path=ROOT / ".env", *, require_key=True):
        values = {**dotenv_values(path), **os.environ}
        url = (values.get("SERVER_BASE_URL") or "").strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and not any(c.isspace() for c in url)
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("SERVER_BASE_URL must be an HTTP(S) server URL without credentials/query/fragment")
        key = (values.get("SERVER_API_KEY") or "").strip()
        if require_key and (not key or key == "REPLACE_WITH_CLIENT_API_KEY"):
            raise ValueError("SERVER_API_KEY is missing")
        role = values.get("CLIENT_ROLE", "all")
        if role not in ROLES:
            raise ValueError("Invalid CLIENT_ROLE")
        try:
            timeout = float(values.get("REQUEST_TIMEOUT", "120"))
            mb = float(values.get("MAX_FILE_SIZE_MB", "20"))
            retries = int(values.get("UPLOAD_RETRY_COUNT", "3"))
            assert math.isfinite(timeout) and timeout > 0 and 0 < mb <= 20 and 0 <= retries <= 10
        except (ValueError, AssertionError):
            raise ValueError("Invalid timeout, size (0 < MB <= 20), or retry count (0..10)") from None
        return cls(values.get("CLIENT_ID", "local-client"), role, url, key, timeout, int(mb * 1024 * 1024), retries)
