"""
algonomy_client.py — Low-level HTTP client for the Algonomy AM API.

Reads credentials from .env:
    ALGONOMY_BASE_URL   — e.g. https://api-dev.algonomy.com/am
    ALGONOMY_JSESSIONID — session cookie value
    ALGONOMY_XSRF_TOKEN — XSRF token (used in both cookie and header)
    ALGONOMY_DIM_TYPE   — dimension type, default: profile_data

Raises AlgonomyAuthError on 401/403 with a clear message to refresh .env.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class AlgonomyAuthError(Exception):
    """Raised when the Algonomy session has expired."""


class AlgonomyClient:
    def __init__(self):
        self._load_env()
        self.base_url = os.getenv("ALGONOMY_BASE_URL", "https://api-dev.algonomy.com/am").rstrip("/")
        self.jsessionid = os.getenv("ALGONOMY_JSESSIONID", "")
        self.xsrf_token = os.getenv("ALGONOMY_XSRF_TOKEN", "")
        # Header xsrf-token may differ from the cookie value — falls back to cookie value if not set
        self.xsrf_header = os.getenv("ALGONOMY_XSRF_HEADER", "") or self.xsrf_token
        self.dim_type = os.getenv("ALGONOMY_DIM_TYPE", "profile_data")

        if not self.jsessionid or not self.xsrf_token:
            raise RuntimeError(
                "ALGONOMY_JSESSIONID and ALGONOMY_XSRF_TOKEN must be set in .env. "
                "Copy them from your browser's DevTools → Network tab → cookie header."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_env() -> None:
        env_path = Path(__file__).parent / ".env"
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key.startswith("ALGONOMY_"):
                os.environ[key] = value

    def _headers(self) -> dict[str, str]:
        cookie = (
            f"JSESSIONID={self.jsessionid}; "
            f"XSRF-TOKEN={self.xsrf_token}"
        )
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "xsrf-token": self.xsrf_header,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> any:
        """
        GET request to the Algonomy API.
        path should start with / e.g. "/getAllAudienceType"
        params are appended as query string.
        """
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        req = urllib.request.Request(url, headers=self._headers(), method="GET")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)

        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                raise AlgonomyAuthError(
                    f"Algonomy session expired (HTTP {ex.code}). "
                    "Update ALGONOMY_JSESSIONID and ALGONOMY_XSRF_TOKEN in .env "
                    "by copying fresh cookies from your browser DevTools."
                )
            body = ""
            try:
                body = ex.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"Algonomy API HTTP {ex.code} for {path}: {body or ex.reason}"
            )

        except urllib.error.URLError as ex:
            raise RuntimeError(
                f"Network error calling Algonomy API ({url}): {ex.reason}"
            )

        except json.JSONDecodeError as ex:
            raise RuntimeError(
                f"Algonomy API returned non-JSON for {path}: {ex}"
            )

    def post(self, path: str, body: dict) -> any:
        """POST request with JSON body."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=self._headers(), method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)

        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                raise AlgonomyAuthError(
                    f"Algonomy session expired (HTTP {ex.code}). "
                    "Update ALGONOMY_JSESSIONID and ALGONOMY_XSRF_TOKEN in .env."
                )
            body_str = ""
            try:
                body_str = ex.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"Algonomy API HTTP {ex.code} for {path}: {body_str or ex.reason}"
            )

        except urllib.error.URLError as ex:
            raise RuntimeError(
                f"Network error calling Algonomy API ({url}): {ex.reason}"
            )
