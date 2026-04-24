import os
import uuid
import sqlite3
import logging
import logging.handlers
import secrets
import time
import io
import wave
import struct
import threading
import asyncio
import json as json_lib
from datetime import datetime, timedelta, date
from typing import Optional
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import shutil


def setup_logger(name: str, filepath: str, max_bytes: int, backup_count: int) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(filepath, maxBytes=max_bytes, backupCount=backup_count)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    logger.addHandler(sh)
    return logger


os.makedirs("/app/logs", exist_ok=True)
access_logger = setup_logger("sqs.access", "/app/logs/access.log", 5 * 1024 * 1024, 3)
transcribe_logger = setup_logger("sqs.transcribe", "/app/logs/transcribe.log", 10 * 1024 * 1024, 5)
error_logger = setup_logger("sqs.error", "/app/logs/error.log", 5 * 1024 * 1024, 3)

app = FastAPI(title="SQS Signal", version="2.0.0")


@app.on_event("startup")
async def start_cleanup_task():
    asyncio.create_task(cleanup_old_uploads())


async def cleanup_old_uploads():
    """Periodically delete files in UPLOAD_DIR older than 1 hour."""
    import time as _t
    while True:
        try:
            now = _t.time()
            one_hour = 3600
            for entry in os.listdir(UPLOAD_DIR):
                path = os.path.join(UPLOAD_DIR, entry)
                try:
                    if os.path.isfile(path) and now - os.path.getmtime(path) > one_hour:
                        os.unlink(path)
                    elif os.path.isdir(path) and now - os.path.getmtime(path) > one_hour:
                        import shutil
                        shutil.rmtree(path, ignore_errors=True)
                except:
                    pass
        except:
            pass
        await asyncio.sleep(600)  # every 10 minutes

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WHISPER_SERVICE_URL = os.getenv("WHISPER_SERVICE_URL", "http://whisper-service:8000")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_CALLBACK_URL = os.getenv("GITHUB_CALLBACK_URL", "https://sqs.chat/auth/github/callback")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "100"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
SESSION_COOKIE_NAME = "sqs_session"
SESSION_MAX_AGE_REMEMBER = 60 * 60 * 24 * 30
SESSION_MAX_AGE_SHORT = 60 * 60 * 8
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "False").lower() in ("true", "1", "yes")
COOKIE_SAMESITE = "lax"
MIC_DAILY_LIMIT_SECONDS = 15 * 60

UPLOAD_DIR = "/app/uploads"
DATA_DIR = "/app/data"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "sqs.db")

transcription_executor = ThreadPoolExecutor(max_workers=2)
live_sessions: dict = {}
live_sessions_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            user_id TEXT,
            username TEXT,
            avatar_url TEXT,
            auth_method TEXT DEFAULT 'pat',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            remember INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS github_users (
            id INTEGER PRIMARY KEY,
            login TEXT,
            avatar_url TEXT,
            name TEXT,
            email TEXT,
            first_seen TEXT DEFAULT (datetime('now')),
            last_login TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS mic_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            usage_date TEXT NOT NULL,
            seconds_used REAL DEFAULT 0,
            UNIQUE(user_id, usage_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            user_id TEXT,
            username TEXT,
            action TEXT,
            detail TEXT,
            ip_address TEXT
        )
    """)
    # Migration: rename old sessions.pat_id -> user_id, add missing columns
    try:
        c.execute("SELECT pat_id FROM sessions LIMIT 1")
        has_old_schema = True
    except Exception:
        has_old_schema = False
    if has_old_schema:
        c.execute("PRAGMA table_info(sessions)")
        cols = {r[1] for r in c.fetchall()}
        if "pat_id" in cols and "user_id" not in cols:
            c.execute("ALTER TABLE sessions RENAME TO sessions_old")
            c.execute("""
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    user_id TEXT,
                    username TEXT,
                    avatar_url TEXT,
                    auth_method TEXT DEFAULT 'pat',
                    created_at TEXT DEFAULT (datetime('now')),
                    expires_at TEXT NOT NULL,
                    remember INTEGER DEFAULT 0
                )
            """)
            c.execute("""INSERT INTO sessions (session_id, user_id, created_at, expires_at, remember)
                         SELECT session_id, pat_id, created_at, expires_at, remember FROM sessions_old""")
            c.execute("DROP TABLE sessions_old")
    conn.commit()
    conn.close()


def create_session(user_id=None, username=None, avatar_url=None, auth_method="github", remember=False):
    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(seconds=SESSION_MAX_AGE_REMEMBER if remember else SESSION_MAX_AGE_SHORT)
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO sessions (session_id, user_id, username, avatar_url, auth_method, expires_at, remember)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (session_id, user_id, username, avatar_url, auth_method, expires_at.isoformat(), 1 if remember else 0))
    conn.commit()
    conn.close()
    return session_id, expires_at


def validate_session(session_id):
    if not session_id:
        return None
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT * FROM sessions
                 WHERE session_id=? AND expires_at > datetime('now')""", (session_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def make_cookie_value(session_id, remember=False):
    max_age = SESSION_MAX_AGE_REMEMBER if remember else SESSION_MAX_AGE_SHORT
    expires = int(time.time()) + max_age
    return f"{session_id}|{expires}"


def verify_cookie_value(value):
    try:
        session_id, ts = value.split("|")
        if time.time() > int(ts):
            return None
        return session_id
    except:
        return None


def get_github_oauth_url(state):
    params = urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_CALLBACK_URL,
        "scope": "read:user",
        "state": state,
    })
    return f"https://github.com/login/oauth/authorize?{params}"


async def exchange_github_code(code):
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="GitHub token exchange failed")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=401, detail="GitHub OAuth failed - no access_token")
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="GitHub user fetch failed")
        return user_resp.json()


def upsert_github_user(gh_user):
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO github_users (id, login, avatar_url, name, email, first_seen, last_login)
                 VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                 ON CONFLICT(id) DO UPDATE SET
                     login=excluded.login,
                     avatar_url=excluded.avatar_url,
                     name=excluded.name,
                     email=excluded.email,
                     last_login=datetime('now')""",
              (gh_user["id"], gh_user.get("login"), gh_user.get("avatar_url"),
               gh_user.get("name"), gh_user.get("email")))
    conn.commit()
    conn.close()
    return {
        "user_id": str(gh_user["id"]),
        "username": gh_user.get("login", ""),
        "avatar_url": gh_user.get("avatar_url", ""),
    }


def get_github_user_info(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM github_users WHERE id=?", (int(user_id) if user_id and user_id.isdigit() else -1,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def log_access(user_id, username, action, detail="", ip=""):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO access_log (user_id, username, action, detail, ip_address)
                     VALUES (?, ?, ?, ?, ?)""",
                  (user_id, username, action, detail, ip))
        conn.commit()
        conn.close()
        access_logger.info(f"user={username} action={action} detail={detail} ip={ip}")
    except Exception as e:
        error_logger.error(f"log_access failed: {e}")


def check_mic_quota(user_id, additional_seconds=0):
    today = date.today().isoformat()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT seconds_used FROM mic_usage WHERE user_id=? AND usage_date=?", (user_id, today))
    row = c.fetchone()
    conn.close()
    used = row["seconds_used"] if row else 0
    return (used + additional_seconds) <= MIC_DAILY_LIMIT_SECONDS


def get_mic_usage(user_id):
    today = date.today().isoformat()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT seconds_used FROM mic_usage WHERE user_id=? AND usage_date=?", (user_id, today))
    row = c.fetchone()
    conn.close()
    used = row["seconds_used"] if row else 0
    remaining = max(0, MIC_DAILY_LIMIT_SECONDS - used)
    return {"used": used, "remaining": remaining, "limit": MIC_DAILY_LIMIT_SECONDS}


