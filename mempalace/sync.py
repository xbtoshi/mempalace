"""
mempalace sync — bidirectional incremental sync between local ChromaDB and Cloudflare.

Commands:
  mempalace sync push [--palace PATH] [--batch N] [--dry-run]
    Incremental push: only sends drawers that changed since last sync.

  mempalace sync pull [--palace PATH] [--batch N] [--dry-run]
    Incremental pull: only fetches drawers that are new/changed on CF.

  mempalace sync daemon [--palace PATH] [--interval N] [--dry-run]
    Watch loop: runs incremental push+pull every N minutes (default: 5).

Conflict resolution:
  - Same content (same hash) → skip, no-op
  - Local changed, CF unchanged since last sync → push (local wins)
  - CF changed, local unchanged since last sync → pull (CF wins)
  - Both changed since last sync → CF wins (remote is shared truth),
    local version logged to sync_conflicts.jsonl for review
  - ID only in local → push
  - ID only in CF → pull

State file: ~/.mempalace/sync_state.json  (tracks what was last synced)
Conflict log: ~/.mempalace/sync_conflicts.jsonl
"""

import os
import time
from typing import List, Dict, Any

from .sync_state import (
    content_hash, load_state, save_state,
    mark_synced, mark_deleted, log_conflict
)

DEFAULT_BATCH = 100
DEFAULT_INTERVAL = 5  # minutes


def _local_backend(palace_path: str, create: bool = False):
    from .backends.chroma import ChromaBackend
    return ChromaBackend().get_collection(palace_path, collection_name="mempalace_drawers", create=create)


def _cf_backend():
    from .backends.cloudflare import CloudflareBackend
    return CloudflareBackend().get_collection()


def _fetch_all_local(palace_path: str) -> List[Dict[str, Any]]:
    """Fetch all drawers from local palace.

    Tries ChromaDB API first. If that fails (version mismatch), falls back
    to reading directly from SQLite — same approach as mempalace migrate.
    """
    try:
        col = _local_backend(palace_path, create=False)
        data = col.get(limit=200_000)
        drawers = []
        for i, drawer_id in enumerate(data["ids"]):
            drawers.append({
                "id": drawer_id,
                "document": data["documents"][i],
                "metadata": data["metadatas"][i],
            })
        return drawers
    except Exception as e:
        # ChromaDB version mismatch or corrupt palace — fall back to raw SQLite
        import os
        db_path = os.path.join(palace_path, "chroma.sqlite3")
        if not os.path.isfile(db_path):
            raise FileNotFoundError(
                f"No palace found at {palace_path}. "
                "Run: mempalace init <dir> && mempalace mine <dir>"
            ) from e
        print(f"  ChromaDB API failed ({e})")
        print("  Falling back to direct SQLite read (version mismatch — run 'mempalace migrate' to fix permanently)")
        from .migrate import extract_drawers_from_sqlite
        raw = extract_drawers_from_sqlite(db_path)
        return [
            {
                "id": d["id"],
                "document": d["document"],
                "metadata": d["metadata"],
            }
            for d in raw
        ]


def _fetch_all_remote(batch_size: int = DEFAULT_BATCH) -> List[Dict[str, Any]]:
    """Fetch all drawers from CF paginated."""
    from .backends.cloudflare import CloudflareCollection
    cf = _cf_backend()
    assert isinstance(cf, CloudflareCollection)

    all_drawers = []
    offset = 0
    while True:
        with cf._client() as client:
            resp = client.get(f"{cf._url}/drawers", params={"limit": batch_size, "offset": offset})
            resp.raise_for_status()
            data = resp.json()

        batch = data.get("drawers", [])
        if not batch:
            break
        all_drawers.extend(batch)
        offset += len(batch)
        if len(batch) < batch_size:
            break
    return all_drawers


