from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import whisper
import tempfile
import os
import logging
import threading
import uuid
import time
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Whisper CPU Transcription Service", version="1.2.0")

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
# we can report real progress and bound memory. Chunks are small enough that a
# fast CPU (tiny.en/small.en) finishes each in a few seconds.
CHUNK_SECONDS = 30
CHUNK_OVERLAP_SECONDS = 2


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


@app.get("/")
async def root():
    return {"status": "healthy", "service": "whisper-cpu-transcription"}


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "model_loaded": model is not None,
        "service": "whisper-cpu-transcription",
        "busy": transcribe_lock.locked(),
    }


def _save_upload(file: UploadFile, filename: str):
    """Persist an uploaded file to a temp path, return the path."""
    suffix = f".{filename.split('.')[-1]}" if "." in filename else ".tmp"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    try:
        with open(tmp_path, "wb") as f:
            f.write(file.file.read())
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return tmp_path


def _load_audio_mono(tmp_path: str):
    """Load audio as mono 16k float32 numpy via whisper's loader."""
    audio = whisper.load_audio(tmp_path)  # np float32 mono 16k
    return audio


def _chunk_audio(audio, chunk_sec: int = CHUNK_SECONDS, overlap_sec: int = CHUNK_OVERLAP_SECONDS):
    """Yield (start_sample, chunk_array) windows with a small overlap.

    Overlap avoids cutting words at boundaries; the merge step drops the
    duplicate tail of each window.
    """
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
    """Transcribe a file in chunks, calling progress_cb(fraction_done) as each
    chunk completes. Returns segments (list of dicts) with global timestamps."""
    import numpy as np
    whisper_model = load_model(model_name or os.getenv("WHISPER_MODEL", "tiny.en"))
    logger.info(f"Transcribing (chunked): {filename}")
    audio = _load_audio_mono(tmp_path)
    chunks = list(_chunk_audio(audio))
    total = len(chunks)
    all_segments = []
    for idx, (start_sample, chunk_audio) in enumerate(chunks):
        # Convert numpy to torch tensor for direct model call (no temp file).
        import torch
        tensor = torch.from_numpy(chunk_audio)
        with transcribe_lock:
            res = whisper_model.transcribe(
                tensor,
                language=language if language != "auto" else None,
                fp16=False,
                word_timestamps=word_timestamps,
            )
        segs = res.get("segments", [])
        offset = start_sample / 16000.0
        for s in segs:
            all_segments.append({
                "start": round((s.get("start") or 0) + offset, 3),
                "end": round((s.get("end") or 0) + offset, 3),
                "text": (s.get("text") or "").strip(),
            })
        if progress_cb:
            progress_cb((idx + 1) / total)
    # Dedupe overlapping tails: drop any segment whose start is inside the
    # previous chunk's overlap region (i.e., its text was already captured).
    merged = []
    last_end = -1.0
    for s in sorted(all_segments, key=lambda x: (x["start"], x["end"])):
        if not s["text"]:
            continue
        if s["start"] < last_end - 0.05:
            continue
        merged.append(s)
        last_end = max(last_end, s["end"])
    logger.info(f"Transcription completed: {filename} ({total} chunks)")
    return merged


# ---------------------------------------------------------------------------
# Job registry for async transcription with progress (used by the web app for
# browser uploads so it can show an accurate progress bar).
# ---------------------------------------------------------------------------

jobs = {}
jobs_lock = threading.Lock()


@app.post("/transcribe-async")
async def transcribe_async(
    file: UploadFile = File(...),
    model_name: str = Form("tiny.en"),
    language: Optional[str] = Form("en"),
):
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio file")
    job_id = uuid.uuid4().hex
    tmp_path = _save_upload(file, file.filename)
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "filename": file.filename,
            "result": None,
            "error": None,
            "created_at": time.time(),
        }

    def _run():
        try:
            with jobs_lock:
                jobs[job_id]["status"] = "running"
            def _cb(frac):
                with jobs_lock:
                    jobs[job_id]["progress"] = round(frac, 4)
            segs = _transcribe_chunked(
                tmp_path, language, word_timestamps=False, model_name=model_name,
                filename=file.filename, progress_cb=_cb,
            )
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 1.0
                jobs[job_id]["result"] = {
                    "segments": segs,
                    "filename": file.filename,
                    "language": language or "en",
                    "model": model_name or os.getenv("WHISPER_MODEL", "tiny.en"),
                }
        except Exception as e:
            logger.error(f"job {job_id} failed: {e}")
            with jobs_lock:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = str(e)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    threading.Thread(target=_run, daemon=True).start()
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
            "progress": job["progress"],  # 0..1
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
# Synchronous transcription — kept for the live-mic websocket path (short clips).
# ---------------------------------------------------------------------------

@app.post("/transcribe")
def transcribe_audio(
    file: UploadFile = File(...),
    model_name: str = Form("tiny.en"),
    language: Optional[str] = Form("en"),
):
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio file")
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
        logger.error(f"Error transcribing audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
