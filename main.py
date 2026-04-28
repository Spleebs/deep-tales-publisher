import os
import sqlite3
import uuid
import hashlib
import json
import time
import re
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory
import requests

app = Flask(__name__, static_folder="static", static_url_path="")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Use /data/ on Railway (persistent volume), fall back to local ./data/ for dev
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "deep_tales.db"))
API_SECRET = os.environ.get("X_API_SECRET", "ShoboloBoboloAlpha232323")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
SUBSTACK_COOKIE = os.environ.get("SUBSTACK_COOKIE_STRING", "")
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
DRIVE_FOLDER_ID = "1kEj_8ZLNWzSGieUrKysozGxRx9O3nw2z"

COINGECKO_IDS = "bitcoin,rain,solana,power-ledger,arbitrum,energy-web-token"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Simple in-process cache for market data
_market_cache = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chapters (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            body_formatted TEXT,
            image_prompt TEXT,
            image_url TEXT,
            sha256 TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            revision_notes TEXT,
            substack_post_id TEXT,
            prediction_cta TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS revision_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chapter_id TEXT NOT NULL,
            body_before TEXT NOT NULL,
            body_after TEXT NOT NULL,
            notes TEXT NOT NULL,
            revised_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    print("[DB] Initialized.")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = request.headers.get("X-API-Secret", "")
        if secret != API_SECRET:
            print(f"[AUTH] Rejected — bad secret on {request.path}")
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": now_iso()})


# ---------------------------------------------------------------------------
# Serve Control Room UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# Chapters — list
# ---------------------------------------------------------------------------
@app.route("/chapters", methods=["GET"])
@require_auth
def list_chapters():
    print("--- GET /chapters called ---")
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, status, created_at, updated_at, sha256 FROM chapters ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    print(f"[SUCCESS] /chapters returned {len(result)} rows")
    return jsonify(result)


# ---------------------------------------------------------------------------
# Token/character map — used by weighting model and AI draft
# ---------------------------------------------------------------------------
TOKEN_MAP = {
    "bitcoin":          {"name": "Tobi Nic",     "symbol": "BTC",  "pair": "BINANCE:BTCUSDT"},
    "rain":             {"name": "Carl Optionor", "symbol": "RAIN", "pair": "KUCOIN:RAINUSDT"},
    "solana":           {"name": "Ana Sol",       "symbol": "SOL",  "pair": "BINANCE:SOLUSDT"},
    "power-ledger":     {"name": "Pol Reedgrew",  "symbol": "POWR", "pair": "BINANCE:POWRUSDT"},
    "arbitrum":         {"name": "Brita Rum",     "symbol": "ARB",  "pair": "BINANCE:ARBUSDT"},
    "energy-web-token": {"name": "Gybe Newer",    "symbol": "EWT",  "pair": "KUCOIN:EWTUSDT"},
}


# ---------------------------------------------------------------------------
# AI Draft Generation — the main entry point for a new chapter
# ---------------------------------------------------------------------------
@app.route("/chapters/ai-draft", methods=["POST"])
@require_auth
def ai_draft():
    """Pull market data, apply weighting model, write full chapter with GPT-4.1, save as draft."""
    print("--- POST /chapters/ai-draft called ---")

    if not OPENAI_API_KEY:
        return jsonify({"error": "OPENAI_API_KEY not configured"}), 500

    # 1. Get market weights
    market = _fetch_market_data()
    if not market:
        return jsonify({"error": "Market data unavailable"}), 500

    weights_resp = _compute_weights(market)
    ranked = weights_resp["ranked"]
    focal = weights_resp["focal"]
    events = weights_resp["events"]

    # 2. Count existing chapters to determine chapter number + whether to include prediction CTA
    conn = get_db()
    chapter_count = conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    conn.close()
    chapter_number = chapter_count + 1
    include_prediction = chapter_number >= 5

    # 3. Build the AI prompt
    focal_names = " and ".join(f['character'] for f in focal)
    focal_tokens = " and ".join(f['symbol'] for f in focal)

    market_summary_lines = []
    for t in ranked:
        direction = "up" if t["price_change_7d_pct"] >= 0 else "down"
        market_summary_lines.append(
            f"  - {t['character']} ({t['symbol']}): {direction} {abs(t['price_change_7d_pct']):.1f}% over 7 days, "
            f"weight={t['weight']:.3f}"
        )
    market_summary = "\n".join(market_summary_lines)

    event_lines = []
    for ev in events:
        if ev["type"] == "termination_risk":
            event_lines.append(f"  - TERMINATION RISK: {ev['character']} ({ev['symbol']}) — near-zero volume")
        elif ev["type"] == "isolated_move":
            event_lines.append(f"  - ISOLATED MOVE: {ev['character']} moved {ev['move_pct']:+.1f}%")
        elif ev["type"] == "market_surge_all":
            event_lines.append("  - MARKET-WIDE SURGE: all 6 tokens up >5%")
        elif ev["type"] == "market_crash_all":
            event_lines.append("  - MARKET-WIDE CRASH: all 6 tokens down >5%")
    event_summary = "\n".join(event_lines) if event_lines else "  None"

    prediction_instruction = ""
    if include_prediction:
        prediction_instruction = (
            "\n\nPREDICTION_CTA: Write one specific, measurable prediction question tied to this chapter's "
            "focal token. Format: 'Will [TOKEN] close [above/below] $[PRICE] on [EXCHANGE] by Monday "
            "[DATE] at 11:00 UTC?' Use realistic current price levels."
        )

    system_prompt = """You are the AI writer for Deep Tales — a weekly serialized fiction published on Substack.
Deep Tales is driven by real crypto market signals. You write short, cinematic chapters (200-300 words).

THE 6 CHARACTERS (never break these):
- Tobi Nic = Bitcoin (BTC) — male, late 50s, weathered, amber-gold eyes, navy pea coat
- Carl Optionor = Rain Protocol (RAIN) — male, mid 20s, jittery, dark hair, oversized dark grey jacket
- Ana Sol = Solana (SOL) — female, early 30s, dark hair tied back, purple-black jacket
- Pol Reedgrew = Powerledger (POWR) — male, early 40s, self-contained, green-grey jacket
- Brita Rum = Arbitrum (ARB) — female, late 20s, blonde bun, navy/sky-blue, pregnant
- Gybe Newer = Energy Web (EWT) — male, mid 40s, dark skin, silver hair, improvised captain's coat

WRITING RULES (follow strictly):
- 200-300 words of story text only
- NO em dashes (never use —)
- No hashtags
- No mention of exact word count
- Must end on an unresolved hook
- Structure follows character weight, not appearance order
- The focal character(s) are the catalytic presence — they drive what happens
- Opening sentence is a single standalone statement (the hook). It is its own paragraph.
- Paragraphs are separated by ' ||| ' (space, three pipes, space)
- Do NOT include the CTA or SHA-256 in the story text — the pipeline handles that
- Setting: aboard a ship / maritime environment — the characters are crew

OUTPUT FORMAT (return valid JSON, nothing else):
{
  "title": "chapter title (3-6 words, cinematic)",
  "body": "Opening sentence. ||| Rest of paragraph one... ||| Paragraph two... ||| Final paragraph ending on hook...",
  "image_prompt": "Detailed cinematic image prompt for an oil painting / digital art illustration. Portrait orientation. Dark maritime atmosphere. Describe the scene, lighting, mood, characters present.",
  "prediction_cta": "Will X close above/below $Y on EXCHANGE by Monday DATE at 11:00 UTC?"
}

If chapter number < 5, return empty string for prediction_cta."""

    user_prompt = f"""CHAPTER NUMBER: {chapter_number}

THIS WEEK'S MARKET DATA (7-day):
{market_summary}

FOCAL CHARACTER(S): {focal_names} ({focal_tokens})
These character(s) had the highest market activity this week and must drive this chapter.

SPECIAL EVENTS THIS WEEK:
{event_summary}
{prediction_instruction}

Write Chapter {chapter_number} now. Return only the JSON object."""

    print(f"[ai_draft] Calling GPT-4.1 for chapter {chapter_number}, focal={focal_names}...")
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4.1",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 1200,
                "temperature": 0.85,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"[ERROR] ai_draft GPT-4.1 failed: {resp.status_code} {resp.text[:300]}")
            return jsonify({"error": f"GPT-4.1 failed: {resp.status_code}"}), 500

        raw = resp.json()["choices"][0]["message"]["content"]
        generated = json.loads(raw)
    except Exception as e:
        print(f"[ERROR] ai_draft exception: {e}")
        return jsonify({"error": str(e)}), 500

    title = generated.get("title", "").strip()
    body = generated.get("body", "").strip()
    image_prompt = generated.get("image_prompt", "").strip()
    prediction_cta = generated.get("prediction_cta", "").strip() if include_prediction else ""

    if not title or not body:
        print(f"[ERROR] ai_draft — GPT returned incomplete data: {generated}")
        return jsonify({"error": "AI returned incomplete chapter data", "raw": generated}), 500

    # 4. Save to DB
    chapter_id = str(uuid.uuid4())
    ts = now_iso()
    conn = get_db()
    conn.execute(
        """INSERT INTO chapters (id, title, body, image_prompt, prediction_cta, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (chapter_id, title, body, image_prompt, prediction_cta, ts, ts),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()

    print(f"[SUCCESS] ai_draft — chapter {chapter_number} created id={chapter_id}, title='{title}'")
    return jsonify(dict(row)), 201


def _compute_weights(market):
    """Shared weight computation used by both ai_draft and market_weights endpoint."""
    prices = market.get("prices", {})
    tokens = []
    for cg_id, meta in TOKEN_MAP.items():
        p = prices.get(cg_id, {})
        price_change_7d = p.get("usd_7d_change", 0) or 0
        vol_24h = p.get("usd_24h_vol", 0) or 0
        tokens.append({
            "cg_id": cg_id,
            "character": meta["name"],
            "symbol": meta["symbol"],
            "pair": meta["pair"],
            "price_usd": p.get("usd", 0),
            "price_change_7d_pct": round(price_change_7d, 4),
            "vol_24h_usd": round(vol_24h, 2),
        })

    max_vol = max((t["vol_24h_usd"] for t in tokens), default=1) or 1
    max_move = max((abs(t["price_change_7d_pct"]) for t in tokens), default=1) or 1

    for t in tokens:
        vol_score = t["vol_24h_usd"] / max_vol
        move_score = abs(t["price_change_7d_pct"]) / max_move
        t["weight"] = round(0.65 * vol_score + 0.35 * move_score, 4)

    tokens.sort(key=lambda x: x["weight"], reverse=True)

    focal = [tokens[0]]
    if len(tokens) > 1:
        diff = abs(tokens[0]["price_change_7d_pct"] - tokens[1]["price_change_7d_pct"])
        if diff <= 1.0:
            focal.append(tokens[1])

    events = []
    for t in tokens:
        if t["vol_24h_usd"] < 1000:
            events.append({"type": "termination_risk", "character": t["character"], "symbol": t["symbol"]})
        if abs(t["price_change_7d_pct"]) >= 12:
            events.append({"type": "isolated_move", "character": t["character"], "symbol": t["symbol"],
                           "move_pct": t["price_change_7d_pct"]})

    all_moves = [t["price_change_7d_pct"] for t in tokens]
    if all(m > 5 for m in all_moves):
        events.append({"type": "market_surge_all"})
    elif all(m < -5 for m in all_moves):
        events.append({"type": "market_crash_all"})

    return {"ranked": tokens, "focal": focal, "events": events}


# ---------------------------------------------------------------------------
# Chapters — create (manual override, kept for edge cases)
# ---------------------------------------------------------------------------
@app.route("/chapters", methods=["POST"])
@require_auth
def create_chapter():
    print("--- POST /chapters called ---")
    data = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()
    body = data.get("body", "").strip()
    image_prompt = data.get("image_prompt", "").strip()
    prediction_cta = data.get("prediction_cta", "").strip()

    if not title:
        print("[ERROR] /chapters — missing title")
        return jsonify({"error": "title is required"}), 400
    if not body:
        print("[ERROR] /chapters — missing body")
        return jsonify({"error": "body is required"}), 400

    chapter_id = str(uuid.uuid4())
    ts = now_iso()
    conn = get_db()
    conn.execute(
        """INSERT INTO chapters (id, title, body, image_prompt, prediction_cta, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (chapter_id, title, body, image_prompt, prediction_cta, ts, ts),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] /chapters created id={chapter_id}")
    return jsonify(dict(row)), 201


# ---------------------------------------------------------------------------
# Chapters — get single
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>", methods=["GET"])
@require_auth
def get_chapter(chapter_id):
    print(f"--- GET /chapters/{chapter_id} called ---")
    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    if not row:
        print(f"[ERROR] /chapters/{chapter_id} — not found")
        return jsonify({"error": "Chapter not found"}), 404
    print(f"[SUCCESS] /chapters/{chapter_id} returned")
    return jsonify(dict(row))


# ---------------------------------------------------------------------------
# Chapters — update (title/body/image_prompt/prediction_cta)
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>", methods=["PATCH"])
@require_auth
def update_chapter(chapter_id):
    print(f"--- PATCH /chapters/{chapter_id} called ---")
    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Chapter not found"}), 404
    data = request.get_json(silent=True) or {}
    fields = {}
    for key in ("title", "body", "image_prompt", "prediction_cta"):
        if key in data:
            fields[key] = data[key]
    if not fields:
        conn.close()
        return jsonify({"error": "No fields to update"}), 400
    fields["updated_at"] = now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [chapter_id]
    conn.execute(f"UPDATE chapters SET {set_clause} WHERE id = ?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] PATCH /chapters/{chapter_id}")
    return jsonify(dict(row))


# ---------------------------------------------------------------------------
# Generate — image + story formatting
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>/generate", methods=["POST"])
@require_auth
def generate_chapter(chapter_id):
    print(f"--- POST /chapters/{chapter_id}/generate called ---")
    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        print(f"[ERROR] generate — chapter {chapter_id} not found")
        return jsonify({"error": "Chapter not found"}), 404
    row = dict(row)
    conn.close()

    # 1. Generate image
    print(f"[generate] Generating image for chapter {chapter_id}")
    image_url = _generate_image(row.get("image_prompt") or row["title"])
    if isinstance(image_url, tuple):  # error tuple
        return image_url

    # 2. Format story
    print(f"[generate] Formatting story for chapter {chapter_id}")
    body_formatted, sha = _format_story(row["body"])
    if body_formatted is None:
        return jsonify({"error": sha}), 500

    # 3. Save
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "UPDATE chapters SET image_url=?, body_formatted=?, sha256=?, status='pending_review', updated_at=? WHERE id=?",
        (image_url, body_formatted, sha, ts, chapter_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] generate complete for {chapter_id}")
    return jsonify(dict(row))


def _generate_image(prompt):
    """Generate image with gpt-image-1, upload to Drive, return Drive alternateLink."""
    if not OPENAI_API_KEY:
        print("[ERROR] _generate_image — OPENAI_API_KEY not set")
        return jsonify({"error": "OPENAI_API_KEY not configured"}), 500

    print(f"[_generate_image] Calling OpenAI with prompt: {prompt[:80]}...")
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-image-1",
            "prompt": prompt,
            "n": 1,
            "size": "1024x1536",
            "quality": "high",
            "output_format": "png",
        },
        timeout=120,
    )
    if resp.status_code != 200:
        print(f"[ERROR] OpenAI image gen failed: {resp.status_code} {resp.text[:200]}")
        return jsonify({"error": f"OpenAI image gen failed: {resp.status_code}"}), 500

    resp_json = resp.json()
    # gpt-image-1 returns base64 by default
    b64_data = resp_json["data"][0].get("b64_json")
    if not b64_data:
        # Try URL fallback
        image_download_url = resp_json["data"][0].get("url")
        if image_download_url:
            img_bytes = requests.get(image_download_url, timeout=60).content
        else:
            print("[ERROR] No image data in OpenAI response")
            return jsonify({"error": "No image data in OpenAI response"}), 500
    else:
        import base64
        img_bytes = base64.b64decode(b64_data)

    print("[_generate_image] Image generated, uploading to Drive...")
    drive_url = _upload_to_drive(img_bytes, "Chapter illustration.png", "image/png")
    if drive_url is None:
        return jsonify({"error": "Drive upload failed"}), 500
    print(f"[_generate_image] Drive URL: {drive_url}")
    return drive_url


