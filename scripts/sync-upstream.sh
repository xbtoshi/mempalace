#!/usr/bin/env bash
# Sync this fork (xbtoshi/mempalace) with upstream (MemPalace/mempalace).
#
# Strategy:
#   - develop is kept as a pure fast-forward mirror of upstream/develop (no local commits).
#   - feature/cloudflare-d1-backend carries our Cloudflare D1/Vectorize backend
#     and is rebased on top of upstream/develop periodically (manual step — may conflict
#     since upstream changed backends/__init__.py, mcp_server.py, miner.py, palace.py).
#
# Run this from any working tree. Pass --rebase-feature to also attempt the feature-branch
# rebase after syncing develop.
#
# Usage:
#   ./scripts/sync-upstream.sh
#   ./scripts/sync-upstream.sh --rebase-feature

set -euo pipefail

UPSTREAM_URL="https://github.com/MemPalace/mempalace.git"
FEATURE_BRANCH="feature/cloudflare-d1-backend"

if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is dirty. Commit or stash first." >&2
  exit 1
fi

# Ensure upstream remote exists
if ! git remote | grep -qx upstream; then
  echo "Adding upstream remote: $UPSTREAM_URL"
  git remote add upstream "$UPSTREAM_URL"
fi

echo "Fetching upstream..."
git fetch upstream --tags --prune

current_branch=$(git rev-parse --abbrev-ref HEAD)

echo "Syncing develop..."
git checkout develop
if git merge-base --is-ancestor HEAD upstream/develop; then
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse upstream/develop)" ]; then
    echo "  develop already in sync"
  else
    git merge --ff-only upstream/develop
    git push origin develop
    git push origin --tags
    echo "  develop fast-forwarded and pushed"
  fi
else
  echo "  error: develop has diverged from upstream/develop — investigate manually" >&2
  git checkout "$current_branch"
  exit 2
fi

# Report feature-branch drift
behind=$(git log --oneline "$FEATURE_BRANCH..upstream/develop" | wc -l | tr -d ' ')
ahead=$(git log --oneline "upstream/develop..$FEATURE_BRANCH" | wc -l | tr -d ' ')
echo "$FEATURE_BRANCH: $ahead ahead, $behind behind upstream/develop"

if [ "${1:-}" = "--rebase-feature" ] && [ "$behind" -gt 0 ]; then
  echo "Rebasing $FEATURE_BRANCH onto upstream/develop..."
  git checkout "$FEATURE_BRANCH"
  if git rebase upstream/develop; then
    echo "  rebase clean. Review before pushing with: git push --force-with-lease origin $FEATURE_BRANCH"
  else
    echo "  rebase hit conflicts. Resolve manually then: git rebase --continue"
    exit 3
  fi
fi

git checkout "$current_branch"
echo "done."
