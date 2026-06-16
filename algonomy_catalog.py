"""
algonomy_catalog.py — Dynamic catalog builder for the Algonomy AM API.

Build strategy:
  1. getAllAudienceType           → all type IDs
  2. For each type (uiActionType=1):
       → getAllActivityEvents     → if non-empty: type has events
       → getMetadataContentForType per event (or directly if no events)
  3. Deduplicate field lists by MD5 hash — identical field sets stored once
  4. uiActionType=2 (segment types) → stored as "segment", no field fetch

At query time (not at catalog build time):
  → get_attribute_options(field_id) → operators + isSearchable for one field
  → search_attribute(field_id, term) → resolve value string to id::description token
"""

import hashlib
import json
import sys
from typing import Any

from algonomy_client import AlgonomyClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_fields(fields: list[dict]) -> str:
    """Stable hash of a field list based on sorted field IDs."""
    key = json.dumps(sorted(f["id"] for f in fields))
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _extract_fields(response: dict | list) -> list[dict]:
    """
    Extract compact field definitions from a getMetadataContentForType response.
    Pulls from both 'attributes' and 'browsing' sections.
    Skips duplicates and non-selectable / hidden fields.
    Returns list of: {id, label, group, hasChild}
    """
    if not isinstance(response, dict):
        return []

    fields: list[dict] = []
    seen_ids: set[str] = set()

    for section in ("attributes", "browsing"):
        for f in response.get(section) or []:
            fid = f.get("id", "").strip()
            if not fid or fid in seen_ids:
                continue
            if str(f.get("display", "true")).lower() == "false":
                continue
            if str(f.get("isSelectable", "true")).lower() == "false":
                continue
            seen_ids.add(fid)
            fields.append({
                "id": fid,
                "label": f.get("displayName", fid),
                "group": f.get("groupName", ""),
                "hasChild": str(f.get("hasChildAttribute", "false")).lower() == "true",
            })

    return fields


# ---------------------------------------------------------------------------
# Main catalog builder
# ---------------------------------------------------------------------------