def _upload_to_drive(file_bytes, filename, mime_type):
    """Upload bytes to Google Drive using service account, return alternateLink."""
    if not GOOGLE_SA_JSON:
        print("[ERROR] _upload_to_drive — GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return None

    try:
        import google.oauth2.service_account as sa
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaInMemoryUpload

        creds_dict = json.loads(GOOGLE_SA_JSON)
        creds = sa.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
        )
        drive = build("drive", "v3", credentials=creds)

        media = MediaInMemoryUpload(file_bytes, mimetype=mime_type, resumable=False)
        meta = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
        file_obj = drive.files().create(body=meta, media_body=media, fields="id,webContentLink,webViewLink").execute()

        # Make publicly readable
        drive.permissions().create(fileId=file_obj["id"], body={"role": "reader", "type": "anyone"}).execute()

        alternate_link = f"https://drive.google.com/file/d/{file_obj['id']}/view"
        print(f"[_upload_to_drive] Uploaded: {alternate_link}")
        return alternate_link
    except Exception as e:
        print(f"[ERROR] _upload_to_drive exception: {e}")
        return None


def _format_story(body_raw):
    """Run GPT-4.1 formatter on raw body. Returns (body_formatted, sha256) or (None, error_str)."""
    if not OPENAI_API_KEY:
        return None, "OPENAI_API_KEY not configured"

    # Compute sha256 of raw body first (pipeline appends it)
    sha = hashlib.sha256(body_raw.encode()).hexdigest()

    prompt = f"""Format the following story for an email. Use HTML line breaks only.

STORY:
{body_raw}

Output ONLY this format, nothing else:

[Story text exactly as provided]

Want your project featured in an upcoming Deep Tales chapter? Message @yotzhaviver on X and let's talk!

SHA-256: {sha}

No markdown, no code blocks, no JSON. Plain text only."""

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000,
            "temperature": 0.7,
            "top_p": 0.9,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"[ERROR] GPT-4.1 format failed: {resp.status_code} {resp.text[:200]}")
        return None, f"GPT-4.1 failed: {resp.status_code}"

    formatted = resp.json()["choices"][0]["message"]["content"].strip()
    print(f"[_format_story] Formatted {len(formatted)} chars, sha={sha[:12]}...")
    return formatted, sha


