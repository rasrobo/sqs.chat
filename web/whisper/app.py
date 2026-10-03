from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import whisper
import tempfile
import os
import math
import shutil
import asyncio
import logging
import threading
import queue
import uuid
import time
import wave
import subprocess
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Whisper CPU Transcription Service", version="1.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

model = None
model_lock = threading.Lock()
transcribe_lock = threading.Lock()

# Chunked transcription config — transcribe long files in fixed-size windows so
# we can report real progress and bound memory. Long videos are never loaded
# whole: ffmpeg extracts each window to a small 16k mono wav on demand.
CHUNK_SECONDS = 30
CHUNK_OVERLAP_SECONDS = 2
# Keep finished jobs for a while so a client can re-fetch the result after a
# dropped connection / page refresh (results are idempotent within the TTL).
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", str(6 * 3600)))
# Optional GPU backend: a faster-whisper (speaches) OpenAI-compatible endpoint,
# reached over an SSH tunnel from the hub. When set, each ffmpeg window is sent
# there (much faster + more accurate); local whisper is the fallback.
GPU_WHISPER_URL = os.getenv("GPU_WHISPER_URL", "").strip()
GPU_WHISPER_MODEL = os.getenv("GPU_WHISPER_MODEL", "Systran/faster-whisper-large-v3")


def load_model(model_name: str = "tiny.en"):
    """Load Whisper model. Serialized so concurrent requests don't reload."""
    global model
    with model_lock:
        if model is None or getattr(load_model, "current_model", None) != model_name:
            logger.info(f"Loading Whisper model: {model_name}")
            model = whisper.load_model(model_name)
            load_model.current_model = model_name
            logger.info("Model loaded successfully")
        return model


@app.on_event("startup")
async def startup_event():
    load_model(os.getenv("WHISPER_MODEL", "tiny.en"))
    threading.Thread(target=_job_worker, daemon=True).start()
    threading.Thread(target=_cleanup_worker, daemon=True).start()


@app.get("/")
async def root():
    return {"status": "healthy", "service": "whisper-cpu-transcription"}


@app.get("/health")
async def health_check():
    with jobs_lock:
        queued = sum(1 for j in jobs.values() if j["status"] in ("queued", "running"))
    return {
        "status": "healthy",
        "model_loaded": model is not None,
        "service": "whisper-cpu-transcription",
        "backend": "gpu" if GPU_WHISPER_URL else "local",
        "busy": (job_queue.qsize() > 0) or transcribe_lock.locked(),
        "queued": queued,
    }


def _save_upload(file: UploadFile, filename: str):
    """Persist an uploaded file to a temp path, streaming (never buffering the
    whole file in memory — videos can be multi-GB). Returns the path."""
    suffix = f".{filename.split('.')[-1]}" if "." in filename else ".tmp"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()
    try:
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return tmp_path


def _load_audio_mono(tmp_path: str):
    """Load audio as mono 16k float32 numpy via whisper's loader (fallback)."""
    return whisper.load_audio(tmp_path)


def _read_wav_16k_mono(path: str):
    """Read a 16k mono pcm_s16le wav directly (no ffmpeg round-trip). Falls back
    to whisper's loader for anything unexpected."""
    try:
        with wave.open(path, "rb") as w:
            if (w.getframerate() == 16000 and w.getnchannels() == 1
                    and w.getsampwidth() == 2):
                frames = w.readframes(w.getnframes())
                import numpy as np
                return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    except Exception:
        pass
    return _load_audio_mono(path)


