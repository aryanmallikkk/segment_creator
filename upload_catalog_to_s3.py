"""
upload_catalog_to_s3.py

Builds the Algonomy catalog from the API and uploads it to S3.
Run this whenever tokens are refreshed or the catalog changes.

Usage:
    python upload_catalog_to_s3.py

Requires in .env (or environment):
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_REGION          (e.g. ap-south-1)
    S3_BUCKET           (bucket name)
    S3_CATALOG_KEY      (optional, defaults to algonomy_catalog.json)

    Plus the usual ALGONOMY_* tokens.
"""

import json
import sys
from pathlib import Path


def _load_env() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    import os
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def main() -> None:
    _load_env()
    import os, datetime

    # ── Validate env ─────────────────────────────────────────────────────────
    missing = [k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "S3_BUCKET") if not os.getenv(k)]
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        print("Add them to your .env file and retry.", file=sys.stderr)
        sys.exit(1)

    bucket     = os.environ["S3_BUCKET"]
    region     = os.environ["AWS_REGION"]
    catalog_key = os.getenv("S3_CATALOG_KEY", "algonomy_catalog.json")

    # ── Build catalog from Algonomy API ──────────────────────────────────────
    print("Building catalog from Algonomy API...")
    try:
        from algonomy_client import AlgonomyClient
        from algonomy_catalog import build_catalog
        client  = AlgonomyClient()
        catalog = build_catalog(client)
        catalog["_source"]   = "algonomy"
        catalog["_saved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    except Exception as ex:
        print(f"ERROR: Failed to build catalog: {ex}", file=sys.stderr)
        sys.exit(1)

    catalog_json = json.dumps(catalog, indent=2)
    print(f"Catalog built — {len(catalog_json):,} bytes")

    # ── Also save local snapshot ──────────────────────────────────────────────
    local_path = Path(__file__).with_name("algonomy_catalog_snapshot.json")
    local_path.write_text(catalog_json, encoding="utf-8")
    print(f"Local snapshot saved → {local_path}")

    # ── Upload to S3 ─────────────────────────────────────────────────────────
    try:
        import boto3
    except ImportError:
        print("ERROR: boto3 is not installed. Run: pip install boto3", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client(
        "s3",
        region_name=os.environ["AWS_REGION"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )

    # Create bucket if it doesn't exist
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"Bucket '{bucket}' found.")
    except s3.exceptions.ClientError as ex:
        code = ex.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            print(f"Bucket '{bucket}' not found — creating in {region}...")
            if region == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": region},
                )
            # Block all public access (safe default)
            s3.put_public_access_block(
                Bucket=bucket,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
            print(f"Bucket '{bucket}' created with public access blocked.")
        else:
            print(f"ERROR checking bucket: {ex}", file=sys.stderr)
            sys.exit(1)

    print(f"Uploading to s3://{bucket}/{catalog_key} ...")
    s3.put_object(
        Bucket=bucket,
        Key=catalog_key,
        Body=catalog_json.encode("utf-8"),
        ContentType="application/json",
    )
    print(f"Done. Catalog is live at s3://{bucket}/{catalog_key}")
    print(f"Saved at: {catalog['_saved_at']}")


if __name__ == "__main__":
    main()