def build_catalog(client: AlgonomyClient | None = None) -> dict[str, Any]:
    """
    Build the full Algonomy catalog dynamically.

    Returns:
        {
            "catalog": {
                "<type_id>": {
                    "id": str,
                    "label": str,
                    "type": "direct" | "events" | "segment",
                    # direct:
                    "fields": [...],          # compact list, inline for Gemini
                    "fields_ref": str,        # hash key into field_store
                    # events:
                    "events": [
                        {
                            "id": str,
                            "label": str,
                            "fields": [...],
                            "fields_ref": str | None,
                        }
                    ]
                }
            },
            "field_store": {
                "<hash>": [compact field list]
            },
            "type_defs": [raw type objects from getAllAudienceType]
        }
    """
    if client is None:
        client = AlgonomyClient()

    dim = client.dim_type
    field_store: dict[str, list[dict]] = {}
    catalog: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1: get all types
    # ------------------------------------------------------------------
    print("[Catalog] Fetching audience types...", file=sys.stderr, flush=True)
    types_resp = client.get("/getAllAudienceType")
    # The API may return a list directly or wrap it in a key
    if isinstance(types_resp, list):
        types = types_resp
    else:
        # Try common wrapper keys
        types = (
            types_resp.get("audienceTypeList")
            or types_resp.get("types")
            or types_resp.get("data")
            or types_resp.get("results")
            or []
        )
    print(f"[Catalog] {len(types)} types found: {[t.get('id', t) for t in types]}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------
    # Step 2: process each type
    # ------------------------------------------------------------------
    for t in types:
        type_id: str = t["id"]
        type_label: str = t.get("displayName", type_id)
        ui_action_type: int = t.get("uiActionType", 1)

        # ---- Segment types (belongs, doesnotbelong) -------------------
        if ui_action_type == 2:
            catalog[type_id] = {
                "id": type_id,
                "label": type_label,
                "type": "segment",
            }
            print(f"[Catalog] {type_id}: segment type — skipped", file=sys.stderr, flush=True)
            continue

        # ---- Try to get events for this type --------------------------
        events_list: list[dict] = []
        try:
            resp = client.get(
                "/getAllActivityEvents",
                params={"metadataId": type_id, "dimType": dim},
            )
            if isinstance(resp, list):
                events_list = resp
            elif isinstance(resp, dict):
                # some endpoints wrap in a key
                events_list = resp.get("events") or resp.get("data") or []
        except Exception as ex:
            # Not an error — just means no events for this type
            print(f"[Catalog] {type_id}: getAllActivityEvents → {ex}", file=sys.stderr, flush=True)

        has_events = len(events_list) > 0

        # ---- Type with events (didactivity, didnotactivity, etc.) -----
        if has_events:
            print(
                f"[Catalog] {type_id}: {len(events_list)} events",
                file=sys.stderr, flush=True,
            )
            events_catalog: list[dict] = []

            for event in events_list:
                event_id: str = event.get("id", "")
                event_label: str = event.get("displayName", event_id)
                if not event_id:
                    continue

                try:
                    fields_resp = client.get(
                        "/getMetadataContentForType",
                        params={
                            "metadataId": type_id,
                            "dimType": dim,
                            "eventId": event_id,
                        },
                    )
                    fields = _extract_fields(fields_resp)
                except Exception as ex:
                    print(
                        f"[Catalog]   {event_id}: field fetch failed — {ex}",
                        file=sys.stderr, flush=True,
                    )
                    fields = []

                if fields:
                    h = _hash_fields(fields)
                    if h not in field_store:
                        field_store[h] = fields
                        print(
                            f"[Catalog]   {event_id}: {len(fields)} fields (stored {h})",
                            file=sys.stderr, flush=True,
                        )
                    else:
                        print(
                            f"[Catalog]   {event_id}: {len(fields)} fields (deduped → {h})",
                            file=sys.stderr, flush=True,
                        )
                    fields_ref = h
                else:
                    fields_ref = None

                events_catalog.append({
                    "id": event_id,
                    "label": event_label,
                    "fields": fields,
                    "fields_ref": fields_ref,
                })

            catalog[type_id] = {
                "id": type_id,
                "label": type_label,
                "type": "events",
                "events": events_catalog,
            }

        # ---- Direct field type (match, donotmatch, etc.) --------------
        else:
            print(f"[Catalog] {type_id}: direct field type", file=sys.stderr, flush=True)

            try:
                fields_resp = client.get(
                    "/getMetadataContentForType",
                    params={
                        "metadataId": type_id,
                        "dimType": dim,
                        "eventId": dim,
                    },
                )
                fields = _extract_fields(fields_resp)
            except Exception as ex:
                print(
                    f"[Catalog] {type_id}: field fetch failed — {ex}",
                    file=sys.stderr, flush=True,
                )
                fields = []

            if fields:
                h = _hash_fields(fields)
                if h not in field_store:
                    field_store[h] = fields
                    print(
                        f"[Catalog] {type_id}: {len(fields)} fields (stored {h})",
                        file=sys.stderr, flush=True,
                    )
                else:
                    print(
                        f"[Catalog] {type_id}: {len(fields)} fields (deduped → {h})",
                        file=sys.stderr, flush=True,
                    )
                fields_ref = h
            else:
                fields_ref = None

            catalog[type_id] = {
                "id": type_id,
                "label": type_label,
                "type": "direct",
                "fields": fields,
                "fields_ref": fields_ref,
            }

    unique = len(field_store)
    total_types = len(catalog)
    print(
        f"[Catalog] Complete — {total_types} types, {unique} unique field sets.",
        file=sys.stderr, flush=True,
    )

    _enrich_fields_with_operators(client, catalog)

    return {
        "catalog": catalog,
        "field_store": field_store,
        "type_defs": types,
    }


def _enrich_fields_with_operators(client: AlgonomyClient, catalog: dict) -> None:
    """
    Enrich every field in the catalog with its operator list from getAttributeOptions.
    Full params: metadataId, fieldName (display label), type=attribute, dimType, eventId.
    Deduplicates by (metadataId, eventId, fieldId).
    """
    # (metadataId, eventId, fieldId) -> list of field dicts to update
    triples: dict[tuple[str, str, str], list[dict]] = {}

    for type_id, type_entry in catalog.items():
        if type_entry.get("type") == "segment":
            continue
        if type_entry.get("type") == "direct":
            event_id = client.dim_type
            for f in type_entry.get("fields") or []:
                triples.setdefault((type_id, event_id, f["id"]), []).append(f)
        elif type_entry.get("type") == "events":
            for event in type_entry.get("events") or []:
                event_id = event["id"]
                for f in event.get("fields") or []:
                    triples.setdefault((type_id, event_id, f["id"]), []).append(f)

    print(
        f"[Catalog] Enriching {len(triples)} (type, event, field) combos with operator metadata...",
        file=sys.stderr, flush=True,
    )

    for i, ((meta_id, event_id, field_id), field_dicts) in enumerate(triples.items(), start=1):
        try:
            opts = client.get(
                "/getAttributeOptions",
                params={
                    "metadataId": meta_id,
                    "fieldName": field_id,
                    "type": "attribute",
                    "dimType": client.dim_type,
                    "eventId": event_id,
                },
            )
            operators = [op["id"] for op in opts.get("operators", []) if op.get("id")]
        except Exception as ex:
            print(
                f"[Catalog]   {meta_id}/{event_id}/{field_id}: failed — {ex}",
                file=sys.stderr, flush=True,
            )
            operators = []

        for fd in field_dicts:
            fd["operators"] = operators

        if i % 50 == 0:
            print(f"[Catalog]   {i}/{len(triples)} combos enriched...", file=sys.stderr, flush=True)

    print("[Catalog] Operator enrichment complete.", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Query-time helpers (called per query, not at startup)
# ---------------------------------------------------------------------------

def get_attribute_options(client: AlgonomyClient, field_id: str, metadata_id: str = "") -> dict[str, Any]:
    """
    Fetch operator metadata for a specific field.
    Called at query time after Gemini selects a field.
    Returns operators list, isSearchable, dataType, etc.
    """
    params: dict = {"fieldName": field_id}
    if metadata_id:
        params["metadataId"] = metadata_id
    return client.get("/getAttributeOptions", params=params)


def get_child_attribute_options(
    client: AlgonomyClient, field_id: str, parent_field_id: str
) -> dict[str, Any]:
    """Fetch options for a child attribute (e.g. product sub-fields)."""
    return client.get(
        "/getChildAttributeOptions",
        params={"attributeId": field_id, "parentAttributeId": parent_field_id},
    )


def search_attribute(
    client: AlgonomyClient, field_id: str, term: str
) -> list[dict]:
    """
    Search for attribute values by term.
    Used for isSearchable fields to resolve "Nike" → "4821::Nike".
    """
    resp = client.get(
        "/searchAttribute",
        params={"attributeId": field_id, "searchString": term},
    )
    if isinstance(resp, list):
        return resp
    return resp.get("results") or resp.get("data") or []


def search_child_attribute(
    client: AlgonomyClient, field_id: str, parent_field_id: str, term: str
) -> list[dict]:
    """Search values for a child attribute field."""
    resp = client.get(
        "/searchChildAttribute",
        params={
            "attributeId": field_id,
            "parentAttributeId": parent_field_id,
            "searchString": term,
        },
    )
    if isinstance(resp, list):
        return resp
    return resp.get("results") or resp.get("data") or []
