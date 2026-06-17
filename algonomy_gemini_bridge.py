"""
algonomy_gemini_bridge.py

Converts the Algonomy catalog (from algonomy_catalog.build_catalog) into
a flat text block that fits inside a Gemini prompt, so Gemini can select
the right type / dataset / field / operator / value.

Gemini output format (algonomy_rules list):
  [
    {
      "type":     "match" | "donotmatch" | "didactivity" | "didnotactivity",
      "event":    "<dataset id>",          e.g. "profile_data"
      "field":    "<algonomy field id>",   e.g. "gender"
      "operator": "EQ" | "NEQ" | "IN" | "NOTIN" | "CONTAINS" | "NOTCONTAINS" |
                  "GT" | "GTE" | "LT" | "LTE" | "BETWEEN" |
                  "IN_LAST_DAYS" | "IN_LAST_MONTHS" | "NOTNULL" | "NULL" | "ALL",
      "value":    <string | number | [min, max] | [val1, val2, ...]>
    },
    ...
  ]
"""

from typing import Any


def get_dataset_valid_types(algonomy_catalog: dict[str, Any]) -> dict[str, list[str]]:
    """Map every dataset/event id -> the list of rule 'type' ids it can legally appear under."""
    catalog = algonomy_catalog.get("catalog", {})
    valid_types: dict[str, list[str]] = {}
    for type_id, type_entry in catalog.items():
        t = type_entry.get("type")
        if t == "direct":
            eid = type_entry["id"]
            if type_entry.get("fields"):
                valid_types.setdefault(eid, []).append(type_id)
        elif t == "events":
            for ev in type_entry.get("events", []):
                eid = ev["id"]
                if ev.get("fields"):
                    valid_types.setdefault(eid, []).append(type_id)
    return valid_types


def fix_rule_types(rules: list[dict[str, Any]], algonomy_catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Correct each rule's 'type' if Gemini paired it with a dataset that isn't valid for that type.
    Remaps within the same polarity (match<->didactivity, donotmatch<->didnotactivity)
    based on which family the dataset actually belongs to.
    """
    valid_types = get_dataset_valid_types(algonomy_catalog)
    opposite_family = {
        "match": "didactivity", "didactivity": "match",
        "donotmatch": "didnotactivity", "didnotactivity": "donotmatch",
    }
    fixed: list[dict[str, Any]] = []
    for rule in rules:
        rtype = rule.get("type", "match")
        event = rule.get("event", "")
        allowed = valid_types.get(event)
        if allowed and rtype not in allowed:
            swapped = opposite_family.get(rtype)
            rule = {**rule, "type": swapped if swapped in allowed else allowed[0]}
        fixed.append(rule)
    return fixed


def build_gemini_catalog_text(algonomy_catalog: dict[str, Any]) -> str:
    """
    Convert an Algonomy catalog dict into readable text for a Gemini prompt.
    Only includes datasets with fields; groups them by logical area.
    """
    catalog = algonomy_catalog.get("catalog", {})
    lines: list[str] = []

    lines.append("AUDIENCE RULE TYPES (use in the 'type' field):")
    type_descriptions = {
        "match":          "customer MATCHES a profile property",
        "donotmatch":     "customer DOES NOT MATCH a profile property",
        "didactivity":    "customer DID a real browsing/engagement activity event",
        "didnotactivity": "customer DID NOT DO a real browsing/engagement activity event",
    }
    for type_id, desc in type_descriptions.items():
        if type_id in catalog:
            lines.append(f"  {type_id}  ->  {desc}")

    # Collect unique datasets (by dataset id, deduplicated across types)
    # and track which rule type(s) each dataset is valid for.
    seen: dict[str, list[dict]] = {}
    valid_types = get_dataset_valid_types(algonomy_catalog)
    for type_id, type_entry in catalog.items():
        t = type_entry.get("type")
        if t == "direct":
            eid = type_entry["id"]
            if type_entry.get("fields"):
                seen.setdefault(eid, type_entry["fields"])
        elif t == "events":
            for ev in type_entry.get("events", []):
                eid = ev["id"]
                if ev.get("fields"):
                    seen.setdefault(eid, ev["fields"])

    # Show profile datasets first, then activity datasets
    priority = [
        "profile_data", "fct_sale_transaction", "fct_sale_trans_line_discount",
        "snap_cust_prod_affinity", "customer_affinity_mostly", "customer_affinity_minimum",
        "loyalty_point", "loyalty_point_expiry", "dim_vehicle",
    ]
    ordered = [d for d in priority if d in seen]
    ordered += [d for d in seen if d not in ordered]

    lines.append("")
    lines.append(
        "DATASETS AND FIELDS (use the exact 'field id' shown; "
        "only pair a dataset with a 'type' listed in its 'valid for'):"
    )

    for dataset_id in ordered:
        fields = seen[dataset_id]
        if not fields:
            continue
        types_for_dataset = ", ".join(valid_types.get(dataset_id, []))
        lines.append(f"\n  [{dataset_id}]  (valid for: {types_for_dataset})")
        by_group: dict[str, list[dict]] = {}
        for f in fields:
            g = f.get("group") or "Other"
            by_group.setdefault(g, []).append(f)
        for group_name, gfields in by_group.items():
            lines.append(f"    {group_name}:")
            for f in gfields:
                ops = f.get("operators") or []
                ops_str = f"  [{', '.join(ops)}]" if ops else ""
                child_note = "  ← has sub-fields" if f.get("hasChild") else ""
                vl = f.get("valueList") or []
                vl_str = f"  values: {vl}" if vl else ""
                lines.append(f"      {f['id']}  ({f['label']}){ops_str}{vl_str}{child_note}")

    lines.append("")
    lines.append("OPERATOR NOTES:")
    lines.append("  - Each field shows its allowed operators in [brackets] above.")
    lines.append("  - Use ONLY operators listed for that field.")
    lines.append("  - NOTNULL / NULL / ALL require no value — set value to null.")
    lines.append("  - IN / NOTIN expect a list value: [v1, v2, ...]")
    lines.append("  - BETWEEN expects a range: [min, max]")
    lines.append("  - IN_LAST_DAYS / IN_LAST_MONTHS expect an integer value.")

    return "\n".join(lines)


def is_algonomy_catalog(catalog: dict[str, Any]) -> bool:
    """Return True if the catalog dict came from the Algonomy API."""
    return isinstance(catalog, dict) and (
        catalog.get("_source") == "algonomy" or "catalog" in catalog
    )
