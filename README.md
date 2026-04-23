# SQS Signal — Open-Source Speech-to-Text with OpenAI Whisper

[![License: Sustainable Use](https://img.shields.io/badge/License-Sustainable%20Use-blue)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker)](https://www.docker.com)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com)

**SQS Signal** is a self-hosted, CPU-friendly speech-to-text (STT) transcription service powered by OpenAI Whisper. Record from your microphone or upload audio/video files through a clean web UI — all running on a single VPS with no GPU required.

## Why SQS Signal?

- **CPU-Optimized Whisper** — Runs `small.en` model efficiently on CPU. No GPU needed.
- **Self-Hosted & Private** — Your audio never leaves your server. Full data control.
- **Docker, One Command** — `docker compose up -d` and you're running.
- **GitHub OAuth** — Let any GitHub user sign in securely.
- **Built for Production** — Caddy HTTPS, SQLite persistence, access logging, daily mic quotas.

## Features

- **Microphone Recording** — Speak, stop, get instant transcription via WebSocket
- **File Upload Transcription** — Upload audio/video files for higher-quality results
- **GitHub OAuth Sign-In** — Secure sign-in with any GitHub account
- **Daily Mic Quota** — 15 minutes of microphone transcription per user per day (upload is unlimited)
- **Access Logging** — All sign-ins, transcription activity, and usage logged to SQLite + rotating files
- **No GPU Required** — Optimized for single CPU VPS (tested on 2 vCPU / 4GB RAM)
- **Docker Compose Stack** — Caddy + FastAPI Web + Whisper Service in 3 containers

## Architecture

```
Internet → Caddy (:443/:80) → Web (:8892 → :8000) → Whisper (:8891 → :8000)
```

| Container | Role |
|---|---|
| **Caddy** | Reverse proxy, automatic HTTPS, TLS termination, gzip |
| **Web** | FastAPI app — auth, UI, transcription orchestration, SQLite DB |
| **Whisper Service** | OpenAI Whisper CPU inference with `small.en` model |

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

Visit `https://your-domain.com` — sign in with GitHub and start transcribing.

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
