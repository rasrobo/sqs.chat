# SQS Signal

[![License: Sustainable Use](LICENSE)](LICENSE)

Self-hosted, CPU-friendly audio transcription service powered by OpenAI Whisper.
Supports microphone recording and file upload transcription through a clean web UI.

## Features

- **Record & Transcribe** — Speak into your microphone, stop, and get a transcription
- **File Upload** — Upload audio/video files for higher-quality transcription (`small.en` model)
- **GitHub OAuth Sign-in** — Any GitHub user can access the app
- **Legacy Token Auth** — Backward-compatible token auth (being phased out)
- **Daily Mic Quota** — 15 minutes of mic transcription per user per day (upload is unlimited)
- **Access Logging** — All sign-ins, app access, and transcription activity are logged
- **CPU-Optimized** — Designed to run on a single CPU VPS (no GPU required)

## Architecture

SQS Signal runs in **3 Docker containers**:

| Container | Image | Internal Port | Exposed Port | Purpose |
|---|---|---|---|---|
| **Caddy** | `caddy:alpine` | 443/80 → 8892 | 443/80 | Reverse proxy, TLS termination, gzip |
| **Web** | `sqschat-web` | 8000 | 8892 | FastAPI app (auth, UI, orchestration) |
| **Whisper Service** | `docker-whisper-service:latest` | 8000 | 8891 | OpenAI Whisper Python (CPU inference) |

```
Internet → Caddy (:443/:80) → Web (:8892 → :8000) → Whisper (:8891 → :8000)
```

### Container Details

**Caddy** — Handles HTTPS, reverse-proxies to the web container, serves uploaded files for direct access.

**Web** — Python FastAPI application containing:
- Landing page (README/sign-in)
- GitHub OAuth + session management
- Transcription file upload endpoint
- WebSocket endpoint for mic recording
- SQLite database for sessions, users, mic quotas, and access logs

**Whisper Service** — Standalone OpenAI Whisper container. Loads the model once on startup and exposes a REST API for transcription. Uses CPU-only inference (`fp16=False`).

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A GitHub OAuth App (for sign-in)

### Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/rasrobo/sqs.chat.git
   cd sqs.chat
   ```

2. Create your environment file:
   ```bash
   cp .env.example .env
   ```

3. Edit `.env` and fill in your values:
   ```
   # Required — GitHub OAuth
   GITHUB_CLIENT_ID=your_client_id
   GITHUB_CLIENT_SECRET=your_client_secret
   GITHUB_CALLBACK_URL=https://your-domain.com/auth/github/callback

   # Optional — legacy token auth
   AUTH_TOKEN=your-random-token
   ```

4. Start the stack:
   ```bash
   docker compose up -d
   ```

5. Visit `https://your-domain.com` (or `http://localhost:8892` for local testing)

### GitHub OAuth Setup

1. Go to [GitHub Developer Settings](https://github.com/settings/developers) → OAuth Apps → New OAuth App
2. Homepage URL: `https://your-domain.com`
3. Callback URL: `https://your-domain.com/auth/github/callback`
4. Copy the Client ID and Client Secret into your `.env`

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_CLIENT_ID` | Yes* | — | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | Yes* | — | GitHub OAuth App client secret |
| `GITHUB_CALLBACK_URL` | Yes* | `https://sqs.chat/auth/github/callback` | OAuth callback URL |
| `AUTH_TOKEN` | No | — | Legacy token auth (being removed) |
| `WHISPER_SERVICE_URL` | No | `http://whisper-service:8000` | Internal whisper service URL |
| `MAX_FILE_SIZE_MB` | No | `50` | Max upload file size in MB |
| `COOKIE_SECURE` | No | `False` | Set to `True` for HTTPS production |

*\* GitHub OAuth is optional if you only use legacy token auth, but it is the recommended sign-in method.*

## Quota System

- **Mic**: 15 minutes per user per day (resets at midnight UTC)
- **Upload**: Unlimited
- Quota is enforced server-side using SQLite persistence
- Users see their remaining mic time in the app UI
- When quota is exhausted, the mic button is disabled with a clear message

## Access Logging

All access events are logged to both:
- **SQLite** (`/app/data/sqs.db` — `access_log` table)
- **Rotating log files** (`/app/logs/access.log`, `/app/logs/transcribe.log`, `/app/logs/error.log`)

Logged events: sign-in (success/failure), app access, transcription requests, mic usage, quota denials.

## License

This project uses a **Sustainable Use License** — see [LICENSE](LICENSE) for details.

In summary: source is visible and self-hostable, but you may not offer it as a competing commercial hosted transcription service without permission. This is inspired by the fair-code model (n8n, GitLab EE).

## Buy Me a Coffee

<!-- TODO: Add your Buy Me a Coffee link here if you have one -->
<!-- Example: https://buymeacoffee.com/yourusername -->

If you find this project useful, consider supporting its development.

---

Built by [Ras Robo / Side Quest Studios](https://sidequeststudios.xyz)
