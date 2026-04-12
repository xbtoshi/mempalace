"""
mempalace sync — bidirectional migration between local ChromaDB and Cloudflare.

  mempalace sync push [--palace PATH] [--batch N] [--dry-run]
    Push local palace → CF (MEMPALACE_CF_API_URL + MEMPALACE_CF_API_KEY must be set)

  mempalace sync pull [--palace PATH] [--batch N] [--dry-run]
    Pull CF palace → local ChromaDB
"""

import os

DEFAULT_BATCH = 100


def push(palace_path: str, batch_size: int = DEFAULT_BATCH, dry_run: bool = False) -> bool:
    """Push local ChromaDB palace to Cloudflare D1/Vectorize."""
    from .backends.chroma import ChromaBackend
    from .backends.cloudflare import CloudflareBackend

    print(f"\n{'=' * 60}")
    print("  MemPalace Sync — Push (Local → Cloudflare)")
    print(f"{'=' * 60}\n")
    print(f"  Local palace: {palace_path}")

    # Read all from local ChromaDB
    local = ChromaBackend().get_collection(palace_path, create=False)
    total = local.count()
    print(f"  Total local drawers: {total}")

    if total == 0:
        print("  Nothing to push.")
        return True

    if dry_run:
        print("  DRY RUN — no changes made.")
        return True

    # Fetch all docs — ChromaDB returns everything in one get() call
    all_data = local.get(limit=200_000)
    ids = all_data["ids"]
    docs = all_data["documents"]
    metas = all_data["metadatas"]

    if not ids:
        print("  No drawers returned from local palace.")
        return True

    cf = CloudflareBackend().get_collection()
    pushed = 0

    for i in range(0, len(ids), batch_size):
        b_ids = ids[i:i + batch_size]
        b_docs = docs[i:i + batch_size]
        b_metas = metas[i:i + batch_size]
        cf.upsert(documents=b_docs, ids=b_ids, metadatas=b_metas)
        pushed += len(b_ids)
        print(f"  Pushed {pushed}/{len(ids)} drawers...")

    cf_total = cf.count()
    print(f"\n  Done. {pushed} drawers pushed.")
    print(f"  CF palace now has {cf_total} total drawers.")
    print(f"{'=' * 60}\n")
    return True


def pull(palace_path: str, batch_size: int = DEFAULT_BATCH, dry_run: bool = False) -> bool:
    """Pull Cloudflare D1/Vectorize palace to local ChromaDB."""
    from .backends.chroma import ChromaBackend
    from .backends.cloudflare import CloudflareBackend, CloudflareCollection

    print(f"\n{'=' * 60}")
    print("  MemPalace Sync — Pull (Cloudflare → Local)")
    print(f"{'=' * 60}\n")

    cf_backend = CloudflareBackend()
    cf = cf_backend.get_collection()
    assert isinstance(cf, CloudflareCollection)

    total_remote = cf.count()
    print(f"  Remote CF drawers: {total_remote}")
    print(f"  Local palace: {palace_path}")

    if total_remote == 0:
        print("  Nothing to pull.")
        return True

    if dry_run:
        print("  DRY RUN — no changes made.")
        return True

    local = ChromaBackend().get_collection(palace_path, create=True)
    pulled = 0
    offset = 0

    while True:
        with cf._client() as client:
            resp = client.get(
                f"{cf._url}/drawers",
                params={"limit": batch_size, "offset": offset},
            )
            resp.raise_for_status()
            data = resp.json()

        drawers = data.get("drawers", [])
        if not drawers:
            break

        local.upsert(
            documents=[d["document"] for d in drawers],
            ids=[d["id"] for d in drawers],
            metadatas=[{
                "wing": d["wing"],
                "room": d["room"],
                "source_file": d.get("source_file", ""),
            } for d in drawers],
        )

        pulled += len(drawers)
        offset += len(drawers)
        print(f"  Pulled {pulled}/{total_remote} drawers...")

        if len(drawers) < batch_size:
            break

    local_total = local.count()
    print(f"\n  Done. {pulled} drawers pulled.")
    print(f"  Local palace now has {local_total} total drawers.")
    print(f"{'=' * 60}\n")
    return True


def run_sync_command(args: list) -> None:
    """CLI entry point: mempalace sync push|pull [options]"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="mempalace sync",
        description="Bidirectional sync between local ChromaDB and Cloudflare",
    )
    parser.add_argument("direction", choices=["push", "pull"], help="push or pull")
    parser.add_argument("--palace", default=None, help="Path to local palace")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="Batch size (default: 100)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, make no changes")

    parsed = parser.parse_args(args)

    # Resolve palace path
    palace_path = parsed.palace or os.environ.get(
        "MEMPALACE_PALACE_PATH",
        os.path.expanduser("~/.mempalace/palace"),
    )

    if parsed.direction == "push":
        push(palace_path, batch_size=parsed.batch, dry_run=parsed.dry_run)
    else:
        pull(palace_path, batch_size=parsed.batch, dry_run=parsed.dry_run)