def record_mic_usage(user_id, seconds):
    today = date.today().isoformat()
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO mic_usage (user_id, usage_date, seconds_used)
                 VALUES (?, ?, ?)
                 ON CONFLICT(user_id, usage_date) DO UPDATE SET
                     seconds_used = seconds_used + excluded.seconds_used""",
              (user_id, today, seconds))
    conn.commit()
    conn.close()


async def get_current_user(request: Request):
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val:
        session_id = verify_cookie_value(cookie_val)
        if session_id:
            session = validate_session(session_id)
            if session:
                return {
                    "user_id": session.get("user_id", ""),
                    "username": session.get("username", ""),
                    "avatar_url": session.get("avatar_url", ""),
                    "auth_method": session.get("auth_method", "github"),
                }
    raise HTTPException(status_code=401, detail="Unauthorized")


async def optional_user(request: Request):
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


init_db()

@app.get("/")
async def index(user: dict = Depends(optional_user)):
    if user and user.get("username"):
        page = LANDING_PAGE_SIGNED_IN.replace("__USERNAME__", user["username"])
        page = page.replace("__AVATAR_URL__", user.get("avatar_url", ""))
        return HTMLResponse(page)
    return HTMLResponse(LANDING_PAGE_SIGNED_OUT)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "2.0.0",
        "github_oauth_configured": bool(GITHUB_CLIENT_ID),
    }


@app.get("/signin")
async def signin(request: Request):
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val:
        session_id = verify_cookie_value(cookie_val)
        if session_id and validate_session(session_id):
            return RedirectResponse(url="/app", status_code=303)
    return HTMLResponse(SIGNIN_PAGE)





@app.get("/auth/github")
async def auth_github(request: Request):
    state = secrets.token_urlsafe(16)
    oauth_url = get_github_oauth_url(state)
    response = RedirectResponse(url=oauth_url, status_code=303)
    response.set_cookie(key="oauth_state", value=state, path="/", max_age=300, httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)
    return response


@app.get("/auth/github/callback")
async def auth_github_callback(request: Request, code: str, state: str):
    stored_state = request.cookies.get("oauth_state", "")
    if not stored_state or stored_state != state:
        return HTMLResponse(SIGNIN_PAGE.replace(
            'class="back-link"',
            'class="error" id="error-msg" style="display:block">State mismatch. Try again.</p><div class="back-link"'
        ))
    gh_user = await exchange_github_code(code)
    user_info = upsert_github_user(gh_user)
    session_id, expires_at = create_session(
        user_id=user_info["user_id"], username=user_info["username"],
        avatar_url=user_info["avatar_url"], auth_method="github", remember=True
    )
    max_age = SESSION_MAX_AGE_REMEMBER
    cookie_val = make_cookie_value(session_id, True)
    response = RedirectResponse(url="/app", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME, value=cookie_val, path="/",
        max_age=max_age, httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE
    )
    response.delete_cookie(key="oauth_state", path="/")
    ip = request.client.host if request.client else ""
    log_access(user_info["user_id"], user_info["username"], "signin", "github oauth", ip)
    return response


@app.get("/auth/github/status")
async def auth_github_status(user: dict = Depends(optional_user)):
    if not user:
        return {"authenticated": False}
    used = get_mic_usage(user["user_id"]) if user.get("user_id") else 0
    return {
        "authenticated": True,
        "user_id": user.get("user_id", ""),
        "username": user.get("username", ""),
        "avatar_url": user.get("avatar_url", ""),
        "auth_method": user.get("auth_method", ""),
        "mic_used_seconds": used,
        "mic_remaining_seconds": max(0, MIC_DAILY_LIMIT_SECONDS - used),
        "mic_limit_seconds": MIC_DAILY_LIMIT_SECONDS,
    }


@app.post("/signout")
async def signout(response: Response):
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/app")
async def app_page(request: Request):
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val:
        session_id = verify_cookie_value(cookie_val)
        if session_id:
            session = validate_session(session_id)
            if session:
                user_id = session.get("user_id", "")
                username = session.get("username", "")
                avatar_url = session.get("avatar_url", "")
                auth_method = session.get("auth_method", "pat")
                mic = get_mic_usage(user_id) if user_id else {"used": 0, "remaining": 0, "limit": MIC_DAILY_LIMIT_SECONDS}
                remaining_min = int(mic["remaining"] / 60)
                limit_min = int(mic["limit"] / 60)
                page = APP_PAGE_TEMPLATE
                page = page.replace("__USERNAME__", username)
                page = page.replace("__AVATAR_URL__", avatar_url)
                page = page.replace("__AUTH_METHOD__", auth_method)
                page = page.replace("__MIC_REMAINING__", str(remaining_min))
                page = page.replace("__MIC_LIMIT__", str(limit_min))
                page = page.replace("__MIC_REMAINING_SEC__", str(int(mic["remaining"])))
                page = page.replace("__MIC_LIMIT_SEC__", str(int(mic["limit"])))
                return HTMLResponse(page, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return RedirectResponse(url="/signin", status_code=303)


class LiveTranscriber:
    def __init__(self, ws: WebSocket, language: str):
        self.ws = ws
        self.language = language
        self.audio_buffer = bytearray()
        self.lock = threading.Lock()
        self.transcribing = False
        self.stopped = False
        self.last_transcript = ""
        self.chunk_count = 0
        self.max_buffer_sec = 30
        self.pcm_rate = 16000
        self.session_id = str(uuid.uuid4())
        self.tmp_dir = os.path.join(UPLOAD_DIR, f"live_{self.session_id}")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.current_task = None
        self.flush_window_sec = 2
        self.stop_window_sec = 4
        self.last_transcribe_time = 0

    def add_chunk(self, data: bytes):
        with self.lock:
            self.audio_buffer.extend(data)
            # Cap buffer at 60 seconds to avoid OOM
            max_bytes = 60 * self.pcm_rate * 2
            if len(self.audio_buffer) > max_bytes:
                del self.audio_buffer[:len(self.audio_buffer) - max_bytes]
            self.chunk_count += 1

    def get_buffer(self) -> bytes:
        with self.lock:
            return bytes(self.audio_buffer)

    def get_windowed_buffer(self, window_seconds: float) -> bytes:
        with self.lock:
            total_bytes = len(self.audio_buffer)
            window_bytes = int(window_seconds * self.pcm_rate * 2)
            if total_bytes <= window_bytes:
                return bytes(self.audio_buffer)
            return bytes(self.audio_buffer[-window_bytes:])

    def is_transcribing(self) -> bool:
        with self.lock:
            return self.transcribing

    def set_transcribing(self, val: bool):
        with self.lock:
            self.transcribing = val

    def is_stopped(self) -> bool:
        with self.lock:
            return self.stopped

    def mark_stopped(self):
        with self.lock:
            self.stopped = True

    def cleanup(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


def pcm_to_wav_file(pcm_data: bytes, output_path: str, sample_rate: int = 16000):
    with wave.open(output_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_data)


def decode_webm_to_wav(webm_data: bytes, output_path: str) -> bool:
    import subprocess
    tmp_webm = output_path + ".tmp.webm"
    try:
        with open(tmp_webm, "wb") as f:
            f.write(webm_data)
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_webm, "-ar", "16000", "-ac", "1",
             "-sample_fmt", "s16", "-f", "wav", output_path],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            print(f"[FFmpeg] Error decoding WebM: {result.stderr.decode()[:200]}", flush=True)
            return False
        # Validate the output WAV has actual audio content
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
            print(f"[FFmpeg] Decoded WAV too small or missing: {os.path.getsize(output_path) if os.path.exists(output_path) else 0} bytes", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[FFmpeg] Exception decoding WebM: {e}", flush=True)
        return False
    finally:
        if os.path.exists(tmp_webm):
            os.unlink(tmp_webm)


async def transcribe_buffer(transcriber: LiveTranscriber, window_seconds: float = None):
    import time as _time
    transcribe_start = _time.time()

    if window_seconds is None:
        window_seconds = transcriber.stop_window_sec if transcriber.is_stopped() else transcriber.flush_window_sec

    audio = transcriber.get_windowed_buffer(window_seconds) if window_seconds > 0 else transcriber.get_buffer()
    total_buffer_len = len(transcriber.get_buffer())
    window_len = len(audio)
    is_stopped = transcriber.is_stopped()

    approx_window_duration = window_len / (transcriber.pcm_rate * 2) if window_len > 0 else 0.0
    approx_total_duration = total_buffer_len / (transcriber.pcm_rate * 2) if total_buffer_len > 0 else 0.0

    # Guardrail: reject empty or tiny audio
    if len(audio) < 100:
        print(f"[WS:{transcriber.session_id[:8]}] REJECT: audio too small ({len(audio)} bytes)", flush=True)
        if is_stopped:
            try:
                await transcriber.ws.send_json({"type": "error", "message": "Recording too short — try holding the mic button a bit longer."})
            except:
                pass
        transcriber.set_transcribing(False)
        return

    # Energy check for live (not stopped) — skip silent buffers
    if not is_stopped and len(audio) >= 2:
        try:
            samples = struct.unpack(f"<{len(audio)//2}h", audio[:len(audio)//2*2])
            max_amplitude = max(abs(s) for s in samples) if samples else 0
            if max_amplitude < 1000:
                print(f"[WS:{transcriber.session_id[:8]}] REJECT: audio too quiet (max_amp={max_amplitude})", flush=True)
                transcriber.set_transcribing(False)
                return
        except Exception as e:
            print(f"[WS:{transcriber.session_id[:8]}] Energy check failed: {e}", flush=True)

    if not is_stopped and len(audio) < 8000:
        print(f"[WS:{transcriber.session_id[:8]}] REJECT: buffer too small for live: {len(audio)} bytes", flush=True)
        transcriber.set_transcribing(False)
        return

    tmp_wav = os.path.join(transcriber.tmp_dir, f"chunk_{uuid.uuid4()}.wav")
    decoded_ok = False
    try:
        decoded_ok = decode_webm_to_wav(audio, tmp_wav)
        if not decoded_ok:
            print(f"[WS:{transcriber.session_id[:8]}] WebM decode failed, falling back to raw PCM", flush=True)
            pcm_to_wav_file(audio, tmp_wav)
    except Exception as e:
        print(f"[WS:{transcriber.session_id[:8]}] Write audio failed: {e}", flush=True)
        transcriber.set_transcribing(False)
        return

    # Validate decoded WAV file has actual audio frames, not just header
    try:
        decoded_size = os.path.getsize(tmp_wav)
        print(f"[WS:{transcriber.session_id[:8]}] Decoded WAV: {decoded_size} bytes (raw input: {len(audio)} bytes)", flush=True)
        if decoded_size < 100:
            print(f"[WS:{transcriber.session_id[:8]}] REJECT: decoded WAV too small ({decoded_size} bytes)", flush=True)
            if is_stopped:
                try:
                    await transcriber.ws.send_json({"type": "error", "message": "Recording too short — try holding the mic button a bit longer."})
                except:
                    pass
            transcriber.set_transcribing(False)
            return
    except Exception as e:
        print(f"[WS:{transcriber.session_id[:8]}] WAV validation failed: {e}", flush=True)
        transcriber.set_transcribing(False)
        return

    # Silence and duration check on the decoded PCM audio
    # Opens the WAV, reads PCM samples, measures amplitude and duration
    try:
        with wave.open(tmp_wav, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            framerate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            nchannels = wf.getnchannels()
            nframes = wf.getnframes()
            decoded_dur = nframes / framerate if framerate > 0 else 0
            if len(frames) >= sampwidth * nchannels:
                samples = struct.unpack(f"<{len(frames)//sampwidth//nchannels * nchannels}h", frames[:len(frames)//sampwidth//nchannels * sampwidth * nchannels])
                if nchannels > 1:
                    samples = samples[0::nchannels]
                max_amp = max(abs(s) for s in samples) if samples else 0
                rms = (sum(s*s for s in samples) / len(samples)) ** 0.5 if samples else 0
                pct_above_50 = sum(1 for s in samples if abs(s) > 50) / len(samples) * 100 if samples else 0
                print(f"[WS:{transcriber.session_id[:8]}] WAV check: {decoded_size} bytes, {nframes} frames, {framerate}Hz, {sampwidth*8}bit, ch={nchannels}, dur={decoded_dur:.2f}s, max_amp={max_amp}, rms={rms:.1f}, pct>50={pct_above_50:.1f}%", flush=True)
                # Save failing WAV for debugging (keep last 5)
                if is_stopped and max_amp < 200:
                    debug_wav_dir = "/tmp/sqs_debug_wav"
                    try:
                        os.makedirs(debug_wav_dir, exist_ok=True)
                        import shutil
                        debug_path = os.path.join(debug_wav_dir, f"rejected_{transcriber.session_id[:8]}_{int(_time.time())}.wav")
                        shutil.copy2(tmp_wav, debug_path)
                        # Clean old debug files
                        for f in sorted(os.listdir(debug_wav_dir), key=lambda f: os.path.getmtime(os.path.join(debug_wav_dir, f))):
                            if len(os.listdir(debug_wav_dir)) > 5:
                                os.unlink(os.path.join(debug_wav_dir, f))
                    except:
                        pass
                # Silence detection: reject only if ALL signals are near zero
                # True silence: max_amp < 20, rms < 3, effectively no audio
                # Accept anything with sustained non-zero energy, especially for longer clips
                true_silence = max_amp < 20 and rms < 3
                borderline_with_duration = decoded_dur >= 1.0 and pct_above_50 >= 1.0
                is_silence = true_silence and not borderline_with_duration
                if is_silence:
                    print(f"[WS:{transcriber.session_id[:8]}] REJECT: silence (max_amp={max_amp}, rms={rms:.1f}, pct>50={pct_above_50:.1f}%)", flush=True)
                    transcriber.set_transcribing(False)
                    try:
                        await transcriber.ws.send_json({"type": "error", "message": "No speech detected — try speaking closer to your mic."})
                    except:
                        pass
                    return
                if decoded_dur < 0.5:
                    print(f"[WS:{transcriber.session_id[:8]}] REJECT: too short ({decoded_dur:.2f}s)", flush=True)
                    transcriber.set_transcribing(False)
                    try:
                        await transcriber.ws.send_json({"type": "error", "message": "Recording too short — try holding the mic button a bit longer."})
                    except:
                        pass
                    return
    except Exception as e:
        print(f"[WS:{transcriber.session_id[:8]}] WAV amplitude check failed: {e}", flush=True)

    try:
        print(f"[WS:{transcriber.session_id[:8]}] Calling Whisper ({decoded_size} bytes WAV)", flush=True)
        async with httpx.AsyncClient(timeout=30.0) as client:
            with open(tmp_wav, "rb") as f:
                form_data = {"file": ("audio.wav", f, "audio/wav")}
                data = {
                    "language": transcriber.language if transcriber.language != "auto" else "en",
                    "model_name": "tiny.en"
                }
                resp = await client.post(f"{WHISPER_SERVICE_URL}/transcribe", files=form_data, data=data)

        print(f"[WS:{transcriber.session_id[:8]}] Whisper status {resp.status_code}", flush=True)
        if resp.status_code != 200:
            print(f"[WS:{transcriber.session_id[:8]}] Whisper error: {resp.text[:200]}", flush=True)
            transcriber.set_transcribing(False)
            return

        result = resp.json()
        text = result.get("text", "").strip()
        is_stopped = transcriber.is_stopped()

        original_text = text
        if not is_stopped:
            common_hallucinations = [
                "thank you", "thanks for watching", "please like and subscribe",
                "thank you for watching", "thanks for listening", "please subscribe",
                "hello", "hi", "hey", "welcome", "goodbye", "bye",
                "okay", "ok", "yes", "no", "maybe", "well",
                "so", "um", "uh", "ah", "oh", "hmm"
            ]
            if len(text) < 25:
                text_lower = text.lower().strip()
                for hallucination in common_hallucinations:
                    if (text_lower == hallucination or
                        text_lower.startswith(hallucination + " ") or
                        (hallucination in text_lower and len(text_lower.split()) <= 4)):
                        print(f"[WS:{transcriber.session_id[:8]}] Filtered hallucination: '{original_text}' -> ''", flush=True)
                        text = ""
                        break

        print(f"[WS:{transcriber.session_id[:8]}] Whisper: '{text}' ({'final' if is_stopped else 'partial'})", flush=True)

        # Post-transcription check: reject blank/whitespace-only results
        if is_stopped and not text.strip():
            print(f"[WS:{transcriber.session_id[:8]}] REJECT: empty transcript", flush=True)
            try:
                await transcriber.ws.send_json({"type": "error", "message": "No speech detected — try speaking closer to your mic."})
            except:
                pass
            transcriber.set_transcribing(False)
            import os as _os
            if _os.path.exists(tmp_wav):
                try: _os.unlink(tmp_wav)
                except: pass
            return

        msg = {
            "type": "final" if is_stopped else "partial",
            "text": text,
            "chunk": transcriber.chunk_count,
            "model": result.get("model", "small.en"),
            "language": result.get("language", transcriber.language),
        }
        try:
            await transcriber.ws.send_json(msg)
            transcriber.last_transcript = text
            elapsed = _time.time() - transcribe_start
            print(f"[WS:{transcriber.session_id[:8]}] Sent {msg['type']} ({elapsed:.1f}s)", flush=True)
        except Exception as e:
            print(f"[WS:{transcriber.session_id[:8]}] Send failed: {e}", flush=True)
    except httpx.TimeoutException:
        print(f"[WS:{transcriber.session_id[:8]}] Whisper timeout", flush=True)
        elapsed = _time.time() - transcribe_start
        safe_dur = approx_window_duration if approx_window_duration > 0 else 0.5
        print(f"[METRICS] mode=mic audio_sec={safe_dur:.1f} transcribe_sec={elapsed:.1f} realtime_ratio={elapsed/safe_dur:.2f} result_len=0 result=timeout", flush=True)
        try:
            await transcriber.ws.send_json({
                "type": "error",
                "message": "Something went wrong — please try again."
            })
        except:
            pass
        transcriber.set_transcribing(False)
        return
    except Exception as e:
        print(f"[WS:{transcriber.session_id[:8]}] Transcribe error: {e}", flush=True)
        elapsed = _time.time() - transcribe_start
        safe_dur = approx_window_duration if approx_window_duration > 0 else 0.5
        print(f"[METRICS] mode=mic audio_sec={safe_dur:.1f} transcribe_sec={elapsed:.1f} realtime_ratio={elapsed/safe_dur:.2f} result_len=0 result=error:{str(e)[:50]}", flush=True)
        try:
            await transcriber.ws.send_json({
                "type": "error",
                "message": "Something went wrong — please try again."
            })
        except:
            pass
    finally:
        if os.path.exists(tmp_wav):
            try: os.unlink(tmp_wav)
            except: pass
        transcriber.set_transcribing(False)


async def run_transcribe(transcriber: LiveTranscriber, window_seconds: float = None):
    try:
        await transcribe_buffer(transcriber, window_seconds)
    except Exception as e:
        print(f"run_transcribe error: {e}")
        transcriber.set_transcribing(False)
    return transcriber.last_transcript


@app.websocket("/ws/transcribe")
async def websocket_transcribe(websocket: WebSocket, language: str = "en"):
    cookie_val = websocket.cookies.get(SESSION_COOKIE_NAME)
    session_id = verify_cookie_value(cookie_val) if cookie_val else None
    valid = validate_session(session_id) if session_id else None
    if not valid:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    transcriber = LiveTranscriber(websocket, language)

    with live_sessions_lock:
        live_sessions[transcriber.session_id] = transcriber

    await websocket.accept()
    print(f"[WS:{transcriber.session_id[:8]}] Connect (lang={language})", flush=True)

    try:
        await websocket.send_json({"type": "started", "message": "Ready"})

        while True:
            data = await websocket.receive()
            if data["type"] == "websocket.disconnect":
                break

            if data["type"] != "websocket.receive":
                continue

            if "bytes" in data:
                transcriber.add_chunk(data["bytes"])

            elif "text" in data:
                try:
                    msg = json_lib.loads(data["text"])
                    msg_type = msg.get("type", "")
                    if msg_type == "stop":
                        import time as _t
                        stop_start = _t.time()
                        full_buffer = transcriber.get_buffer()
                        buffer_len = len(full_buffer)
                        print(f"[WS:{transcriber.session_id[:8]}] Stop ({transcriber.chunk_count} chunks, {buffer_len} raw bytes)", flush=True)

                        if buffer_len < 100:
                            print(f"[WS:{transcriber.session_id[:8]}] REJECT: buffer too small ({buffer_len} bytes)", flush=True)
                            transcriber.mark_stopped()
                            try:
                                await websocket.send_json({"type": "error", "message": "Recording too short — try holding the mic button a bit longer."})
                            except:
                                pass
                            break

                        transcriber.mark_stopped()
                        transcriber.set_transcribing(True)
                        t = LiveTranscriber(websocket, transcriber.language)
                        t.audio_buffer = transcriber.get_buffer()
                        t.chunk_count = transcriber.chunk_count
                        t.session_id = transcriber.session_id
                        t.tmp_dir = transcriber.tmp_dir
                        t.mark_stopped()
                        transcriber.last_transcribe_time = _t.time()
                        transcribe_start = _t.time()
                        result = await run_transcribe(t, 0)
                        transcribe_elapsed = _t.time() - transcribe_start
                        total_elapsed = _t.time() - stop_start
                        approx_secs = max(buffer_len / 32000.0, 0.5)
                        print(f"[WS:{transcriber.session_id[:8]}] Done ({transcribe_elapsed:.1f}s, ~{approx_secs:.1f}s audio)", flush=True)
                        print(f"[METRICS] mode=mic audio_sec={approx_secs:.1f} transcribe_sec={transcribe_elapsed:.1f} realtime_ratio={transcribe_elapsed/approx_secs:.2f} result_len={len(result) if result else 0} result=success", flush=True)

                        final_text = (result or transcriber.last_transcript or "").strip()
                        if not final_text:
                            print(f"[WS:{transcriber.session_id[:8]}] REJECT: empty transcription", flush=True)
                            try:
                                await websocket.send_json({"type": "error", "message": "No speech detected — try speaking closer to your mic."})
                            except:
                                pass
                        else:
                            await websocket.send_json({
                                "type": "done",
                                "text": final_text,
                                "model": "tiny.en",
                                "language": transcriber.language,
                            })
                        break
                    elif msg_type == "flush":
                        flush_buf = len(transcriber.get_buffer())
                        flush_secs = flush_buf / 32000.0
                        try:
                            await websocket.send_json({
                                "type": "listening",
                                "chunk": transcriber.chunk_count,
                                "buffer_seconds": flush_secs
                            })
                        except:
                            pass
                    elif msg_type == "language":
                        transcriber.language = msg.get("value", "en")
                except json_lib.JSONDecodeError as e:
                    print(f"[WS:{transcriber.session_id[:8]}] JSON decode error: {e}", flush=True)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS:{transcriber.session_id[:8]}] Error: {e}", flush=True)
        try:
            await websocket.send_json({"type": "error", "message": "Something went wrong — please try again."})
        except:
            pass
    finally:
        with live_sessions_lock:
            live_sessions.pop(transcriber.session_id, None)
        transcriber.cleanup()
        print(f"[WS:{transcriber.session_id[:8]}] Disconnected", flush=True)


@app.post("/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("en"),
    auth: dict = Depends(get_current_user)
):
    import time as _time
    start_time = _time.time()

    if not file.content_type or not (file.content_type.startswith("audio/") or file.content_type.startswith("video/")):
        raise HTTPException(status_code=400, detail="File must be audio or video")
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Max {MAX_FILE_SIZE_MB}MB")
    suffix = file.filename.split(".")[-1] if "." in file.filename else "tmp"
    tmp_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}.{suffix}")
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        file_size_kb = os.path.getsize(tmp_path) / 1024

        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(tmp_path, "rb") as f:
                form_data = {"file": (file.filename, f, file.content_type)}
                data = {"language": language if language != "auto" else "en", "model_name": "small.en"}
                resp = await client.post(
                    f"{WHISPER_SERVICE_URL}/transcribe",
                    files=form_data, data=data
                )
        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=resp.text)

        elapsed = _time.time() - start_time
        result = resp.json()
        text = result.get("text", "")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [METRICS] mode=upload file_kb={file_size_kb:.0f} transcribe_sec={elapsed:.1f} result_len={len(text)} result=success", flush=True)

        log_access(auth.get("user_id", ""), auth.get("username", ""), "transcribe", f"upload {file_size_kb:.0f}kb", request.client.host if request.client else "")

        return result
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Transcription timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.get("/favicon.ico")
async def favicon():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#6366f1"/><path d="M16 6L6 11l10 5 10-5-10-5z" fill="none" stroke="white" stroke-width="2"/><path d="M6 21l10 5 10-5" fill="none" stroke="white" stroke-width="2"/><path d="M6 16l10 5 10-5" fill="none" stroke="white" stroke-width="2"/></svg>'
    return Response(content=svg, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


LANDING_PAGE_SIGNED_OUT = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SQS Signal — Self-Hosted Open Source Dictation App | Speech to Text with Whisper</title>
    <meta name="description" content="SQS Signal is a self-hosted, open-source dictation app powered by Whisper. Turn speech into text faster than typing. Privacy-first, CPU-friendly, and free to self-host.">
    <meta property="og:title" content="SQS Signal — Self-Hosted Open Source Dictation App">
    <meta property="og:description" content="Speak your draft faster than you'd type it. Open-source speech-to-text dictation powered by Whisper, self-hosted for privacy.">
    <meta property="og:type" content="website">
    <meta property="og:url" content="https://sqs.chat">
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="SQS Signal — Self-Hosted Dictation App">
    <meta name="twitter:description" content="Open-source speech-to-text dictation powered by Whisper. Self-hosted, CPU-friendly, privacy-first.">
    <!-- Google tag (gtag.js) -->
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-7KGKH11K48"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){dataLayer.push(arguments);}
      gtag('js', new Date());
      gtag('config', 'G-7KGKH11K48');
    </script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0a; color: #e0e0e0; min-height: 100vh;
            display: flex; align-items: center; justify-content: center; padding: 2rem;
        }
        .container { width: 100%; max-width: 680px; }
        .logo { font-size: 2.5rem; font-weight: 700; margin-bottom: 0.5rem; letter-spacing: -0.02em; }
        .logo span { color: #6366f1; }
        .tagline { color: #888; font-size: 1.1rem; line-height: 1.6; margin-bottom: 2rem; }
        .section { margin-bottom: 2rem; }
        .section h2 { font-size: 1.2rem; color: #fff; margin-bottom: 0.75rem; }
        .section p, .section li { color: #a1a1aa; font-size: 0.9rem; line-height: 1.6; }
        .section ul { list-style: none; padding: 0; }
        .section ul li::before { content: "> "; color: #6366f1; }
        .section ul li { margin-bottom: 0.4rem; }
        .arch-box {
            background: #141414; border: 1px solid #222; border-radius: 12px; padding: 1.25rem;
            margin-bottom: 1rem;
        }
        .arch-box h3 { color: #fff; font-size: 1rem; margin-bottom: 0.5rem; }
        .arch-box p, .arch-box code { color: #a1a1aa; font-size: 0.85rem; }
        .arch-box code {
            background: #1a1a1a; padding: 0.2rem 0.4rem; border-radius: 4px;
            font-family: 'SF Mono', 'Fira Code', monospace; color: #6366f1;
        }
        .arch-flow {
            display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
            margin-top: 0.75rem; font-size: 0.8rem;
        }
        .arch-flow .node {
            background: #1a1a1a; border: 1px solid #333; border-radius: 8px;
            padding: 0.5rem 0.75rem; color: #e0e0e0;
        }
        .arch-flow .arrow { color: #52525b; font-size: 1.2rem; }
        .arch-flow .port { color: #6366f1; font-size: 0.7rem; }
        .cta-group { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin: 1.5rem 0; }
        .cta-btn {
            display: inline-flex; align-items: center; gap: 0.5rem;
            padding: 0.875rem 2rem; background: #6366f1; color: #fff;
            border-radius: 8px; font-weight: 600; text-decoration: none;
            transition: background 0.2s; border: none; cursor: pointer; font-size: 1rem;
        }
        .cta-btn:hover { background: #4f46e5; }
        .cta-btn svg { width: 20px; height: 20px; }
        .support { margin-top: 1.5rem; padding: 1rem; background: #141414; border: 1px solid #222; border-radius: 8px; }
        .support p { color: #71717a; font-size: 0.85rem; }
        .footer { margin-top: 2rem; color: #444; font-size: 0.8rem; }
        .footer a { color: #6366f1; text-decoration: none; }
        .footer a:hover { text-decoration: underline; }
        .oss-note { color: #52525b; font-size: 0.8rem; margin-top: 1.5rem; line-height: 1.5; }
        .oss-note a { color: #6366f1; text-decoration: none; }
        .oss-note a:hover { text-decoration: underline; }
        .hero-line { font-size: 1.3rem; font-weight: 600; color: #e0e0e0; margin-bottom: 1rem; line-height: 1.5; }
        .badge-row { display: flex; gap: 0.5rem; justify-content: center; flex-wrap: wrap; margin: 1rem 0; }
        .badge { font-size: 0.75rem; padding: 0.3rem 0.75rem; border-radius: 999px; border: 1px solid #27272a; color: #a1a1aa; background: #111; }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">SQS <span>Signal</span></div>
        <p class="tagline">self-hosted dictation &middot; open source &middot; powered by Whisper</p>
        <div class="hero-line">Speak your draft faster than you'd type it.</div>

        <div class="section">
            <p style="color:#a1a1aa;font-size:0.9rem;line-height:1.6;margin-bottom:1rem;">
                SQS Signal is an <strong>open-source dictation app</strong> that turns speech into text using
                <a href="https://github.com/openai/whisper" style="color:#6366f1;">OpenAI Whisper</a>.
                Self-host it for full privacy and control — no cloud dependency, no data leakage.
            </p>
            <div class="badge-row">
                <span class="badge">open source</span>
                <span class="badge">self-hosted</span>
                <span class="badge">speech-to-text</span>
                <span class="badge">Whisper</span>
            </div>
        </div>

        <div class="section">
            <h2>Why dictation?</h2>
            <p>
                Speaking is often faster than typing — 2-3x faster for rough drafts, meeting notes,
                code comments, and brain-dumping ideas. SQS Signal gives you a simple, private way
                to do that on your own hardware.
            </p>
        </div>

        <div class="section">
            <h2>Architecture</h2>
            <div class="arch-box">
                <h3>Three-container stack</h3>
                <div class="arch-flow">
                    <div class="node">Browser</div>
                    <span class="arrow">→</span>
                    <div class="node">Caddy<br><span class="port">:80/:443</span></div>
                    <span class="arrow">→</span>
                    <div class="node">Web App (FastAPI)<br><span class="port">:8000</span></div>
                    <span class="arrow">→</span>
                    <div class="node">Whisper Service<br><span class="port">:8000</span></div>
                </div>
                <p style="margin-top:0.75rem;">
                    Caddy terminates TLS and reverse-proxies to the FastAPI web app.
                    The web app handles auth, sessions, file uploads, and WebSocket live transcription,
                    forwarding audio to a dedicated Whisper inference service.
                </p>
                <p style="margin-top:0.5rem;">
                    Build: <code>docker compose build web</code> &mdash;
                    the Python code is baked into the Docker image via <code>COPY . .</code>.
                </p>
            </div>
        </div>

        <div class="section">
            <p style="color:#a1a1aa;font-size:0.9rem;line-height:1.6;margin-bottom:1rem;">
                Sign in with GitHub to start dictating. Choose your mode:
            </p>
            <ul>
                <li><strong>Mic mode</strong> — Record short clips (5-20s ideal) for quick CPU notes</li>
                <li><strong>Upload mode</strong> — Longer or higher-quality audio (up to 50 MB) using the better <code>small.en</code> model</li>
            </ul>
            <ul style="margin-top:0.75rem;">
                <li>15-minute daily mic quota per user; upload is unlimited</li>
                <li>Language selection (11 languages + auto-detect)</li>
                <li>Anti-hallucination filtering for short clips</li>
            </ul>
        </div>

        <div class="cta-group">
            <a href="/auth/github" class="cta-btn">
                <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
                Sign in with GitHub
            </a>
        </div>
        <div class="oss-note">
            <p style="color:#52525b;font-size:0.8rem;margin:1rem 0 0.5rem;">
                Open-source stack:
                <a href="https://github.com/openai/whisper" style="color:#6366f1;">OpenAI Whisper</a> &middot;
                <a href="https://github.com/ggml-org/whisper.cpp" style="color:#6366f1;">whisper.cpp</a> compat &middot;
                <a href="https://github.com/SYSTRAN/faster-whisper" style="color:#6366f1;">faster-whisper</a> compat &middot;
                <a href="https://github.com/fastapi/fastapi" style="color:#6366f1;">FastAPI</a> &middot;
                <a href="https://github.com/caddyserver/caddy" style="color:#6366f1;">Caddy</a>
            </p>
        </div>
        <div class="support">
            <p>Support development:</p>
            <p style="margin-top:0.5rem;"><a href="https://ko-fi.com/sidequeststudios" target="_blank" style="display:inline-flex;align-items:center;gap:0.5rem;padding:0.5rem 1rem;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e0e0e0;text-decoration:none;font-size:0.85rem;transition:all 0.15s;" onmouseover="this.style.borderColor='#6366f1'" onmouseout="this.style.borderColor='#3f3f46'">☕ Buy me a Ko-fi</a></p>
        </div>

        <div class="footer">
            <a href="https://github.com/rasrobo/sqs.chat">github.com/rasrobo/sqs.chat</a>
            &nbsp;&middot;&nbsp;
            <a href="https://sidequeststudios.xyz">sidequeststudios.xyz</a>
            &nbsp;&middot;&nbsp;
            <a href="https://ko-fi.com/sidequeststudios">Ko-fi</a>
        </div>
    </div>
</body>
</html>"""


