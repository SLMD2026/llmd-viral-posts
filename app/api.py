"""FastAPI server for LLMD Viral Post Intelligence Dashboard."""

import json
import mimetypes
import os
import re
import sqlite3
import threading
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, Response

app = FastAPI(title="LLMD Viral Post Intelligence")

BASE_DIR = Path(__file__).parent.parent
STATIC_DIR = BASE_DIR / "static"
IMAGES_DIR = STATIC_DIR / "images"
DATA_DIR = BASE_DIR / "data"
PROMPTS_DIR = BASE_DIR / "prompts"
MARKS_DB = BASE_DIR / "marks.db"

IMAGES_DIR.mkdir(parents=True, exist_ok=True)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Thumbnail URL map (post_id → CDN URL) ──

_THUMB_URLS: dict[str, str] = {}
_thumb_lock = threading.Lock()


def _load_thumb_urls():
    path = DATA_DIR / "thumbnails.json"
    if path.exists():
        with _thumb_lock:
            _THUMB_URLS.update(json.loads(path.read_text()))


def _prefetch_thumbnails():
    _load_thumb_urls()
    with _thumb_lock:
        items = list(_THUMB_URLS.items())
    for post_id, cdn_url in items:
        local = IMAGES_DIR / post_id
        if not local.exists() or local.stat().st_size == 0:
            try:
                req = urllib.request.Request(
                    cdn_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; llmd-bot/1.0)"}
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    local.write_bytes(r.read())
            except Exception:
                pass


threading.Thread(target=_prefetch_thumbnails, daemon=True).start()


# ── Marks persistence ──

def _marks_conn():
    conn = sqlite3.connect(str(MARKS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_marks (
            post_key TEXT PRIMARY KEY,
            saved INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


# ── Claude replicate ──

def _claude_replicate(post_data: dict) -> dict:
    prompt_path = PROMPTS_DIR / "replicate.md"
    if not prompt_path.exists():
        raise FileNotFoundError("prompts/replicate.md not found")

    template = prompt_path.read_text()
    prompt = template.replace("{{POST_JSON}}", json.dumps(post_data, indent=2))

    payload = {
        "model": "claude-opus-4-6",
        "max_tokens": 2000,
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
    with urllib.request.urlopen(req, timeout=45) as r:
        result = json.loads(r.read())

    raw = result["content"][0]["text"].strip()
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        raise ValueError("No JSON in Claude response")
    return json.loads(json_match.group())


# ── Endpoints ──

@app.get("/health")
def health():
    posts_count = 0
    posts_path = DATA_DIR / "posts.json"
    if posts_path.exists():
        try:
            posts_count = len(json.loads(posts_path.read_text()))
        except Exception:
            pass
    images_cached = len(list(IMAGES_DIR.glob("*")))
    return {
        "status": "ok",
        "service": "llmd-viral-posts",
        "posts": posts_count,
        "images_cached": images_cached,
    }


@app.get("/images/{post_id}")
def serve_image(post_id: str):
    local = IMAGES_DIR / post_id
    if local.exists() and local.stat().st_size > 0:
        media_type, _ = mimetypes.guess_type(str(local))
        return FileResponse(str(local), media_type=media_type or "image/jpeg")

    # Reload thumb map in case it was updated
    if not _THUMB_URLS:
        _load_thumb_urls()

    with _thumb_lock:
        cdn_url = _THUMB_URLS.get(post_id)

    if not cdn_url:
        raise HTTPException(status_code=404, detail="Image not found")

    try:
        req = urllib.request.Request(
            cdn_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; llmd-bot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        local.write_bytes(data)
        media_type, _ = mimetypes.guess_type(cdn_url.split("?")[0])
        return Response(content=data, media_type=media_type or "image/jpeg")
    except Exception:
        raise HTTPException(status_code=404, detail="Image unavailable")


@app.get("/api/marks")
def get_marks():
    with _marks_conn() as conn:
        rows = conn.execute("SELECT * FROM post_marks").fetchall()
    return {r["post_key"]: {"saved": bool(r["saved"])} for r in rows}


@app.post("/api/mark")
async def set_mark(request: Request):
    body = await request.json()
    key = (body.get("key") or "").strip()
    field = body.get("field")
    value = body.get("value")
    if not key or field not in ("saved",) or not isinstance(value, bool):
        raise HTTPException(status_code=400, detail="Bad request")
    with _marks_conn() as conn:
        conn.execute(
            f"INSERT INTO post_marks (post_key, {field}) VALUES (?, ?) "
            f"ON CONFLICT(post_key) DO UPDATE SET {field}=excluded.{field}",
            (key, int(value))
        )
    return {"ok": True}


@app.post("/api/replicate")
async def replicate_post(request: Request):
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    body = await request.json()
    post_data = body.get("post", {})
    if not post_data:
        raise HTTPException(status_code=400, detail="Missing post data")
    try:
        brief = _claude_replicate(post_data)
        return {"ok": True, "brief": brief}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            content="<h2 style='font-family:sans-serif;padding:40px'>Dashboard not yet generated."
                    " Run <code>python -m app.refresh</code> to populate.</h2>",
            status_code=200
        )
    return HTMLResponse(content=html_path.read_text(), status_code=200)
