"""
metadata_api.py — Local Flask API for audience manager filter catalog metadata.

Run with:
    python metadata_api.py

Endpoints (Algonomy API-backed):
    GET  /health                              → health check
    GET  /algonomy/catalog                    → dynamic catalog from Algonomy
    GET  /algonomy/catalog?refresh=true       → force rebuild
    GET  /algonomy/attribute_options/<id>     → operator metadata for a field
    GET  /algonomy/search?field=&term=        → value search for a field
    GET  /algonomy/search_child?field=&parent=&term=
"""

from flask import Flask, jsonify, request as flask_request

app = Flask(__name__)

# Lazy-loaded Algonomy client — created once on first request
_algonomy_client = None
_algonomy_catalog_cache: dict | None = None


def _get_algonomy_client():
    global _algonomy_client
    if _algonomy_client is None:
        from algonomy_client import AlgonomyClient
        _algonomy_client = AlgonomyClient()
    return _algonomy_client


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Algonomy-backed endpoints
# ---------------------------------------------------------------------------

@app.get("/algonomy/catalog")
def algonomy_catalog():
    """
    Build and return the full dynamic Algonomy catalog.
    Cached in process memory — restart the server to refresh.
    Add ?refresh=true to force a rebuild.
    """
    global _algonomy_catalog_cache
    force = flask_request.args.get("refresh", "").lower() in ("true", "1", "yes")

    if _algonomy_catalog_cache is None or force:
        try:
            from algonomy_catalog import build_catalog
            client = _get_algonomy_client()
            _algonomy_catalog_cache = build_catalog(client)
        except Exception as ex:
            return jsonify({"error": str(ex)}), 500

    return jsonify(_algonomy_catalog_cache)


@app.get("/algonomy/attribute_options/<field_id>")
def algonomy_attribute_options(field_id: str):
    """
    Fetch operator metadata for a single field at query time.
    Returns operators list, isSearchable, dataType, etc.
    """
    try:
        from algonomy_catalog import get_attribute_options
        client = _get_algonomy_client()
        result = get_attribute_options(client, field_id)
        return jsonify(result)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.get("/algonomy/search")
def algonomy_search():
    """
    Search attribute values.  ?field=<field_id>&term=<search_term>
    Used to resolve user-typed value strings (e.g. "Nike") to IDs.
    """
    field_id = flask_request.args.get("field", "").strip()
    term = flask_request.args.get("term", "").strip()
    if not field_id or not term:
        return jsonify({"error": "Both 'field' and 'term' query params are required."}), 400

    try:
        from algonomy_catalog import search_attribute
        client = _get_algonomy_client()
        results = search_attribute(client, field_id, term)
        return jsonify(results)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.get("/algonomy/search_child")
def algonomy_search_child():
    """
    Search child attribute values.  ?field=<field_id>&parent=<parent_id>&term=<term>
    """
    field_id = flask_request.args.get("field", "").strip()
    parent_id = flask_request.args.get("parent", "").strip()
    term = flask_request.args.get("term", "").strip()
    if not field_id or not parent_id or not term:
        return jsonify({"error": "'field', 'parent', and 'term' are all required."}), 400

    try:
        from algonomy_catalog import search_child_attribute
        client = _get_algonomy_client()
        results = search_child_attribute(client, field_id, parent_id, term)
        return jsonify(results)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": str(e)}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("Metadata API running at http://localhost:5050")
    print("Endpoints:")
    print("  GET /health")
    print("  GET /algonomy/catalog              (dynamic Algonomy catalog)")
    print("  GET /algonomy/catalog?refresh=true (force rebuild)")
    print("  GET /algonomy/attribute_options/<id>")
    print("  GET /algonomy/search?field=<id>&term=<text>")
    print("  GET /algonomy/search_child?field=<id>&parent=<id>&term=<text>")
    app.run(host="0.0.0.0", port=5050, debug=True)
