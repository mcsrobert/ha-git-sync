import hashlib
import http.server
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile

TOKEN = 'secret-token'

VALID_HACS = """
integrations:
  - repository: amitfin/retry
    version: v4.2.0
    asset: retry.zip
  - repository: hacs/integration
    version: 2.0.5
    sha256: 97be6b824a4f38e683728cc6dd72367f6b8bad0a43428b1b3b987a3087adf413
    asset: hacs.zip
  - repository: custom-components/nordpool
    version: 0.0.18
    commit: 1eaa2490943bb41a2e322e9349ba2d7c6746a94b
cards:
  - repository: RomRider/apexcharts-card
    version: v2.2.3
    asset: apexcharts-card.js
  - repository: mlamberts78/weather-chart-card
    version: V2.4.11
    commit: 71be08feeced42427ac69954168ff4ba05990dfc
    path: dist/weather-chart-card.js
"""


class Server:
    """Serves fixed responses and records the headers of each request."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append((self.path, dict(self.headers)))
                if self.path not in server.routes:
                    self.send_error(404)
                    return
                body = server.routes[self.path]
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def release(self, repository, tag, assets, digests=None):
        """Publish a release whose assets are served with their real digests unless overridden."""
        digests = digests or {}
        listing = []
        for name, data in assets.items():
            self.routes[f'/download/{repository}/{tag}/{name}'] = data
            listing.append({'name': name, 'browser_download_url': f'{self.url}/download/{repository}/{tag}/{name}',
                            'digest': digests.get(name, 'sha256:' + hashlib.sha256(data).hexdigest())})
        self.routes[f'/repos/{repository}/releases/tags/{tag}'] = json.dumps({'assets': listing}).encode()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in (files.items() if isinstance(files, dict) else files):
            archive.writestr(name, data)
    return buffer.getvalue()


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def repository(root, name, files, tag, annotated=False, symlinks=None):
    """Create a Git repository under root/name with one commit and a tag; returns the commit."""
    path = Path(root, name)
    path.mkdir(parents=True)
    git(path, 'init', '-q', '-b', 'main')
    for file, data in files.items():
        (path / file).parent.mkdir(parents=True, exist_ok=True)
        (path / file).write_text(data)
    for link, target in (symlinks or {}).items():
        (path / link).parent.mkdir(parents=True, exist_ok=True)
        (path / link).symlink_to(target)
    git(path, 'add', '-A')
    git(path, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '-m', 'init')
    if annotated:
        git(path, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'tag', '-a', tag, '-m', tag)
    else:
        git(path, 'tag', tag)
    return git(path, 'rev-parse', 'HEAD')

class DeployTest(unittest.TestCase):
    """A deployed branch to push deploys to, a work clone of it, a /config folder and a fake Home Assistant."""
    DOCKER = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / 'config'
        self.config.mkdir()
        self.remote = self.tmp / 'remote.git'
        self.work = self.tmp / 'work'
        self.secret = self.tmp / 'secret'
        self.secret.mkdir()
        (self.secret / 'ha-token').write_text(f'{TOKEN}\n')
        (self.secret / 'bad-token').write_text('wrong\n')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', str(self.remote))
        git(self.tmp, 'clone', '-q', str(self.remote), str(self.work))
        self.ha = HomeAssistant(docker=self.DOCKER)
        self.addCleanup(self.ha.close)

    def deploy(self, files, source=None, remove=()):
        for name, text in files.items():
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text(text)
        for name in remove:
            shutil.rmtree(self.work / name) if (self.work / name).is_dir() else (self.work / name).unlink()
        git(self.work, 'add', '-A')
        message = ['-m', 'Deploy']
        if source:
            message += ['-m', f'Source-Commit: {source}']
        git(self.work, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty',
            *message)
        git(self.work, 'push', '-q', 'origin', 'HEAD:refs/heads/deployed')
        return git(self.work, 'rev-parse', 'HEAD')

    def links(self):
        return {path.name: str(path.readlink()) for path in self.config.iterdir() if path.is_symlink()}

    def hacs_era_config(self):
        for name, text in {'configuration.yaml': 'old\n', 'automations.yaml': '[]\n', 'scripts.yaml': '{}\n',
                           'scenes.yaml': '[]\n', 'blueprints/automation/old.yaml': 'x', 'pyscript/old.py': '',
                           'custom_components/hacs/manifest.json': '{}', 'www/community/oldcard/x.js': '',
                           'www/local.png': '', '.storage/core.config': '{}'}.items():
            (self.config / name).parent.mkdir(parents=True, exist_ok=True)
            (self.config / name).write_text(text)


class HomeAssistant:
    """Answers the API calls the hook makes and records them. Until start(), nothing listens on its port, as when
    Home Assistant is down."""

    def __init__(self, docker=False):
        self.docker = docker
        self.state = 'STARTING'
        self.status = 200
        self.message = 'The configuration is not valid'
        self.truncated_error_path = None
        self.restart_closes = True
        self.redirect = None
        self.drop_notifications = False
        self.notification_status = 200
        self.calls = []
        self.notes = []
        self.running = False
        ha = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def respond(self, status, body):
                data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                if self.path == ha.truncated_error_path:
                    data = data[:data.index('é'.encode()) + 1]
                self.end_headers()
                self.wfile.write(data)

            def redirected(self):
                if ha.redirect:
                    self.send_response(302)
                    self.send_header('Location', ha.redirect)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                return bool(ha.redirect)

            def do_GET(self):
                ha.calls.append(('GET', self.path, self.headers.get('Authorization'), None))
                if self.redirected():
                    return
                if self.headers.get('Authorization') != f'Bearer {TOKEN}':
                    return self.respond(401, {'message': 'unauthorized'})
                self.respond(200, {'state': ha.state, 'version': '2026.10.4'})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                ha.calls.append(('POST', self.path, self.headers.get('Authorization'), body))
                if self.redirected():
                    return
                if self.headers.get('Authorization') != f'Bearer {TOKEN}':
                    return self.respond(401, {'message': 'unauthorized'})
                try:
                    data = json.loads(body.decode())
                except ValueError:
                    return self.respond(400, {'message': 'Data should be valid JSON.'})
                if self.path == ha.truncated_error_path:
                    return self.respond(503, {'message': 'échec'})
                if self.path.endswith('/persistent_notification/create'):
                    if ha.drop_notifications:
                        self.close_connection = True
                        return
                    if ha.notification_status != 200:
                        return self.respond(ha.notification_status, {})
                    ha.notes.append(data)
                if self.path.endswith(('/reload_all', '/restart')) and ha.status != 200:
                    body = ha.message if isinstance(ha.message, bytes) else {'message': ha.message}
                    return self.respond(ha.status, body)
                if self.path.endswith('/restart') and ha.restart_closes:
                    self.close_connection = True
                    return
                self.respond(200, [])

            def log_message(self, *args):
                pass

        self.handler = Handler
        self.reserve_port()

    def reserve_port(self):
        # Docker Desktop forwards host.docker.internal to the host's loopback; on Linux it is the bridge gateway.
        address = '0.0.0.0' if self.docker and sys.platform != 'darwin' else '127.0.0.1'
        self.httpd = http.server.ThreadingHTTPServer((address, 0), self.handler, bind_and_activate=False)
        self.httpd.server_bind()
        self.port = self.httpd.server_port
        host = 'host.docker.internal' if self.docker else '127.0.0.1'
        self.url = f'http://{host}:{self.port}'

    def start(self):
        self.httpd.server_activate()
        self.running = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.close()
        self.reserve_port()

    def close(self):
        if self.running:
            self.httpd.shutdown()
            self.running = False
        self.httpd.server_close()

    def posts(self, suffix=''):
        return [call for call in self.calls if call[0] == 'POST' and call[1].endswith(suffix)]

    def notifications(self, name=None):
        return [note for note in self.notes if name is None or note['notification_id'] == f'git_sync_{name}']


def wait(condition, what, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(f'timed out waiting for {what}')
