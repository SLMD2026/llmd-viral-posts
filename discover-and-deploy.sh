#!/bin/bash
# Monthly discovery: find new viral accounts → update accounts.json → run refresh → deploy
set -e

echo "=== LLMD Viral Post Intelligence — Monthly Account Discovery ==="
echo "Started: $(date)"

# 1. Discover new accounts
python -m app.discover

# 2. Run a full refresh to include any new accounts
python -m app.refresh

# 3. Commit and push
git add static/index.html data/accounts.json
git diff --staged --quiet && echo "No changes to commit." && exit 0

git commit -m "chore: monthly discovery + refresh $(date +'%Y-%m-%d')"
git push origin main

echo ""
echo "Done. New accounts added and dashboard updated."
