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
      "operator": "equals" | "not_equals" | "contains" | "gte" | "lte" |
                  "between" | "in_last_days" | "in_list",
      "value":    <string | number | [min, max] | [val1, val2, ...]>
    },
    ...
  ]
"""

from typing import Any


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
        "didactivity":    "customer DID a browsing/transaction activity",
        "didnotactivity": "customer DID NOT DO a browsing/transaction activity",
    }
    for type_id, desc in type_descriptions.items():
        if type_id in catalog:
            lines.append(f"  {type_id}  ->  {desc}")

    # Collect unique datasets (by dataset id, deduplicated across types)
    seen: dict[str, list[dict]] = {}
    for type_entry in catalog.values():
        t = type_entry.get("type")
        if t == "direct":
            eid = type_entry["id"]
            if eid not in seen and type_entry.get("fields"):
                seen[eid] = type_entry["fields"]
        elif t == "events":
            for ev in type_entry.get("events", []):
                eid = ev["id"]
                if eid not in seen and ev.get("fields"):
                    seen[eid] = ev["fields"]

    # Show profile datasets first, then activity datasets
    priority = [
        "profile_data", "fct_sale_transaction", "fct_sale_trans_line_discount",
        "snap_cust_prod_affinity", "customer_affinity_mostly", "customer_affinity_minimum",
        "loyalty_point", "loyalty_point_expiry", "dim_vehicle",
    ]
    ordered = [d for d in priority if d in seen]
    ordered += [d for d in seen if d not in ordered]

    lines.append("")
    lines.append("DATASETS AND FIELDS (use the exact 'field id' shown):")

    for dataset_id in ordered:
        fields = seen[dataset_id]
        if not fields:
            continue
        lines.append(f"\n  [{dataset_id}]")
        by_group: dict[str, list[dict]] = {}
        for f in fields:
            g = f.get("group") or "Other"
            by_group.setdefault(g, []).append(f)
        for group_name, gfields in by_group.items():
            lines.append(f"    {group_name}:")
            for f in gfields:
                child_note = "  ← has sub-fields" if f.get("hasChild") else ""
                lines.append(f"      {f['id']}  ({f['label']}){child_note}")

    lines.append("")
    lines.append("OPERATORS:")
    lines.append("  equals        exact match")
    lines.append("  not_equals    does not equal")
    lines.append("  contains      substring match")
    lines.append("  gte           numeric >=")
    lines.append("  lte           numeric <=")
    lines.append("  between       numeric range, value=[min, max]")
    lines.append("  in_last_days  within last N days, value=N (integer)")
    lines.append("  in_list       one of a list, value=[v1, v2, ...]")

    return "\n".join(lines)


def is_algonomy_catalog(catalog: dict[str, Any]) -> bool:
    """Return True if the catalog dict came from the Algonomy API."""
    return isinstance(catalog, dict) and (
        catalog.get("_source") == "algonomy" or "catalog" in catalog
    )