# ---------------------------------------------------------------------------
# Review (status → pending_review)
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>/review", methods=["POST"])
@require_auth
def submit_review(chapter_id):
    print(f"--- POST /chapters/{chapter_id}/review called ---")
    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        print(f"[ERROR] review — {chapter_id} not found")
        return jsonify({"error": "Chapter not found"}), 404
    conn.execute(
        "UPDATE chapters SET status='pending_review', updated_at=? WHERE id=?",
        (now_iso(), chapter_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] review — {chapter_id} → pending_review")
    return jsonify(dict(row))


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>/approve", methods=["POST"])
@require_auth
def approve_chapter(chapter_id):
    print(f"--- POST /chapters/{chapter_id}/approve called ---")
    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        print(f"[ERROR] approve — {chapter_id} not found")
        return jsonify({"error": "Chapter not found"}), 404
    conn.execute(
        "UPDATE chapters SET status='approved', updated_at=? WHERE id=?",
        (now_iso(), chapter_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] approve — {chapter_id} → approved")
    return jsonify(dict(row))


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>/reject", methods=["POST"])
@require_auth
def reject_chapter(chapter_id):
    print(f"--- POST /chapters/{chapter_id}/reject called ---")
    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "").strip()
    print(f"[reject] chapter_id={chapter_id}, notes={notes[:80] if notes else '(none)'}")
    if not notes:
        print(f"[ERROR] reject — missing notes for {chapter_id}")
        return jsonify({"error": "notes are required for rejection"}), 400
    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        print(f"[ERROR] reject — {chapter_id} not found")
        return jsonify({"error": "Chapter not found"}), 404
    conn.execute(
        "UPDATE chapters SET status='revision_requested', revision_notes=?, updated_at=? WHERE id=?",
        (notes, now_iso(), chapter_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] reject — {chapter_id} → revision_requested")
    return jsonify(dict(row))


# ---------------------------------------------------------------------------
# Revise (Gemini)
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>/revise", methods=["POST"])
@require_auth
def revise_chapter(chapter_id):
    print(f"--- POST /chapters/{chapter_id}/revise called ---")
    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "").strip()
    print(f"[revise] chapter_id={chapter_id}, notes={notes[:80] if notes else '(none)'}")

    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        print(f"[ERROR] revise — {chapter_id} not found")
        return jsonify({"error": "Chapter not found"}), 404

    row = dict(row)
    original_body = row["body"]
    effective_notes = notes or row.get("revision_notes") or ""

    if not effective_notes:
        conn.close()
        print(f"[ERROR] revise — no notes available for {chapter_id}")
        return jsonify({"error": "revision notes are required"}), 400

    if not GOOGLE_API_KEY:
        conn.close()
        print("[ERROR] revise — GOOGLE_API_KEY not set")
        return jsonify({"error": "GOOGLE_API_KEY not configured"}), 500

    print(f"[revise] Calling Gemini for {chapter_id}...")
    revised_body = _gemini_revise(original_body, effective_notes)
    if revised_body is None:
        conn.close()
        return jsonify({"error": "Gemini revision failed"}), 500

    ts = now_iso()
    # Save revision history
    conn.execute(
        "INSERT INTO revision_history (chapter_id, body_before, body_after, notes, revised_at) VALUES (?, ?, ?, ?, ?)",
        (chapter_id, original_body, revised_body, effective_notes, ts),
    )
    # Update chapter: new body, clear formatting (needs re-generate), back to pending_review
    conn.execute(
        "UPDATE chapters SET body=?, body_formatted=NULL, sha256=NULL, image_url=NULL, status='draft', revision_notes=?, updated_at=? WHERE id=?",
        (revised_body, effective_notes, ts, chapter_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] revise — {chapter_id} revised, status=draft (needs re-generate)")
    return jsonify(dict(row))


def _gemini_revise(original_body, notes):
    """Use Gemini flash-latest to apply revision notes to story body."""
    prompt = (
        f"You are editing a short fictional story (200-300 words). "
        f"Apply the following revision notes to the story.\n\n"
        f"REVISION NOTES:\n{notes}\n\n"
        f"ORIGINAL STORY (paragraphs separated by ' ||| '):\n{original_body}\n\n"
        f"Return ONLY the revised story text, paragraphs separated by ' ||| '. "
        f"No markdown, no explanations, no em dashes."
    )
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash-latest:generateContent?key={GOOGLE_API_KEY}"
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        resp = requests.post(url, json=body, timeout=60)
        if resp.status_code != 200:
            print(f"[ERROR] Gemini failed: {resp.status_code} {resp.text[:200]}")
            return None
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        print(f"[_gemini_revise] Revised: {len(text)} chars")
        return text
    except Exception as e:
        print(f"[ERROR] _gemini_revise exception: {e}")
        return None


# ---------------------------------------------------------------------------
# POST to Substack (hard publish action)
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>/post", methods=["POST"])
@require_auth
def post_chapter(chapter_id):
    print(f"--- POST /chapters/{chapter_id}/post called ---")
    conn = get_db()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        print(f"[ERROR] post — {chapter_id} not found")
        return jsonify({"error": "Chapter not found"}), 404

    row = dict(row)
    print(f"[post] chapter={chapter_id}, status={row['status']}, title={row['title']}")

    if row["status"] == "published":
        conn.close()
        return jsonify({"error": "Already published", "substack_post_id": row.get("substack_post_id")}), 409

    body_html = row.get("body_formatted") or row["body"].replace(" ||| ", "<br><br>")
    image_url = row.get("image_url") or ""

    post_id = _publish_to_substack(row["title"], body_html, image_url)
    if post_id is None:
        conn.close()
        return jsonify({"error": "Substack publish failed — check logs"}), 500

    ts = now_iso()
    conn.execute(
        "UPDATE chapters SET status='published', substack_post_id=?, updated_at=? WHERE id=?",
        (post_id, ts, chapter_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    print(f"[SUCCESS] post — {chapter_id} published, substack_post_id={post_id}")
    return jsonify(dict(row))


def _publish_to_substack(title, body_html, image_url):
    """Publish directly to Substack. Returns post_id string or None."""
    if not SUBSTACK_COOKIE:
        print("[ERROR] _publish_to_substack — SUBSTACK_COOKIE_STRING not set")
        return None

    headers = {
        "Cookie": SUBSTACK_COOKIE,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://substack.com",
    }

    # Step 1: Create draft
    print("[_publish_to_substack] Creating draft...")
    draft_payload = {
        "type": "newsletter",
        "draft_title": title,
        "draft_body": body_html,
        "draft_subtitle": "",
        "audience": "everyone",
    }
    r = requests.post(
        "https://substack.com/api/v1/drafts",
        headers=headers,
        json=draft_payload,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"[ERROR] Substack draft create failed: {r.status_code} {r.text[:300]}")
        return None

    draft = r.json()
    post_id = draft.get("id")
    print(f"[_publish_to_substack] Draft created id={post_id}")

    # Step 2: Attach cover image if available
    if image_url:
        print(f"[_publish_to_substack] Attaching cover image: {image_url}")
        cover_payload = {"url": image_url}
        r2 = requests.post(
            f"https://substack.com/api/v1/drafts/{post_id}/cover_image",
            headers=headers,
            json=cover_payload,
            timeout=30,
        )
        if r2.status_code not in (200, 201):
            print(f"[WARN] Cover image attach failed: {r2.status_code} — continuing without it")

    # Step 3: Publish
    print(f"[_publish_to_substack] Publishing draft {post_id}...")
    pub_payload = {"send": True, "share_automatically": False}
    r3 = requests.post(
        f"https://substack.com/api/v1/drafts/{post_id}/publish",
        headers=headers,
        json=pub_payload,
        timeout=30,
    )
    if r3.status_code not in (200, 201):
        print(f"[ERROR] Substack publish failed: {r3.status_code} {r3.text[:300]}")
        return None

    print(f"[SUCCESS] _publish_to_substack — post_id={post_id}")
    return str(post_id)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
@app.route("/analytics", methods=["GET"])
@require_auth
def analytics():
    print("--- GET /analytics called ---")
    if not SUBSTACK_COOKIE:
        return jsonify({"error": "SUBSTACK_COOKIE_STRING not configured"}), 500

    headers = {
        "Cookie": SUBSTACK_COOKIE,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    try:
        # Get publication info
        r = requests.get("https://substack.com/api/v1/user/profile", headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] analytics — profile fetch failed: {r.status_code}")
            return jsonify({"error": f"Substack API error: {r.status_code}"}), 500

        profile = r.json()
        pub = profile.get("primaryPublication") or {}
        pub_id = pub.get("id") or pub.get("subdomain")

        stats = {}
        if pub_id:
            r2 = requests.get(
                f"https://substack.com/api/v1/publication/{pub_id}/posts?limit=10",
                headers=headers,
                timeout=15,
            )
            if r2.status_code == 200:
                stats["recent_posts"] = r2.json().get("posts", [])

        print(f"[SUCCESS] analytics returned, pub_id={pub_id}")
        return jsonify({
            "publication": pub,
            "stats": stats,
        })
    except Exception as e:
        print(f"[ERROR] analytics exception: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Market data — weights
# ---------------------------------------------------------------------------


@app.route("/market/prices", methods=["GET"])
@require_auth
def market_prices():
    print("--- GET /market/prices called ---")
    data = _fetch_market_data()
    if data is None:
        return jsonify({"error": "CoinGecko fetch failed"}), 500
    return jsonify(data)


@app.route("/market/weights", methods=["GET"])
@require_auth
def market_weights():
    print("--- GET /market/weights called ---")
    data = _fetch_market_data()
    if data is None:
        return jsonify({"error": "CoinGecko fetch failed"}), 500

    result = _compute_weights(data)
    print(f"[SUCCESS] /market/weights — focal={[f['character'] for f in result['focal']]}, events={len(result['events'])}")
    return jsonify({
        **result,
        "cache_age_seconds": int(time.time() - _market_cache["ts"]),
        "updated_at": datetime.fromtimestamp(_market_cache["ts"], tz=timezone.utc).isoformat() if _market_cache["ts"] else None,
    })


def _fetch_market_data():
    """Fetch from CoinGecko with 5-minute cache."""
    now = time.time()
    if _market_cache["data"] and (now - _market_cache["ts"]) < CACHE_TTL:
        return _market_cache["data"]

    print("[_fetch_market_data] Fetching from CoinGecko...")
    try:
        url = (
            f"{COINGECKO_BASE}/simple/price"
            f"?ids={COINGECKO_IDS}"
            f"&vs_currencies=usd"
            f"&include_7d_change=true"
            f"&include_24h_vol=true"
        )
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] CoinGecko failed: {r.status_code}")
            return _market_cache["data"]  # return stale if available

        raw = r.json()
        prices = {}
        for cg_id in TOKEN_MAP:
            entry = raw.get(cg_id, {})
            prices[cg_id] = {
                "usd": entry.get("usd", 0),
                "usd_7d_change": entry.get("usd_7d_change", 0),
                "usd_24h_vol": entry.get("usd_24h_vol", 0),
            }

        _market_cache["data"] = {"prices": prices}
        _market_cache["ts"] = now
        print("[_fetch_market_data] Cache updated.")
        return _market_cache["data"]
    except Exception as e:
        print(f"[ERROR] _fetch_market_data exception: {e}")
        return _market_cache["data"]


# ---------------------------------------------------------------------------
# Revision history
# ---------------------------------------------------------------------------
@app.route("/chapters/<chapter_id>/revisions", methods=["GET"])
@require_auth
def get_revisions(chapter_id):
    print(f"--- GET /chapters/{chapter_id}/revisions called ---")
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM revision_history WHERE chapter_id = ? ORDER BY revised_at DESC",
        (chapter_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Startup — init DB on module load so it works under gunicorn too
# ---------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[STARTUP] Deep Tales Control Room starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
