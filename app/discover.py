"""
Monthly account discovery — searches hashtags for viral peptide/longevity content,
identifies new high-performing accounts, evaluates with Claude, updates accounts.json.

Usage:
  python -m app.discover
  python -m app.discover --dry-run   # show candidates without saving
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

APIFY_BASE = "https://api.apify.com/v2"
TT_ACTOR = "clockworks/tiktok-scraper"
IG_ACTOR = "apify/instagram-post-scraper"

# Hashtags to mine for viral content
TT_HASHTAGS = ["peptides", "biohacking", "longevity", "bpc157", "peptideprotocol", "peptidetherapy", "antiaging"]
IG_HASHTAGS = ["peptides", "biohacking", "longevity", "peptidetherapy", "functionalmedicine", "healthoptimization"]

# Thresholds for a candidate account to be worth evaluating
MIN_FOLLOWERS = 10_000
MIN_AVG_VIEWS = 20_000
MIN_AVG_ENGAGEMENT = 500  # likes + comments


def _apify_request(method: str, path: str, body=None, timeout=30) -> dict:
    sep = "&" if "?" in path else "?"
    url = f"{APIFY_BASE}{path}{sep}token={APIFY_TOKEN}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "User-Agent": "llmd-discover/1.0"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def apify_run_actor(actor_id: str, input_data: dict, timeout: int = 480) -> list:
    actor_slug = actor_id.replace("/", "~")
    print(f"  Running {actor_id}...")
    run = _apify_request("POST", f"/acts/{actor_slug}/runs", input_data)
    run_id = run["data"]["id"]
    dataset_id = run["data"]["defaultDatasetId"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(12)
        status = _apify_request("GET", f"/actor-runs/{run_id}")["data"]["status"]
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Run {run_id} ended: {status}")

    items_data = _apify_request("GET", f"/datasets/{dataset_id}/items?limit=2000", timeout=60)
    return items_data if isinstance(items_data, list) else items_data.get("items", [])


def load_accounts() -> dict:
    return json.loads((DATA_DIR / "accounts.json").read_text())


def get_existing_handles(accounts_data: dict) -> set[str]:
    handles = set()
    for a in accounts_data["accounts"]:
        if a.get("instagram"):
            handles.add(a["instagram"].lower())
        if a.get("tiktok"):
            handles.add(a["tiktok"].lower())
    return handles


# ── TikTok hashtag discovery ──

def discover_tiktok(existing_handles: set[str]) -> list[dict]:
    print(f"Searching TikTok hashtags: {TT_HASHTAGS}")
    items = apify_run_actor(TT_ACTOR, {
        "hashtags": TT_HASHTAGS,
        "maxItems": 150,
    })

    # Group posts by account
    accounts: dict[str, dict] = {}
    for item in items:
        author = item.get("authorMeta") or item.get("author") or {}
        handle = (author.get("nickName") or author.get("uniqueId") or "").lower().lstrip("@").strip()
        if not handle or handle in existing_handles:
            continue

        followers = int(author.get("fans") or author.get("followerCount") or 0)
        if followers < MIN_FOLLOWERS:
            continue

        views = int(item.get("playCount") or 0)
        likes = int(item.get("diggCount") or 0)
        comments = int(item.get("commentCount") or 0)
        caption = (item.get("text") or "")[:300]

        if handle not in accounts:
            accounts[handle] = {
                "handle": handle,
                "name": author.get("nickname") or author.get("name") or handle,
                "platform": "tiktok",
                "followers": followers,
                "posts": [],
                "captions": [],
            }

        accounts[handle]["posts"].append({
            "views": views,
            "likes": likes,
            "comments": comments,
        })
        if caption:
            accounts[handle]["captions"].append(caption)

    # Compute averages and filter
    candidates = []
    for handle, data in accounts.items():
        posts = data["posts"]
        if not posts:
            continue
        avg_views = sum(p["views"] for p in posts) / len(posts)
        avg_eng = sum(p["likes"] + p["comments"] for p in posts) / len(posts)
        if avg_views >= MIN_AVG_VIEWS or avg_eng >= MIN_AVG_ENGAGEMENT:
            candidates.append({
                **data,
                "avg_views": round(avg_views),
                "avg_engagement": round(avg_eng),
                "post_count": len(posts),
            })

    candidates.sort(key=lambda x: -x["avg_views"])
    print(f"  Found {len(candidates)} TikTok candidates with sufficient reach")
    return candidates


# ── Instagram hashtag discovery ──

def discover_instagram(existing_handles: set[str]) -> list[dict]:
    print(f"Searching Instagram hashtags: {IG_HASHTAGS}")
    try:
        items = apify_run_actor(IG_ACTOR, {
            "directUrls": [f"https://www.instagram.com/explore/tags/{tag}/" for tag in IG_HASHTAGS],
            "resultsLimit": 20,
        })
    except Exception as e:
        print(f"  Instagram discovery failed: {e}")
        return []

    accounts: dict[str, dict] = {}
    for item in items:
        handle = (item.get("ownerUsername") or "").lower().strip()
        if not handle or handle in existing_handles:
            continue

        views = int(item.get("videoViewCount") or 0)
        likes = int(item.get("likesCount") or 0)
        comments = int(item.get("commentsCount") or 0)
        caption = (item.get("caption") or "")[:300]

        if handle not in accounts:
            accounts[handle] = {
                "handle": handle,
                "name": handle,
                "platform": "instagram",
                "followers": 0,  # not available from hashtag search
                "posts": [],
                "captions": [],
            }

        accounts[handle]["posts"].append({
            "views": views,
            "likes": likes,
            "comments": comments,
        })
        if caption:
            accounts[handle]["captions"].append(caption)

    candidates = []
    for handle, data in accounts.items():
        posts = data["posts"]
        if not posts:
            continue
        avg_views = sum(p["views"] for p in posts) / len(posts)
        avg_eng = sum(p["likes"] + p["comments"] for p in posts) / len(posts)
        if avg_views >= MIN_AVG_VIEWS or avg_eng >= MIN_AVG_ENGAGEMENT:
            candidates.append({
                **data,
                "avg_views": round(avg_views),
                "avg_engagement": round(avg_eng),
                "post_count": len(posts),
            })

    candidates.sort(key=lambda x: -x["avg_engagement"])
    print(f"  Found {len(candidates)} Instagram candidates")
    return candidates


# ── Claude evaluation ──

EVAL_PROMPT = """You are evaluating social media accounts for LLMD's viral post intelligence dashboard.

