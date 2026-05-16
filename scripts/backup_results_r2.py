"""Upload local results/ and data/.bt_cache/ to Cloudflare R2.

Requires R2 credentials in environment or .env.monitor:
  R2_ACCOUNT_ID       (32 hex chars)
  R2_ACCESS_KEY_ID    (32 hex chars)
  R2_SECRET_ACCESS_KEY (64 hex chars)
  R2_BUCKET_NAME      (bucket name)

Usage:
  python scripts/backup_results_r2.py [--prefix backups/YYYY-MM-DD] [--dry-run]
"""
import argparse, boto3, datetime, os, pathlib, sys

REPO = pathlib.Path(__file__).parent.parent

def load_env():
    env_file = REPO / ".env.monitor"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

def get_client():
    acct = os.environ.get("R2_ACCOUNT_ID", "").strip()
    key  = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    sec  = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    if not acct or not key or not sec:
        print("ERROR: R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY not set.")
        print("Add them to .env.monitor or export as env vars.")
        sys.exit(1)
    return boto3.client(
        "s3",
        endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=key,
        aws_secret_access_key=sec,
        region_name="auto",
    )

def upload_dir(s3, bucket, local_dir, r2_prefix, dry_run, extensions=None):
    root = REPO / local_dir
    if not root.exists():
        print(f"  {local_dir}: not found, skipping")
        return 0, 0
    count = size = 0
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if extensions and f.suffix not in extensions:
            continue
        rel = f.relative_to(root)
        key = f"{r2_prefix}/{rel}"
        sz = f.stat().st_size
        size += sz
        count += 1
        if dry_run:
            print(f"  [DRY] {key}  ({sz/1024:.1f} KB)")
        else:
            s3.upload_file(str(f), bucket, key)
            print(f"  {key}  ({sz/1024:.1f} KB)")
    return count, size

def main():
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default=f"backups/{datetime.date.today()}")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-cache", action="store_true", help="Skip .bt_cache/")
    args = parser.parse_args()

    bucket = os.environ.get("R2_BUCKET_NAME", "tradegenius").strip()
    s3 = None if args.dry_run else get_client()

    print(f"Backup prefix: {args.prefix}  bucket: {bucket}  dry_run: {args.dry_run}")
    print()

    total_files = total_size = 0

    print("=== results/ (sweep outputs) ===")
    n, sz = upload_dir(s3, bucket, "results", f"{args.prefix}/results",
                        args.dry_run, extensions={".json"})
    total_files += n; total_size += sz
    print(f"  -> {n} files  {sz/1024/1024:.1f} MB")

    if not args.no_cache:
        print("\n=== data/.bt_cache/ (pkl bar cache) ===")
        n, sz = upload_dir(s3, bucket, "data/.bt_cache", f"{args.prefix}/bt_cache",
                            args.dry_run, extensions={".pkl"})
        total_files += n; total_size += sz
        print(f"  -> {n} files  {sz/1024/1024:.1f} MB")

    print("\n=== docs/ (PDF + grids) ===")
    n, sz = upload_dir(s3, bucket, "docs", f"{args.prefix}/docs",
                        args.dry_run, extensions={".md", ".pdf", ".py", ".json"})
    total_files += n; total_size += sz
    print(f"  -> {n} files  {sz/1024/1024:.1f} MB")

    print(f"\nTotal: {total_files} files  {total_size/1024/1024:.1f} MB")
    if not args.dry_run:
        print(f"All uploaded to r2://{bucket}/{args.prefix}/")

if __name__ == "__main__":
    main()
