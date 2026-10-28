"""Entrypoint for hosting panels that start a Python file directly."""
import os
import secrets
from pathlib import Path

if not os.environ.get('API_KEY'):
    secret_path = Path('.api-key')
    if not secret_path.exists():
        fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as secret_file:
            secret_file.write(secrets.token_urlsafe(32))
    os.environ['API_KEY'] = secret_path.read_text().strip()

import uvicorn
from app import app

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('SERVER_PORT', os.environ.get('PORT', '8080'))), workers=1)
