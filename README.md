# SQS Signal — Self-Hosted Open Source Dictation App

[![License: Sustainable Use](https://img.shields.io/badge/License-Sustainable%20Use-blue)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker)](https://www.docker.com)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com)

**SQS Signal** is a self-hosted dictation app that turns speech into text using [OpenAI Whisper](https://github.com/openai/whisper). Speak your drafts, notes, prompts, and memos faster than you'd type them — all on your own infrastructure with GitHub OAuth.

## Why dictation?

Speaking is often faster than typing — 2-3x faster for rough drafts, meeting notes, code comments, journal entries, or brain-dumping ideas. SQS Signal gives you a simple, private way to do that on your own hardware.

## Why SQS Signal?

- **Dictation-first** — Optimized for short voice clips via microphone. Upload mode for longer audio.
- **CPU-Optimized Whisper** — Runs `small.en` model efficiently on CPU. No GPU needed.
- **Self-Hosted & Private** — Your audio never leaves your server. Full data control.
- **Docker, One Command** — `docker compose up -d` and you're running.
- **GitHub OAuth** — Let any GitHub user sign in securely.
- **Built for Production** — Caddy HTTPS, SQLite persistence, access logging, daily mic quotas.

## Open-source stack

SQS Signal is built on and compatible with several open-source speech-to-text projects:

- **[OpenAI Whisper](https://github.com/openai/whisper)** — The core transcription engine. Runs on CPU (FP16 disabled), supports multiple model sizes. MIT licensed.
- **[whisper.cpp](https://github.com/ggml-org/whisper.cpp)** — A C/C++ port of Whisper with CPU-only inference, VAD support, and broad platform compatibility. MIT licensed. The transcription pipeline is compatible with whisper.cpp-based backends.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** — A reimplementation of Whisper using CTranslate2 that benchmarks faster and more memory-efficient than openai/whisper. MIT licensed. The service API can be adapted to use faster-whisper as a drop-in backend.

Additional open-source components:
- **[FastAPI](https://github.com/fastapi/fastapi)** — Web framework for the API and frontend server
- **[Caddy](https://github.com/caddyserver/caddy)** — Reverse proxy with automatic HTTPS

## Features

- **Microphone Dictation** — Speak, stop, get instant text via WebSocket
- **File Upload** — Upload audio/video files for higher-quality results
- **GitHub OAuth Sign-In** — Secure sign-in with any GitHub account
- **Daily Mic Quota** — 15 minutes of microphone dictation per user per day (upload is unlimited)
- **Access Logging** — All sign-ins and activity logged to SQLite + rotating files
- **No GPU Required** — Optimized for single CPU VPS (tested on 2 vCPU / 4GB RAM)
- **Docker Compose Stack** — Caddy + FastAPI Web + Whisper Service in 3 containers

## Architecture

```
Internet → Caddy (:443/:80) → Web (:8892 → :8000) → Whisper (:8891 → :8000)
```

| Container | Role |
|---|---|
| **Caddy** | Reverse proxy, automatic HTTPS, TLS termination, gzip |
| **Web** | FastAPI app — auth, UI, dictation orchestration, SQLite DB |
| **Whisper Service** | OpenAI Whisper CPU inference with `small.en` model |

## How dictation works

1. Audio is captured via microphone (WebSocket streaming) or uploaded as a file
2. The web service forwards audio to the Whisper transcription service
3. Whisper processes the audio on CPU and returns transcribed text
4. Results are displayed live in the browser or returned as JSON via API

## Current behavior

- Optimized for **short dictation clips** (5-20 seconds ideal)
- Uses the `small.en` Whisper model by default
- Live dictation via WebSocket with configurable language support (11 languages + auto-detect)
- File upload for pre-recorded audio and video files (up to 50 MB)
- CPU-only inference (no GPU backend)
- Self-hosted deployment on a single VPS or local machine

## Quick Start

### Prerequisites
- Docker & Docker Compose
- A [GitHub OAuth App](https://github.com/settings/developers)

### Setup

```bash
git clone https://github.com/rasrobo/sqs.chat.git
cd sqs.chat
cp .env.example .env
```

Edit `.env` with your GitHub OAuth credentials:

```
GITHUB_CLIENT_ID=your_client_id
GITHUB_CLIENT_SECRET=your_client_secret
GITHUB_CALLBACK_URL=https://your-domain.com/auth/github/callback
```

Start the stack:

```bash
docker compose up -d
```

Visit `https://your-domain.com` — sign in with GitHub and start dictating.

### GitHub OAuth Setup

1. Go to [GitHub Developer Settings](https://github.com/settings/developers) → OAuth Apps → New OAuth App
2. Homepage URL: `https://your-domain.com`
3. Callback URL: `https://your-domain.com/auth/github/callback`
4. Copy Client ID and Client Secret into `.env`

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_CLIENT_ID` | Yes | — | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | Yes | — | GitHub OAuth App client secret |
| `GITHUB_CALLBACK_URL` | Yes | `https://sqs.chat/auth/github/callback` | OAuth callback URL |
| `WHISPER_SERVICE_URL` | No | `http://whisper-service:8000` | Internal whisper service URL |
| `MAX_FILE_SIZE_MB` | No | `50` | Max upload file size in MB |
| `COOKIE_SECURE` | No | `False` | Set `True` for HTTPS production |

## Quota System

- **Mic**: 15 minutes per user per day (resets at midnight UTC)
- **Upload**: Unlimited
- Quota enforced server-side with SQLite — users see remaining time in the UI

## Access Logging

All events logged to SQLite (`access_log` table) and rotating files (`access.log`, `transcribe.log`, `error.log`).

## License

[Sustainable Use License](LICENSE) — source is visible and self-hostable, but you may not offer it as a competing commercial hosted transcription service without permission. Inspired by the fair-code model (n8n, GitLab EE).

## Support

If you find this project useful, consider [supporting development on Ko-fi](https://ko-fi.com/sidequeststudios).

---

Built by [Ras Robo / Side Quest Studios](https://sidequeststudios.xyz)
