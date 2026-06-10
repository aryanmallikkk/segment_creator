# Audience Manager Mockup (Claude + MCP + Postgres)

## What this adds

- `audience_manager_mockup.py`  
  Streamlit UI that behaves like an audience builder mockup and auto-generates segments.

- `segment_orchestrator.py`  
  Backend orchestration that:
  - starts `mcpserver2.py` over stdio (JSON-RPC),
  - asks Claude to generate segment SQL when `ANTHROPIC_API_KEY` exists,
  - falls back to deterministic segment templates when Claude is not configured,
  - executes each segment via MCP `execute_query` and returns members/count.

## How to run

1. Ensure your Postgres objects are loaded by your ingest pipeline (`Ingest3.py`).
2. Install Streamlit:
   - `pip install streamlit`
3. Optional Claude integration:
   - set `ANTHROPIC_API_KEY` in your shell environment.
4. Run:
   - `streamlit run audience_manager_mockup.py`

## Notes about your requested segments

The mockup auto-creates these examples:
- Active customers
- Active lifestyle customers (mapped via `top_category ILIKE '%active%'`)
- Customers bought > 200 (mapped via `ltv_sales > 200`)
- Customers bought > 100 in milk category
- Customers bought in last 2 months and active

If your DB also has explicit customer profile fields such as `gender`, `lifecycle_stage`,
or `lifestyle_segment`, Claude-generated SQL can target those columns as well.
