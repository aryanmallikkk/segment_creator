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
import uuid
from pathlib import Path
from typing import Any


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

    def get_count(self, rules: list[dict]) -> int:
        """Call /getCount with Algonomy rules. Returns customer count or -1 on failure."""
        if not rules:
            return -1
        body = self._build_getcount_body(rules)
        try:
            result = self.post("/getCount", body)
            if isinstance(result, int):
                return result
            if isinstance(result, dict):
                for key in ("filterCount", "totalCount", "count", "size"):
                    if key in result:
                        return int(result[key])
            return -1
        except Exception as ex:
            print(f"[getCount] Failed: {ex}", file=sys.stderr)
            return -1

    def _build_getcount_body(self, rules: list[dict]) -> dict:
        from collections import defaultdict

        # Group rules by (type, event) — one ruleBlock per group
        groups: dict = defaultdict(list)
        for rule in rules:
            key = (rule.get("type", "match"), rule.get("event", self.dim_type))
            groups[key].append(rule)

        rule_blocks: dict = {}
        block_ids: list[str] = []

        for (rule_type, event_id), group_rules in groups.items():
            block_id = str(uuid.uuid4())
            block_ids.append(block_id)
            sub_rules: dict = {}
            has_count_rule = False

            parent_placeholders: set[str] = set()
            for rule in group_rules:
                field    = rule.get("field", "")
                operator = rule.get("operator", "EQ")
                value    = rule.get("value")

                if field == "cs_event_id::count_cs_event_id":
                    has_count_rule = True

                # Child attribute rules require a parent ALL [] placeholder
                parent_attr = rule.get("parentAttributeId")
                if parent_attr and parent_attr not in parent_placeholders:
                    sub_rules[str(uuid.uuid4())] = {
                        "type": "attribute",
                        "fieldName": parent_attr,
                        "operatorSelected": "ALL",
                        "values": [],
                    }
                    parent_placeholders.add(parent_attr)

                sub_rule: dict = {
                    "type": "attribute",
                    "fieldName": field,
                    "operatorSelected": operator,
                    "values": self._fmt_values(value, operator),
                }
                if parent_attr:
                    sub_rule["parentAttributeId"] = parent_attr
                if rule.get("groupId"):
                    sub_rule["groupId"] = rule["groupId"]

                sub_rules[str(uuid.uuid4())] = sub_rule

            # didactivity/didnotactivity blocks need a count sub-rule
            if rule_type in ("didactivity", "didnotactivity") and not has_count_rule:
                sub_rules[str(uuid.uuid4())] = {
                    "type": "attribute",
                    "fieldName": "cs_event_id::count_cs_event_id",
                    "operatorSelected": "GT",
                    "values": ["0::0"],
                }

            rule_blocks[block_id] = {
                "metadataId": rule_type,
                "eventId": [event_id],
                "ruleGroupId": str(uuid.uuid4()),
                "rules": sub_rules,
            }

        return {
            "definitionVersion": "v1",
            "overrideGlobalSetting": False,
            "dimType": self.dim_type,
            "funnelType": False,
            "ruleBlocks": rule_blocks,
            "ruleExpression": " AND ".join(f"({bid})" for bid in block_ids),
        }

    @staticmethod
    def _fmt_values(value: Any, operator: str) -> list:
        """Format rule value(s) into Algonomy's val::val wire format."""
        if operator in ("NOTNULL", "NULL", "ALL") or value is None:
            return []

        def _enc(v) -> str:
            s = str(v)
            return f"{s}::{s}"

        if isinstance(value, list):
            if operator == "BETWEEN" and len(value) == 2:
                return [f"{value[0]}::{value[1]}"]
            return [_enc(v) for v in value]
        return [_enc(value)]

    def search_attribute(self, field_name: str, search_text: str, metadata_id: str, event_id: str) -> list[dict]:
        """Search top-level attribute values (e.g. product_code by name)."""
        try:
            result = self.get("/searchAttribute", {
                "metadataId": metadata_id,
                "fieldName": field_name,
                "searchText": search_text,
                "type": "attribute",
                "dimType": self.dim_type,
                "eventId": event_id,
            })
            return result.get("valueList", [])
        except Exception as ex:
            print(f"[searchAttribute] Failed: {ex}", file=sys.stderr)
            return []

    def search_child_attribute(self, field_name: str, parent_attribute_id: str, group_id: str,
                               search_text: str, metadata_id: str, event_id: str) -> list[dict]:
        """Search child attribute values (e.g. product_brand under product_code)."""
        try:
            result = self.get("/searchChildAttribute", {
                "metadataId": metadata_id,
                "dimType": self.dim_type,
                "fieldName": field_name,
                "parentAttributeId": parent_attribute_id,
                "groupId": group_id,
                "searchText": search_text,
                "type": "attribute",
                "eventId": event_id,
                "parentOperator": "ALL",
                "parentAttributeValues": "",
            })
            return result.get("valueList", [])
        except Exception as ex:
            print(f"[searchChildAttribute] Failed: {ex}", file=sys.stderr)
            return []

    def search_lookup_values(self, field_name: str, parent_attribute_id: str, group_id: str,
                             metadata_id: str, event_id: str) -> list[dict]:
        """Fetch all lookup values for a field (e.g. all product categories)."""
        try:
            result = self.get("/searchLookupValues", {
                "metadataId": metadata_id,
                "dimType": self.dim_type,
                "fieldName": field_name,
                "type": "attribute",
                "eventId": event_id,
                "parentOperator": "ALL",
                "parentAttributeValues": "",
                "groupId": group_id,
                "parentAttributeId": parent_attribute_id,
            })
            return result.get("valueList", [])
        except Exception as ex:
            print(f"[searchLookupValues] Failed: {ex}", file=sys.stderr)
            return []

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
