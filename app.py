"""Single-process, authenticated media download jobs for a small Render instance."""
import asyncio
import contextlib
import hmac
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

ROOT = Path(tempfile.mkdtemp(prefix='media-dl-'))
KEY = os.getenv('API_KEY', '')
MAX_BYTES = int(os.getenv('MAX_FILE_MB', '250')) * 1024 * 1024
MAX_DISK = int(os.getenv('MAX_STORAGE_MB', '1200')) * 1024 * 1024
TIMEOUT = int(os.getenv('JOB_TIMEOUT_SECONDS', '1800'))
TTL = int(os.getenv('FILE_TTL_SECONDS', '3600'))
MAX_JOBS = 5
jobs = {}
lock = threading.RLock()


def canonical_url(value: str) -> str:
    p = urlsplit(value.strip())
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Use a public HTTPS YouTube or Instagram video URL')
    host = (p.hostname or '').lower()
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'):
        if host == 'youtu.be':
            vid = p.path.strip('/')
        elif p.path == '/watch':
            vid = parse_qs(p.query).get('v', [''])[0]
        else:
            match = re.fullmatch(r'/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?', p.path)
            vid = match.group(1) if match else ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            raise ValueError('Provide one YouTube video, not a playlist or channel')
        return 'https://www.youtube.com/watch?v=' + vid
    if host in ('instagram.com', 'www.instagram.com'):
        match = re.fullmatch(r'/(p|reel|reels|tv)/([A-Za-z0-9_-]+)/?', p.path)
        if match:
            return f'https://www.instagram.com/{match.group(1)}/{match.group(2)}/'
    raise ValueError('Only individual public YouTube and Instagram media URLs are supported')


class Download(BaseModel):
    url: str = Field(max_length=2048)
    mode: Literal['audio', 'video'] = 'audio'
    quality: Literal[360, 480, 720, 1080] = 720
    _url = field_validator('url')(canonical_url)


def authenticate(x_api_key: str = Header(default='')):
    if not KEY:
        raise HTTPException(503, 'API_KEY has not been configured')
    if not hmac.compare_digest(x_api_key, KEY):
        raise HTTPException(401, 'Invalid API key')


def disk_size(directory=ROOT):
    total = 0
    for p in directory.rglob('*'):
        try:
            if p.is_file():
                total += p.stat().st_size
        except FileNotFoundError:
            pass
    return total


def stop_process(proc):
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def run_download(job_id, request):
    directory = ROOT / job_id
    directory.mkdir()
    command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
               '--no-progress', '--no-warnings', '--socket-timeout', '20', '--retries', '2',
               '--fragment-retries', '2', '--concurrent-fragments', '1', '--restrict-filenames',
               '--max-filesize', str(MAX_BYTES), '--match-filters', '!is_live',
               '--js-runtimes', 'node', '--output', str(directory / 'media.%(ext)s'),
               '--print-to-file', 'after_move:filepath', str(directory / 'result.txt')]
    if request.mode == 'audio':
        command += ['-f', 'bestaudio/best', '-x', '--audio-format', 'mp3', '--audio-quality', '128K']
    else:
        command += ['-f', f'bv[height<={request.quality}][ext=mp4]+ba[ext=m4a]/b[height<={request.quality}][ext=mp4]',
                    '--merge-output-format', 'mp4']
    command += ['--', request.url]
    proc = None
    try:
        with open(directory / 'worker.log', 'w') as log:
            proc = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
            started = time.monotonic()
            while proc.poll() is None:
                if time.monotonic() - started > TIMEOUT:
                    raise RuntimeError('Download exceeded the time limit')
                if disk_size(directory) > MAX_BYTES * 3 or disk_size() > MAX_DISK:
                    raise RuntimeError('Temporary storage limit reached')
                time.sleep(1)
        if proc.returncode:
            log = (directory / 'worker.log').read_text(errors='replace')
            if any(s in log.lower() for s in ['sign in', 'login required', 'cookies', 'not a bot', '403', '429']):
                raise RuntimeError('Source blocked the request or requires login. Cloud IP downloads are not guaranteed.')
            raise RuntimeError('Source download failed. Media may be unavailable, restricted, or unsupported.')
        result = directory / 'result.txt'
        if not result.exists():
            raise RuntimeError('No media produced; it may exceed the size limit')
        file = Path(result.read_text().splitlines()[-1]).resolve()
        if file.parent != directory.resolve() or not file.is_file():
            raise RuntimeError('Invalid output file')
        if file.stat().st_size > MAX_BYTES:
            raise RuntimeError('Output exceeds the file size limit')
        with lock:
            jobs[job_id].update(status='ready', path=file, size=file.stat().st_size, completed=time.time())
    except Exception as exc:
        if proc:
            stop_process(proc)
        shutil.rmtree(directory, ignore_errors=True)
        with lock:
            jobs[job_id].update(status='failed', error=str(exc), completed=time.time())
    finally:
        if proc:
            stop_process(proc)


