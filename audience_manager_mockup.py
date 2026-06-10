from __future__ import annotations

import hashlib
import json
from typing import Any

import streamlit as st

from segment_orchestrator import (
    AudienceOrchestrator,
    _gemini_get_segment_suggestions as _claude_get_segment_suggestions,
)


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

_OPERATOR_OPTIONS = ["equals", "not_equals", "contains", "gte", "lte", "between", "in_last_days", "in_list"]
_TYPE_ICONS = {"match": "🟢", "donotmatch": "🔴", "didactivity": "🔵", "didnotactivity": "🟠"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_rule_value(op: str, raw: str) -> Any:
    """Coerce a user-typed string value to the right Python type for an Algonomy rule operator."""
    if op in ("in_list", "between") and "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        try:
            return [float(p) if "." in p else int(p) for p in parts]
        except ValueError:
            return parts
    if op == "in_last_days":
        try:
            return int(raw)
        except ValueError:
            return raw
    if op in ("gte", "lte"):
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
        with st.container(border=True):
            if seg_name:
                st.markdown(f"🤖 **{seg_name}** — {len(alg_rules)} rule(s)")
            else:
                st.markdown(f"🤖 **Algonomy rules** — {len(alg_rules)} rule(s)")
            for rule in alg_rules:
                icon = _TYPE_ICONS.get(rule.get("type", "match"), "⚪")
                st.markdown(
                    f"{icon} `{rule.get('event','')}` → `{rule.get('field','')}` "
                    f"**{rule.get('operator','')}** {rule.get('value','')}"
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
                            f"{icon} `{r.get('event','')}`.`{r.get('field','')}` "
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
                op    = rule.get("operator", "equals")
                val   = rule.get("value", "")
                icon  = _TYPE_ICONS.get(rtype, "⚪")
                st.markdown(f"{icon} **{rtype}** · `{rule.get('event','')}` → `{rule.get('field','')}`")
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
                st.success("Rules updated. ⏳ Count will refresh when getCount API is connected.")
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

    with st.sidebar:
        src = catalog.get("_source", "unknown")
        saved = catalog.get("_saved_at", "")
        st.caption(f"Catalog source: **{src}**" + (f"  \nSaved: {saved}" if saved else ""))
        if st.button("🔄 Refresh catalog", help="Rebuilds from Algonomy API — update .env tokens first"):
            orchestrator._catalog_cache = None
            st.session_state["_catalog_cache"] = None
            st.session_state["_suggestion_cache"] = {}
            st.rerun()
except Exception as ex:  # noqa: BLE001
    st.error(f"Failed loading filter catalog: {ex}")
    st.stop()

customer_attrs, sales_attrs = _get_attr_groups(catalog)

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
                _rop    = _rule.get("operator", "equals")
                _rval   = _rule.get("value", "")
                _icon   = _TYPE_ICONS.get(_rtype, "⚪")
                st.markdown(f"{_icon} `{_revent}` → `{_rfield}`")
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
                st.success("Rules updated. ⏳ Count pending getCount API integration.")
                st.rerun()
            if _mb2.button("Clear loaded rules", key="manual_alg_clear", use_container_width=True):
                st.session_state.manual_algonomy_rules = []
                st.session_state.manual_algonomy_segment_name = ""
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

    if st.button("Auto-create segments with Gemini", type="primary", use_container_width=True):
        st.session_state.claude_generated = orchestrator.create_segments(user_prompt, catalog=catalog)
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
                    expander_title = f"{idx}. {result.name}  ·  ⏳ count pending"
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
                        for rule in algonomy_rules:
                            rtype = rule.get("type", "match")
                            icon = _TYPE_ICONS.get(rtype, "⚪")
                            st.markdown(
                                f"{icon} **{rtype}** &nbsp;·&nbsp; "
                                f"`{rule.get('event','')}` → `{rule.get('field','')}` "
                                f"&nbsp; {rule.get('operator','')} &nbsp; **{rule.get('value','')}**"
                            )

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