def push(palace_path: str, batch_size: int = DEFAULT_BATCH, dry_run: bool = False) -> dict:
    """Incremental push: only send drawers changed since last sync."""
    print(f"\n{'=' * 60}")
    print("  MemPalace Sync — Incremental Push (Local → Cloudflare)")
    print(f"{'=' * 60}\n")
    print(f"  Palace: {palace_path}")

    state = load_state()
    local_drawers = _fetch_all_local(palace_path)
    print(f"  Local drawers: {len(local_drawers)}")

    to_push = []
    skipped = 0

    for d in local_drawers:
        doc_id = d["id"]
        content = d["document"]
        h = content_hash(content)
        prev = state.get(doc_id)

        if prev and prev["hash"] == h:
            skipped += 1
            continue  # unchanged since last sync

        to_push.append(d)

    print(f"  Unchanged (skip): {skipped}")
    print(f"  To push: {len(to_push)}")

    if not to_push:
        print("\n  Nothing new to push.")
        print(f"{'=' * 60}\n")
        return {"pushed": 0, "skipped": skipped, "conflicts": 0}

    if dry_run:
        print("  DRY RUN — no changes made.")
        for d in to_push[:5]:
            print(f"    would push: {d['id']} ({d['metadata'].get('wing','?')}/{d['metadata'].get('room','?')})")
        if len(to_push) > 5:
            print(f"    ... and {len(to_push)-5} more")
        print(f"{'=' * 60}\n")
        return {"pushed": 0, "skipped": skipped, "conflicts": 0, "dry_run": True}

    # Check CF for conflicts before pushing
    # Only check IDs that already exist in state (previously synced)
    # New IDs (not in state) are safe to push without conflict check
    conflict_count = 0
    safe_to_push = []

    # Fetch CF versions for IDs we're about to push that were previously synced
    previously_synced_ids = [d["id"] for d in to_push if d["id"] in state]
    if previously_synced_ids:
        from .backends.cloudflare import CloudflareCollection
        cf = _cf_backend()
        assert isinstance(cf, CloudflareCollection)
        cf_versions: Dict[str, dict] = {}
        # Fetch in batches
        for i in range(0, len(previously_synced_ids), 50):
            batch_ids = previously_synced_ids[i:i+50]
            placeholders = ",".join(batch_ids)
            with cf._client() as client:
                # Use GET /drawers with source_file filter isn't ideal here;
                # we'll fetch each by querying D1 via GET /drawers and filtering client-side
                # For now fetch all and filter — acceptable for reasonable palace sizes
                pass
            # Actually fetch all remote and build a map for conflict check
        # Simpler: fetch all remote IDs we care about
        all_remote = {d["id"]: d for d in _fetch_all_remote(batch_size)}
        cf_versions = {k: v for k, v in all_remote.items() if k in previously_synced_ids}

        for d in to_push:
            doc_id = d["id"]
            if doc_id not in state:
                # New ID — safe to push
                safe_to_push.append(d)
                continue

            remote = cf_versions.get(doc_id)
            if remote is None:
                # Not on CF yet — safe to push
                safe_to_push.append(d)
                continue

            # Both local and CF have changed since last sync
            remote_hash = content_hash(remote["document"])
            local_hash = content_hash(d["document"])
            last_synced_hash = state[doc_id]["hash"]

            if remote_hash == last_synced_hash:
                # CF unchanged — local wins, safe to push
                safe_to_push.append(d)
            elif local_hash == last_synced_hash:
                # Local unchanged since sync but we got here? Shouldn't happen (filtered above)
                safe_to_push.append(d)
            else:
                # Both changed — CF wins (shared source of truth), log conflict
                conflict_count += 1
                log_conflict(
                    drawer_id=doc_id,
                    reason="both_changed_since_last_sync",
                    local={"content": d["document"][:200], "wing": d["metadata"].get("wing"), "room": d["metadata"].get("room")},
                    remote={"content": remote["document"][:200], "wing": remote["wing"], "room": remote["room"]},
                )
                print(f"  CONFLICT: {doc_id} — both sides changed. CF wins. Local version logged to sync_conflicts.jsonl")
    else:
        safe_to_push = to_push

    # Push safe drawers in batches — checkpoint state every 500 drawers
    cf = _cf_backend()
    pushed = 0
    CHECKPOINT_EVERY = 500
    for i in range(0, len(safe_to_push), batch_size):
        batch = safe_to_push[i:i + batch_size]
        cf.upsert(
            documents=[d["document"] for d in batch],
            ids=[d["id"] for d in batch],
            metadatas=[d["metadata"] for d in batch],
        )
        for d in batch:
            mark_synced(state, d["id"], d["document"], "push")
        pushed += len(batch)
        print(f"  Pushed {pushed}/{len(safe_to_push)}...")
        # Save state periodically so a crash mid-run resumes from here
        if pushed % CHECKPOINT_EVERY < batch_size:
            save_state(state)

    save_state(state)

    print(f"\n  Done.")
    print(f"  Pushed:    {pushed}")
    print(f"  Skipped:   {skipped}")
    print(f"  Conflicts: {conflict_count} (check ~/.mempalace/sync_conflicts.jsonl)")
    print(f"{'=' * 60}\n")
    return {"pushed": pushed, "skipped": skipped, "conflicts": conflict_count}