LANDING_PAGE_SIGNED_IN = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SQS Signal — Self-Hosted Open Source Dictation App | Speech to Text with Whisper</title>
    <meta name="description" content="Live dictation using Whisper speech-to-text. SQS Signal is an open-source, self-hosted dictation app for turning speech into text.">
    <meta property="og:title" content="SQS Signal — Self-Hosted Open Source Dictation App">
    <meta property="og:description" content="Open-source self-hosted dictation. Speak your draft faster than you'd type it.">
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="SQS Signal — Self-Hosted Dictation App">
    <meta name="twitter:description" content="Open-source speech-to-text dictation powered by Whisper. Self-hosted, CPU-friendly, privacy-first.">
    <!-- Google tag (gtag.js) -->
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-7KGKH11K48"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){dataLayer.push(arguments);}
      gtag('js', new Date());
      gtag('config', 'G-7KGKH11K48');
    </script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0a; color: #e0e0e0; min-height: 100vh;
            display: flex; align-items: center; justify-content: center; padding: 2rem;
        }
        .container { width: 100%; max-width: 680px; }
        .logo { font-size: 2.5rem; font-weight: 700; margin-bottom: 0.5rem; letter-spacing: -0.02em; }
        .logo span { color: #6366f1; }
        .tagline { color: #888; font-size: 1.1rem; line-height: 1.6; margin-bottom: 2rem; }
        .user-card {
            display: flex; align-items: center; gap: 0.75rem;
            background: #141414; border: 1px solid #222; border-radius: 12px;
            padding: 1rem 1.25rem; margin-bottom: 1.5rem;
        }
        .avatar {
            width: 40px; height: 40px; border-radius: 50%;
            background: #27272a; flex-shrink: 0; overflow: hidden;
        }
        .avatar img { width: 100%; height: 100%; object-fit: cover; }
        .user-info { flex: 1; }
        .user-name { font-weight: 600; color: #fff; font-size: 0.95rem; }
        .user-method { font-size: 0.75rem; color: #52525b; }
        .cta-group { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin: 1rem 0; }
        .cta-btn {
            display: inline-flex; align-items: center; gap: 0.5rem;
            padding: 0.875rem 2rem; background: #6366f1; color: #fff;
            border-radius: 8px; font-weight: 600; text-decoration: none;
            transition: background 0.2s; border: none; cursor: pointer; font-size: 1rem;
        }
        .cta-btn:hover { background: #4f46e5; }
        .cta-btn.secondary {
            background: transparent; border: 1px solid #3f3f46; color: #a1a1aa;
        }
        .cta-btn.secondary:hover { background: #18181b; color: #fff; }
        .section { margin-bottom: 2rem; }
        .section h2 { font-size: 1.2rem; color: #fff; margin-bottom: 0.75rem; }
        .section p, .section li { color: #a1a1aa; font-size: 0.9rem; line-height: 1.6; }
        .section ul { list-style: none; padding: 0; }
        .section ul li::before { content: "> "; color: #6366f1; }
        .section ul li { margin-bottom: 0.4rem; }
        .support { margin-top: 1.5rem; padding: 1rem; background: #141414; border: 1px solid #222; border-radius: 8px; }
        .support p { color: #71717a; font-size: 0.85rem; }
        .footer { margin-top: 2rem; color: #444; font-size: 0.8rem; }
        .footer a { color: #6366f1; text-decoration: none; }
        .footer a:hover { text-decoration: underline; }
        .signout-link { color: #52525b; font-size: 0.85rem; margin-left: auto; }
        .signout-link a { color: #ef4444; text-decoration: none; font-size: 0.8rem; }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">SQS <span>Signal</span></div>
        <div class="user-card">
            <div class="avatar">
                <img src="__AVATAR_URL__" alt="" onerror="this.style.display='none'">
            </div>
            <div class="user-info">
                <div class="user-name">__USERNAME__</div>
                <div class="user-method">Signed in</div>
            </div>
            <div class="signout-link"><a href="/signout">Sign out</a></div>
        </div>
        <div class="cta-group">
            <a href="/app" class="cta-btn">Go to App</a>
        </div>
        <div class="section">
            <h2>Transcription Modes</h2>
            <ul>
                <li><strong>Mic (record then transcribe)</strong> — Quick CPU notes, 5-20s ideal. 15 min/day quota.</li>
                <li><strong>File upload</strong> — Longer audio/video files (up to 50 MB). Better quality model. Unlimited.</li>
                <li><strong>Language selection</strong> — 11 languages supported</li>
            </ul>
        </div>
        <div class="support">
            <p>Support development:</p>
            <p style="margin-top:0.5rem;"><a href="https://ko-fi.com/sidequeststudios" target="_blank" style="display:inline-flex;align-items:center;gap:0.5rem;padding:0.5rem 1rem;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e0e0e0;text-decoration:none;font-size:0.85rem;">☕ Buy me a Ko-fi</a></p>
        </div>
        <div class="footer">
            <a href="https://github.com/rasrobo/sqs.chat">github.com/rasrobo/sqs.chat</a>
            &nbsp;&middot;&nbsp;
            <a href="https://sidequeststudios.xyz">sidequeststudios.xyz</a>
            &nbsp;&middot;&nbsp;
            <a href="https://ko-fi.com/sidequeststudios">Ko-fi</a>
        </div>
    </div>
</body>
</html>"""



SIGNIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign in — SQS Signal Dictation | Self-Hosted Speech to Text</title>
    <meta name="description" content="Sign in to SQS Signal, the self-hosted open-source dictation app powered by Whisper speech-to-text.">
    <meta property="og:title" content="SQS Signal — Self-Hosted Open Source Dictation App">
    <meta property="og:description" content="Open-source self-hosted dictation. Sign in to start turning speech into text.">
    <!-- Google tag (gtag.js) -->
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-7KGKH11K48"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){dataLayer.push(arguments);}
      gtag('js', new Date());
      gtag('config', 'G-7KGKH11K48');
    </script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 2rem; }
        .card { background: #141414; border: 1px solid #222; border-radius: 16px; padding: 2.5rem; width: 100%; max-width: 420px; }
        h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
        .subtitle { color: #666; margin-bottom: 2rem; font-size: 0.9rem; }
        .divider { display: flex; align-items: center; gap: 1rem; margin: 1.5rem 0; color: #3f3f46; font-size: 0.8rem; }
        .divider::before, .divider::after { content: ""; flex: 1; height: 1px; background: #27272a; }
        .github-btn { display: flex; align-items: center; justify-content: center; gap: 0.5rem; width: 100%; padding: 0.875rem; border-radius: 8px; border: 1px solid #3f3f46; background: #1a1a1a; color: #fff; font-weight: 600; cursor: pointer; font-size: 1rem; text-decoration: none; transition: all 0.2s; }
        .github-btn:hover { background: #27272a; border-color: #6366f1; }
        .github-btn svg { width: 20px; height: 20px; }
        .error { color: #ef4444; font-size: 0.875rem; margin-top: 1rem; display: none; }
        .back-link { text-align: center; margin-top: 1.5rem; }
        .back-link a { color: #52525b; text-decoration: none; font-size: 0.85rem; }
        .back-link a:hover { color: #6366f1; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Sign in</h1>
        <p class="subtitle">to SQS Signal dictation service</p>

        <a href="/auth/github" class="github-btn">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
            Sign in with GitHub
        </a>

        <div class="back-link"><a href="/">&larr; Back to home</a></div>
    </div>
</body>
</html>"""

APP_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SQS Signal — Live Dictation | Self-Hosted Speech to Text</title>
    <meta name="description" content="Live dictation using Whisper speech-to-text. SQS Signal is an open-source, self-hosted dictation app for turning speech into text.">
    <meta property="og:title" content="SQS Signal — Live Dictation App">
    <meta property="og:description" content="Open-source self-hosted dictation. Speak your draft faster than you'd type it.">
    <meta name="twitter:title" content="SQS Signal — Live Dictation">
    <meta name="twitter:description" content="Self-hosted open-source dictation powered by Whisper speech-to-text.">
    <!-- Google tag (gtag.js) -->
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-7KGKH11K48"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){dataLayer.push(arguments);}
      gtag('js', new Date());
      gtag('config', 'G-7KGKH11K48');
    </script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; }
        body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #09090b; color: #e4e4e7; min-height: 100vh; display: flex; align-items: center; justify-content: center; overflow: hidden; }
        #bg-canvas { position: fixed; inset: 0; z-index: 0; pointer-events: none; opacity: 0.4; }
        .app-shell { position: relative; z-index: 1; width: 100%; max-width: 940px; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; height: 100vh; max-height: 800px; overflow-y: auto; }
        .app-shell::-webkit-scrollbar { width: 4px; }
        .app-shell::-webkit-scrollbar-thumb { background: #27272a; border-radius: 2px; }
        @media (max-width: 640px) { .app-shell { padding: 0.875rem; } }
        .topbar { display: flex; justify-content: space-between; align-items: center; padding-bottom: 0.75rem; border-bottom: 1px solid #27272a; flex-shrink: 0; }
        .brand { display: flex; align-items: center; gap: 0.75rem; }
        .brand-icon { width: 28px; height: 28px; border-radius: 6px; background: #27272a; display: flex; align-items: center; justify-content: center; }
        .brand-icon svg { width: 16px; height: 16px; }
        .brand-name { font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; }
        .signout-btn { padding: 0.5rem 1rem; border-radius: 6px; border: 1px solid #3f3f46; background: transparent; color: #a1a1aa; cursor: pointer; font-size: 0.8125rem; font-weight: 500; transition: all 0.15s; }
        .signout-btn:hover { background: #27272a; color: #e4e4e7; border-color: #52525b; }
        .main-area { display: flex; flex-direction: column; gap: 0.875rem; }
        .lang-row { display: flex; gap: 0.75rem; align-items: center; }
        .lang-label { font-size: 0.8125rem; color: #71717a; white-space: nowrap; }
        .lang-select { flex: 1; max-width: 200px; padding: 0.5rem 0.75rem; border-radius: 6px; border: 1px solid #27272a; background: #18181b; color: #e4e4e7; font-size: 0.8125rem; cursor: pointer; appearance: none; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2371717a' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 0.75rem center; padding-right: 2rem; }
        .lang-select:focus { outline: none; border-color: #6366f1; }
        .input-row { display: flex; gap: 0.75rem; align-items: stretch; }
        .drop-zone { flex: 1; border: 1px dashed #27272a; border-radius: 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.75rem; padding: 1.5rem 2rem; cursor: pointer; transition: all 0.15s; min-height: 140px; }
        .drop-zone:hover, .drop-zone.drag-over { border-color: #6366f1; background: rgba(99,102,241,0.05); }
        .drop-zone input { display: none; }
        .drop-zone-icon { width: 36px; height: 36px; border-radius: 8px; background: #18181b; display: flex; align-items: center; justify-content: center; }
        .drop-zone-icon svg { width: 18px; height: 18px; stroke: #71717a; }
        .drop-zone-label { font-size: 0.875rem; color: #71717a; text-align: center; line-height: 1.4; }
        .drop-zone-label strong { color: #a1a1aa; }
        .drop-zone-hint { font-size: 0.75rem; color: #52525b; }
        .mic-btn { width: 100px; min-width: 100px; border-radius: 12px; border: 1px dashed #27272a; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.4rem; cursor: pointer; transition: all 0.15s; padding: 0.875rem; background: #18181b; color: #71717a; flex-shrink: 0; }
        .mic-btn:hover { border-color: #6366f1; color: #a1a1aa; background: rgba(99,102,241,0.05); }
        .mic-btn.recording { border-color: #ef4444; border-style: solid; background: rgba(239,68,68,0.1); color: #ef4444; animation: mic-pulse 2s infinite; }
        .mic-btn.requesting { border-color: #6366f1; border-style: solid; background: rgba(99,102,241,0.05); color: #6366f1; }
        .mic-btn.processing { border-color: #f59e0b; border-style: solid; background: rgba(245,158,11,0.05); color: #f59e0b; }
        .mic-btn.success { border-color: #22c55e; border-style: solid; background: rgba(34,197,94,0.05); color: #22c55e; }
        .mic-btn.compact { padding: 0.375rem 0.75rem; border-radius: 6px; font-size: 0.75rem; border-color: #3f3f46; color: #a1a1aa; }
        .mic-btn.compact:hover { transform: scale(1.05); transition: transform 0.2s ease; border-color: #6366f1; color: #a1a1aa; }
        .mic-btn.error { border-color: #ef4444; border-style: solid; background: rgba(239,68,68,0.05); color: #ef4444; }
        @keyframes mic-pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(239,68,68,0.4); } 50% { box-shadow: 0 0 0 8px rgba(239,68,68,0); } }
        .mic-icon { width: 32px; height: 32px; display: flex; align-items: center; justify-content: center; }
        .mic-icon svg { width: 22px; height: 22px; }
        .mic-label { font-size: 0.6875rem; font-weight: 500; text-align: center; line-height: 1.3; }
        .mic-error { background: #18181b; border: 1px solid #27272a; border-radius: 8px; padding: 0.75rem 1rem; font-size: 0.8125rem; color: #ef4444; display: none; }
        .mic-error.visible { display: block; }
        .mic-activity-panel { background: #111113; border: 1px solid #27272a; border-radius: 10px; padding: 0.875rem 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
        .mic-activity-header { display: flex; justify-content: space-between; align-items: center; }
        .mic-activity-title { font-size: 0.6875rem; color: #52525b; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em; }
        .mic-state-label { font-size: 0.6875rem; color: #71717a; font-weight: 500; padding: 2px 8px; border-radius: 4px; background: #18181b; border: 1px solid #27272a; }
        .mic-state-label.speech { color: #22c55e; border-color: #166534; background: #052e16; }
        .mic-state-label.silence { color: #f59e0b; border-color: #92400e; background: #1c1408; }
        .mic-state-label.countdown { color: #ef4444; border-color: #991b1b; background: #1c0a0a; }
        .mic-state-label.streaming { color: #6366f1; border-color: #3730a3; background: #0d0d1a; }
        .mic-meter-row { display: flex; gap: 3px; align-items: flex-end; height: 28px; }
        .mic-meter-bar { flex: 1; background: #27272a; border-radius: 2px; min-width: 4px; height: 4px; transition: height 0.05s ease-out, background 0.1s; }
        .mic-meter-bar.active { background: #6366f1; }
        .mic-meter-bar.speech { background: #22c55e; }
        .mic-meter-bar.loud { background: #f59e0b; }
        .mic-meter-bar.clip { background: #ef4444; }
        .mic-level-text { font-size: 0.6875rem; color: #52525b; font-family: 'SF Mono', 'Fira Code', monospace; }
        .live-transcript-panel { background: #111113; border: 1px solid #27272a; border-radius: 10px; padding: 0.875rem 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
        .live-transcript-header { display: flex; justify-content: space-between; align-items: center; }
        .live-transcript-title { font-size: 0.6875rem; color: #52525b; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em; }
        .live-indicator { display: flex; align-items: center; gap: 0.375rem; font-size: 0.625rem; color: #ef4444; font-weight: 500; }
        .live-dot { width: 6px; height: 6px; border-radius: 50%; background: #ef4444; animation: live-pulse 0.8s infinite; }
        @keyframes live-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.2; } }
        .caption-line { background: #0c0c0e; border-radius: 6px; padding: 0.875rem; font-size: 1rem; line-height: 1.4; color: #ffffff; font-family: 'Inter', -apple-system, sans-serif; min-height: 48px; display: flex; align-items: center; margin-bottom: 0.5rem; border: 1px solid #27272a; }
        .caption-text { flex: 1; }
        .caption-cursor { color: #6366f1; animation: cursor-blink 1s infinite; margin-left: 2px; }
        @keyframes cursor-blink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }
        .live-text { background: #0c0c0e; border-radius: 6px; padding: 0.875rem; font-size: 0.8125rem; line-height: 1.65; color: #d4d4d8; font-family: 'SF Mono', 'Fira Code', monospace; min-height: 64px; max-height: 160px; overflow-y: auto; }
        .live-text::-webkit-scrollbar { width: 5px; }
        .live-text::-webkit-scrollbar-thumb { background: #27272a; border-radius: 3px; }
        .transcript-segment { margin-bottom: 0.5rem; padding-bottom: 0.5rem; border-bottom: 1px solid #27272a; }
        .transcript-segment:last-child { margin-bottom: 0; padding-bottom: 0; border-bottom: none; }
        .live-text.empty { color: #3f3f46; font-style: italic; }
        .live-meta { font-size: 0.625rem; color: #52525b; font-family: 'SF Mono', 'Fira Code', monospace; min-height: 1em; }
        .result-panel { background: #111113; border: 1px solid #27272a; border-radius: 10px; padding: 1rem; display: none; flex-direction: column; gap: 0.5rem; }
        .result-panel.visible { display: flex; }
        .result-header { display: flex; justify-content: space-between; align-items: center; }
        .result-label { font-size: 0.6875rem; color: #52525b; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em; }
        .result-meta { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
        .result-meta-badge { font-size: 0.625rem; padding: 2px 6px; border-radius: 4px; background: #18181b; border: 1px solid #27272a; color: #71717a; font-family: 'SF Mono', 'Fira Code', monospace; }
        .result-actions { display: flex; gap: 0.375rem; }
        .icon-btn { width: 26px; height: 26px; border-radius: 6px; border: 1px solid #27272a; background: #18181b; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.15s; }
        .icon-btn:hover { background: #27272a; }
        .icon-btn svg { width: 13px; height: 13px; stroke: #a1a1aa; }
        .result-text { background: #0c0c0e; border-radius: 6px; padding: 0.875rem; font-size: 0.8125rem; line-height: 1.65; color: #d4d4d8; font-family: 'SF Mono', 'Fira Code', monospace; max-height: 220px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }
        .result-text::-webkit-scrollbar { width: 5px; }
        .result-text::-webkit-scrollbar-thumb { background: #27272a; border-radius: 3px; }
        .debug-panel { background: #0d0d0f; border: 1px solid #1f1f23; border-radius: 10px; padding: 0.75rem 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
        .debug-header { display: flex; justify-content: space-between; align-items: center; }
        .debug-title { font-size: 0.6875rem; color: #52525b; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em; }
        .debug-actions { display: flex; gap: 0.375rem; }
        .debug-btn { padding: 2px 8px; border-radius: 4px; border: 1px solid #27272a; background: #18181b; color: #71717a; cursor: pointer; font-size: 0.625rem; font-weight: 500; transition: all 0.15s; font-family: inherit; }
        .debug-btn:hover { background: #27272a; color: #a1a1aa; }
        .debug-log { background: #0a0a0c; border-radius: 6px; padding: 0.5rem 0.75rem; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.6875rem; line-height: 1.6; color: #a1a1aa; max-height: 120px; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; }
        .debug-log::-webkit-scrollbar { width: 4px; }
        .debug-log::-webkit-scrollbar-thumb { background: #27272a; border-radius: 2px; }
        .debug-entry { display: flex; gap: 0.5rem; }
        .debug-time { color: #52525b; flex-shrink: 0; }
        .debug-msg { color: #d4d4d8; word-break: break-word; }
        .debug-msg.info { color: #6366f1; }
        .debug-msg.warn { color: #f59e0b; }
        .debug-msg.error { color: #ef4444; }
        .debug-msg.success { color: #22c55e; }
        .debug-empty { color: #3f3f46; font-style: italic; padding: 0.25rem 0; }
        .status-dock { position: fixed; bottom: 1.5rem; right: 1.5rem; z-index: 100; background: #18181b; border: 1px solid #27272a; border-radius: 10px; padding: 0.75rem 1rem; display: none; align-items: center; gap: 0.75rem; box-shadow: 0 8px 32px rgba(0,0,0,0.5); min-width: 240px; }
        .status-dock.visible { display: flex; }
        .status-dock-inner { flex: 1; }
        .status-dock-title { font-size: 0.75rem; color: #a1a1aa; font-weight: 500; }
        .status-dock-sub { font-size: 0.6875rem; color: #52525b; margin-top: 2px; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
        .status-dot.idle { background: #52525b; }
        .status-dot.active { background: #6366f1; animation: pulse 1.5s infinite; }
        .status-dot.recording { background: #ef4444; animation: pulse 0.8s infinite; }
        .status-dot.done { background: #22c55e; }
        .status-dot.error { background: #ef4444; }
        .status-dot.streaming { background: #f59e0b; animation: pulse 0.6s infinite; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
        .toast { position: fixed; bottom: 1.5rem; left: 50%; transform: translateX(-50%); background: #18181b; border: 1px solid #27272a; border-radius: 8px; padding: 0.625rem 1rem; font-size: 0.8125rem; color: #a1a1aa; opacity: 0; transition: opacity 0.2s; pointer-events: none; z-index: 200; }
        .toast.show { opacity: 1; }
        @media (max-width: 640px) { .input-row { flex-direction: column; } .mic-btn { width: 100%; min-width: 0; flex-direction: row; padding: 0.75rem 1.5rem; } .mic-icon { width: auto; height: auto; } .status-dock { bottom: 1rem; right: 1rem; left: 1rem; min-width: 0; } .result-panel { margin-bottom: 80px; } }
        @media (prefers-reduced-motion: reduce) { .status-dot.active, .status-dot.recording, .status-dot.streaming, .mic-btn.recording, .live-dot { animation: none; } .mic-meter-bar { transition: none; } #bg-canvas { display: none; } }
        .help-btn {
            width: 28px; height: 28px; border-radius: 6px; border: 1px solid #3f3f46;
            background: transparent; color: #a1a1aa; cursor: pointer; font-size: 0.875rem;
            font-weight: 600; display: flex; align-items: center; justify-content: center;
            transition: all 0.15s;
        }
        .help-btn:hover { background: #27272a; color: #e4e4e7; border-color: #6366f1; }
        .help-overlay {
            position: fixed; inset: 0; z-index: 999; background: rgba(0,0,0,0.6);
            display: none; align-items: center; justify-content: center; padding: 1rem;
        }
        .help-overlay.visible { display: flex; }
        .help-modal {
            background: #18181b; border: 1px solid #27272a; border-radius: 16px;
            padding: 1.5rem; max-width: 480px; width: 100%; max-height: 80vh; overflow-y: auto;
        }
        .help-modal h2 { font-size: 1.1rem; color: #fff; margin-bottom: 1rem; }
        .help-modal h3 { font-size: 0.9rem; color: #e0e0e0; margin: 1rem 0 0.5rem; }
        .help-modal p, .help-modal li { font-size: 0.8125rem; color: #a1a1aa; line-height: 1.6; }
        .help-modal ul { list-style: none; padding: 0; }
        .help-modal ul li::before { content: "> "; color: #6366f1; }
        .help-modal ul li { margin-bottom: 0.35rem; }
        .help-modal .close-btn {
            float: right; width: 28px; height: 28px; border-radius: 6px;
            border: 1px solid #3f3f46; background: transparent; color: #a1a1aa;
            cursor: pointer; font-size: 1rem; display: flex; align-items: center;
            justify-content: center;
        }
        .help-modal .close-btn:hover { background: #27272a; color: #fff; }
        .help-modal a { color: #6366f1; text-decoration: none; }
        .help-modal a:hover { text-decoration: underline; }
        .mic-remaining { font-size: 0.75rem; color: #71717a; padding: 0.25rem 0.5rem; border-radius: 4px; background: #18181b; border: 1px solid #27272a; white-space: nowrap; }
        .mic-remaining.low { color: #f59e0b; border-color: #92400e; background: #1c1408; }
        .mic-remaining.empty { color: #ef4444; border-color: #991b1b; background: #1c0a0a; }
</style>
</head>
<body>
    <canvas id="bg-canvas"></canvas>
    <div class="app-shell">
        <div class="topbar">
            <div class="brand">
                <div class="brand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="#6366f1" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg></div>
                <span class="brand-name">SQS Signal</span>
            </div>
            <div style="display:flex;align-items:center;gap:0.5rem;">
                <span class="mic-remaining" id="mic-remaining">__MIC_REMAINING__ / __MIC_LIMIT__ min</span>
                <button class="help-btn" onclick="toggleHelp()" title="Help">?</button>
                <button class="signout-btn" onclick="signout()">Sign out</button>
            </div>
        </div>
        <div class="main-area">
            <div class="lang-row">
                <span class="lang-label">Language</span>
                <select class="lang-select" id="language">
                    <option value="en">English</option>
                    <option value="es">Spanish</option>
                    <option value="fr">French</option>
                    <option value="de">German</option>
                    <option value="it">Italian</option>
                    <option value="pt">Portuguese</option>
                    <option value="ru">Russian</option>
                    <option value="ja">Japanese</option>
                    <option value="ko">Korean</option>
                    <option value="zh">Chinese</option>
                    <option value="auto">Auto-detect</option>
                </select>
        </div>
        <div class="input-row" id="input-row">
                <label class="drop-zone" id="drop-zone" for="audio-file">
                    <input type="file" id="audio-file" accept="audio/*,video/*">
                    <div class="drop-zone-icon"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg></div>
                    <div class="drop-zone-label"><strong>Drop audio</strong> or click to browse</div>
                    <div class="drop-zone-hint">For longer/higher-quality audio &middot; Max 100 MB</div>
                </label>
                <div class="mic-btn" id="mic-btn" role="button" tabindex="0" title="Click to record">
                    <div class="mic-icon" id="mic-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg></div>
                    <span class="mic-label" id="mic-label">__MIC_REMAINING__min remaining</span>
                </div>
            </div>
            <div class="mic-error" id="mic-error"></div>
            <div class="mic-quota-exceeded" id="mic-quota-exceeded" style="display:none;background:#1c0a0a;border:1px solid #991b1b;border-radius:8px;padding:0.75rem 1rem;font-size:0.8125rem;color:#ef4444;">
                Daily mic limit reached. Upload audio instead, or try again tomorrow.
            </div>
            <div class="mic-activity-panel" id="mic-activity-panel">
                <div class="mic-activity-header">
                    <span class="mic-activity-title">Mic Activity</span>
                    <span class="mic-state-label" id="mic-state-label">Idle</span>
                </div>
                <div class="mic-meter-row" id="mic-meter-row"></div>
                <div class="mic-level-text" id="mic-level-text">Level: --</div>
            </div>
            <div class="live-transcript-panel" id="live-transcript-panel">
                <div class="live-transcript-header">
                    <span class="live-transcript-title">Live Transcript</span>
                    <div class="live-indicator" id="live-indicator" style="display:none;"><div class="live-dot"></div><span>LIVE</span></div>
                </div>
                <div class="caption-line" id="caption-line" style="display:none;">
                    <span class="caption-text" id="caption-text"></span>
                    <span class="caption-cursor">|</span>
                </div>
                <div class="live-text empty" id="live-text"><strong>Mic:</strong> Quick dictation clips (5-20s). <strong>Upload:</strong> Longer/higher-quality audio.</div>
                <div class="live-meta" id="live-meta"></div>
            </div>
            <div class="result-panel" id="result-panel">
                <div class="result-header">
                    <div style="display:flex;align-items:center;gap:0.5rem;">
                        <span class="result-label">Final Output</span>
                        <div class="result-meta" id="result-meta"></div>
                    </div>
                    <div class="result-actions">
                        <button class="icon-btn" onclick="copyResult()" title="Copy"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
                        <button class="icon-btn" onclick="downloadResult()" title="Download"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button>
                        <button class="icon-btn" onclick="resetForm()" title="New"><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg></button>
                    </div>
                </div>
                <div class="result-text" id="result-text"></div>
            </div>
            <div class="debug-panel" id="debug-panel">
                <div class="debug-header">
                    <span class="debug-title">Debug Console</span>
                    <div class="debug-actions">
                        <button class="debug-btn" onclick="copyDebug()">Copy</button>
                        <button class="debug-btn" onclick="clearDebug()">Clear</button>
                    </div>
                </div>
                <div class="debug-log" id="debug-log"><div class="debug-empty">Waiting for activity...</div></div>
            </div>
        </div>
    </div>
    <div class="help-overlay" id="help-overlay" onclick="closeHelp(event)">
        <div class="help-modal" onclick="event.stopPropagation()">
            <button class="close-btn" onclick="closeHelp()">&times;</button>
            <h2>How to use SQS Signal</h2>

            <h3>What is SQS Signal?</h3>
            <p>A self-hosted, open-source dictation app. Speak your draft faster than you'd type it.
            Your audio stays under your control — everything runs on your own infrastructure.</p>

            <h3>Mic Mode (dictate then transcribe)</h3>
            <ul>
                <li>Click the mic button to start dictating</li>
                <li>Speak naturally — short bursts of 5-20 seconds work best on this CPU server</li>
                <li>Click stop to transcribe — the app sends your audio to Whisper</li>
                <li>You have a daily limit of <strong>15 minutes</strong> per user (shown in the top bar)</li>
                <li>When the quota runs out, the mic button is disabled. Try upload instead.</li>
                <li>Uses the <code>tiny.en</code> model for fast dictation turnaround</li>
            </ul>

            <h3>Upload Mode</h3>
            <ul>
                <li>Drag and drop or click to browse audio/video files (up to 50 MB)</li>
                <li>Uses the better <code>small.en</code> model for accurate transcription</li>
                <li>No daily quota — upload as much as you like</li>
                <li>Supports most audio/video formats via ffmpeg</li>
            </ul>

            <h3>Tips</h3>
            <ul>
                <li>Use mic for quick dictation, upload for finished recordings</li>
                <li>Anti-hallucination filtering removes common false positives from short clips</li>
                <li>Results can be copied to clipboard or downloaded as .txt</li>
                <li>The debug panel shows what the app is doing in real time</li>
            </ul>

            <h3>Open-source stack</h3>
            <ul>
                <li><a href="https://github.com/openai/whisper" target="_blank">OpenAI Whisper</a> — speech-to-text model</li>
                <li><a href="https://fastapi.tiangolo.com/" target="_blank">FastAPI</a> — Python web framework</li>
                <li><a href="https://caddyserver.com/" target="_blank">Caddy</a> — reverse proxy with automatic HTTPS</li>
            </ul>

            <h3>Support</h3>
            <p>
                If you find this project useful, consider
                <a href="https://ko-fi.com/sidequeststudios" target="_blank">supporting development on Ko-fi</a>.
                Source code: <a href="https://github.com/rasrobo/sqs.chat" target="_blank">github.com/rasrobo/sqs.chat</a>
            </p>
        </div>
    </div>
    <div class="status-dock" id="status-dock">
        <div class="status-dot" id="status-dot"></div>
        <div class="status-dock-inner">
            <div class="status-dock-title" id="status-title">Ready</div>
            <div class="status-dock-sub" id="status-sub">Awaiting input</div>
        </div>
    </div>
    <div class="toast" id="toast"></div>
    <script>
    (function() {
        var fileInput = document.getElementById('audio-file');
        var dropZone = document.getElementById('drop-zone');
        var langSelect = document.getElementById('language');
        var resultPanel = document.getElementById('result-panel');
        var resultText = document.getElementById('result-text');
        var resultMeta = document.getElementById('result-meta');
        var statusDock = document.getElementById('status-dock');
        var statusDot = document.getElementById('status-dot');
        var statusTitle = document.getElementById('status-title');
        var statusSub = document.getElementById('status-sub');
        var toast = document.getElementById('toast');
        var micBtn = document.getElementById('mic-btn');
        var micLabel = document.getElementById('mic-label');
        var micError = document.getElementById('mic-error');
        var micStateLabel = document.getElementById('mic-state-label');
        var micMeterRow = document.getElementById('mic-meter-row');
        var micLevelText = document.getElementById('mic-level-text');
        var debugLog = document.getElementById('debug-log');
        var liveText = document.getElementById('live-text');
        var liveIndicator = document.getElementById('live-indicator');
        var liveMeta = document.getElementById('live-meta');
        var captionLine = document.getElementById('caption-line');
        var captionText = document.getElementById('caption-text');
        var currentFileName = '';
        var resultTextValue = '';
        var NUM_BARS = 24;
        for (var i = 0; i < NUM_BARS; i++) { var bar = document.createElement('div'); bar.className = 'mic-meter-bar'; bar.style.height = '4px'; micMeterRow.appendChild(bar); }
        var bars = micMeterRow.querySelectorAll('.mic-meter-bar');
        var debugEntries = [];
        var MAX_DEBUG = 80;
        function debug(level, msg) {
            var ts = new Date().toISOString().substr(11, 12);
            debugEntries.push({ t: ts, l: level, m: msg });
            if (debugEntries.length > MAX_DEBUG) debugEntries.shift();
            renderDebug();
            var prefix = '[SQS Signal]';
            if (level === 'error') console.error(prefix, msg);
            else if (level === 'warn') console.warn(prefix, msg);
            else if (level === 'info') console.info(prefix, msg);
            else console.log(prefix, msg);
        }
        function renderDebug() {
            if (debugEntries.length === 0) { debugLog.innerHTML = '<div class="debug-empty">Waiting for activity...</div>'; return; }
            var html = '';
            for (var i = 0; i < debugEntries.length; i++) { var e = debugEntries[i]; var cls = e.l === 'warn' ? 'warn' : e.l === 'error' ? 'error' : e.l === 'success' ? 'success' : e.l === 'info' ? 'info' : ''; html += '<div class="debug-entry"><span class="debug-time">' + e.t + '</span><span class="debug-msg ' + cls + '">' + escapeHtml(e.m) + '</span></div>'; }
            debugLog.innerHTML = html; debugLog.scrollTop = debugLog.scrollHeight;
        }
        window.copyDebug = function() { var text = debugEntries.map(function(e) { return e.t + ' [' + e.l.toUpperCase() + '] ' + e.m; }).join('\\n'); navigator.clipboard.writeText(text || 'No debug entries.'); showToast('Debug copied'); };
        window.clearDebug = function() { debugEntries = []; renderDebug(); debug('info', 'Debug cleared'); };
        function setStatus(state, title, sub) { statusDock.classList.add('visible'); statusDot.className = 'status-dot ' + state; statusTitle.textContent = title || ''; statusSub.textContent = sub || ''; }
        function hideStatus() { statusDock.classList.remove('visible'); }
        function showToast(msg) { toast.textContent = msg; toast.classList.add('show'); setTimeout(function() { toast.classList.remove('show'); }, 2000); }
        function showMicError(msg) { micError.textContent = msg; micError.classList.add('visible'); }
        function hideMicError() { micError.classList.remove('visible'); }
        function escapeHtml(text) { if (!text) return ''; var div = document.createElement('div'); div.textContent = text; return div.innerHTML; }
        function mergeOverlap(prev, next) {
            if (!prev || !next) return next || prev;
            // Clean whitespace and punctuation for comparison
            var prevClean = prev.trim();
            var nextClean = next.trim();
            // Try to find overlap: check if next starts with the end of prev
            var minOverlap = 3; // Minimum characters to consider as overlap
            var maxCheck = Math.min(prevClean.length, nextClean.length);
            for (var i = maxCheck; i >= minOverlap; i--) {
                var prevEnd = prevClean.substring(prevClean.length - i);
                var nextStart = nextClean.substring(0, i);
                if (prevEnd === nextStart) {
                    // Found overlap, merge
                    return prev + next.substring(i);
                }
            }
            // No overlap found, return concatenated with space
            return prev + ' ' + next;
        }

        var isRecording = false;
        var isStopping = false;
        var isClientRejected = false;
        var ws = null, wsConnected = false, wsLanguage = 'en';
        var finalSegments = [], accumulatedText = '', lastPartialText = '', captionUpdateThrottle = null;
        var wsDoneFlag = false, wsDoneTimeout = null;
        var recorder = null, audioContext = null, analyser = null, micStream = null;
        var chunks = [], silenceTimeout = 15000, lastSpeechTime = 0;
        var speechThreshold = 0.025, checkInterval = null, meterInterval = null;
        var elapsedSeconds = 0, elapsedInterval = null;

        function updateMeter() {
            if (!analyser || !isRecording) return;
            var dataArray = new Uint8Array(analyser.frequencyBinCount);
            analyser.getByteFrequencyData(dataArray);
            var sum = 0, max = 0;
            for (var i = 0; i < dataArray.length; i++) { sum += dataArray[i]; if (dataArray[i] > max) max = dataArray[i]; }
            var avg = sum / dataArray.length / 255;
            var peak = max / 255;
            for (var i = 0; i < NUM_BARS; i++) {
                var thresh = (i / NUM_BARS);
                var val = avg > thresh ? Math.min(1, (avg - thresh) * NUM_BARS * 1.5) : 0;
                var h = Math.max(4, val * 28);
                bars[i].style.height = h + 'px';
                if (peak > 0.85 && i >= NUM_BARS * 0.85) bars[i].className = 'mic-meter-bar active clip';
                else if (peak > 0.6 && i >= NUM_BARS * 0.7) bars[i].className = 'mic-meter-bar active loud';
                else if (avg > speechThreshold) bars[i].className = 'mic-meter-bar active speech';
                else bars[i].className = 'mic-meter-bar active';
            }
            var pct = Math.round(avg * 100);
            micLevelText.textContent = 'Level: ' + pct + '%  Peak: ' + Math.round(peak * 100) + '%';
            var now = Date.now();
            var silenceElapsed = now - lastSpeechTime;
            if (avg > speechThreshold) lastSpeechTime = now;
            if (silenceElapsed > silenceTimeout) { setMicState('countdown', 'Auto-stop'); if (isRecording) stopRecording(); }
            else if (silenceElapsed > 4000) { var rem = Math.ceil((silenceTimeout - silenceElapsed) / 1000); setMicState('silence', 'Silence: ' + rem + 's'); }
            else if (avg > speechThreshold) setMicState('speech', 'Hearing speech');
            else setMicState('streaming', 'Listening');
        }
        function setMicState(cls, text) { micStateLabel.className = 'mic-state-label ' + cls; micStateLabel.textContent = text || 'Idle'; }
        function setMicButtonState(state, text) {
            // Clear any existing collapse timeout
            if (window.micCollapseTimeout) {
                clearTimeout(window.micCollapseTimeout);
                window.micCollapseTimeout = null;
            }
            
            // Remove all state classes
            micBtn.classList.remove("recording", "requesting", "processing", "success", "error", "compact");
            // Add current state class (except for "idle" which is default)
            if (state !== "idle") {
                micBtn.classList.add(state);
            }
            // Update label text
            micLabel.textContent = text || "";
            
            // Show/hide activity panel based on state
            var activityPanel = document.getElementById("mic-activity-panel");
            var livePanel = document.getElementById("live-transcript-panel");
            
            if (state === "idle" || state === "success" || state === "error") {
                // Hide activity panels when not actively recording/processing
                if (activityPanel) activityPanel.style.display = "none";
                if (livePanel) livePanel.style.display = "none";
                
                // Auto-collapse success/error states after 3 seconds to neutral "Record again"
                if (state === "success" || state === "error") {
                    window.micCollapseTimeout = setTimeout(function() {
                        // Remove success/error styling, keep only compact
                        micBtn.classList.remove("success", "error");
                        micBtn.classList.add("compact");
                        micLabel.textContent = "Record again";
                    }, 3000);
                }
            } else {
                // Show activity panels when recording/processing
                if (activityPanel) activityPanel.style.display = "block";
                if (livePanel) livePanel.style.display = "block";
            }
        }
        function startMeter() { if (meterInterval) return; meterInterval = setInterval(updateMeter, 60); }
        function stopMeter() { if (meterInterval) { clearInterval(meterInterval); meterInterval = null; } for (var i = 0; i < bars.length; i++) { bars[i].style.height = '4px'; bars[i].className = 'mic-meter-bar'; } micLevelText.textContent = 'Level: --'; setMicState('', 'Idle'); }

        function getSupportedMimeType() { var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/ogg', 'audio/mp4', 'audio/wav']; for (var i = 0; i < types.length; i++) { if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(types[i])) { return types[i]; } } return ''; }

        function connectWS() {
            if (ws && wsConnected) return;
            var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            var wsUrl = protocol + '//' + window.location.host + '/ws/transcribe?language=' + encodeURIComponent(wsLanguage);
            ws = new WebSocket(wsUrl);
            ws.binaryType = 'arraybuffer';
            ws.onopen = function() { wsConnected = true; debug('info', 'WS connected | lang=' + wsLanguage); };
            ws.onmessage = function(event) { try { var data = JSON.parse(event.data); handleWSMessage(data); } catch(e) { debug('warn', 'WS parse error: ' + e); } };
            ws.onerror = function() { debug('error', 'WS error'); };
            ws.onclose = function() { wsConnected = false; debug('info', 'WS disconnected'); };
        }

        function handleWSMessage(data) {
            if (data.type === 'started') { debug('info', 'WS ready'); setStatus('streaming', 'Streaming', 'Ready'); }
            else if (data.type === 'listening') {
                // Show "Listening..." indicator instead of partial text
                lastPartialText = 'Listening...';
                updateCaptionDisplay();
                debug('info', 'Listening indicator (buffer: ' + (data.buffer_seconds || 0).toFixed(1) + 's)');
            }
            else if (data.type === 'partial') {
                var partialText = data.text || '';
                debug('info', 'Received PARTIAL message: "' + partialText + '" (chunk=' + data.chunk + ')');
                // If we have final segments, check if partial overlaps with the end
                if (finalSegments.length > 0 && partialText) {
                    var lastFinal = finalSegments[finalSegments.length - 1];
                    // Check if partial starts with the end of last final
                    var minOverlap = 3;
                    var maxCheck = Math.min(lastFinal.length, partialText.length);
                    for (var i = maxCheck; i >= minOverlap; i--) {
                        var finalEnd = lastFinal.substring(lastFinal.length - i);
                        var partialStart = partialText.substring(0, i);
                        if (finalEnd === partialStart) {
                            // Overlap found, show only the novel part
                            lastPartialText = partialText.substring(i);
                            break;
                        }
                    }
                    if (!lastPartialText) lastPartialText = partialText;
                } else {
                    lastPartialText = partialText;
                }
                // Update caption display with partial text
                updateCaptionDisplay();
                liveMeta.textContent = 'chunk=' + data.chunk + ' model=' + data.model + ' lang=' + data.language;
                debug('info', 'partial [' + data.chunk + ']: ' + (data.text || '').substr(0, 80));
            } else if (data.type === 'final') {
                var seg = data.text || '';
                if (seg) {
                    // Deduplicate: check if this segment overlaps with the last final segment
                    var lastSeg = finalSegments.length > 0 ? finalSegments[finalSegments.length - 1] : '';
                    var merged = mergeOverlap(lastSeg, seg);
                    if (merged !== lastSeg) {
                        // If we merged, replace the last segment
                        if (finalSegments.length > 0) {
                            finalSegments[finalSegments.length - 1] = merged;
                        } else {
                            finalSegments.push(merged);
                        }
                    } else {
                        // No overlap, append as new segment
                        finalSegments.push(seg);
                    }
                    accumulatedText = finalSegments.join(' ');
                }
                lastPartialText = '';
                // Update caption display (will hide caption line since no partial text)
                updateCaptionDisplay();
                debug('success', 'final [' + data.chunk + ']: ' + (data.text || '').substr(0, 80) + ' (segments=' + finalSegments.length + ')');
            } else if (data.type === 'done') {
                wsDoneFlag = true;
                if (wsDoneTimeout) { clearTimeout(wsDoneTimeout); wsDoneTimeout = null; }
                accumulatedText = data.text || accumulatedText;
                finalSegments = [accumulatedText];
                lastPartialText = '';
                // Update caption display and render final text
                updateCaptionDisplay();
                resultTextValue = accumulatedText;
                resultText.textContent = accumulatedText;
                resultPanel.classList.add('visible');
                resultMeta.innerHTML = '<span class="result-meta-badge">model:' + (data.model || 'small.en') + '</span><span class="result-meta-badge">lang:' + (data.language || wsLanguage) + '</span>';
                debug('success', 'done: ' + accumulatedText.substr(0, 120));
                setStatus('done', 'Transcript ready', accumulatedText.length + ' chars');
                showToast('Transcript ready');
                setTimeout(function() { if (ws) { ws.close(); ws = null; wsConnected = false; } cleanupRecording('success'); }, 500);
            } else if (data.type === 'error') {
                debug('error', 'WS error: ' + data.message);
                wsDoneFlag = true;
                if (wsDoneTimeout) { clearTimeout(wsDoneTimeout); wsDoneTimeout = null; }
                setStatus('error', 'Recording too short', data.message);
                clearCaptionLine();
                showToast(data.message || 'Recording issue');
                if (ws && ws.readyState < 2) { ws.close(); ws = null; wsConnected = false; }
                    cleanupRecording('error');
            }
        }

        function renderLiveText() {
            if (finalSegments.length === 0 && !lastPartialText) { 
                liveText.className = 'live-text empty'; 
                liveText.innerHTML = 'Speak for live captions. Partial text appears above, finalized text appears here.'; 
                return; 
            }
            liveText.className = 'live-text';
            var html = '';
            // Show only finalized segments in the transcript area
            for (var i = 0; i < finalSegments.length; i++) {
                html += '<div class="transcript-segment">' + escapeHtml(finalSegments[i]) + '</div>';
            }
            liveText.innerHTML = html; 
            liveText.scrollTop = liveText.scrollHeight;
        }

        function updateCaptionLine() {
            // Legacy function, use updateCaptionDisplay instead
            updateCaptionDisplay();
        }

        function clearCaptionLine() {
            captionLine.style.display = 'none';
            captionText.textContent = '';
            if (captionUpdateThrottle) {
                clearTimeout(captionUpdateThrottle);
                captionUpdateThrottle = null;
            }
        }

        function updateCaptionDisplay() {
            // Show caption line if we have partial text
            if (lastPartialText) {
                captionLine.style.display = 'flex';
                captionText.textContent = lastPartialText;
            } else {
                // Hide caption line if no partial text
                captionLine.style.display = 'none';
                captionText.textContent = '';
            }
            
            // Update live text area with finalized segments
            renderLiveText();
        }

        function clearCaptionLine() {
            captionLine.style.display = 'none';
            captionText.textContent = '';
            if (captionUpdateThrottle) {
                clearTimeout(captionUpdateThrottle);
                captionUpdateThrottle = null;
            }
        }



        function startRecording() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { showMicError('Microphone not supported. Use file upload.'); debug('error', 'getUserMedia not available'); return; }
            hideMicError();
            wsLanguage = langSelect.value;
            finalSegments = []; accumulatedText = ''; lastPartialText = '';
            clearCaptionLine();
            // Show empty caption line when recording starts
            captionLine.style.display = 'flex';
            captionText.textContent = 'Listening...';
            debug('info', 'Starting live recording | lang=' + wsLanguage);
            setStatus('active', 'Requesting mic', 'Awaiting permission...');
            setMicButtonState('requesting', '...');
            navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: { ideal: 1 }, sampleRate: { ideal: 16000 } } })
            .then(function(stream) {
                debug('info', 'Mic permission granted');
                micStream = stream;
                var tracks = stream.getAudioTracks();
                if (tracks.length > 0) {
                    try { var settings = tracks[0].getSettings(); debug('info', 'Track settings: ' + JSON.stringify(settings)); } catch(e) { debug('warn', 'getSettings not supported'); }
                    try { var constraints = tracks[0].getConstraints(); debug('info', 'Track constraints: ' + JSON.stringify(constraints)); } catch(e) {}
                }
                var mimeType = getSupportedMimeType();
                debug('info', 'MediaRecorder MIME: ' + (mimeType || 'default'));
                audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
                audioContext.resume();
                var source = audioContext.createMediaStreamSource(stream);
                analyser = audioContext.createAnalyser();
                analyser.fftSize = 256;
                source.connect(analyser);
                var scriptProc = audioContext.createScriptProcessor(4096, 1, 1);
                scriptProc.onaudioprocess = function(e) { if (!isRecording) return; lastSpeechTime = Date.now(); };
                source.connect(scriptProc); scriptProc.connect(audioContext.destination);
                chunks = [];
                recorder = new MediaRecorder(stream, { mimeType: mimeType || undefined });
                recorder.ondataavailable = function(e) { if (e.data && e.data.size > 0) { chunks.push(e.data); debug('info', 'Data available: ' + e.data.size + ' bytes, total chunks=' + chunks.length); } };
                recorder.onstop = onRecorderStop;
                recorder.start(500);
                lastSpeechTime = Date.now();
                isRecording = true;
                elapsedSeconds = 0;
                startMeter();
                checkInterval = setInterval(detectSpeech, 100);
                elapsedInterval = setInterval(function() { 
                    elapsedSeconds++; 
                    if (isRecording) {
                        var statusText = elapsedSeconds + 's';
                        // Warn if recording is getting long for CPU server
                        if (elapsedSeconds === 25) {
                            statusText += ' (long for CPU - consider upload)';
                            showToast('Long clip for CPU server - consider upload for better results');
                        } else if (elapsedSeconds >= 25) {
                            statusText += ' (long for CPU - consider upload)';
                        } else if (elapsedSeconds >= 15) {
                            statusText += ' (getting long)';
                        }
                        setStatus('recording', 'Recording', statusText);
                    }
                }, 1000);
                liveIndicator.style.display = 'flex';
                liveText.className = 'live-text empty'; liveText.innerHTML = 'Listening... Speak naturally.'; liveMeta.textContent = '';
                connectWS();
                debug('info', 'Recording started');
                setStatus('recording', 'Recording...', 'Speak naturally, then stop');
                setMicButtonState('recording', 'Stop');
                showToast('Live transcription started');
            }).catch(function(err) {
                debug('error', 'Mic error: ' + err.name + ' - ' + err.message);
                setMicButtonState('error', 'Record again');
                if (err.name === 'NotAllowedError') showMicError('Mic access denied.');
                else if (err.name === 'NotFoundError') showMicError('No microphone found.');
                else showMicError('Mic error: ' + err.message);
                setStatus('error', 'Mic error', err.message);
            });
        }

        function detectSpeech() { if (!analyser || !isRecording) return; var dataArray = new Uint8Array(analyser.frequencyBinCount); analyser.getByteFrequencyData(dataArray); var sum = 0; for (var i = 0; i < dataArray.length; i++) sum += dataArray[i]; var avg = sum / dataArray.length / 255; if (avg > speechThreshold) lastSpeechTime = Date.now(); }
        function stopRecording() {
            debug("info", "Stop recording called");
            if (isStopping) { debug("info", "Already stopping, ignoring"); return; }
            
            // Minimum recording duration guard: reject sub-1s recordings client-side
            var recordingDuration = elapsedSeconds || 0;
            var audioChunks = chunks.length;
            if (recordingDuration < 1 && audioChunks < 1) {
                debug("warn", "Recording too short (" + recordingDuration + "s, " + audioChunks + " chunks), rejecting");
                isStopping = true;
                isClientRejected = true;
                setStatus('error', 'Recording too short', 'Please record for at least 1 second.');
                showToast('Recording too short — hold mic button longer');
                setTimeout(function() {
                    isStopping = false;
                    isClientRejected = false;
                    cleanupRecording('error');
                }, 1500);
                if (recorder && recorder.state === "recording") recorder.stop();
                if (ws && wsConnected && ws.readyState === WebSocket.OPEN) { ws.close(); ws = null; wsConnected = false; }
                return;
            }
            
            isStopping = true;
            
            // Update UI immediately
            setMicButtonState('processing', 'Processing...');
            setStatus('processing', 'Processing transcript...', 'Audio length: ~' + chunks.length * 2 + 's');
            // Show processing in caption line
            captionLine.style.display = 'flex';
            captionText.textContent = 'Processing...';
            
            var wsRef = ws;
            var wsConnRef = wsConnected;
            if (recorder && recorder.state === "recording") recorder.stop();
            if (wsRef && wsConnRef && wsRef.readyState === WebSocket.OPEN) {
                // Send all accumulated WebM chunks as final blob
                if (chunks.length > 0) {
                    var finalBlob = new Blob(chunks, { type: 'audio/webm' });
                    debug("info", "Final audio blob size: " + finalBlob.size + " chunks=" + chunks.length + " wsState=" + wsRef.readyState);
                    if (finalBlob.size > 0) {
                        try { wsRef.send(finalBlob); debug('info', 'Final audio sent (' + finalBlob.size + ' bytes)'); }
                        catch(e) { debug('error', 'Final send error: ' + e.message); }
                    }
                }
                try { wsRef.send(JSON.stringify({ type: "stop" })); debug('info', 'Stop msg sent'); }
                catch(e) { debug('error', 'Stop send error: ' + e.message); }
                wsDoneTimeout = setTimeout(function() {
                    debug('warn', 'Stop timeout - no done/final/error received within 45s');
                    if (!wsDoneFlag) { 
                        setStatus('error', 'Timeout', 'Transcription taking too long');
                        showToast("Couldn't finish this clip. Try shorter dictation or upload.");
                        finalizeToResult(); 
                    }
                    if (wsRef && wsRef.readyState < 2) { wsRef.close(); wsRef = null; wsConnected = false; }
                cleanupRecording('error');
                }, 45000);
                
                // Immediately update to processing state
                setStatus('processing', 'Transcribing...', 'Please wait (CPU server)');
                micLabel.textContent = "Transcribing...";
                
                // Update status after 10 seconds to be more informative if still processing
                setTimeout(function() {
                    if (!wsDoneFlag && isStopping) {
                        setStatus('processing', 'Still transcribing...', 'Longer clips take more time on this CPU server');
                    }
                }, 10000);
            } else {
                cleanupRecording('error');
            }
        }

        function cleanupRecording(finalState) {
            isRecording = false;
            isStopping = false;
            isClientRejected = false;
            stopMeter();
            if (checkInterval) { clearInterval(checkInterval); checkInterval = null; }
            if (elapsedInterval) { clearInterval(elapsedInterval); elapsedInterval = null; }
            if (micStream) { micStream.getTracks().forEach(function(t) { t.stop(); }); micStream = null; }
            if (audioContext) { audioContext.close(); audioContext = null; analyser = null; }
            liveIndicator.style.display = 'none';
            
            // Set appropriate final state
            if (finalState === 'success') {
                setMicButtonState('success', 'Complete');
            } else if (finalState === 'error') {
                setMicButtonState('error', 'Record again');
            } else {
                // Default: reset to idle
                setMicButtonState('idle', 'Live dictation');
            }
            
            debug('info', 'Recording cleaned up (state: ' + (finalState || 'idle') + ')');
        }

        function onRecorderStop() {
            debug('info', 'Recorder stopped, chunks=' + chunks.length);
            if (wsDoneFlag || isClientRejected) return;
            if (chunks.length === 0 && (!accumulatedText && finalSegments.length === 0)) { debug('error', 'No audio captured'); setStatus('error', 'No audio', 'Try again'); showToast('No audio captured'); return; }
            finalizeToResult();
        }

        function finalizeToResult() {
            if (accumulatedText || finalSegments.length > 0) {
                var fullText = finalSegments.join(' ') || accumulatedText;
                resultTextValue = fullText; resultText.textContent = fullText;
                resultPanel.classList.add('visible');
                resultMeta.innerHTML = '<span class="result-meta-badge">model:small.en</span><span class="result-meta-badge">lang:' + wsLanguage + '</span>';
                debug('success', 'Final output: ' + fullText.substr(0, 120));
            }
        }

        micBtn.addEventListener("click", function() { if (isRecording && !isStopping) stopRecording(); else if (!isRecording && !isStopping) startRecording(); });
        micBtn.addEventListener("keydown", function(e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (isRecording && !isStopping) stopRecording(); else if (!isRecording && !isStopping) startRecording(); } });

        dropZone.addEventListener('dragover', function(e) { e.preventDefault(); dropZone.classList.add('drag-over'); });
        dropZone.addEventListener('dragleave', function() { dropZone.classList.remove('drag-over'); });
        dropZone.addEventListener('drop', function(e) { e.preventDefault(); dropZone.classList.remove('drag-over'); if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; fileInput.dispatchEvent(new Event('change')); } });

        fileInput.addEventListener('change', function(e) {
            var file = e.target.files[0];
            if (!file) return;
            if (file.size > """ + str(MAX_FILE_SIZE) + """) { showToast('File exceeds """ + str(MAX_FILE_SIZE_MB) + """ MB limit'); debug('warn', 'File too large: ' + file.size + ' bytes'); return; }
            currentFileName = file.name;
            var formData = new FormData();
            formData.append('file', file); formData.append('language', langSelect.value);
            resultPanel.classList.remove('visible');
            debug('info', 'Upload started: ' + file.name + ' (' + Math.round(file.size / 1024) + ' KB) lang=' + langSelect.value);
            setStatus('active', 'Uploading', file.name);
            var t0 = Date.now();
            fetch('/transcribe', { method: 'POST', body: formData })
            .then(function(res) {
                var ms = Date.now() - t0;
                debug('info', 'Response: ' + res.status + ' in ' + ms + 'ms');
                return res.json().then(function(data) {
                    if (res.ok) {
                        resultTextValue = data.text || ''; resultText.textContent = resultTextValue || 'No output';
                        resultPanel.classList.add('visible');
                        var model = data.model || '?'; var lang = data.language || '?';
                        debug('success', 'Transcription done | model=' + model + ' lang=' + lang + ' text_len=' + resultTextValue.length);
                        debug('info', 'Text: ' + resultTextValue.substr(0, 120) + (resultTextValue.length > 120 ? '...' : ''));
                        resultMeta.innerHTML = '<span class="result-meta-badge">model:' + model + '</span><span class="result-meta-badge">lang:' + lang + '</span>';
                        setStatus('done', 'Complete', Math.round(ms / 1000) + 's'); showToast('Transcription ready');
                    } else { debug('error', 'Error: ' + (data.detail || 'Unknown (' + res.status + ')')); setStatus('error', 'Error', data.detail || 'Unknown error'); showToast(data.detail || 'Error'); }
                });
            }).catch(function(err) { debug('error', 'Network error: ' + err.message); setStatus('error', 'Network error', err.message); showToast('Network error'); });
        });

        window.copyResult = function() { navigator.clipboard.writeText(resultTextValue || resultText.textContent); showToast('Copied'); };
        window.downloadResult = function() { var text = resultTextValue || resultText.textContent; var blob = new Blob([text], { type: 'text/plain' }); var url = URL.createObjectURL(blob); var a = document.createElement('a'); a.href = url; a.download = (currentFileName.replace(/\.[^/.]+$/, '') || 'signal') + '_transcript.txt'; a.click(); URL.revokeObjectURL(url); };
        window.resetForm = function() { fileInput.value = ''; resultPanel.classList.remove('visible'); resultTextValue = ''; resultMeta.innerHTML = ''; currentFileName = ''; accumulatedText = ''; finalSegments = []; lastPartialText = ''; clearCaptionLine(); liveText.className = 'live-text empty'; liveText.innerHTML = 'Speak for live dictation. Partial text appears above, finalized text appears here.'; liveMeta.textContent = ''; liveIndicator.style.display = 'none'; hideStatus(); setMicButtonState('idle', 'Live dictation'); };
        
        window.toggleHelp = function() {
            var overlay = document.getElementById("help-overlay");
            overlay.classList.toggle("visible");
        };
        window.closeHelp = function(e) {
            if (e && e.target !== e.currentTarget) return;
            document.getElementById("help-overlay").classList.remove("visible");
        };
        document.addEventListener("keydown", function(e) {
            if (e.key === "Escape") document.getElementById("help-overlay").classList.remove("visible");
        });
        function updateMicRemaining(usedSec) {
            var limitSec = parseInt("__MIC_LIMIT_SEC__" || "900");
            var remainingSec = Math.max(0, limitSec - (usedSec || 0));
            var remainingMin = Math.ceil(remainingSec / 60);
            var el = document.getElementById("mic-remaining");
            if (el) el.textContent = remainingMin + " / " + Math.ceil(limitSec/60) + " min";
            var quotaBanner = document.getElementById("mic-quota-exceeded");
            if (quotaBanner) quotaBanner.style.display = remainingSec <= 0 ? "block" : "none";
        }
        window.addEventListener("load", function() {
            var remaining = parseInt("__MIC_REMAINING_SEC__" || "900");
            var limit = parseInt("__MIC_LIMIT_SEC__" || "900");
            var q = document.getElementById("mic-quota-exceeded");
            if (q && remaining <= 0) q.style.display = "block";
        });
window.signout = function() { if (isRecording) stopRecording(); fetch('/signout', { method: 'POST', credentials: 'same-origin' }).then(function() { window.location.href = '/'; }).catch(function() { document.cookie = 'sqs_session=; path=/; max-age=0'; window.location.href = '/'; }); };

        var canvas = document.getElementById('bg-canvas'); var ctx = canvas.getContext('2d'); var particles = []; var animFrame; var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        function resize() { canvas.width = window.innerWidth; canvas.height = window.innerHeight; }
        window.addEventListener('resize', resize); resize();
        function createParticles() { particles = []; var count = Math.floor((canvas.width * canvas.height) / 12000); for (var i = 0; i < count; i++) { particles.push({ x: Math.random() * canvas.width, y: Math.random() * canvas.height, vx: (Math.random() - 0.5) * 0.3, vy: (Math.random() - 0.5) * 0.3, r: Math.random() * 1.5 + 0.5, a: Math.random() * 0.4 + 0.1 }); } }
        createParticles();
        function draw() { ctx.clearRect(0, 0, canvas.width, canvas.height); for (var i = 0; i < particles.length; i++) { var p = particles[i]; p.x += p.vx; p.y += p.vy; if (p.x < 0) p.x = canvas.width; if (p.x > canvas.width) p.x = 0; if (p.y < 0) p.y = canvas.height; if (p.y > canvas.height) p.y = 0; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fillStyle = 'rgba(99, 102, 241, ' + p.a + ')'; ctx.fill(); } for (var i = 0; i < particles.length; i++) { for (var j = i + 1; j < particles.length; j++) { var dx = particles[i].x - particles[j].x; var dy = particles[i].y - particles[j].y; var dist = Math.sqrt(dx * dx + dy * dy); if (dist < 80) { ctx.beginPath(); ctx.moveTo(particles[i].x, particles[i].y); ctx.lineTo(particles[j].x, particles[j].y); ctx.strokeStyle = 'rgba(99, 102, 241, ' + (0.05 * (1 - dist / 80)) + ')'; ctx.lineWidth = 0.5; ctx.stroke(); } } } animFrame = requestAnimationFrame(draw); }
        if (!prefersReducedMotion) draw();
        window.addEventListener('beforeunload', function() { if (animFrame) cancelAnimationFrame(animFrame); });
        // Initialize mic button to idle state
        setMicButtonState('idle', 'Live dictation');
        debug('info', 'SQS Signal loaded (live streaming v3)');
    })();
    </script>
</body>
</html>"""
