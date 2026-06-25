from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import streamlit as st

from segment_orchestrator import (
    AudienceOrchestrator,
    _gemini_get_segment_suggestions as _claude_get_segment_suggestions,
)


def _fetch_count(rules: list[dict]) -> int:
    try:
        from algonomy_client import AlgonomyClient
        return AlgonomyClient().get_count(rules)
    except Exception:
        return -1


st.set_page_config(page_title="Audience Manager", layout="wide")

st.markdown(
    """
    <style>
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    [data-testid="stToolbar"] { visibility: hidden; }
    [data-testid="stDecoration"] { visibility: hidden; }
    [data-testid="stStatusWidget"] { visibility: hidden; }
    ._profileContainer_gzau3_53 { display: none !important; }
    ._container_gzau3_1 { display: none !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Filter label display names (Algonomy field IDs fall back to title-cased key) ──
_FILTER_LABELS: dict[str, str] = {
    "engagement_status": "Engagement Status",
    "monetary_tier": "Monetary Tier",
    "top_category": "Top Category",
    "ltv_sales": "Lifetime Sales",
    "days_since_last_order": "Days Since Last Order",
    "gender": "Gender",
    "lifecycle_stage": "Lifecycle Stage",
    "min_total_spend": "Min Total Spend",
    "product_category": "Category",
    "department": "Department",
    "product_subcategory": "Subcategory",
    "article_desc": "Article",
    "brand_name": "Brand",
    "product_brand": "Brand",
    "store_name": "Store",
    "channel_name": "Channel",
}

_OPERATOR_OPTIONS = [
    "EQ", "NEQ", "IN", "NOTIN",
    "CONTAINS", "NOTCONTAINS", "STARTSWITH", "ENDSWITH",
    "GT", "GTE", "LT", "LTE", "BETWEEN",
    "IN_LAST_DAYS", "IN_LAST_MONTHS",
    "NOTNULL", "NULL", "ALL",
]
_TYPE_ICONS = {"match": "🟢", "donotmatch": "🔴", "didactivity": "🔵", "didnotactivity": "🟠"}


# ── Helpers ──────────────────────────────────────────────────────────────────

# Resolution config for hasChild fields
_CHILD_RESOLUTION = {
    "brand": {
        "label": "Brand name",
        "field": "product_brand",
        "group_id": "product_attribute_frm_master",
        "group_name": "Product attribute master",
        "display_name": "Brand",
        "parent": "product_code",
        "api": "search_child_attribute",
    },
    "category": {
        "label": "Category",
        "field": "product_category_code",
        "group_id": "map_product_category",
        "group_name": "Category",
        "display_name": "Category",
        "parent": "product_code",
        "api": "search_child_attribute",
    },
    "product": {
        "label": "Specific product",
        "field": "product_code",
        "group_id": None,
        "group_name": None,
        "display_name": "Product",
        "parent": None,
        "api": "search_attribute",
    },
}


def _build_haschild_set(catalog: dict) -> set[str]:
    """Return set of field ids that have hasChild=True across all catalog events."""
    result = set()
    for type_data in catalog.get("catalog", {}).values():
        for event in type_data.get("events", []):
            for field in event.get("fields", []):
                if field.get("hasChild"):
                    result.add(field["id"])
    return result


def _needs_resolution(rule: dict, haschild_fields: set[str]) -> bool:
    """True if rule uses a hasChild field with a plain text value."""
    if rule.get("field") not in haschild_fields:
        return False
    val = rule.get("value")
    if not val or not isinstance(val, str):
        return False
    # Skip values that are already codes (numeric, bool, date-like, empty)
    v = val.strip()
    if not v or v.lower() in ("true", "false", "null"):
        return False
    try:
        float(v)
        return False
    except ValueError:
        pass
    return True


def _parse_rule_value(op: str, raw: str) -> Any:
    """Coerce a user-typed string value to the right Python type for an Algonomy rule operator."""
    if op in ("NOTNULL", "NULL", "ALL"):
        return None
    if op in ("IN", "NOTIN", "BETWEEN") and "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        try:
            return [float(p) if "." in p else int(p) for p in parts]
        except ValueError:
            return parts
    if op in ("IN_LAST_DAYS", "IN_LAST_MONTHS"):
        try:
            return int(raw)
        except ValueError:
            return raw
    if op in ("GT", "GTE", "LT", "LTE"):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _suggestion_key(cf: dict, sf: dict, count: int, algonomy_rules: list | None = None) -> str:
    payload = json.dumps(
        {"cf": cf, "sf": sf, "count": count, "alg": algonomy_rules or []},
        sort_keys=True,
    )
    return hashlib.md5(payload.encode()).hexdigest()


def _filter_chips(cf: dict[str, Any], sf: dict[str, Any]) -> None:
    parts: list[str] = []
    for k, v in {**cf, **sf}.items():
        if k == "purchased_last_n_days":
            continue
        label = _FILTER_LABELS.get(k, k.replace("_", " ").title())
        val = " or ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        parts.append(f"**{label}**: {val}")
    if parts:
        st.markdown("  \n".join(f"- {p}" for p in parts))
    else:
        st.caption("No additional filters applied.")


def _show_manual_filter_summary(blocks: list[dict[str, Any]], operator: str) -> None:
    # Show Algonomy rules if loaded
    alg_rules: list[dict] = st.session_state.get("manual_algonomy_rules") or []
    if alg_rules:
        seg_name = st.session_state.get("manual_algonomy_segment_name", "")
        manual_count = st.session_state.get("manual_algonomy_count", -1)
        count_str = f" · {manual_count:,} customers" if manual_count >= 0 else ""
        with st.container(border=True):
            if seg_name:
                st.markdown(f"🤖 **{seg_name}** — {len(alg_rules)} rule(s){count_str}")
            else:
                st.markdown(f"🤖 **Algonomy rules** — {len(alg_rules)} rule(s){count_str}")
            for rule in alg_rules:
                icon = _TYPE_ICONS.get(rule.get("type", "match"), "⚪")
                _fd0 = _rule_field_display(field_label_map, rule)
                st.markdown(
                    f"{icon} `{_event_label(event_label_map, rule.get('event',''))}` → `{_fd0}` "
                    f"**{operator_label_map.get(rule.get('operator',''), rule.get('operator',''))}** "
                    f"{value_label_map.get(rule.get('field',''), {}).get(str(rule.get('value','')), rule.get('value',''))}"
                )
        return

    active = [b for b in blocks if b.get("customer_filters") or b.get("sales_filters")]
    if not active:
        st.caption("No active filters.")
        return
    for i, block in enumerate(active, start=1):
        cf = block.get("customer_filters") or {}
        sf = block.get("sales_filters") or {}
        with st.container(border=True):
            if len(active) > 1:
                st.markdown(f"**Rule Block {i}**")
            _filter_chips(cf, sf)
    if len(active) > 1:
        st.caption(f"Blocks combined with **{operator}**")


def _get_suggestions_cached(
    cf: dict,
    sf: dict,
    count: int,
    catalog: dict,
    algonomy_rules: list[dict] | None = None,
) -> list[dict] | None:
    key = _suggestion_key(cf, sf, count, algonomy_rules)
    cache: dict = st.session_state.setdefault("_suggestion_cache", {})
    if key in cache:
        return cache[key]
    try:
        with st.spinner("✨ Generating smart suggestions..."):
            result = _claude_get_segment_suggestions(
                cf, sf, count, filter_catalog=catalog, algonomy_rules=algonomy_rules
            )
        cache[key] = result or []
        return cache[key]
    except Exception as exc:
        st.warning(f"Suggestions error: {exc}")
        return None


def _render_suggestions(
    idx: int,
    suggestions: list[dict],
    base_cf: dict,
    base_sf: dict,
    base_algonomy_rules: list[dict] | None = None,
) -> None:
    st.markdown("---")
    st.markdown("**✨ Suggested refinements**")
    cols = st.columns(min(len(suggestions), 3))
    for s_idx, sug in enumerate(suggestions):
        col = cols[s_idx % len(cols)]
        label = sug.get("label", f"Option {s_idx + 1}")
        desc = sug.get("description", "")
        sug_alg_rules: list[dict] | None = sug.get("algonomy_rules")
        with col:
            with st.container(border=True):
                st.markdown(f"**{label}**")
                if desc:
                    st.caption(desc)
                if sug_alg_rules:
                    for r in sug_alg_rules:
                        icon = _TYPE_ICONS.get(r.get("type", "match"), "⚪")
                        st.caption(
                            f"{icon} `{_event_label(event_label_map, r.get('event',''))}`.`{_field_label(field_label_map, r.get('field',''))}` "
                            f"{r.get('operator','')} {r.get('value','')}"
                        )
                if st.button("Apply", key=f"apply_sug_{idx}_{s_idx}", use_container_width=True):
                    if sug_alg_rules and base_algonomy_rules is not None:
                        merged_rules = list(base_algonomy_rules) + list(sug_alg_rules)
                        results = st.session_state.claude_generated
                        if 0 < idx <= len(results):
                            results[idx - 1].selected_filters["algonomy_rules"] = merged_rules
                        st.rerun()


def _render_filter_editor(
    idx: int,
    eff_cf: dict[str, Any],
    eff_sf: dict[str, Any],
    customer_attrs: list[dict],
    sales_attrs: list[dict],
    algonomy_rules: list[dict] | None = None,
) -> None:
    with st.expander("Edit filters", expanded=False):
        if algonomy_rules:
            new_rules = []
            for i, rule in enumerate(algonomy_rules):
                rtype = rule.get("type", "match")
                op    = rule.get("operator", "EQ")
                val   = rule.get("value", "")
                icon  = _TYPE_ICONS.get(rtype, "⚪")
                _fd = _rule_field_display(field_label_map, rule)
                st.markdown(f"{icon} **{rtype}** · `{_event_label(event_label_map, rule.get('event',''))}` → `{_fd}`")
                col_op, col_val = st.columns([1, 2])
                with col_op:
                    new_op = st.selectbox(
                        "Operator",
                        _OPERATOR_OPTIONS,
                        index=_OPERATOR_OPTIONS.index(op) if op in _OPERATOR_OPTIONS else 0,
                        key=f"alg_op_{idx}_{i}",
                        label_visibility="collapsed",
                    )
                with col_val:
                    new_val = st.text_input(
                        "Value",
                        value=str(val) if not isinstance(val, list) else ", ".join(str(v) for v in val),
                        key=f"alg_val_{idx}_{i}",
                        label_visibility="collapsed",
                    )
                new_rules.append({**rule, "operator": new_op, "value": _parse_rule_value(new_op, new_val)})
                st.divider()

            if st.button("Save edited rules", key=f"fedit_run_{idx}", use_container_width=True):
                results = st.session_state.claude_generated
                if 0 < idx <= len(results):
                    results[idx - 1].selected_filters["algonomy_rules"] = new_rules
                    with st.spinner("Fetching count..."):
                        results[idx - 1].count = _fetch_count(new_rules)
                st.rerun()
            return

        st.caption("No active filters to edit.")


def _input_for_attribute(attr_key: str, attr_type: str | None) -> Any:
    if attr_type == "number":
        return st.number_input("Value", value=0.0, step=1.0, key=f"manual_value_{attr_key}")
    return st.text_input("Value", key=f"manual_value_{attr_key}")


def _get_attr_groups(catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if catalog.get("_source") == "algonomy" or "catalog" in catalog:
        alg_catalog = catalog.get("catalog", {})

        def _fields_from_event(type_id: str, event_id: str) -> list[dict]:
            type_entry = alg_catalog.get(type_id, {})
            for ev in type_entry.get("events", []):
                if ev.get("id") == event_id:
                    return ev.get("fields", [])
            return []

        def _alg_fields_to_attrs(fields: list[dict]) -> list[dict[str, Any]]:
            attrs = []
            for f in fields:
                fid = f.get("id", "")
                if not fid or fid.startswith("tag_"):
                    continue
                attrs.append({
                    "key":    fid,
                    "label":  f.get("label", fid),
                    "type":   "string",
                    "source": "algonomy",
                    "group":  f.get("group", ""),
                })
            return attrs

        customer_attrs = _alg_fields_to_attrs(_fields_from_event("match", "profile_data"))
        sales_attrs = _alg_fields_to_attrs(_fields_from_event("match", "fct_sale_transaction"))
        return customer_attrs, sales_attrs

    return [], []


def _build_field_label_map(catalog: dict[str, Any]) -> dict[str, str]:
    """Map every Algonomy field id -> its display label, across all types/events."""
    label_map: dict[str, str] = {}
    alg_catalog = catalog.get("catalog", {})
    for type_entry in alg_catalog.values():
        for f in type_entry.get("fields") or []:
            fid = f.get("id")
            if fid:
                label_map[fid] = f.get("label", fid)
        for ev in type_entry.get("events") or []:
            for f in ev.get("fields") or []:
                fid = f.get("id")
                if fid:
                    label_map[fid] = f.get("label", fid)
    return label_map


def _field_label(field_label_map: dict[str, str], field_id: str) -> str:
    return field_label_map.get(field_id, field_id)


def _rule_field_display(field_label_map: dict[str, str], rule: dict) -> str:
    """Return a human-readable field label for a rule, respecting attribute hierarchy."""
    hierarchy = rule.get("_field_hierarchy")
    if hierarchy:
        parts = [_field_label(field_label_map, hierarchy[0])] + [p for p in hierarchy[1:] if p]
        return " › ".join(parts)
    return rule.get("_field_display") or _field_label(field_label_map, rule.get("field", ""))


def _build_operator_label_map(catalog: dict[str, Any]) -> dict[str, str]:
    """Map operator id -> displayText, built from all fields in the catalog."""
    label_map: dict[str, str] = {}
    for type_entry in catalog.get("catalog", {}).values():
        for ev in type_entry.get("events") or []:
            for f in ev.get("fields") or []:
                for op in f.get("operators") or []:
                    if isinstance(op, dict) and op.get("id"):
                        label_map.setdefault(op["id"], op.get("displayText", op["id"]))
    return label_map


def _build_value_label_map(catalog: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map field_id -> {code -> desc} for fields with a valueList."""
    label_map: dict[str, dict[str, str]] = {}
    for type_entry in catalog.get("catalog", {}).values():
        for ev in type_entry.get("events") or []:
            for f in ev.get("fields") or []:
                fid = f.get("id")
                vl = f.get("valueList") or []
                if fid and vl:
                    if fid not in label_map:
                        label_map[fid] = {}
                    for item in vl:
                        if isinstance(item, dict) and item.get("code"):
                            label_map[fid].setdefault(item["code"], item.get("desc", item["code"]))
    return label_map


def _build_event_label_map(catalog: dict[str, Any]) -> dict[str, str]:
    """Map every Algonomy event/dataset id -> its display label, across all types."""
    label_map: dict[str, str] = {}
    alg_catalog = catalog.get("catalog", {})
    for type_id, type_entry in alg_catalog.items():
        label_map.setdefault(type_id, type_entry.get("label", type_id))
        for ev in type_entry.get("events") or []:
            eid = ev.get("id")
            if eid:
                label_map[eid] = ev.get("label", eid)
    return label_map


def _event_label(event_label_map: dict[str, str], event_id: str) -> str:
    return event_label_map.get(event_id, event_id)


# ── Resolution widget ────────────────────────────────────────────────────────

def _render_resolution_widget(seg_idx: int, rule_idx: int, rule: dict, result: Any, all_rules: list[dict]) -> None:
    val = rule.get("value", "")
    key_type   = f"resolve_type_{seg_idx}_{rule_idx}"
    key_results = f"resolve_results_{seg_idx}_{rule_idx}"
    key_picked = f"resolve_picked_{seg_idx}_{rule_idx}"

    with st.container():
        st.caption(f"⚠️ Resolve **'{val}'** — what does this refer to?")
        col_type, col_search = st.columns([1, 2])

        with col_type:
            res_type = st.radio(
                "Type",
                options=list(_CHILD_RESOLUTION.keys()),
                format_func=lambda k: _CHILD_RESOLUTION[k]["label"],
                key=key_type,
                horizontal=True,
                label_visibility="collapsed",
            )

        with col_search:
            if st.button("Search", key=f"resolve_search_{seg_idx}_{rule_idx}"):
                try:
                    from algonomy_client import AlgonomyClient
                    client = AlgonomyClient()
                    cfg = _CHILD_RESOLUTION[res_type]
                    metadata_id = rule.get("type", "didactivity")
                    event_id = rule.get("event", "transaction_complete")

                    if cfg["api"] == "search_child_attribute":
                        items = client.search_child_attribute(
                            field_name=cfg["field"],
                            parent_attribute_id="product_code",
                            group_id=cfg["group_id"],
                            search_text=val,
                            metadata_id=metadata_id,
                            event_id=event_id,
                        )
                    elif cfg["api"] == "search_lookup_values":
                        items = client.search_lookup_values(
                            field_name=cfg["field"],
                            parent_attribute_id="product_code",
                            group_id=cfg["group_id"],
                            metadata_id=metadata_id,
                            event_id=event_id,
                        )
                        # client-side filter by search text
                        items = [i for i in items if val.lower() in i.get("desc", "").lower()][:50]
                    else:
                        items = client.search_attribute(
                            field_name=cfg["field"],
                            search_text=val,
                            metadata_id=metadata_id,
                            event_id=event_id,
                        )
                    st.session_state[key_results] = items
                    st.session_state.pop(key_picked, None)
                except Exception as ex:
                    st.error(f"Search failed: {ex}")

        results = st.session_state.get(key_results)
        if results is not None:
            if not results:
                st.warning(f"No results found for '{val}' as {_CHILD_RESOLUTION[res_type]['label']}.")
            else:
                options = [f"{i['desc']} ({i['code']})" for i in results]
                col_sel, col_all = st.columns([4, 1])
                with col_all:
                    if st.button("Select all", key=f"resolve_all_{seg_idx}_{rule_idx}"):
                        st.session_state[key_picked] = options
                with col_sel:
                    picked_labels = st.multiselect(
                        "Pick value(s)",
                        options,
                        default=st.session_state.get(key_picked, []),
                        key=key_picked,
                        label_visibility="collapsed",
                    )
                if st.button("Apply", key=f"resolve_apply_{seg_idx}_{rule_idx}"):
                    picked_labels = st.session_state.get(key_picked) or []
                    if not picked_labels:
                        st.warning("Select at least one value.")
                        st.stop()
                    picked_codes = [results[options.index(lbl)]["code"] for lbl in picked_labels]
                    cfg = _CHILD_RESOLUTION[st.session_state[key_type]]
                    operator = "IN" if len(picked_codes) > 1 else "EQ"
                    value = picked_codes if len(picked_codes) > 1 else picked_codes[0]
                    new_rule = {
                        **rule,
                        "field": cfg["field"],
                        "operator": operator,
                        "value": value,
                        "parentAttributeId": "product_code",
                        "groupId": cfg["group_id"],
                    }
                    gens = st.session_state.claude_generated
                    if 0 < seg_idx <= len(gens):
                        rules = list(gens[seg_idx - 1].selected_filters.get("algonomy_rules", []))
                        rules[rule_idx] = new_rule
                        gens[seg_idx - 1].selected_filters["algonomy_rules"] = rules
                        # Only fetch count when all rules in this segment are resolved
                        still_pending = any(_needs_resolution(r, haschild_fields) for r in rules)
                        if not still_pending:
                            with st.spinner("Fetching count..."):
                                gens[seg_idx - 1].count = _fetch_count(rules)
                    # clear resolution state
                    st.session_state.pop(key_results, None)
                    st.session_state.pop(key_picked, None)
                    st.rerun()


# ── Auto-resolve helpers ─────────────────────────────────────────────────────

def _gemini_identify_attribute(val: str, attributes: list[dict]) -> dict | None:
    """Ask Gemini which attribute field best matches the term. Returns the attribute dict or None."""
    import urllib.request as _req, json as _json
    api_key = os.getenv("GEMINI_API_KEY", "")
    model   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key or not attributes:
        return None
    product_attrs = [
        a for a in attributes
        if a.get("groupId") == "product_attribute_child" and a.get("display") == "true"
    ]
    if not product_attrs:
        return None
    attr_lines = "\n".join(
        f"- id={a['id']} displayName={a.get('displayName', a['id'])}"
        for a in product_attrs
    )
    prompt = (
        f"A user searched for '{val}' as a product attribute (e.g. color, size, material, style).\n"
        f"Pick the single best matching attribute from this list:\n{attr_lines}\n"
        "Reply with ONLY the id value (e.g. tag_123). If none match, reply: none."
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    url  = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    try:
        request = _req.Request(
            url, data=_json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST"
        )
        with _req.urlopen(request, timeout=10) as resp:
            raw  = _json.loads(resp.read().decode())
            import sys as _sys
            print(f"[Gemini identify_attribute] response:\n{_json.dumps(raw, indent=2)}", file=_sys.stderr)
            text = raw["candidates"][0]["content"]["parts"][0]["text"].strip()
            for a in product_attrs:
                if a["id"] in text:
                    return a
    except Exception:
        pass
    return None


def _try_auto_resolve(rule: dict) -> dict | None:
    """Use resolution_hint from Gemini rule, then call the single matching API."""
    import sys
    val         = rule.get("value", "")
    metadata_id = rule.get("type", "didactivity")
    event_id    = rule.get("event", "transaction_complete")
    res_type    = rule.get("resolution_hint", "category")

    if res_type not in _CHILD_RESOLUTION and res_type != "attribute":
        res_type = "category"

    print(f"[auto_resolve] '{val}' → {res_type} (from hint)", file=sys.stderr)

    try:
        from algonomy_client import AlgonomyClient
        client = AlgonomyClient()

        if res_type == "attribute":
            attrs   = client.get_child_attributes(metadata_id, event_id)
            matched = _gemini_identify_attribute(val, attrs)
            if not matched:
                print(f"[auto_resolve] No attribute matched for '{val}'", file=sys.stderr)
                return None
            field_id = matched["id"]
            group_id = matched.get("groupId", "product_attribute_child")
            items = client.search_child_attribute(
                field_name=field_id,
                parent_attribute_id="product_code",
                group_id=group_id,
                search_text=val,
                metadata_id=metadata_id,
                event_id=event_id,
            )
            if not items:
                print(f"[auto_resolve] No values for '{val}' in attribute {field_id}", file=sys.stderr)
                return None
            codes    = [i["code"] for i in items]
            descs    = [i.get("desc", i["code"]) for i in items]
            operator = "IN" if len(codes) > 1 else "EQ"
            value    = codes if len(codes) > 1 else codes[0]
            return {
                **rule,
                "field": field_id,
                "operator": operator,
                "value": value,
                "_display_value": descs if len(descs) > 1 else descs[0],
                "_field_hierarchy": [
                    "product_code",
                    matched.get("groupName", ""),
                    matched.get("displayName", field_id),
                ],
                "parentAttributeId": "product_code",
                "groupId": group_id,
                "_auto_resolved_as": f"attribute ({matched.get('displayName', field_id)})",
            }

        cfg = _CHILD_RESOLUTION[res_type]
        if cfg["api"] == "search_child_attribute":
            items = client.search_child_attribute(
                field_name=cfg["field"],
                parent_attribute_id="product_code",
                group_id=cfg["group_id"],
                search_text=val,
                metadata_id=metadata_id,
                event_id=event_id,
            )
        else:
            items = client.search_attribute(
                field_name=cfg["field"],
                search_text=val,
                metadata_id=metadata_id,
                event_id=event_id,
            )

        if not items:
            print(f"[auto_resolve] No results for '{val}' as {res_type}", file=sys.stderr)
            return None

        codes    = [i["code"] for i in items]
        descs    = [i.get("desc", i["code"]) for i in items]
        operator = "IN" if len(codes) > 1 else "EQ"
        value    = codes if len(codes) > 1 else codes[0]
        hierarchy = [p for p in [cfg.get("parent"), cfg.get("group_name"), cfg.get("display_name")] if p]
        resolved = {
            **rule,
            "field": cfg["field"],
            "operator": operator,
            "value": value,
            "_display_value": descs if len(descs) > 1 else descs[0],
            "_field_hierarchy": hierarchy if len(hierarchy) > 1 else None,
            "groupId": cfg["group_id"],
            "_auto_resolved_as": res_type,
        }
        if cfg.get("parent"):
            resolved["parentAttributeId"] = cfg["parent"]
        return resolved

    except Exception as ex:
        print(f"[auto_resolve] Failed for '{val}': {ex}", file=sys.stderr)
    return None


def _auto_resolve_all(results: list, haschild_fields: set[str]) -> None:
    """Run auto-resolution on every pending rule across all segments in-place."""
    for result in results:
        rules = list(result.selected_filters.get("algonomy_rules") or [])
        changed = False
        for ri, rule in enumerate(rules):
            if _needs_resolution(rule, haschild_fields):
                resolved = _try_auto_resolve(rule)
                if resolved:
                    rules[ri] = resolved
                    changed = True
        if changed:
            result.selected_filters["algonomy_rules"] = rules
            still_pending = any(_needs_resolution(r, haschild_fields) for r in rules)
            if not still_pending:
                result.count = _fetch_count(rules)


# ── State ────────────────────────────────────────────────────────────────────

def _init_state() -> None:
    defaults: dict[str, Any] = {
        "manual_rule_blocks": [{"customer_filters": {}, "sales_filters": {}}],
        "manual_block_operator": "AND",
        "claude_generated": [],
        "_suggestion_cache": {},
        "_catalog_cache": None,
        "manual_algonomy_rules": [],
        "manual_algonomy_segment_name": "",
        "manual_algonomy_count": -1,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Bootstrap ─────────────────────────────────────────────────────────────────

_init_state()
orchestrator = AudienceOrchestrator()


# ── Load catalog ──────────────────────────────────────────────────────────────

try:
    catalog: dict[str, Any] = st.session_state.get("_catalog_cache") or {}
    if not catalog or (
        not catalog.get("_source") == "algonomy" and "catalog" not in catalog
    ):
        catalog = orchestrator.get_filter_catalog()
        st.session_state["_catalog_cache"] = catalog

except Exception as ex:  # noqa: BLE001
    st.error(f"Failed loading filter catalog: {ex}")
    st.stop()

customer_attrs, sales_attrs = _get_attr_groups(catalog)
field_label_map    = _build_field_label_map(catalog)
event_label_map    = _build_event_label_map(catalog)
operator_label_map = _build_operator_label_map(catalog)
value_label_map    = _build_value_label_map(catalog)
haschild_fields    = _build_haschild_set(catalog)

# ── Main tabs ─────────────────────────────────────────────────────────────────

builder_manual, builder_claude = st.tabs(["Manual segment builder", "AI-assisted builder"])

with builder_manual:
    st.markdown("### Manual Builder")

    # Show any Algonomy rules loaded from the AI builder
    _loaded_rules: list[dict] = st.session_state.get("manual_algonomy_rules") or []
    if _loaded_rules:
        _seg_name = st.session_state.get("manual_algonomy_segment_name", "")
        with st.container(border=True):
            st.markdown(
                f"🤖 **Loaded from AI segment{': ' + _seg_name if _seg_name else ''}**  "
                f"  ·  {len(_loaded_rules)} rule(s)"
            )
            _edited_rules: list[dict] = []
            for _ri, _rule in enumerate(_loaded_rules):
                _rtype  = _rule.get("type", "match")
                _revent = _rule.get("event", "")
                _rfield = _rule.get("field", "")
                _rop    = _rule.get("operator", "EQ")
                _rval   = _rule.get("value", "")
                _icon   = _TYPE_ICONS.get(_rtype, "⚪")
                _fd2 = _rule_field_display(field_label_map, _rule)
                st.markdown(f"{_icon} `{_event_label(event_label_map, _revent)}` → `{_fd2}`")
                _rc1, _rc2 = st.columns([1, 2])
                with _rc1:
                    _new_op = st.selectbox(
                        "Op", _OPERATOR_OPTIONS,
                        index=_OPERATOR_OPTIONS.index(_rop) if _rop in _OPERATOR_OPTIONS else 0,
                        key=f"manual_alg_op_{_ri}",
                        label_visibility="collapsed",
                    )
                with _rc2:
                    _val_str = (
                        ", ".join(str(v) for v in _rval) if isinstance(_rval, list) else str(_rval)
                    )
                    _new_val_str = st.text_input(
                        "Value", value=_val_str,
                        key=f"manual_alg_val_{_ri}",
                        label_visibility="collapsed",
                    )
                _edited_rules.append({**_rule, "operator": _new_op, "value": _parse_rule_value(_new_op, _new_val_str)})
                st.divider()

            _mb1, _mb2 = st.columns(2)
            if _mb1.button("Save rule changes", key="manual_alg_save", use_container_width=True):
                st.session_state.manual_algonomy_rules = _edited_rules
                with st.spinner("Fetching count..."):
                    st.session_state.manual_algonomy_count = _fetch_count(_edited_rules)
                st.rerun()
            if _mb2.button("Clear loaded rules", key="manual_alg_clear", use_container_width=True):
                st.session_state.manual_algonomy_rules = []
                st.session_state.manual_algonomy_segment_name = ""
                st.session_state.manual_algonomy_count = -1
                st.rerun()

    tab_match, tab_not_match, tab_activity, tab_not_activity, tab_segment, tab_not_segment = st.tabs(
        ["Match property", "Didn't match property", "Did activity",
         "Didn't do activity", "Belongs to segment", "Doesn't belong to segment"]
    )

    with tab_match:
        h1, h2, h3, h4 = st.columns(4)
        if h1.button("Add rule block", use_container_width=True):
            st.session_state.manual_rule_blocks.append({"customer_filters": {}, "sales_filters": {}})
        if h2.button("Remove last block", use_container_width=True):
            if len(st.session_state.manual_rule_blocks) > 1:
                st.session_state.manual_rule_blocks.pop()
        st.session_state.manual_block_operator = h3.selectbox(
            "Combine blocks with", ["AND", "OR"],
            index=0 if st.session_state.manual_block_operator == "AND" else 1,
        )
        if h4.button("Clear all filters", use_container_width=True):
            st.session_state.manual_rule_blocks = [{"customer_filters": {}, "sales_filters": {}}]
            st.session_state.manual_block_operator = "AND"
            st.info("Cleared all filters.")

        block_labels = [f"Rule block {i + 1}" for i in range(len(st.session_state.manual_rule_blocks))]
        active_block_idx = st.selectbox(
            "Edit block", range(len(block_labels)), format_func=lambda i: block_labels[i]
        )
        active_block = st.session_state.manual_rule_blocks[active_block_idx]

        filter_scope = st.radio("Filter group", ["Customer attributes", "Sales attributes"], horizontal=True)
        if filter_scope == "Customer attributes":
            attrs = customer_attrs
            target_filters = active_block["customer_filters"]
        else:
            attrs = sales_attrs
            target_filters = active_block["sales_filters"]

        attr_by_label = {a["label"]: a for a in attrs}
        selected_label = st.selectbox("Attribute", list(attr_by_label.keys()) or ["(no attributes)"])
        selected_attr = attr_by_label.get(selected_label)

        if selected_attr:
            value = _input_for_attribute(selected_attr["key"], selected_attr.get("type"))
            a_col, r_col, c_col = st.columns(3)
            if a_col.button("Add / update filter", use_container_width=True):
                target_filters[selected_attr["key"]] = value
                st.success(f"Saved: {selected_attr['label']} = {value}")
            if r_col.button("Remove selected filter", use_container_width=True):
                target_filters.pop(selected_attr["key"], None)
                st.info(f"Removed: {selected_attr['label']}")
            if c_col.button("Clear active block", use_container_width=True):
                active_block["customer_filters"] = {}
                active_block["sales_filters"] = {}
                st.info("Cleared active block.")

        st.markdown("#### Active filters")
        _show_manual_filter_summary(
            st.session_state.manual_rule_blocks,
            st.session_state.manual_block_operator,
        )


with builder_claude:
    st.markdown("### AI-assisted segment builder")
    default_prompt = ""
    user_prompt = st.text_area(
        "Describe audiences in natural language",
        value=default_prompt,
        height=220,
    )

    resolve_col, btn_col = st.columns([1, 2])
    with resolve_col:
        auto_resolve = st.toggle(
            "Auto-resolve",
            value=st.session_state.get("auto_resolve_mode", True),
            key="auto_resolve_mode",
            help="ON: automatically resolve brand/category/product references. OFF: pick manually.",
        )
    with btn_col:
        generate_clicked = st.button("Auto-create segments with Gemini", type="primary", use_container_width=True)

    if generate_clicked:
        with st.spinner("Generating segments..."):
            st.session_state.claude_generated = orchestrator.create_segments(user_prompt, catalog=catalog)
        if st.session_state.get("auto_resolve_mode", True):
            with st.spinner("Auto-resolving product references..."):
                _auto_resolve_all(st.session_state.claude_generated, haschild_fields)
        st.session_state["_suggestion_cache"] = {}

    if st.session_state.claude_generated:
        st.subheader("Generated segments")
        for idx, result in enumerate(st.session_state.claude_generated, start=1):
            clarification_payload = result.selected_filters or {}
            is_clarification = bool(
                isinstance(clarification_payload, dict)
                and clarification_payload.get("clarification_state")
            )

            eff_cf = result.selected_filters.get("customer_filters") or {}
            eff_sf = result.selected_filters.get("sales_filters") or {}

            # Hoist once — used for expander title and inner logic
            algonomy_rules = result.selected_filters.get("algonomy_rules")

            if is_clarification:
                product_term = clarification_payload.get("product_term", "")
                ct = clarification_payload.get("clarification_type", "")
                if ct == "spell_check":
                    expander_title = "Did you mean...?"
                else:
                    expander_title = (
                        clarification_payload.get("question")
                        or "Clarification needed"
                    )
            else:
                if algonomy_rules:
                    count_label = f"{result.count:,}" if result.count >= 0 else "⏳"
                    expander_title = f"{idx}. {result.name}  ·  {count_label} customers"
                else:
                    expander_title = f"{idx}. {result.name}"

            with st.expander(expander_title, expanded=True):
                st.caption(f"Generated by: {result.source}")

                _desc = result.description
                if _desc:
                    for _ch in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!"):
                        _desc = _desc.replace(_ch, "\\" + _ch)
                    st.markdown(_desc)
                else:
                    st.markdown("_No description_")

                if not is_clarification:
                    if algonomy_rules:
                        st.markdown("**Selected rules:**")
                        for ri, rule in enumerate(algonomy_rules):
                            rtype = rule.get("type", "match")
                            icon = _TYPE_ICONS.get(rtype, "⚪")
                            auto_tag = f" _(auto: {rule['_auto_resolved_as']})_" if rule.get("_auto_resolved_as") else ""
                            display_val = rule.get("_display_value") or rule.get("value", "")
                            if not isinstance(display_val, list):
                                fid = rule.get("field", "")
                                display_val = value_label_map.get(fid, {}).get(str(display_val), display_val)
                            if isinstance(display_val, list):
                                fid = rule.get("field", "")
                                vmap = value_label_map.get(fid, {})
                                display_val = ", ".join(vmap.get(str(v), str(v)) for v in display_val)
                            op_id = rule.get("operator", "")
                            op_label = operator_label_map.get(op_id, op_id)
                            field_display = _rule_field_display(field_label_map, rule)
                            val_part = f" &nbsp; **{display_val}**" if display_val not in (None, "", "None", []) else ""
                            st.markdown(
                                f"{icon} **{rtype}** &nbsp;·&nbsp; "
                                f"`{_event_label(event_label_map, rule.get('event',''))}` → `{field_display}` "
                                f"&nbsp; {op_label}{val_part}{auto_tag}"
                            )
                            if _needs_resolution(rule, haschild_fields):
                                if st.session_state.get("auto_resolve_mode", True):
                                    st.caption(f"⚠️ Could not auto-resolve **'{rule.get('value')}'** — resolve manually below.")
                                _render_resolution_widget(idx, ri, rule, result, algonomy_rules)

                if result.error:
                    options = clarification_payload.get("options")
                    clarification_state = clarification_payload.get("clarification_state")
                    question = clarification_payload.get("question")

                    if isinstance(options, list) and options and question:
                        st.info(question)
                        selected_options = st.multiselect(
                            "Choose option(s):", options, default=[], key=f"clarify_scope_{idx}"
                        )
                        if st.button(f"Apply clarification for segment {idx}", key=f"apply_clarify_{idx}"):
                            if not selected_options:
                                st.error("Please choose at least one option.")
                            else:
                                clarification_answer = f"Use these: {', '.join(selected_options)}."
                                st.session_state.claude_generated = orchestrator.resolve_clarification(
                                    user_prompt=user_prompt,
                                    clarification_state=clarification_state,
                                    clarification_answer=clarification_answer,
                                    catalog=catalog,
                                )
                                st.rerun()
                    elif not is_clarification:
                        st.error(result.error)

                else:
                    # Suggestions
                    suggestions = _get_suggestions_cached(
                        eff_cf, eff_sf, result.count, catalog,
                        algonomy_rules=algonomy_rules,
                    )
                    if suggestions is None:
                        st.caption("Could not load suggestions.")
                    elif suggestions:
                        _render_suggestions(
                            idx, suggestions, eff_cf, eff_sf,
                            base_algonomy_rules=algonomy_rules,
                        )

                    # Edit filters
                    _render_filter_editor(
                        idx, eff_cf, eff_sf, customer_attrs, sales_attrs,
                        algonomy_rules=algonomy_rules,
                    )

                    # Load into manual builder
                    if st.button(f"Load segment {idx} into manual builder", key=f"load_to_manual_{idx}"):
                        if algonomy_rules:
                            st.session_state.manual_algonomy_rules = list(algonomy_rules)
                            st.session_state.manual_algonomy_segment_name = result.name
                            st.session_state.manual_rule_blocks = [{"customer_filters": {}, "sales_filters": {}}]
                        st.session_state.manual_block_operator = "AND"
                        st.success("Loaded into manual builder — switch to the 'Manual segment builder' tab.")
