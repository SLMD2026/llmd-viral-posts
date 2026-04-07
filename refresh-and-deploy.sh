#!/bin/bash
# Weekly refresh: fetch posts → classify → rebuild HTML → commit → push → Railway auto-deploys
set -e

echo "=== LLMD Viral Post Intelligence — Weekly Refresh ==="
echo "Started: $(date)"

# 1. Run the refresh
python -m app.refresh

# 2. Commit updated HTML and accounts
git add static/index.html data/accounts.json
git diff --staged --quiet && echo "No changes to commit." && exit 0

git commit -m "chore: weekly viral post refresh $(date +'%Y-%m-%d')"

# 3. Push — Railway auto-deploys on push
git push origin main

echo ""
echo "Done. Railway will auto-deploy in ~1 minute."
echo "Dashboard: https://\$RAILWAY_PUBLIC_DOMAIN"