def pull(palace_path: str, batch_size: int = DEFAULT_BATCH, dry_run: bool = False) -> dict:
    """Incremental pull: only fetch drawers new/changed on CF since last sync."""
    print(f"\n{'=' * 60}")
    print("  MemPalace Sync — Incremental Pull (Cloudflare → Local)")
    print(f"{'=' * 60}\n")
    print(f"  Palace: {palace_path}")

    state = load_state()
    remote_drawers = _fetch_all_remote(batch_size)
    print(f"  Remote drawers: {len(remote_drawers)}")

    to_pull = []
    skipped = 0

    for d in remote_drawers:
        doc_id = d["id"]
        content = d["document"]
        h = content_hash(content)
        prev = state.get(doc_id)

        if prev and prev["hash"] == h:
            skipped += 1
            continue  # unchanged since last sync

        to_pull.append(d)

    print(f"  Unchanged (skip): {skipped}")
    print(f"  To pull: {len(to_pull)}")

    if not to_pull:
        print("\n  Nothing new to pull.")
        print(f"{'=' * 60}\n")
        return {"pulled": 0, "skipped": skipped, "conflicts": 0}

    if dry_run:
        print("  DRY RUN — no changes made.")
        for d in to_pull[:5]:
            print(f"    would pull: {d['id']} ({d.get('wing','?')}/{d.get('room','?')})")
        if len(to_pull) > 5:
            print(f"    ... and {len(to_pull)-5} more")
        print(f"{'=' * 60}\n")
        return {"pulled": 0, "skipped": skipped, "conflicts": 0, "dry_run": True}

    local = _local_backend(palace_path, create=True)
    pulled = 0
    conflict_count = 0

    for i in range(0, len(to_pull), batch_size):
        batch = to_pull[i:i + batch_size]
        for d in batch:
            doc_id = d["id"]
            remote_hash = content_hash(d["document"])
            prev = state.get(doc_id)

            if prev:
                # Check if local also changed
                try:
                    local_data = local.get(where=None, limit=1, ids=[doc_id])  # type: ignore
                    # ChromaDB get by ID
                except Exception:
                    local_data = {"ids": []}

                local_ids = local_data.get("ids", [])
                if local_ids:
                    local_content = local_data["documents"][0]
                    local_hash = content_hash(local_content)
                    if local_hash != prev["hash"] and remote_hash != prev["hash"]:
                        # Both changed — CF wins, log local for review
                        conflict_count += 1
                        log_conflict(
                            drawer_id=doc_id,
                            reason="both_changed_since_last_sync",
                            local={"content": local_content[:200]},
                            remote={"content": d["document"][:200], "wing": d.get("wing"), "room": d.get("room")},
                        )
                        print(f"  CONFLICT: {doc_id} — both sides changed. CF wins. Logged.")

        # Upsert batch into local
        local.upsert(
            documents=[d["document"] for d in batch],
            ids=[d["id"] for d in batch],
            metadatas=[{"wing": d.get("wing", "default"), "room": d.get("room", "general"), "source_file": d.get("source_file", "")} for d in batch],
        )
        for d in batch:
            mark_synced(state, d["id"], d["document"], "pull")
        pulled += len(batch)
        print(f"  Pulled {pulled}/{len(to_pull)}...")

    save_state(state)

    print(f"\n  Done.")
    print(f"  Pulled:    {pulled}")
    print(f"  Skipped:   {skipped}")
    print(f"  Conflicts: {conflict_count} (check ~/.mempalace/sync_conflicts.jsonl)")
    print(f"{'=' * 60}\n")
    return {"pulled": pulled, "skipped": skipped, "conflicts": conflict_count}


def daemon(palace_path: str, interval_minutes: int = DEFAULT_INTERVAL, dry_run: bool = False) -> None:
    """Run incremental push+pull in a loop every N minutes."""
    import signal

    print(f"\n  MemPalace Sync Daemon starting")
    print(f"  Palace:   {palace_path}")
    print(f"  Interval: {interval_minutes} min")
    print(f"  Press Ctrl+C to stop\n")

    running = True

    def _stop(sig, frame):
        nonlocal running
        print("\n  Stopping sync daemon...")
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"  [{ts}] Running sync...")
            push_result = push(palace_path, dry_run=dry_run)
            pull_result = pull(palace_path, dry_run=dry_run)
            total_changes = push_result.get("pushed", 0) + pull_result.get("pulled", 0)
            conflicts = push_result.get("conflicts", 0) + pull_result.get("conflicts", 0)
            print(f"  [{ts}] Sync complete — {total_changes} changes, {conflicts} conflicts")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  Sync error: {e}")

        if not running:
            break

        # Sleep in small increments so Ctrl+C is responsive
        sleep_secs = interval_minutes * 60
        for _ in range(sleep_secs):
            if not running:
                break
            time.sleep(1)

    print("  Daemon stopped.")


def run_sync_command(args: list) -> None:
    """CLI entry point: mempalace sync push|pull|daemon [options]"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="mempalace sync",
        description="Incremental bidirectional sync between local ChromaDB and Cloudflare",
    )
    parser.add_argument("direction", choices=["push", "pull", "daemon"], help="Direction or daemon mode")
    parser.add_argument("--palace", default=None, help="Path to local palace")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="Batch size (default: 100)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Daemon interval in minutes (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, make no changes")

    parsed = parser.parse_args(args)
    palace_path = parsed.palace or os.environ.get(
        "MEMPALACE_PALACE_PATH",
        os.path.expanduser("~/.mempalace/palace"),
    )

    if parsed.direction == "push":
        push(palace_path, batch_size=parsed.batch, dry_run=parsed.dry_run)
    elif parsed.direction == "pull":
        pull(palace_path, batch_size=parsed.batch, dry_run=parsed.dry_run)
    else:
        daemon(palace_path, interval_minutes=parsed.interval, dry_run=parsed.dry_run)
