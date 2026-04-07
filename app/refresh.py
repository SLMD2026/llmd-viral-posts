"""
Weekly Apify refresh — scrapes Instagram + TikTok posts from tracked accounts,
classifies with Claude, scores virality, rebuilds static/index.html.

Usage:
  python -m app.refresh
  python -m app.refresh --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
PROMPTS_DIR = BASE_DIR / "prompts"
DATA_DIR.mkdir(exist_ok=True)

APIFY_BASE = "https://api.apify.com/v2"
IG_ACTOR = "apify/instagram-post-scraper"
TT_ACTOR = "clockworks/tiktok-scraper"
IG_HASHTAG_ACTOR = "apify/instagram-hashtag-scraper"


# ── Apify helpers ──

def _apify_request(method: str, path: str, body=None, timeout=30) -> dict:
    sep = "&" if "?" in path else "?"
    url = f"{APIFY_BASE}{path}{sep}token={APIFY_TOKEN}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "User-Agent": "llmd-viral-refresh/1.0"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def apify_run_actor(actor_id: str, input_data: dict, timeout: int = 600) -> list:
    """Start an Apify actor run and return dataset items when complete."""
    actor_slug = actor_id.replace("/", "~")
    print(f"    Starting actor {actor_id}...")
    run = _apify_request("POST", f"/acts/{actor_slug}/runs", input_data)
    run_id = run["data"]["id"]
    dataset_id = run["data"]["defaultDatasetId"]

    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        time.sleep(12)
        status_data = _apify_request("GET", f"/actor-runs/{run_id}")
        status = status_data["data"]["status"]
        if status != last_status:
            print(f"    Run {run_id[:8]}... status: {status}")
            last_status = status
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Actor {actor_id} run {run_id} ended with status: {status}")
    else:
        raise TimeoutError(f"Actor {actor_id} did not complete within {timeout}s")

    items_data = _apify_request("GET", f"/datasets/{dataset_id}/items?limit=2000", timeout=60)
    items = items_data if isinstance(items_data, list) else items_data.get("items", [])
    print(f"    Got {len(items)} items")
    return items


# ── Account loading ──

def load_accounts() -> list[dict]:
    path = DATA_DIR / "accounts.json"
    return json.loads(path.read_text())["accounts"]


def get_follower_count(account: dict, platform: str) -> int:
    key = f"followers_{platform[:2]}"
    return account.get(key, 100000) or 100000


# ── Instagram scraping ──

def fetch_instagram_posts(accounts: list[dict]) -> list[dict]:
    usernames = [a["instagram"] for a in accounts if a.get("instagram")]
    if not usernames:
        return []
    print(f"  Fetching Instagram: {len(usernames)} accounts...")

    follower_map = {
        a["instagram"]: get_follower_count(a, "instagram")
        for a in accounts if a.get("instagram")
    }
    account_map = {a["instagram"]: a for a in accounts if a.get("instagram")}

    items = apify_run_actor(IG_ACTOR, {
        "usernames": usernames,
        "resultsLimit": 12,
    })

    posts = []
    for item in items:
        if not item:
            continue
        handle = (item.get("ownerUsername") or item.get("username") or "").lower().strip()
        if not handle:
            continue
        thumbnail = (
            item.get("displayUrl") or
            item.get("thumbnailUrl") or
            item.get("thumbnail_url") or
            ""
        )
        if not thumbnail:
            continue

        caption = (item.get("caption") or item.get("description") or "").strip()
        post_id = f"ig_{item.get('id') or item.get('shortCode') or item.get('pk') or ''}"
        if not post_id or post_id == "ig_":
            continue

        acc = account_map.get(handle, {})
        posts.append({
            "post_id": post_id,
            "platform": "instagram",
            "account_name": acc.get("name", handle),
            "account_handle": handle,
            "account_tier": acc.get("tier", 2),
            "account_category": acc.get("category", "influencer"),
            "follower_count": follower_map.get(handle, 100000),
            "post_url": item.get("url") or f"https://www.instagram.com/p/{item.get('shortCode', '')}",
            "thumbnail_url": thumbnail,
            "caption": caption[:600],
            "posted_at": item.get("timestamp") or item.get("taken_at_timestamp") or "",
            "views": int(item.get("videoViewCount") or item.get("video_view_count") or 0),
            "likes": int(item.get("likesCount") or item.get("edge_media_preview_like", {}).get("count") or 0),
            "comments": int(item.get("commentsCount") or item.get("edge_media_to_comment", {}).get("count") or 0),
            "shares": 0,
        })
    return posts


# ── TikTok scraping ──

def fetch_tiktok_posts(accounts: list[dict]) -> list[dict]:
    tt_accounts = [a for a in accounts if a.get("tiktok")]
    if not tt_accounts:
        return []
    print(f"  Fetching TikTok: {len(tt_accounts)} accounts...")

    profile_urls = [
        {"url": f"https://www.tiktok.com/@{a['tiktok']}"}
        for a in tt_accounts
    ]
    handle_to_account = {a["tiktok"].lower(): a for a in tt_accounts}

    items = apify_run_actor(TT_ACTOR, {
        "startUrls": profile_urls,
        "maxItems": len(tt_accounts) * 12,
    })

    posts = []
    for item in items:
        if not item:
            continue
        author = item.get("authorMeta") or item.get("author") or {}
        handle = (
            author.get("nickName") or
            author.get("uniqueId") or
            author.get("name") or ""
        ).lower().lstrip("@").strip()
        if not handle:
            continue

        video_meta = item.get("videoMeta") or {}
        covers = item.get("covers") or []
        thumbnail = (
            video_meta.get("coverUrl") or
            video_meta.get("cover") or
            (covers[0] if covers else "") or
            item.get("cover") or
            ""
        )
        if not thumbnail:
            continue

        post_id = f"tt_{item.get('id') or ''}"
        if post_id == "tt_":
            continue

        caption = (item.get("text") or item.get("desc") or "").strip()
        acc = handle_to_account.get(handle, {})
        follower_count = (
            author.get("fans") or
            author.get("followerCount") or
            get_follower_count(acc, "tiktok")
        )

        created_at = (
            item.get("createTimeISO") or
            item.get("createTime") or
            ""
        )
        # Convert Unix timestamp if needed
        if isinstance(created_at, (int, float)) and created_at > 1000000000:
            created_at = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()

        posts.append({
            "post_id": post_id,
            "platform": "tiktok",
            "account_name": acc.get("name", author.get("nickname") or handle),
            "account_handle": handle,
            "account_tier": acc.get("tier", 2),
            "account_category": acc.get("category", "influencer"),
            "follower_count": int(follower_count or 100000),
            "post_url": (
                item.get("webVideoUrl") or
                item.get("video_url") or
                f"https://www.tiktok.com/@{handle}/video/{item.get('id','')}"
            ),
            "thumbnail_url": thumbnail,
            "caption": caption[:600],
            "posted_at": str(created_at),
            "views": int(item.get("playCount") or item.get("play_count") or 0),
            "likes": int(item.get("diggCount") or item.get("like_count") or 0),
            "comments": int(item.get("commentCount") or item.get("comment_count") or 0),
            "shares": int(item.get("shareCount") or item.get("share_count") or 0),
        })
    return posts


# ── Virality scoring ──

def score_virality(post: dict) -> dict:
    followers = max(post.get("follower_count", 1), 1)
    shares = post.get("shares", 0)
    comments = post.get("comments", 0)
    likes = post.get("likes", 0)
    views = post.get("views", 0)

    # Weighted engagement score (higher weight for shares — stronger viral signal)
    engagement = shares * 3 + comments * 2 + likes
    eng_rate = engagement / followers * 100

    # View rate (views relative to follower count)
    view_rate = views / followers * 100

    # Combined virality score (0-100)
    raw = eng_rate * 0.55 + view_rate * 0.015
    virality_score = round(min(100.0, max(0.0, raw)), 1)

    if virality_score >= 15:
        virality_tier = "high"
    elif virality_score >= 3:
        virality_tier = "medium"
    else:
        virality_tier = "low"

    return {**post, "virality_score": virality_score, "virality_tier": virality_tier}


# ── Claude classification ──

def _claude_request(prompt: str, max_tokens: int = 400) -> str:
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode()
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())
    return result["content"][0]["text"]


def _extract_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


CLASSIFY_TEMPLATE = (PROMPTS_DIR / "classify.md").read_text() if (PROMPTS_DIR / "classify.md").exists() else ""

HOOK_TYPES = {"curiosity_gap", "fear_based", "social_proof", "transformation", "educational", "controversy", "personal_story"}
TOPICS = {"peptide_education", "weight_loss", "anti_aging", "biohacking", "functional_medicine", "recovery", "hormones", "spiritual_health", "longevity", "general_wellness"}
FORMATS = {"talking_head", "text_overlay", "b_roll", "before_after", "testimonial", "lab_science", "lifestyle", "podcast_clip"}


def classify_posts(posts: list[dict]) -> list[dict]:
    if not ANTHROPIC_KEY:
        print("  Skipping classification (no ANTHROPIC_API_KEY)")
        for post in posts:
            post.update({"hook_type": "educational", "topic": "general_wellness", "format_guess": "talking_head", "hook_text": ""})
        return posts

    print(f"  Classifying {len(posts)} posts with Claude Haiku...")
    template = CLASSIFY_TEMPLATE

    BATCH = 8
    classified = []
    for i in range(0, len(posts), BATCH):
        batch = posts[i:i + BATCH]
        batch_input = [
            {"post_id": p["post_id"], "caption": p["caption"][:400], "platform": p["platform"]}
            for p in batch
        ]
        prompt = template.replace(
            "{{POST_JSON}}",
            json.dumps(batch_input, indent=2)
        )
        prompt += "\n\nReturn a JSON ARRAY (not object) where each element corresponds to one post in order, with fields: post_id, hook_type, topic, format_guess, hook_text."

        try:
            raw = _claude_request(prompt, max_tokens=600)
            # Try to parse array
            arr_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if arr_match:
                results = json.loads(arr_match.group())
                result_map = {r["post_id"]: r for r in results if isinstance(r, dict)}
                for post in batch:
                    r = result_map.get(post["post_id"], {})
                    post["hook_type"] = r.get("hook_type", "educational") if r.get("hook_type") in HOOK_TYPES else "educational"
                    post["topic"] = r.get("topic", "general_wellness") if r.get("topic") in TOPICS else "general_wellness"
                    post["format_guess"] = r.get("format_guess", "talking_head") if r.get("format_guess") in FORMATS else "talking_head"
                    post["hook_text"] = (r.get("hook_text") or post["caption"][:60]).strip()
            else:
                raise ValueError("No JSON array in response")
        except Exception as e:
            print(f"  Classification batch {i // BATCH + 1} failed: {e} — using defaults")
            for post in batch:
                post.update({"hook_type": "educational", "topic": "general_wellness", "format_guess": "talking_head", "hook_text": post["caption"][:60]})
        classified.extend(batch)
        if i + BATCH < len(posts):
            time.sleep(1)

    return classified


# ── Deduplication ──

def deduplicate(posts: list[dict]) -> list[dict]:
    seen_ids = set()
    out = []
    for post in posts:
        if post["post_id"] not in seen_ids:
            seen_ids.add(post["post_id"])
            out.append(post)
    return out


# ── HTML generation ──

VIRALITY_BADGE = {
    "high":   ("background:#dcfce7;color:#16a34a", "🔥 High Virality"),
    "medium": ("background:#fef9c3;color:#ca8a04",  "📈 Medium"),
    "low":    ("background:#f3f4f6;color:#6b7280",  "Low"),
}

PLATFORM_ICON = {
    "instagram": "📷",
    "tiktok": "🎵",
}

TOPIC_LABELS = {
    "peptide_education": "Peptide Education",
    "weight_loss": "Weight Loss",
    "anti_aging": "Anti-Aging",
    "biohacking": "Biohacking",
    "functional_medicine": "Functional Medicine",
    "recovery": "Recovery",
    "hormones": "Hormones",
    "spiritual_health": "Spiritual Health",
    "longevity": "Longevity",
    "general_wellness": "Wellness",
}

HOOK_LABELS = {
    "curiosity_gap": "Curiosity Gap",
    "fear_based": "Fear-Based",
    "social_proof": "Social Proof",
    "transformation": "Transformation",
    "educational": "Educational",
    "controversy": "Controversy",
    "personal_story": "Personal Story",
}

TIER_META = {
    "high": (
        "🔥 Top Viral Posts",
        "Highest engagement rate relative to account size. Replicate these first.",
        "#0f1a12", "#d4a843"
    ),
    "medium": (
        "📈 Strong Performers",
        "Solid engagement worth studying for hooks, formats, and topic angles.",
        "#1a2420", "#7a9e8a"
    ),
    "low": (
        "📚 Reference Library",
        "Lower virality but useful for topic research and format reference.",
        "#111827", "#6b7280"
    ),
}


def _fmt_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _time_ago(posted_at: str) -> str:
    if not posted_at:
        return ""
    try:
        if "T" in posted_at:
            dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        else:
            return posted_at[:10]
        now = datetime.now(tz=timezone.utc)
        diff = now - dt
        days = diff.days
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        if days < 30:
            return f"{days // 7}w ago"
        return f"{days // 30}mo ago"
    except Exception:
        return posted_at[:10] if posted_at else ""


def _card_html(post: dict) -> str:
    badge_style, badge_label = VIRALITY_BADGE.get(post.get("virality_tier", "low"), VIRALITY_BADGE["low"])
    platform = post.get("platform", "instagram")
    platform_icon = PLATFORM_ICON.get(platform, "")
    topic = post.get("topic", "general_wellness")
    hook = post.get("hook_type", "educational")
    time_ago = _time_ago(post.get("posted_at", ""))
    score = post.get("virality_score", 0)

    views_str = _fmt_number(post.get("views", 0)) if post.get("views", 0) > 0 else ""
    likes_str = _fmt_number(post.get("likes", 0))
    comments_str = _fmt_number(post.get("comments", 0))
    shares_str = _fmt_number(post.get("shares", 0)) if post.get("shares", 0) > 0 else ""

    metrics_parts = []
    if views_str:
        metrics_parts.append(f"👁 {views_str}")
    metrics_parts.append(f"❤ {likes_str}")
    metrics_parts.append(f"💬 {comments_str}")
    if shares_str:
        metrics_parts.append(f"↗ {shares_str}")
    metrics_html = " &nbsp;·&nbsp; ".join(metrics_parts)

    caption_preview = post.get("caption", "")[:160]
    if len(post.get("caption", "")) > 160:
        caption_preview += "…"

    post_url = post.get("post_url", "#")
    post_id = post.get("post_id", "")
    account_name = post.get("account_name", post.get("account_handle", ""))
    handle = post.get("account_handle", "")

    # Build post data JSON for replicate button (escaped for JS)
    post_data_json = json.dumps({
        "post_id": post_id,
        "platform": platform,
        "account_name": account_name,
        "account_handle": handle,
        "caption": post.get("caption", ""),
        "views": post.get("views", 0),
        "likes": post.get("likes", 0),
        "comments": post.get("comments", 0),
        "shares": post.get("shares", 0),
        "virality_score": score,
        "hook_type": hook,
        "topic": topic,
        "format_guess": post.get("format_guess", ""),
        "hook_text": post.get("hook_text", ""),
    }).replace("'", "&#39;").replace('"', "&quot;")

    return f'''<div class="post-card"
  data-post-id="{post_id}"
  data-platform="{platform}"
  data-topic="{topic}"
  data-hook="{hook}"
  data-virality="{post.get('virality_tier','low')}"
  data-score="{score}">
  <div class="card-thumb" onclick="window.open('{post_url}','_blank')">
    <img src="/images/{post_id}" alt="{account_name}" loading="lazy"
      onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
    <div class="no-thumb" style="display:none">
      <span>{platform_icon}</span>
      <span style="font-size:11px;margin-top:4px">@{handle}</span>
    </div>
    <div class="platform-badge {platform}">{platform_icon} {platform.title()}</div>
  </div>
  <div class="card-body">
    <div class="card-top">
      <span class="account-tag">@{handle}</span>
      <span class="virality-tag" style="{badge_style}">{badge_label} · {score}</span>
    </div>
    <div class="card-meta-row">{time_ago} &nbsp;·&nbsp; {metrics_html}</div>
    {"<p class='caption-preview'>" + caption_preview + "</p>" if caption_preview else ""}
    <div class="tag-row">
      <span class="tag topic-tag">{TOPIC_LABELS.get(topic, topic)}</span>
      <span class="tag hook-tag">{HOOK_LABELS.get(hook, hook)}</span>
    </div>
    <div class="card-actions">
      <a href="{post_url}" target="_blank" class="watch-link">{platform_icon} Watch on {platform.title()} →</a>
      <button class="replicate-btn" onclick="openReplicate(this)" data-post="{post_data_json}">✨ Replicate for LLMD</button>
    </div>
    <div class="card-checks">
      <label class="card-check saved-check"><input type="checkbox" data-field="saved"> ⭐ Save</label>
    </div>
  </div>
</div>'''


def _tier_section(tier: str, posts: list[dict]) -> str:
    label, desc, bg, accent = TIER_META[tier]
    tier_id = {"high": 1, "medium": 2, "low": 3}[tier]
    collapsed = " collapsed" if tier == "low" else ""
    tog = "▼" if tier == "low" else "▲"
    cards = "".join(_card_html(p) for p in posts)
    count = len(posts)
    return f'''
<section class="tier{collapsed}" id="t{tier_id}">
  <div class="tier-hdr" style="background:{bg};border-left:5px solid {accent}" onclick="toggleTier({tier_id})">
    <div>
      <h2>{label}</h2>
      <p class="tier-desc">{desc}</p>
    </div>
    <div class="tier-right">
      <span class="tier-count">{count} posts</span>
      <span class="tog" id="tog{tier_id}">{tog}</span>
    </div>
  </div>
  <div class="cards" id="c{tier_id}">{cards}</div>
</section>'''


def _collect_filter_counts(posts: list[dict]) -> tuple[dict, dict]:
    topics: dict[str, int] = {}
    hooks: dict[str, int] = {}
    for p in posts:
        t = p.get("topic", "general_wellness")
        h = p.get("hook_type", "educational")
        topics[t] = topics.get(t, 0) + 1
        hooks[h] = hooks.get(h, 0) + 1
    return topics, hooks


def build_html(posts: list[dict]) -> str:
    total = len(posts)
    high_count = sum(1 for p in posts if p.get("virality_tier") == "high")
    ig_count = sum(1 for p in posts if p.get("platform") == "instagram")
    tt_count = sum(1 for p in posts if p.get("platform") == "tiktok")
    accounts_count = len(set(p["account_handle"] for p in posts))
    updated = datetime.now().strftime("%B %d, %Y")

    # Group by virality tier
    tiers: dict[str, list] = {"high": [], "medium": [], "low": []}
    for p in posts:
        tiers.get(p.get("virality_tier", "low"), tiers["low"]).append(p)

    # Sort each tier by score desc
    for tier_posts in tiers.values():
        tier_posts.sort(key=lambda p: -p.get("virality_score", 0))

    tier_html = "\n".join(
        _tier_section(tier, tiers[tier])
        for tier in ["high", "medium", "low"]
        if tiers[tier]
    )

    # Top topics and hooks for filters
    topics_counts, hooks_counts = _collect_filter_counts(posts)
    top_topics = sorted(topics_counts.items(), key=lambda x: -x[1])[:6]
    top_hooks = sorted(hooks_counts.items(), key=lambda x: -x[1])[:5]

    topic_btns = "\n".join(
        f'<button class="fbtn" data-filter-type="topic" data-filter-val="{t}" onclick="fil(this)">'
        f'{TOPIC_LABELS.get(t, t)} ({c})</button>'
        for t, c in top_topics
    )
    hook_btns = "\n".join(
        f'<button class="fbtn" data-filter-type="hook" data-filter-val="{h}" onclick="fil(this)">'
        f'{HOOK_LABELS.get(h, h)} ({c})</button>'
        for h, c in top_hooks
    )

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLMD Viral Post Intelligence</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Poppins',sans-serif;background:#eef0ec;color:#1a2420}}
.hdr{{background:linear-gradient(135deg,#0f1a12,#1a2e1f);padding:44px 60px 36px;color:#fff}}
.hdr h1{{font-size:28px;font-weight:700;color:#d4a843;margin-bottom:6px}}
.hdr .sub{{font-size:13px;color:rgba(255,255,255,.6);max-width:680px;line-height:1.7}}
.stats{{display:flex;gap:36px;margin-top:24px;flex-wrap:wrap}}
.snum{{font-size:28px;font-weight:700;color:#d4a843}}
.slbl{{font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:1.5px;margin-top:2px}}
.guide{{background:rgba(212,168,67,.1);border-left:3px solid #d4a843;padding:14px 20px;margin-top:20px;border-radius:0 8px 8px 0;max-width:760px}}
.guide h3{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#d4a843;margin-bottom:6px}}
.guide p{{font-size:13px;color:rgba(255,255,255,.65);line-height:1.7}}
.filters{{background:#fff;padding:10px 60px;border-bottom:1px solid #e5e7eb;position:sticky;top:0;z-index:100;display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.filter-group{{display:flex;gap:5px;align-items:center;flex-wrap:wrap}}
.filter-sep{{width:1px;height:22px;background:#e5e7eb;margin:0 4px}}
.flbl{{font-size:10px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:.8px;white-space:nowrap}}
.fbtn{{padding:5px 13px;border-radius:20px;border:1.5px solid #d1d5db;background:#fff;cursor:pointer;font-family:'Poppins',sans-serif;font-size:11px;font-weight:500;color:#374151;transition:all .15s;white-space:nowrap}}
.fbtn:hover{{background:#f3f4f6}}
.fbtn.on{{background:#0f1a12;color:#fff;border-color:#0f1a12}}
.fbtn.on-ig{{background:#e1306c;color:#fff;border-color:#e1306c}}
.fbtn.on-tt{{background:#010101;color:#fff;border-color:#010101}}
.tier{{}}
.tier.collapsed .cards{{display:none}}
.tier-hdr{{padding:18px 60px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none;gap:16px}}
.tier-hdr:hover{{opacity:.95}}
.tier-hdr h2{{font-size:15px;font-weight:600;color:#fff}}
.tier-desc{{font-size:12px;color:rgba(255,255,255,.5);margin-top:4px;line-height:1.5;max-width:700px}}
.tier-right{{display:flex;align-items:center;gap:10px;flex-shrink:0}}
.tier-count{{background:rgba(255,255,255,.12);color:#fff;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600}}
.tog{{color:rgba(255,255,255,.45);font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:20px 60px;background:#eef0ec}}
.post-card{{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.07);display:flex;flex-direction:column;transition:transform .15s,box-shadow .15s}}
.post-card:hover{{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.11)}}
.post-card.hidden{{display:none!important}}
.post-card.is-saved{{border-left:3px solid #16a34a}}
.card-thumb{{position:relative;width:100%;aspect-ratio:4/3;overflow:hidden;background:#1a2a1a;cursor:pointer;flex-shrink:0}}
.card-thumb img{{width:100%;height:100%;object-fit:cover;display:block;transition:transform .2s}}
.card-thumb:hover img{{transform:scale(1.03)}}
.no-thumb{{width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;color:rgba(255,255,255,.6);font-size:28px}}
.platform-badge{{position:absolute;bottom:8px;left:8px;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;backdrop-filter:blur(4px)}}
.platform-badge.instagram{{background:rgba(225,48,108,.85);color:#fff}}
.platform-badge.tiktok{{background:rgba(1,1,1,.85);color:#fff}}
.card-body{{padding:12px 14px;flex:1;display:flex;flex-direction:column;gap:7px}}
.card-top{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.account-tag{{background:#f0f2ee;color:#4b5563;padding:3px 9px;border-radius:9px;font-size:10px;font-weight:700}}
.virality-tag{{padding:3px 9px;border-radius:9px;font-size:10px;font-weight:700}}
.card-meta-row{{font-size:11px;color:#9ca3af;line-height:1.4}}
.caption-preview{{font-size:12px;color:#6b7280;line-height:1.5;flex:1}}
.tag-row{{display:flex;gap:5px;flex-wrap:wrap}}
.tag{{padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600}}
.topic-tag{{background:#dbeafe;color:#1d4ed8}}
.hook-tag{{background:#f3e8ff;color:#7c3aed}}
.card-actions{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:2px}}
.watch-link{{font-size:11px;font-weight:600;color:#0f1a12;text-decoration:none;border-bottom:1px solid #d4a843;padding-bottom:1px}}
.watch-link:hover{{color:#d4a843}}
.replicate-btn{{margin-left:auto;padding:5px 12px;background:#0f1a12;color:#d4a843;border:none;border-radius:8px;font-family:'Poppins',sans-serif;font-size:11px;font-weight:600;cursor:pointer;transition:background .15s}}
.replicate-btn:hover{{background:#1a2e1f}}
.card-checks{{display:flex;gap:8px;padding-top:6px;border-top:1px solid #f3f4f6}}
.card-check{{display:flex;align-items:center;gap:5px;cursor:pointer;font-size:11px;color:#9ca3af;user-select:none}}
.card-check input{{width:13px;height:13px;cursor:pointer;accent-color:#d4a843}}
/* Replicate Panel */
.panel-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:199;display:none}}
.panel-overlay.open{{display:block}}
.replicate-panel{{position:fixed;top:0;right:-520px;width:520px;height:100vh;background:#fff;box-shadow:-4px 0 32px rgba(0,0,0,.18);z-index:200;transition:right .3s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;overflow:hidden}}
.replicate-panel.open{{right:0}}
.panel-hdr{{background:linear-gradient(135deg,#0f1a12,#1a2e1f);padding:20px 24px;flex-shrink:0}}
.panel-hdr h3{{font-size:15px;font-weight:700;color:#d4a843;margin-bottom:3px}}
.panel-hdr .panel-sub{{font-size:11px;color:rgba(255,255,255,.5)}}
.panel-close{{position:absolute;top:18px;right:20px;background:none;border:none;color:rgba(255,255,255,.5);font-size:20px;cursor:pointer;line-height:1}}
.panel-close:hover{{color:#fff}}
.panel-body{{flex:1;overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:14px}}
.panel-loading{{text-align:center;padding:40px 0;color:#6b7280;font-size:13px}}
.panel-error{{background:#fee2e2;border-left:3px solid #dc2626;padding:12px 16px;border-radius:0 8px 8px 0;font-size:13px;color:#dc2626}}
.brief-section{{border:1px solid #e5e7eb;border-radius:10px;overflow:hidden}}
.brief-section-hdr{{background:#f9fafb;padding:8px 14px;display:flex;justify-content:space-between;align-items:center}}
.brief-section-hdr h4{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#374151}}
.copy-btn{{padding:3px 10px;background:#fff;border:1px solid #d1d5db;border-radius:6px;font-size:10px;font-weight:600;cursor:pointer;color:#374151;font-family:'Poppins',sans-serif}}
.copy-btn:hover{{background:#f3f4f6}}
.copy-btn.copied{{background:#dcfce7;color:#16a34a;border-color:#bbf7d0}}
.brief-section-body{{padding:12px 14px;font-size:13px;line-height:1.6;color:#374151;white-space:pre-wrap}}
.brief-section-body.gold{{background:#fffbeb;color:#78350f;border-top:1px solid #fde68a}}
.compliance-ok{{color:#16a34a;font-weight:600}}
.compliance-warn{{color:#dc2626;font-weight:600}}
.copy-all-btn{{width:100%;padding:10px;background:#0f1a12;color:#d4a843;border:none;border-radius:8px;font-family:'Poppins',sans-serif;font-size:13px;font-weight:600;cursor:pointer;margin-top:4px}}
.copy-all-btn:hover{{background:#1a2e1f}}
@media(max-width:768px){{
  .hdr,.filters,.tier-hdr,.cards{{padding-left:16px;padding-right:16px}}
  .cards{{grid-template-columns:1fr}}
  .replicate-panel{{width:100%;right:-100%}}
}}
</style>
</head>
<body>
<div class="hdr">
  <h1>LLMD Viral Post Intelligence</h1>
  <p class="sub">Weekly snapshot of the highest-performing organic posts in the peptide, longevity, and biohacking niche. Use filters to find hooks and formats worth adapting for LLMD.</p>
  <div class="stats">
    <div><div class="snum" id="stat-total">{total}</div><div class="slbl">Posts Tracked</div></div>
    <div><div class="snum" id="stat-viral">{high_count}</div><div class="slbl">High Virality</div></div>
    <div><div class="snum">{ig_count}</div><div class="slbl">Instagram</div></div>
    <div><div class="snum">{tt_count}</div><div class="slbl">TikTok</div></div>
    <div><div class="snum">{accounts_count}</div><div class="slbl">Accounts</div></div>
  </div>
  <div class="guide">
    <h3>How to use this</h3>
    <p>Start with <strong>Top Viral Posts</strong> for the highest-priority content to adapt. Filter by <strong>Topic</strong> to find posts relevant to LLMD's current promotions. Click <strong>✨ Replicate for LLMD</strong> on any card to get an AI-generated content brief — brand-aligned, Seraphiel-grounded, ready to brief your team. Updated weekly. Last refresh: {updated}.</p>
  </div>
</div>

<div class="filters" id="filters">
  <span class="flbl">Platform:</span>
  <div class="filter-group">
    <button class="fbtn on" data-filter-type="platform" data-filter-val="all" onclick="fil(this)">All ({total})</button>
    <button class="fbtn" data-filter-type="platform" data-filter-val="instagram" onclick="fil(this)">📷 Instagram ({ig_count})</button>
    <button class="fbtn" data-filter-type="platform" data-filter-val="tiktok" onclick="fil(this)">🎵 TikTok ({tt_count})</button>
  </div>
  <div class="filter-sep"></div>
  <span class="flbl">Topic:</span>
  <div class="filter-group">
    {topic_btns}
  </div>
  <div class="filter-sep"></div>
  <span class="flbl">Hook:</span>
  <div class="filter-group">
    {hook_btns}
  </div>
  <div class="filter-sep"></div>
  <div class="filter-group">
    <button class="fbtn" data-filter-type="virality" data-filter-val="high" onclick="fil(this)">🔥 High Virality Only</button>
    <button class="fbtn" data-filter-type="saved" data-filter-val="saved" onclick="fil(this)" id="saved-btn">⭐ Saved (0)</button>
  </div>
</div>

{tier_html}

<!-- Replicate Panel -->
<div class="panel-overlay" id="panel-overlay" onclick="closePanel()"></div>
<div class="replicate-panel" id="replicate-panel">
  <div class="panel-hdr" style="position:relative">
    <h3>✨ Replicate for LLMD</h3>
    <div class="panel-sub" id="panel-source">Generating content brief...</div>
    <button class="panel-close" onclick="closePanel()">✕</button>
  </div>
  <div class="panel-body" id="panel-body">
    <div class="panel-loading">Generating LLMD content brief...</div>
  </div>
</div>

<script>
// ── Filter logic ──
var activeFilters = {{platform:'all',topic:null,hook:null,virality:null,saved:false}};

function fil(btn) {{
  var ftype = btn.dataset.filterType;
  var fval = btn.dataset.filterVal;

  if(ftype==='saved') {{
    activeFilters.saved = !activeFilters.saved;
    btn.classList.toggle('on', activeFilters.saved);
  }} else {{
    // Deactivate same-group buttons
    document.querySelectorAll('.fbtn[data-filter-type="'+ftype+'"]').forEach(function(b){{
      b.classList.remove('on','on-ig','on-tt');
    }});
    var current = activeFilters[ftype];
    if(current === fval && ftype !== 'platform') {{
      activeFilters[ftype] = null;
    }} else {{
      activeFilters[ftype] = fval;
      if(ftype==='platform' && fval==='instagram') btn.classList.add('on-ig');
      else if(ftype==='platform' && fval==='tiktok') btn.classList.add('on-tt');
      else btn.classList.add('on');
    }}
  }}

  applyFilters();
}}

function applyFilters() {{
  var cards = document.querySelectorAll('.post-card');
  var visible = 0;
  cards.forEach(function(c) {{
    var show = true;
    var p = activeFilters.platform;
    var t = activeFilters.topic;
    var h = activeFilters.hook;
    var v = activeFilters.virality;
    if(p && p !== 'all' && c.dataset.platform !== p) show = false;
    if(t && c.dataset.topic !== t) show = false;
    if(h && c.dataset.hook !== h) show = false;
    if(v && c.dataset.virality !== v) show = false;
    if(activeFilters.saved && c.dataset.saved !== 'true') show = false;
    c.classList.toggle('hidden', !show);
    if(show) visible++;
  }});
  // Expand all tiers when filtering
  if(activeFilters.platform!=='all' || activeFilters.topic || activeFilters.hook || activeFilters.virality || activeFilters.saved) {{
    document.querySelectorAll('.tier').forEach(function(s){{
      s.classList.remove('collapsed');
      var tog=s.querySelector('.tog');
      if(tog) tog.textContent='▲';
    }});
  }}
}}

function toggleTier(id) {{
  var s=document.getElementById('t'+id);
  var tog=document.getElementById('tog'+id);
  s.classList.toggle('collapsed');
  tog.textContent=s.classList.contains('collapsed')?'▼':'▲';
}}

// ── Save marks ──
function cardKey(card) {{
  return card.dataset.postId || '';
}}

function updateSavedBtn() {{
  var count = document.querySelectorAll('.post-card[data-saved="true"]').length;
  var btn = document.getElementById('saved-btn');
  if(btn) btn.textContent = '⭐ Saved (' + count + ')';
}}

document.addEventListener('DOMContentLoaded', function() {{
  document.querySelectorAll('.post-card').forEach(function(card) {{
    var chk = card.querySelector('[data-field="saved"]');
    if(!chk) return;
    chk.addEventListener('change', function() {{
      var key = cardKey(card);
      var val = this.checked;
      card.dataset.saved = val ? 'true' : '';
      card.classList.toggle('is-saved', val);
      updateSavedBtn();
      fetch('/api/mark', {{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{key:key,field:'saved',value:val}})}});
    }});
  }});
  fetch('/api/marks').then(r=>r.json()).then(function(marks) {{
    document.querySelectorAll('.post-card').forEach(function(card) {{
      var key = cardKey(card);
      if(!key) return;
      var state = marks[key] || {{}};
      var chk = card.querySelector('[data-field="saved"]');
      if(state.saved && chk) {{
        chk.checked = true;
        card.dataset.saved = 'true';
        card.classList.add('is-saved');
      }}
    }});
    updateSavedBtn();
  }}).catch(function(){{}});
}});

// ── Replicate Panel ──
var currentPostData = null;

function openReplicate(btn) {{
  currentPostData = JSON.parse(btn.getAttribute('data-post').replace(/&quot;/g,'"').replace(/&#39;/g,"'"));
  var panel = document.getElementById('replicate-panel');
  var overlay = document.getElementById('panel-overlay');
  var body = document.getElementById('panel-body');
  var src = document.getElementById('panel-source');

  src.textContent = '@' + (currentPostData.account_handle || '') + ' on ' + (currentPostData.platform || '');
  body.innerHTML = '<div class="panel-loading">✨ Generating LLMD content brief...</div>';
  panel.classList.add('open');
  overlay.classList.add('open');

  fetch('/api/replicate', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{post: currentPostData}})
  }})
  .then(r => r.json())
  .then(function(data) {{
    if(!data.ok) throw new Error(data.error || 'Unknown error');
    renderBrief(data.brief);
  }})
  .catch(function(err) {{
    body.innerHTML = '<div class="panel-error">Error generating brief: ' + err.message + '</div>';
  }});
}}

function closePanel() {{
  document.getElementById('replicate-panel').classList.remove('open');
  document.getElementById('panel-overlay').classList.remove('open');
}}

document.addEventListener('keydown', function(e) {{ if(e.key==='Escape') closePanel(); }});

function renderBrief(brief) {{
  var sections = [
    {{key:'adapted_hook', label:'Adapted Hook', gold:false}},
    {{key:'content_brief', label:'Content Brief', gold:false}},
    {{key:'talking_points', label:'Talking Points', gold:false}},
    {{key:'spiritual_layer', label:'Spiritual Layer', gold:true}},
    {{key:'draft_caption', label:'Draft Caption', gold:false}},
    {{key:'compliance_note', label:'Compliance Note', gold:false}},
  ];
  var html = '';
  var allText = [];
  sections.forEach(function(s) {{
    var val = brief[s.key];
    if(!val) return;
    var text = Array.isArray(val) ? val.map((v,i)=>(i+1)+'. '+v).join('\\n') : val;
    var cls = s.gold ? ' gold' : '';
    var isCompliance = s.key === 'compliance_note';
    var valClass = isCompliance && text.toLowerCase().includes('all clear') ? ' compliance-ok' : isCompliance ? ' compliance-warn' : '';
    html += '<div class="brief-section">'
      + '<div class="brief-section-hdr">'
      + '<h4>' + s.label + '</h4>'
      + '<button class="copy-btn" onclick="copySection(this, \`' + text.replace(/`/g,'\\`') + '\`)">Copy</button>'
      + '</div>'
      + '<div class="brief-section-body' + cls + valClass + '">' + text + '</div>'
      + '</div>';
    allText.push('--- ' + s.label + ' ---\\n' + text);
  }});
  html += '<button class="copy-all-btn" onclick="copyAll(\`' + allText.join('\\n\\n').replace(/`/g,'\\`') + '\`)">📋 Copy All Sections</button>';
  document.getElementById('panel-body').innerHTML = html;
}}

function copySection(btn, text) {{
  navigator.clipboard.writeText(text).then(function() {{
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(function(){{ btn.textContent='Copy'; btn.classList.remove('copied'); }}, 2000);
  }});
}}

function copyAll(text) {{
  navigator.clipboard.writeText(text).then(function() {{
    var btn = document.querySelector('.copy-all-btn');
    if(btn) {{ btn.textContent='✅ Copied!'; setTimeout(function(){{btn.textContent='📋 Copy All Sections';}},2000); }}
  }});
}}
</script>
</body>
</html>'''


# ── Main ──

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, don't write HTML")
    parser.add_argument("--no-classify", action="store_true", help="Skip Claude classification")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN env var not set", file=sys.stderr)
        sys.exit(1)

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Starting LLMD Viral Post refresh...")
    accounts = load_accounts()
    print(f"Loaded {len(accounts)} tracked accounts")

    print("Fetching Instagram posts...")
    ig_posts = fetch_instagram_posts(accounts)
    print(f"  Got {len(ig_posts)} Instagram posts")

    print("Fetching TikTok posts...")
    tt_posts = fetch_tiktok_posts(accounts)
    print(f"  Got {len(tt_posts)} TikTok posts")

    all_posts = deduplicate(ig_posts + tt_posts)
    print(f"Total after dedup: {len(all_posts)} posts")

    print("Scoring virality...")
    all_posts = [score_virality(p) for p in all_posts]
    high = sum(1 for p in all_posts if p["virality_tier"] == "high")
    med = sum(1 for p in all_posts if p["virality_tier"] == "medium")
    print(f"  High: {high} | Medium: {med} | Low: {len(all_posts) - high - med}")

    if not args.no_classify:
        all_posts = classify_posts(all_posts)

    # Save thumbnails map
    thumbnails = {p["post_id"]: p["thumbnail_url"] for p in all_posts if p.get("thumbnail_url")}
    (DATA_DIR / "thumbnails.json").write_text(json.dumps(thumbnails, indent=2))

    # Save full post data
    (DATA_DIR / "posts.json").write_text(json.dumps(all_posts, indent=2))
    print(f"Saved {len(all_posts)} posts to data/posts.json")

    if args.dry_run:
        print("Dry run — skipping HTML write")
        return

    print("Building HTML...")
    html = build_html(all_posts)
    (STATIC_DIR / "index.html").write_text(html)
    print(f"Written: {STATIC_DIR / 'index.html'} ({len(html):,} chars)")
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Refresh complete.")


if __name__ == "__main__":
    main()
