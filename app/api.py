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


@app.get("/api/accounts")
def get_accounts():
    path = DATA_DIR / "accounts.json"
    return json.loads(path.read_text())


@app.post("/api/accounts")
async def add_account(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    path = DATA_DIR / "accounts.json"
    data = json.loads(path.read_text())
    # Prevent duplicates by name (case-insensitive)
    if any(a["name"].lower() == name.lower() for a in data["accounts"]):
        raise HTTPException(status_code=409, detail="Account already exists")
    new_account = {
        "name": name,
        "instagram": body.get("instagram") or None,
        "tiktok": body.get("tiktok") or None,
        "followers_ig": int(body.get("followers_ig") or 0),
        "followers_tt": int(body.get("followers_tt") or 0),
        "tier": int(body.get("tier") or 2),
        "category": body.get("category") or "influencer",
        "added": __import__("datetime").date.today().isoformat(),
        "added_by": "dashboard",
    }
    data["accounts"].append(new_account)
    data["last_updated"] = __import__("datetime").date.today().isoformat()
    path.write_text(json.dumps(data, indent=2))
    return {"ok": True, "account": new_account}


@app.delete("/api/accounts/{account_name}")
async def delete_account(account_name: str):
    path = DATA_DIR / "accounts.json"
    data = json.loads(path.read_text())
    original_count = len(data["accounts"])
    data["accounts"] = [
        a for a in data["accounts"]
        if a["name"].lower() != account_name.lower()
    ]
    if len(data["accounts"]) == original_count:
        raise HTTPException(status_code=404, detail="Account not found")
    data["last_updated"] = __import__("datetime").date.today().isoformat()
    path.write_text(json.dumps(data, indent=2))
    return {"ok": True}


@app.get("/accounts", response_class=HTMLResponse)
def serve_accounts_page():
    return HTMLResponse(content=_accounts_html(), status_code=200)


def _accounts_html() -> str:
    path = DATA_DIR / "accounts.json"
    data = json.loads(path.read_text()) if path.exists() else {"accounts": []}
    accounts = data.get("accounts", [])
    rows = ""
    for a in accounts:
        ig = a.get("instagram") or "—"
        tt = a.get("tiktok") or "—"
        tier = a.get("tier", 2)
        cat = a.get("category", "")
        tier_colors = {1: "#dcfce7;color:#16a34a", 2: "#fef9c3;color:#ca8a04", 3: "#f3f4f6;color:#6b7280"}
        tc = tier_colors.get(tier, tier_colors[2])
        rows += f'''<tr data-name="{a['name']}">
  <td class="name-cell">{a['name']}</td>
  <td>{f'<a href="https://instagram.com/{ig}" target="_blank">@{ig}</a>' if ig != '—' else '—'}</td>
  <td>{f'<a href="https://tiktok.com/@{tt}" target="_blank">@{tt}</a>' if tt != '—' else '—'}</td>
  <td><span class="tier-badge" style="background:{tc}">Tier {tier}</span></td>
  <td>{cat.replace('_', ' ').title()}</td>
  <td style="text-align:right"><button class="del-btn" onclick="deleteAccount('{a['name'].replace(chr(39), '')}')">Remove</button></td>
</tr>'''
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLMD — Tracked Accounts</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Poppins',sans-serif;background:#eef0ec;color:#1a2420}}
.hdr{{background:linear-gradient(135deg,#0f1a12,#1a2e1f);padding:36px 60px 28px;color:#fff}}
.hdr h1{{font-size:24px;font-weight:700;color:#d4a843;margin-bottom:4px}}
.hdr .sub{{font-size:13px;color:rgba(255,255,255,.5)}}
.back-link{{display:inline-flex;align-items:center;gap:6px;color:#d4a843;text-decoration:none;font-size:12px;font-weight:600;margin-bottom:16px}}
.back-link:hover{{opacity:.8}}
.main{{max-width:900px;margin:32px auto;padding:0 24px}}
.section{{background:#fff;border-radius:14px;box-shadow:0 1px 4px rgba(0,0,0,.07);margin-bottom:24px;overflow:hidden}}
.section-hdr{{background:#f9fafb;padding:14px 20px;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;justify-content:space-between}}
.section-hdr h2{{font-size:14px;font-weight:700;color:#1a2420}}
.section-hdr span{{font-size:12px;color:#9ca3af}}
table{{width:100%;border-collapse:collapse}}
th{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#9ca3af;padding:10px 20px;text-align:left;border-bottom:1px solid #f3f4f6}}
td{{font-size:13px;padding:12px 20px;border-bottom:1px solid #f9fafb;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#fafafa}}
.name-cell{{font-weight:600;color:#1a2420}}
a{{color:#0f1a12;text-decoration:none;border-bottom:1px solid #d4a843}}
a:hover{{color:#d4a843}}
.tier-badge{{padding:3px 9px;border-radius:8px;font-size:10px;font-weight:700}}
.del-btn{{padding:5px 12px;background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;border-radius:8px;font-family:'Poppins',sans-serif;font-size:11px;font-weight:600;cursor:pointer;transition:all .15s}}
.del-btn:hover{{background:#dc2626;color:#fff}}
.add-form{{padding:20px}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}}
.form-field{{display:flex;flex-direction:column;gap:4px}}
.form-field label{{font-size:11px;font-weight:600;color:#374151;text-transform:uppercase;letter-spacing:.5px}}
.form-field input,.form-field select{{padding:8px 12px;border:1.5px solid #d1d5db;border-radius:8px;font-family:'Poppins',sans-serif;font-size:13px;color:#1a2420;outline:none;transition:border .15s}}
.form-field input:focus,.form-field select:focus{{border-color:#d4a843}}
.add-btn{{padding:10px 24px;background:#0f1a12;color:#d4a843;border:none;border-radius:8px;font-family:'Poppins',sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s}}
.add-btn:hover{{background:#1a2e1f}}
.add-btn:disabled{{opacity:.5;cursor:not-allowed}}
.msg{{padding:10px 16px;border-radius:8px;font-size:12px;font-weight:500;display:none;margin-top:12px}}
.msg.ok{{background:#dcfce7;color:#16a34a;display:block}}
.msg.err{{background:#fee2e2;color:#dc2626;display:block}}
.note{{font-size:11px;color:#9ca3af;margin-top:8px}}
</style>
</head>
<body>
<div class="hdr">
  <a href="/" class="back-link">← Back to Dashboard</a>
  <h1>Tracked Accounts</h1>
  <p class="sub">Manage which accounts are scraped in the weekly refresh. Changes take effect on the next refresh.</p>
</div>
<div class="main">
  <div class="section">
    <div class="section-hdr">
      <h2>Current Accounts</h2>
      <span id="count-label">{len(accounts)} accounts tracked</span>
    </div>
    <table id="accounts-table">
      <thead><tr>
        <th>Name</th><th>Instagram</th><th>TikTok</th><th>Tier</th><th>Category</th><th></th>
      </tr></thead>
      <tbody id="accounts-body">{rows}</tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-hdr">
      <h2>Add Account</h2>
    </div>
    <div class="add-form">
      <div class="form-grid">
        <div class="form-field" style="grid-column:1/-1">
          <label>Name *</label>
          <input type="text" id="f-name" placeholder="e.g. Andrew Huberman">
        </div>
        <div class="form-field">
          <label>Instagram Handle</label>
          <input type="text" id="f-ig" placeholder="hubermanlab (no @)">
        </div>
        <div class="form-field">
          <label>TikTok Handle</label>
          <input type="text" id="f-tt" placeholder="hubermanlab (no @)">
        </div>
        <div class="form-field">
          <label>Est. Instagram Followers</label>
          <input type="number" id="f-ig-followers" placeholder="e.g. 800000">
        </div>
        <div class="form-field">
          <label>Est. TikTok Followers</label>
          <input type="number" id="f-tt-followers" placeholder="e.g. 100000">
        </div>
        <div class="form-field">
          <label>Tier</label>
          <select id="f-tier">
            <option value="1">Tier 1 — Major influencer</option>
            <option value="2" selected>Tier 2 — Niche expert</option>
            <option value="3">Tier 3 — Competitor</option>
          </select>
        </div>
        <div class="form-field">
          <label>Category</label>
          <select id="f-category">
            <option value="influencer">Influencer</option>
            <option value="physician">Physician</option>
            <option value="researcher">Researcher</option>
            <option value="educator">Educator</option>
            <option value="competitor">Competitor</option>
          </select>
        </div>
      </div>
      <button class="add-btn" id="add-btn" onclick="addAccount()">Add Account</button>
      <p class="note">Changes take effect the next time the weekly refresh runs (every Sunday 6am).</p>
      <div class="msg" id="msg"></div>
    </div>
  </div>
</div>

<script>
function deleteAccount(name) {{
  if(!confirm('Remove "' + name + '" from tracked accounts?')) return;
  fetch('/api/accounts/' + encodeURIComponent(name), {{method:'DELETE'}})
    .then(r => r.json())
    .then(function(d) {{
      if(d.ok) {{
        var row = document.querySelector('[data-name="' + name + '"]');
        if(row) row.remove();
        var count = document.querySelectorAll('#accounts-body tr').length;
        document.getElementById('count-label').textContent = count + ' accounts tracked';
      }}
    }});
}}

function addAccount() {{
  var name = document.getElementById('f-name').value.trim();
  if(!name) {{ showMsg('Name is required.', false); return; }}
  var body = {{
    name: name,
    instagram: document.getElementById('f-ig').value.trim() || null,
    tiktok: document.getElementById('f-tt').value.trim() || null,
    followers_ig: parseInt(document.getElementById('f-ig-followers').value) || 0,
    followers_tt: parseInt(document.getElementById('f-tt-followers').value) || 0,
    tier: parseInt(document.getElementById('f-tier').value),
    category: document.getElementById('f-category').value,
  }};
  var btn = document.getElementById('add-btn');
  btn.disabled = true;
  fetch('/api/accounts', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}})
    .then(r => r.json())
    .then(function(d) {{
      btn.disabled = false;
      if(d.ok) {{
        showMsg('Account added! Will appear in the next weekly refresh.', true);
        // Clear form
        ['f-name','f-ig','f-tt','f-ig-followers','f-tt-followers'].forEach(function(id){{
          document.getElementById(id).value='';
        }});
        // Add row to table
        var a = d.account;
        var ig = a.instagram || '—';
        var tt = a.tiktok || '—';
        var tier = a.tier;
        var tierColors = {{1:'background:#dcfce7;color:#16a34a',2:'background:#fef9c3;color:#ca8a04',3:'background:#f3f4f6;color:#6b7280'}};
        var tc = tierColors[tier] || tierColors[2];
        var tr = document.createElement('tr');
        tr.setAttribute('data-name', a.name);
        tr.innerHTML = '<td class="name-cell">'+a.name+'</td>'
          +'<td>'+(ig!=='—'?'<a href="https://instagram.com/'+ig+'" target="_blank">@'+ig+'</a>':'—')+'</td>'
          +'<td>'+(tt!=='—'?'<a href="https://tiktok.com/@'+tt+'" target="_blank">@'+tt+'</a>':'—')+'</td>'
          +'<td><span class="tier-badge" style="'+tc+'">Tier '+tier+'</span></td>'
          +'<td>'+a.category.replace('_',' ')+'</td>'
          +'<td style="text-align:right"><button class="del-btn" onclick="deleteAccount(\\'' + a.name.replace(/'/g,'') + '\\')">Remove</button></td>';
        document.getElementById('accounts-body').appendChild(tr);
        var count = document.querySelectorAll('#accounts-body tr').length;
        document.getElementById('count-label').textContent = count + ' accounts tracked';
      }} else {{
        showMsg(d.detail || 'Error adding account.', false);
      }}
    }})
    .catch(function(){{ btn.disabled=false; showMsg('Network error.', false); }});
}}

function showMsg(text, ok) {{
  var el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  setTimeout(function(){{ el.style.display='none'; }}, 4000);
}}
</script>
</body>
</html>'''


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
