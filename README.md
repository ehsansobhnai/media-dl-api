# Media Download API

Small authenticated Python backend for public YouTube and Instagram media.
Deploy on Render as a **Free Web Service**. This is a hobby deployment, not an unlimited or always-on downloader.

## Render

- Runtime: Python 3.12
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check: `/healthz`
- Instance: **Free**
- Environment: set `API_KEY` to a long random secret. Never commit it.
- Native Render runtimes include FFmpeg and Node.js.
- `render.yaml` provides the equivalent Blueprint with a generated API key.

## Bot integration

Send `X-API-Key` on all `/api/` requests. Do not put the secret in a URL.

1. `POST /api/jobs` with JSON `{"url":"https://www.youtube.com/watch?v=VIDEO_ID","mode":"audio","quality":720}`.
2. Poll `GET /api/jobs/{id}` every 5 seconds until `ready` or `failed`.
3. Download `GET /api/jobs/{id}/file` with the same header.
4. Upload the downloaded file through your Telegram bot, then `DELETE /api/jobs/{id}`.

The bot must download the file itself. A Telegram fetch-by-URL cannot supply the required header.
Interactive OpenAPI documentation is at `/docs`. Health is at `/healthz`.

## Limits

- One download at a time; up to five active/queued jobs.
- Defaults: 250 MiB output, 1200 MiB temporary storage, 30-minute job timeout, one-hour file retention.
- Optional environment values: `MAX_FILE_MB`, `MAX_STORAGE_MB`, `JOB_TIMEOUT_SECONDS`, `FILE_TTL_SECONDS`.
- Only individual public HTTPS YouTube/Instagram video URLs, no playlists, live streams or arbitrary URL fetching.
- Audio output: MP3 128 kbps. Video: MP4, max 360/480/720/1080p; no expensive video transcoding.
- Cloud IP blocking, login requirements, deleted media and upstream extractor changes can prevent downloads.
- Render Free sleeps after inactivity; files and jobs disappear on sleep, restart or deployment.
- Keep polling while a job runs. A missing job after a restart requires resubmission.
- Render bandwidth limits and Telegram upload limits apply independently.
- No browser cookies, login credentials, bot tokens or media are stored in the repository.

## Local run

Install FFmpeg and Node, use Python 3.12+, install `requirements.txt`, set `API_KEY`, then run the start command with a local port.
