from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import whisper
import tempfile
import os
import logging
import threading
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Whisper CPU Transcription Service", version="1.1.0")

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
    """Load model on startup"""
    load_model(os.getenv("WHISPER_MODEL", "tiny.en"))


@app.get("/")
async def root():
    """Health check endpoint"""
    return {"status": "healthy", "service": "whisper-cpu-transcription"}


@app.get("/health")
async def health_check():
    """Detailed health check"""
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


def _run_transcription(tmp_path: str, language: Optional[str], word_timestamps: bool, model_name: str, filename: str):
    """Run the blocking whisper transcription. Serialized via transcribe_lock so
    the CPU model is only ever used by one request, while the event loop stays
    free to answer /health and accept other requests (which queue here)."""
    whisper_model = load_model(model_name or os.getenv("WHISPER_MODEL", "tiny.en"))
    logger.info(f"Transcribing audio file: {filename}")
    with transcribe_lock:
        return whisper_model.transcribe(
            tmp_path,
            language=language if language != "auto" else None,
            fp16=False,
            word_timestamps=word_timestamps,
        )


def _transcribe_common(file: UploadFile, language: Optional[str], word_timestamps: bool, model_name: str):
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio file")
    tmp_path = None
    try:
        tmp_path = _save_upload(file, file.filename)
        result = _run_transcription(tmp_path, language, word_timestamps, model_name, file.filename)
        transcription = {
            "text": result["text"].strip(),
            "language": result.get("language", language),
            "segments": result.get("segments", []),
            "model": model_name or os.getenv("WHISPER_MODEL", "tiny.en"),
            "filename": file.filename,
        }
        if word_timestamps:
            transcription["words"] = result.get("words", [])
        logger.info(f"Transcription completed for {file.filename}")
        return transcription
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error transcribing audio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.post("/transcribe")
def transcribe_audio(
    file: UploadFile = File(...),
    model_name: str = Form("tiny.en"),
    language: Optional[str] = Form("en"),
):
    return _transcribe_common(file, language, word_timestamps=False, model_name=model_name)


@app.post("/transcribe-with-timestamps")
def transcribe_with_timestamps(
    file: UploadFile = File(...),
    model_name: str = Form("tiny.en"),
    language: Optional[str] = Form("en"),
):
    return _transcribe_common(file, language, word_timestamps=True, model_name=model_name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