LLMD focuses on: physician-guided peptide therapy (BPC-157, CJC/Ipamorelin, NAD+, Tirzepatide, Semaglutide), longevity, biohacking, functional medicine, spiritual health integration.

The dashboard helps LLMD's marketing team find viral content to adapt. Good accounts to track:
- Physicians, researchers, or credible educators in peptides/longevity/functional medicine
- Biohackers or influencers who regularly post viral health optimization content
- Competitor brands in the peptide/telehealth space
- NOT: general fitness, basic nutrition, mainstream pharma, unrelated wellness

CANDIDATE ACCOUNT:
Handle: @{handle}
Platform: {platform}
Followers: {followers:,}
Avg Views per Post: {avg_views:,}
Sample Captions:
{captions}

Is this account worth tracking for LLMD's viral content dashboard?

Return ONLY valid JSON:
{{"add": true or false, "reason": "one concise sentence", "tier": 1, 2, or 3, "category": "influencer|physician|researcher|educator|competitor"}}

Tier guide: 1=must-have (major influencer/direct competitor), 2=strong signal, 3=useful reference"""


def _claude_evaluate(candidate: dict) -> dict:
    captions_text = "\n".join(f"- {c}" for c in candidate.get("captions", [])[:5]) or "(no captions)"
    prompt = EVAL_PROMPT.format(
        handle=candidate["handle"],
        platform=candidate["platform"],
        followers=candidate.get("followers", 0),
        avg_views=candidate.get("avg_views", 0),
        captions=captions_text,
    )
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode()
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        result = json.loads(r.read())
    raw = result["content"][0]["text"].strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"add": False, "reason": "parse error", "tier": 3, "category": "influencer"}


def evaluate_candidates(candidates: list[dict]) -> list[dict]:
    if not ANTHROPIC_KEY:
        print("  Skipping Claude evaluation (no ANTHROPIC_API_KEY) — adding all by reach")
        return [dict(**c, claude_decision={"add": True, "reason": "auto-added (no API key)", "tier": 2, "category": "influencer"}) for c in candidates]

    print(f"  Evaluating {len(candidates)} candidates with Claude Haiku...")
    approved = []
    for c in candidates:
        try:
            decision = _claude_evaluate(c)
            print(f"    @{c['handle']}: add={decision.get('add')} | {decision.get('reason', '')[:60]}")
            if decision.get("add"):
                approved.append({**c, "claude_decision": decision})
            time.sleep(0.5)
        except Exception as e:
            print(f"    @{c['handle']}: evaluation error — {e}")
    return approved


# ── accounts.json update ──

def update_accounts(new_accounts: list[dict], accounts_data: dict, dry_run: bool):
    today = datetime.now().strftime("%Y-%m-%d")
    added = []

    for entry in new_accounts:
        decision = entry.get("claude_decision", {})
        tier = decision.get("tier", 2)
        category = decision.get("category", "influencer")
        platform = entry["platform"]
        handle = entry["handle"]
        name = entry.get("name", handle)

        new_acc = {
            "name": name,
            "instagram": handle if platform == "instagram" else None,
            "tiktok": handle if platform == "tiktok" else None,
            "followers_ig": entry.get("followers", 0) if platform == "instagram" else 0,
            "followers_tt": entry.get("followers", 0) if platform == "tiktok" else 0,
            "tier": tier,
            "category": category,
            "added": today,
            "added_by": "discover.py",
            "discovery_reason": decision.get("reason", ""),
        }
        added.append(new_acc)
        print(f"  + @{handle} ({platform}) | tier {tier} | {decision.get('reason','')[:60]}")

    if dry_run:
        print(f"\nDry run — would add {len(added)} accounts. Not saving.")
        return

    accounts_data["accounts"].extend(added)
    accounts_data["last_updated"] = today
    (DATA_DIR / "accounts.json").write_text(json.dumps(accounts_data, indent=2))
    print(f"\nSaved {len(added)} new accounts to accounts.json")


# ── Main ──

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Starting monthly account discovery...")
    accounts_data = load_accounts()
    existing = get_existing_handles(accounts_data)
    print(f"Currently tracking {len(accounts_data['accounts'])} accounts ({len(existing)} unique handles)")

    tt_candidates = discover_tiktok(existing)
    ig_candidates = discover_instagram(existing)
    all_candidates = tt_candidates + ig_candidates

    # Deduplicate candidates by handle
    seen = set()
    unique_candidates = []
    for c in all_candidates:
        if c["handle"] not in seen:
            seen.add(c["handle"])
            unique_candidates.append(c)

    print(f"\nTotal unique candidates to evaluate: {len(unique_candidates)}")
    if not unique_candidates:
        print("No new candidates found. Done.")
        return

    approved = evaluate_candidates(unique_candidates[:20])  # cap at 20 to control cost
    print(f"\nApproved for addition: {len(approved)}")

    if not approved:
        print("No accounts approved. Done.")
        return

    update_accounts(approved, accounts_data, args.dry_run)

    if not args.dry_run:
        print("\nNext step: run refresh.py to include new accounts in the dashboard.")
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Discovery complete.")


if __name__ == "__main__":
    main()
