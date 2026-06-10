import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algonomy_gemini_bridge import build_gemini_catalog_text, is_algonomy_catalog

# Plain-text icons for Algonomy rule types used in Gemini prompts (ASCII-safe)
_TYPE_ICONS_PLAIN = {
    "match": "+",
    "donotmatch": "-",
    "didactivity": ">",
    "didnotactivity": "!",
}


@dataclass
class SegmentResult:
    name: str
    description: str
    selected_filters: dict[str, Any]
    count: int
    rows: list[dict[str, Any]]
    source: str = "claude"
    error: str | None = None


def _load_env_file() -> None:
    """Load key=value pairs from a local .env into process env if missing."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key.startswith("GEMINI_"):
            os.environ[key] = value
        elif key not in os.environ:
            os.environ[key] = value


def _parse_segments_from_claude_text(text: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not text:
        return None, "Gemini response had no text content."

    candidates = [text]
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    object_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0).strip())

    parsed: dict[str, Any] | None = None
    for candidate in candidates:
        try:
            maybe = json.loads(candidate)
            if isinstance(maybe, dict):
                parsed = maybe
                break
        except json.JSONDecodeError:
            continue
    if parsed is None:
        preview = text[:400].replace("\n", " ")
        return None, f"Could not parse Gemini output as JSON. Preview: {preview}"

    segments = parsed.get("segments")

    # Normalise: segments with algonomy_rules always have customer_filters / sales_filters keys
    if isinstance(segments, list):
        for seg in segments:
            if "algonomy_rules" in seg and "customer_filters" not in seg:
                seg.setdefault("customer_filters", {})
                seg.setdefault("sales_filters", {})

    return segments, None


def _call_gemini_generate(
    api_key: str,
    model: str,
    body: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as ex:
        try:
            error_body = ex.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            error_body = ""
        return None, f"Gemini HTTP {ex.code}: {error_body or ex.reason}"
    except urllib.error.URLError as ex:
        return None, f"Network error calling Gemini: {ex.reason}"
    except TimeoutError:
        return None, "Timeout calling Gemini API."
    except json.JSONDecodeError:
        return None, "Gemini response was not valid JSON."


def _gemini_extract_text(raw: dict[str, Any]) -> str:
    try:
        parts = raw["candidates"][0]["content"]["parts"]
        return "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
    except (KeyError, IndexError):
        return ""


def _gemini_get_segment_suggestions(
    customer_filters: dict[str, Any],
    sales_filters: dict[str, Any],
    count: int,
    filter_catalog: dict[str, Any] | None = None,
    algonomy_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Ask Gemini to suggest segment refinements based on existing Algonomy rules."""
    _load_env_file()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return []
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    if not algonomy_rules or not filter_catalog or not is_algonomy_catalog(filter_catalog):
        return []

    catalog_text = build_gemini_catalog_text(filter_catalog)
    rules_desc = "\n".join(
        f"  {_TYPE_ICONS_PLAIN.get(r.get('type', 'match'), '*')} {r.get('type')} "
        f"{r.get('event', '')}.{r.get('field', '')} "
        f"{r.get('operator', '')} {r.get('value', '')}"
        for r in algonomy_rules
    )

    body = {
        "systemInstruction": {
            "parts": [{
                "text": (
                    "You are a segment refinement advisor for an Algonomy Audience Manager. "
                    "Suggest 4-6 useful rule additions to narrow or expand the audience. "
                    "Use ONLY field ids and dataset ids from the catalog below. "
                    "Respond ONLY with JSON in this exact shape:\n"
                    "{\"suggestions\":[{"
                    "\"label\":\"...\","
                    "\"description\":\"...\","
                    "\"algonomy_rules\":[{"
                    "\"type\":\"match|donotmatch|didactivity|didnotactivity\","
                    "\"event\":\"<dataset id>\","
                    "\"field\":\"<field id>\","
                    "\"operator\":\"equals|not_equals|contains|gte|lte|between|in_last_days|in_list\","
                    "\"value\":\"<value>\""
                    "}]}]}"
                )
            }]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{
                    "text": (
                        f"Current segment rules:\n{rules_desc}\n\n"
                        f"Each suggestion adds exactly 1 new rule that refines this segment. "
                        f"Labels: 3-5 words. Descriptions: one sentence.\n\n"
                        f"Available catalog:\n{catalog_text}"
                    )
                }],
            }
        ],
        "generationConfig": {"maxOutputTokens": 8192},
    }

    raw, err = _call_gemini_generate(api_key, model, body)
    if err:
        raise RuntimeError(err)
    if not raw:
        raise RuntimeError("Gemini returned an empty response.")
    text = _gemini_extract_text(raw)
    if not text:
        raise RuntimeError(f"Gemini response had no text. Full response: {json.dumps(raw)[:500]}")

    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1))
    obj = re.search(r"\{.*\}", text, re.DOTALL)
    if obj:
        candidates.append(obj.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and isinstance(parsed.get("suggestions"), list):
                return parsed["suggestions"]
        except (json.JSONDecodeError, TypeError):
            continue
    raise RuntimeError(f"Could not parse suggestions from Gemini response: {text[:400]}")


# Alias kept for existing callers
_claude_get_segment_suggestions = _gemini_get_segment_suggestions


def _claude_generate_segment_filters(
    user_prompt: str,
    filter_catalog: dict[str, Any],
    clarification_state: dict[str, Any] | None = None,
    clarification_answer: str | None = None,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, str | None]:
    _load_env_file()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, None, "GEMINI_API_KEY is not set."
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    catalog_text = build_gemini_catalog_text(filter_catalog)
    system_prompt = (
        "You build audience segment rule sets for an Algonomy Audience Manager. "
        "Output ONLY JSON in this exact shape:\n"
        "{\"segments\":[{"
        "\"name\":\"...\","
        "\"description\":\"...\","
        "\"algonomy_rules\":[{"
        "\"type\":\"match|donotmatch|didactivity|didnotactivity\","
        "\"event\":\"<dataset id>\","
        "\"field\":\"<field id>\","
        "\"operator\":\"equals|not_equals|contains|gte|lte|between|in_last_days|in_list\","
        "\"value\":\"<value or list>\""
        "}]"
        "}]}\n"
        "Rules:\n"
        "- Use ONLY field ids and dataset ids from the provided catalog.\n"
        "- 'type' must be one of: match, donotmatch, didactivity, didnotactivity.\n"
        "- Use 'match' for profile/demographic/behavioural properties.\n"
        "- Use 'didactivity' for browsing/transaction events (product_view, add_to_cart, etc.).\n"
        "- For recency/days: use in_last_days with an integer value.\n"
        "- For numeric ranges: use between with value=[min, max].\n"
        "- If the request is ambiguous, call request_clarification instead of guessing.\n"
        "- Do NOT include customer_filters or sales_filters — only algonomy_rules."
    )
    user_content_suffix = (
        "\n\nExamples:\n"
        "  'women over 30' → type=match, event=profile_data, field=gender, operator=equals, value=F\n"
        "  'bought something last 30 days' → type=didactivity, event=transaction_complete, "
        "field=sale_trans_date::total_visits, operator=in_last_days, value=30\n"
        "  'loyalty tier gold' → type=match, event=profile_data, field=loyalty_tier, operator=equals, value=Gold\n"
        "  'spent more than 1000' → type=match, event=fct_sale_transaction, "
        "field=sale_trans_net_val::total_order_value, operator=gte, value=1000"
    )

    clarification_tool = {
        "functionDeclarations": [{
            "name": "request_clarification",
            "description": (
                "Request a focused clarification from the user when the segment request is ambiguous. "
                "Keep the options short and descriptive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clarification_type": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "product_term": {"type": "string"},
                },
                "required": ["clarification_type", "question", "options"],
            },
        }]
    }

    if clarification_state:
        prior_contents = clarification_state.get("contents")
        fn_name = clarification_state.get("function_call_name", "request_clarification")
        if not isinstance(prior_contents, list):
            return None, None, "Clarification state is invalid or incomplete."
        if not clarification_answer:
            return None, None, "Clarification answer is required."
        contents = prior_contents + [
            {
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"answer": clarification_answer},
                    }
                }],
            }
        ]
    else:
        user_content = (
            "Create segments from this user request.\n"
            f"Request:\n{user_prompt}\n\n"
            "Available catalog:\n"
            f"{catalog_text}"
            f"{user_content_suffix}"
        )
        contents = [{"role": "user", "parts": [{"text": user_content}]}]

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "tools": [clarification_tool],
        "generationConfig": {"maxOutputTokens": 2400},
    }
    raw, call_error = _call_gemini_generate(api_key, model, body)
    if call_error:
        return None, None, call_error
    assert raw is not None

    try:
        response_parts = raw["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        return None, None, "Gemini response had unexpected structure."

    for part in response_parts:
        if "functionCall" in part:
            fn_call = part["functionCall"]
            if fn_call.get("name") == "request_clarification":
                fn_args = fn_call.get("args") or {}
                options = fn_args.get("options") or []
                question = fn_args.get("question") or "Please clarify your request."
                clarification = {
                    "clarification_type": fn_args.get("clarification_type", "generic"),
                    "question": question,
                    "options": options if isinstance(options, list) else [],
                    "product_term": fn_args.get("product_term", ""),
                    "clarification_state": {
                        "contents": contents + [{"role": "model", "parts": response_parts}],
                        "function_call_name": "request_clarification",
                    },
                }
                return None, clarification, None
            return None, None, "Gemini called an unexpected tool."

    text = "\n".join(p.get("text", "") for p in response_parts if "text" in p).strip()
    segments, parse_error = _parse_segments_from_claude_text(text)
    return segments, None, parse_error


def _dedupe_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for seg in segments:
        alg_rules = seg.get("algonomy_rules")
        if alg_rules:
            key = json.dumps(alg_rules, sort_keys=True)
        else:
            key = json.dumps(
                {
                    "customer_filters": seg.get("customer_filters") or {},
                    "sales_filters": seg.get("sales_filters") or {},
                },
                sort_keys=True,
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(seg)
    return out


class AudienceOrchestrator:
    def __init__(self) -> None:
        self._catalog_cache: dict[str, Any] | None = None

    _CATALOG_SNAPSHOT = Path(__file__).parent / "algonomy_catalog_snapshot.json"

    @classmethod
    def _save_catalog_snapshot(cls, catalog: dict[str, Any]) -> None:
        import datetime
        catalog["_saved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            cls._CATALOG_SNAPSHOT.write_text(
                json.dumps(catalog, indent=2), encoding="utf-8"
            )
            print(
                f"[Orchestrator] Catalog snapshot saved to {cls._CATALOG_SNAPSHOT}",
                file=sys.stderr, flush=True,
            )
        except Exception as ex:
            print(f"[Orchestrator] Could not save catalog snapshot: {ex}", file=sys.stderr, flush=True)

    @classmethod
    def _load_catalog_snapshot(cls) -> dict[str, Any] | None:
        if not cls._CATALOG_SNAPSHOT.exists():
            return None
        try:
            catalog = json.loads(cls._CATALOG_SNAPSHOT.read_text(encoding="utf-8"))
            saved_at = catalog.get("_saved_at", "unknown")
            print(
                f"[Orchestrator] Loaded catalog snapshot from disk (saved {saved_at}).",
                file=sys.stderr, flush=True,
            )
            return catalog
        except Exception as ex:
            print(f"[Orchestrator] Could not load catalog snapshot: {ex}", file=sys.stderr, flush=True)
            return None

    @staticmethod
    def _fetch_catalog_from_s3() -> dict[str, Any] | None:
        """Fetch the catalog JSON from S3. Returns None if S3 is not configured or fetch fails."""
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            return None
        key    = os.getenv("S3_CATALOG_KEY", "algonomy_catalog.json")
        region = os.getenv("AWS_REGION", "us-east-1")
        try:
            import boto3
            s3 = boto3.client(
                "s3",
                region_name=region,
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            )
            response = s3.get_object(Bucket=bucket, Key=key)
            catalog  = json.loads(response["Body"].read().decode("utf-8"))
            saved_at = catalog.get("_saved_at", "unknown")
            print(
                f"[Orchestrator] Loaded catalog from s3://{bucket}/{key} (saved {saved_at}).",
                file=sys.stderr, flush=True,
            )
            return catalog
        except ImportError:
            print("[Orchestrator] boto3 not installed — skipping S3 fetch.", file=sys.stderr, flush=True)
            return None
        except Exception as ex:
            print(f"[Orchestrator] S3 fetch failed ({ex}). Trying local fallback.", file=sys.stderr, flush=True)
            return None

    def get_filter_catalog(self) -> dict[str, Any]:
        """
        Fetch the Algonomy filter catalog.
        Priority:
          1. In-memory cache (same session)
          2. S3 bucket (if S3_BUCKET is set in .env)
          3. Disk snapshot (local algonomy_catalog_snapshot.json)
          4. Live Algonomy API build (last resort — requires valid tokens)
        """
        if self._catalog_cache is not None:
            return self._catalog_cache

        _load_env_file()

        # ── S3 ──────────────────────────────────────────────────────────
        catalog = self._fetch_catalog_from_s3()
        if catalog is not None:
            self._catalog_cache = catalog
            return self._catalog_cache

        # ── Disk snapshot ────────────────────────────────────────────────
        catalog = self._load_catalog_snapshot()
        if catalog is not None:
            self._catalog_cache = catalog
            return self._catalog_cache

        # ── Live Algonomy API ────────────────────────────────────────────
        try:
            from algonomy_client import AlgonomyClient
            from algonomy_catalog import build_catalog
            client = AlgonomyClient()
            catalog = build_catalog(client)
            catalog["_source"] = "algonomy"
            self._save_catalog_snapshot(catalog)
            self._catalog_cache = catalog
            print("[Orchestrator] Algonomy catalog built and cached.", file=sys.stderr, flush=True)
            return self._catalog_cache
        except Exception as ex:
            raise RuntimeError(
                f"Could not load Algonomy catalog.\n"
                f"S3 not configured or unreachable, no local snapshot found, "
                f"and live Algonomy build failed: {ex}\n"
                f"Run upload_catalog_to_s3.py or update tokens in .env."
            ) from ex

    def _results_from_generated(
        self, user_prompt: str, generated: list[dict[str, Any]], source: str
    ) -> list[SegmentResult]:
        generated = _dedupe_segments(generated)
        if not generated:
            return [
                SegmentResult(
                    name="No segments generated",
                    description="Gemini returned no valid unique segment rules.",
                    selected_filters={"customer_filters": {}, "sales_filters": {}},
                    count=0,
                    rows=[],
                    source=source,
                    error="Gemini returned no valid segment definitions.",
                )
            ]

        results: list[SegmentResult] = []
        for seg in generated:
            name = seg.get("name", "Unnamed Segment")
            description = seg.get("description", "")
            algonomy_rules = seg.get("algonomy_rules")
            if algonomy_rules:
                results.append(
                    SegmentResult(
                        name=name,
                        description=description,
                        selected_filters={
                            "algonomy_rules": algonomy_rules,
                            "customer_filters": {},
                            "sales_filters": {},
                        },
                        count=-1,   # -1 = pending execution via getCount API
                        rows=[],
                        source=source,
                        error=None,
                    )
                )
            else:
                results.append(
                    SegmentResult(
                        name=name,
                        description=description,
                        selected_filters={
                            "customer_filters": seg.get("customer_filters") or {},
                            "sales_filters": seg.get("sales_filters") or {},
                        },
                        count=0,
                        rows=[],
                        source=source,
                        error="Segment returned no Algonomy rules.",
                    )
                )
        return results

    def _clarification_result(self, clarification: dict[str, Any], source: str) -> SegmentResult:
        return SegmentResult(
            name="Clarification needed",
            description=clarification.get("question") or "Please clarify your request.",
            selected_filters=clarification,
            count=0,
            rows=[],
            source=source,
            error="Clarification required before generating filters.",
        )

    def _error_segment(self, message: str) -> SegmentResult:
        return SegmentResult(
            name="Error",
            description=message,
            selected_filters={},
            count=0,
            rows=[],
            source="direct",
            error=message,
        )

    def create_segments(
        self, user_prompt: str, catalog: dict[str, Any] | None = None
    ) -> list[SegmentResult]:
        catalog = catalog or self.get_filter_catalog()
        source = "claude"

        generated, clarification, generation_error = _claude_generate_segment_filters(
            user_prompt, catalog
        )
        if clarification:
            return [self._clarification_result(clarification, source)]
        if not generated:
            return [
                SegmentResult(
                    name="Generation unavailable",
                    description="Gemini did not return usable segment rules.",
                    selected_filters={"customer_filters": {}, "sales_filters": {}},
                    count=0,
                    rows=[],
                    source=source,
                    error=(
                        generation_error
                        or "Set a valid GEMINI_API_KEY and ensure Gemini returns JSON segment rules."
                    ),
                )
            ]
        return self._results_from_generated(user_prompt, generated, source)

    def resolve_clarification(
        self,
        user_prompt: str,
        clarification_state: dict[str, Any],
        clarification_answer: str,
        catalog: dict[str, Any] | None = None,
    ) -> list[SegmentResult]:
        catalog = catalog or self.get_filter_catalog()
        source = "claude"
        generated, clarification, generation_error = _claude_generate_segment_filters(
            user_prompt,
            catalog,
            clarification_state=clarification_state,
            clarification_answer=clarification_answer,
        )
        if clarification:
            return [self._clarification_result(clarification, source)]
        if not generated:
            return [
                SegmentResult(
                    name="Generation unavailable",
                    description="Gemini did not return usable segment rules after clarification.",
                    selected_filters={"customer_filters": {}, "sales_filters": {}},
                    count=0,
                    rows=[],
                    source=source,
                    error=generation_error or "Gemini did not return valid segment rules after clarification.",
                )
            ]
        return self._results_from_generated(user_prompt, generated, source)