def _probe_duration_seconds(path: str) -> float:
    """Media duration via ffprobe (works for audio AND video). 0.0 on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else 0.0
    except Exception:
        return 0.0


def _extract_wav_window(src_path: str, start_sec: float, dur_sec: float, out_wav: str) -> bool:
    """Extract [start_sec, start_sec+dur_sec] from any audio/video as a 16k mono
    wav using ffmpeg. Bounded memory: only the window is written to disk."""
    cmd = ["ffmpeg", "-y", "-nostdin", "-ss", f"{max(0.0, start_sec):.3f}"]
    if dur_sec and dur_sec > 0:
        cmd += ["-t", f"{dur_sec:.3f}"]
    cmd += ["-i", src_path, "-vn", "-ar", "16000", "-ac", "1",
            "-c:a", "pcm_s16le", "-f", "wav", out_wav]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max(300, int((dur_sec or 60) * 10) + 300))
    except Exception as e:
        logger.error(f"ffmpeg window extraction failed: {e}")
        return False
    if r.returncode != 0:
        logger.error(f"ffmpeg window rc={r.returncode}: {(r.stderr or '')[:200]}")
        return False
    return os.path.exists(out_wav) and os.path.getsize(out_wav) > 44


def _gpu_transcribe_window(win_wav: str, language: Optional[str]):
    """POST one window wav to the GPU faster-whisper endpoint; return segments."""
    import urllib.request
    import json as _json
    import uuid as _uuid
    boundary = "----gpu" + _uuid.uuid4().hex
    with open(win_wav, "rb") as f:
        audio = f.read()

    def _field(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="{name}"\r\n\r\n{value}\r\n').encode()

    body = (f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
    body += audio + b"\r\n"
    body += _field("model", GPU_WHISPER_MODEL)
    body += _field("response_format", "verbose_json")
    body += _field("language", language if language and language != "auto" else "en")
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        GPU_WHISPER_URL.rstrip("/") + "/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        d = _json.loads(r.read().decode())
    return d.get("segments", []) or []


def _local_transcribe_window(whisper_model, win_wav, language, word_timestamps, offset, out_segments):
    """Transcribe one window with the in-process whisper model."""
    import numpy as np
    import torch
    chunk_audio = _read_wav_16k_mono(win_wav)
    if chunk_audio is None or len(chunk_audio) == 0:
        return
    tensor = torch.from_numpy(np.ascontiguousarray(chunk_audio))
    with transcribe_lock:
        res = whisper_model.transcribe(
            tensor, language=language if language != "auto" else None,
            fp16=False, word_timestamps=word_timestamps,
        )
    for s in res.get("segments", []):
        out_segments.append({
            "start": round((s.get("start") or 0) + offset, 3),
            "end": round((s.get("end") or 0) + offset, 3),
            "text": (s.get("text") or "").strip(),
        })


def _chunk_audio(audio, chunk_sec: int = CHUNK_SECONDS, overlap_sec: int = CHUNK_OVERLAP_SECONDS):
    """Yield (start_sample, chunk_array) windows with a small overlap.
    Used only for the rare no-duration fallback path."""
    sr = 16000
    chunk = chunk_sec * sr
    step = chunk - overlap_sec * sr
    n = len(audio)
    if n <= chunk:
        yield 0, audio
        return
    start = 0
    while start < n:
        yield start, audio[start:start + chunk]
        if start + chunk >= n:
            break
        start += step


def _transcribe_chunked(tmp_path: str, language: Optional[str], word_timestamps: bool,
                        model_name: str, filename: str, progress_cb=None):
    """Transcribe any audio/video in ffmpeg-extracted windows with global
    timestamps. Never loads the whole file into memory, so multi-hour videos
    transcribe within a small, bounded footprint."""
    import numpy as np
    import torch
    whisper_model = load_model(model_name or os.getenv("WHISPER_MODEL", "tiny.en"))
    logger.info(f"Transcribing (windowed): {filename}")
    duration = _probe_duration_seconds(tmp_path)

    all_segments = []
    if duration <= 0:
        # Rare: no container duration. Fall back to whisper's own loader
        # (small files only) and chunk in memory.
        audio = _load_audio_mono(tmp_path)
        chunks = list(_chunk_audio(audio))
        total = len(chunks)
        for idx, (start_sample, chunk_audio) in enumerate(chunks):
            tensor = torch.from_numpy(chunk_audio)
            with transcribe_lock:
                res = whisper_model.transcribe(
                    tensor, language=language if language != "auto" else None,
                    fp16=False, word_timestamps=word_timestamps,
                )
            offset = start_sample / 16000.0
            for s in res.get("segments", []):
                all_segments.append({
                    "start": round((s.get("start") or 0) + offset, 3),
                    "end": round((s.get("end") or 0) + offset, 3),
                    "text": (s.get("text") or "").strip(),
                })
            if progress_cb:
                progress_cb((idx + 1) / max(1, total))
    else:
        step = max(1.0, CHUNK_SECONDS - CHUNK_OVERLAP_SECONDS)
        total = max(1, int(math.ceil((duration - CHUNK_OVERLAP_SECONDS) / step)))
        workdir = tempfile.mkdtemp(prefix="whisper_win_")
        start = 0.0
        idx = 0
        try:
            while start < duration:
                win_wav = os.path.join(workdir, f"w{idx}.wav")
                if _extract_wav_window(tmp_path, start, CHUNK_SECONDS + CHUNK_OVERLAP_SECONDS, win_wav):
                    try:
                        if GPU_WHISPER_URL:
                            try:
                                gpu_segs = _gpu_transcribe_window(win_wav, language)
                                for s in gpu_segs:
                                    all_segments.append({
                                        "start": round((s.get("start") or 0) + start, 3),
                                        "end": round((s.get("end") or 0) + start, 3),
                                        "text": (s.get("text") or "").strip(),
                                    })
                            except Exception as _ge:
                                logger.warning(f"GPU window failed ({_ge}); local fallback")
                                _local_transcribe_window(whisper_model, win_wav, language, word_timestamps, start, all_segments)
                        else:
                            _local_transcribe_window(whisper_model, win_wav, language, word_timestamps, start, all_segments)
                    finally:
                        if os.path.exists(win_wav):
                            try:
                                os.unlink(win_wav)
                            except Exception:
                                pass
                idx += 1
                start += step
                if progress_cb:
                    progress_cb(min(1.0, start / duration))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # Dedupe overlapping tails: drop any segment whose start is inside the
    # previous window's overlap region (its text was already captured).
    merged = []
    last_end = -1.0
    for s in sorted(all_segments, key=lambda x: (x["start"], x["end"])):
        if not s["text"]:
            continue
        if s["start"] < last_end - 0.05:
            continue
        merged.append(s)
        last_end = max(last_end, s["end"])
    logger.info(f"Transcription completed: {filename} ({len(merged)} segments)")
    return merged


def _is_media(content_type: Optional[str]) -> bool:
    """Accept audio and video (and a tolerant octet-stream fallback)."""
    if not content_type:
        return False
    ct = content_type.lower()
    return ct.startswith("audio/") or ct.startswith("video/") or ct == "application/octet-stream"


# ---------------------------------------------------------------------------
# Job registry + sequential worker. Long videos are heavy, so jobs run ONE at a
# time (a single worker) — a queue gives each job the full CPU and keeps memory
# bounded instead of thrashing several multi-hour transcodes at once.
# ---------------------------------------------------------------------------

jobs = {}
jobs_lock = threading.Lock()
job_queue: "queue.Queue" = queue.Queue()


def _job_worker():
    while True:
        item = job_queue.get()
        job_id, tmp_path, model_name, language, filename = item
        try:
            with jobs_lock:
                job = jobs.get(job_id)
                if job is None:  # cancelled/expired before it ran
                    continue
                job["status"] = "running"

            def _cb(frac):
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["progress"] = round(frac, 4)

            segs = _transcribe_chunked(
                tmp_path, language, word_timestamps=False, model_name=model_name,
                filename=filename, progress_cb=_cb,
            )
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "done"
                    jobs[job_id]["progress"] = 1.0
                    jobs[job_id]["result"] = {
                        "segments": segs,
                        "filename": filename,
                        "language": language or "en",
                        "model": model_name or os.getenv("WHISPER_MODEL", "tiny.en"),
                    }
        except Exception as e:
            logger.error(f"job {job_id} failed: {e}")
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = str(e)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            job_queue.task_done()


def _cleanup_worker():
    """Expire finished jobs after JOB_TTL_SECONDS (keeps results re-fetchable)."""
    while True:
        time.sleep(300)
        cutoff = time.time() - JOB_TTL_SECONDS
        with jobs_lock:
            stale = [jid for jid, j in jobs.items()
                     if j.get("created_at", 0) < cutoff
                     and j.get("status") in ("done", "error")]
            for jid in stale:
                jobs.pop(jid, None)


@app.post("/transcribe-async")
async def transcribe_async(
    file: UploadFile = File(...),
    model_name: str = Form("tiny.en"),
    language: Optional[str] = Form("en"),
):
    if not _is_media(file.content_type):
        raise HTTPException(status_code=400, detail="File must be audio or video")
    job_id = uuid.uuid4().hex
    # Write the upload to disk OFF the event loop — a multi-GB write here would
    # otherwise block /progress and /health, making the client think jobs died.
    tmp_path = await asyncio.to_thread(_save_upload, file, file.filename)
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "filename": file.filename,
            "result": None,
            "error": None,
            "created_at": time.time(),
        }
    job_queue.put((job_id, tmp_path, model_name, language or "en", file.filename))
    return {"job_id": job_id, "filename": file.filename}


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "filename": job["filename"],
            "error": job["error"],
        }


@app.get("/result/{job_id}")
async def result(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] != "done":
            return {"job_id": job_id, "status": job["status"], "progress": job["progress"]}
        return {"job_id": job_id, "status": "done", **job["result"]}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    with jobs_lock:
        if job_id in jobs:
            del jobs[job_id]
            return {"deleted": True}
        return {"deleted": False}


# ---------------------------------------------------------------------------
# Synchronous transcription — kept for the live-mic websocket path (short clips)
# and the programmatic sync API. Accepts audio and video.
# ---------------------------------------------------------------------------

@app.post("/transcribe")
def transcribe_audio(
    file: UploadFile = File(...),
    model_name: str = Form("tiny.en"),
    language: Optional[str] = Form("en"),
):
    if not _is_media(file.content_type):
        raise HTTPException(status_code=400, detail="File must be audio or video")
    tmp_path = None
    try:
        tmp_path = _save_upload(file, file.filename)
        segs = _transcribe_chunked(tmp_path, language, word_timestamps=False,
                                   model_name=model_name, filename=file.filename)
        text = " ".join(s["text"] for s in segs).strip()
        return {
            "text": text,
            "language": language or "en",
            "segments": segs,
            "model": model_name or os.getenv("WHISPER_MODEL", "tiny.en"),
            "filename": file.filename,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error transcribing media: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