def cleanup():
    now = time.time()
    with lock:
        for job_id, job in list(jobs.items()):
            if job['status'] in ('ready', 'failed') and now - job['completed'] > TTL and not job.get('readers'):
                shutil.rmtree(ROOT / job_id, ignore_errors=True)
                del jobs[job_id]


async def worker():
    while True:
        cleanup()
        with lock:
            item = next(((i, j) for i, j in jobs.items() if j['status'] == 'queued'), None)
            if item:
                item[1]['status'] = 'downloading'
        if item:
            await asyncio.to_thread(run_download, item[0], item[1]['request'])
        else:
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title='Media Download API', version='1.0.0', lifespan=lifespan)


@app.get('/')
@app.get('/healthz')
def health():
    return {'status': 'ok', 'configured': bool(KEY), 'ffmpeg': bool(shutil.which('ffmpeg')),
            'service': 'media-dl-api', 'docs': '/docs'}


@app.post('/api/jobs', status_code=202, dependencies=[Depends(authenticate)])
def create_job(request: Download):
    cleanup()
    with lock:
        if sum(j['status'] in ('queued', 'downloading') for j in jobs.values()) >= MAX_JOBS:
            raise HTTPException(429, 'Queue is full; retry later')
        if len(jobs) >= 50 or disk_size() + MAX_BYTES * 3 > MAX_DISK:
            raise HTTPException(507, 'Storage capacity reached; delete completed jobs')
        job_id = uuid.uuid4().hex
        jobs[job_id] = dict(status='queued', request=request, created=time.time(), readers=0)
    return {'id': job_id, 'status': 'queued', 'status_url': f'/api/jobs/{job_id}'}


def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found or expired; restart may have cleared it')
    return job


@app.get('/api/jobs/{job_id}', dependencies=[Depends(authenticate)])
def status(job_id: str):
    with lock:
        job = get_job(job_id)
        data = {'id': job_id, 'status': job['status']}
        if job['status'] == 'ready':
            data.update(size_bytes=job['size'], download_url=f'/api/jobs/{job_id}/file',
                        expires_at=job['completed'] + TTL)
        if job['status'] == 'failed':
            data['error'] = job['error']
        return data


class LeasedFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with lock:
                if self.job_id in jobs:
                    jobs[self.job_id]['readers'] -= 1


@app.get('/api/jobs/{job_id}/file', dependencies=[Depends(authenticate)])
def download(job_id: str):
    with lock:
        job = get_job(job_id)
        if job['status'] != 'ready':
            raise HTTPException(409, 'File is not ready')
        job['readers'] += 1
        response = LeasedFileResponse(job['path'], filename=job['path'].name,
                    media_type='audio/mpeg' if job['request'].mode == 'audio' else 'video/mp4')
        response.job_id = job_id
        return response


@app.delete('/api/jobs/{job_id}', dependencies=[Depends(authenticate)])
def delete(job_id: str):
    with lock:
        job = get_job(job_id)
        if job['status'] == 'downloading' or job.get('readers'):
            raise HTTPException(409, 'Job is in use')
        shutil.rmtree(ROOT / job_id, ignore_errors=True)
        del jobs[job_id]
    return {'deleted': True}
