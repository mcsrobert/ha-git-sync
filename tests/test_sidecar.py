"""Run the image against a local repository and a fake Home Assistant. Needs Docker and HA_GIT_SYNC_IMAGE."""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from ha_git_sync.check import layout

from helpers import git

IMAGE = os.environ.get('HA_GIT_SYNC_IMAGE')
TOKEN = 'secret-token'


class HomeAssistant:
    """Answers the API calls the hook makes and records them."""

    def __init__(self):
        self.state = 'STARTING'
        self.reload_status = 200
        self.calls = []
        ha = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def respond(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                ha.calls.append(('GET', self.path, self.headers.get('Authorization'), None))
                if self.headers.get('Authorization') != f'Bearer {TOKEN}':
                    return self.respond(401, {'message': 'unauthorized'})
                self.respond(200, {'state': ha.state, 'version': '2026.10.4'})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
                ha.calls.append(('POST', self.path, self.headers.get('Authorization'), body))
                if self.headers.get('Authorization') != f'Bearer {TOKEN}':
                    return self.respond(401, {'message': 'unauthorized'})
                json.loads(body)
                if self.path.endswith('/reload_all') and ha.reload_status != 200:
                    return self.respond(ha.reload_status, {'message': 'Cannot quick reload all YAML configurations '
                                                           'because the configuration is not valid'})
                self.respond(200, [])

            def log_message(self, *args):
                pass

        # Docker Desktop forwards host.docker.internal to the host's loopback; on Linux it is the bridge gateway.
        address = '127.0.0.1' if sys.platform == 'darwin' else '0.0.0.0'
        self.httpd = http.server.ThreadingHTTPServer((address, 0), Handler)
        self.url = f'http://host.docker.internal:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def posts(self, suffix):
        return [call for call in self.calls if call[0] == 'POST' and call[1].endswith(suffix)]

    def notifications(self, name=None):
        notes = [json.loads(call[3]) for call in self.posts('/persistent_notification/create')]
        return [note for note in notes if name is None or note['notification_id'] == f'git_sync_{name}']

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wait(condition, what, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(f'timed out waiting for {what}')


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)


@unittest.skipUnless(IMAGE, 'set HA_GIT_SYNC_IMAGE to the image to test')
class SidecarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / 'config'
        self.config.mkdir()
        self.state = self.config / '.git-sync' / 'state'
        self.remote = self.tmp / 'remote.git'
        self.work = self.tmp / 'work'
        secret = self.tmp / 'secret'
        secret.mkdir()
        (secret / 'ha-token').write_text(f'{TOKEN}\n')
        (secret / 'bad-token').write_text('wrong\n')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', str(self.remote))
        git(self.tmp, 'clone', '-q', str(self.remote), str(self.work))
        self.ha = HomeAssistant()
        self.addCleanup(self.ha.close)
        self.mounts = ['--user', f'{os.getuid()}:{os.getgid()}', '--add-host', 'host.docker.internal:host-gateway',
                       '-v', f'{self.config}:/config', '-v', f'{self.remote}:/remote.git:ro',
                       '-v', f'{secret}:/etc/git-secret:ro', '-e', f'HA_URL={self.ha.url}']

    def start(self):
        name = f'ha-git-sync-test-{uuid.uuid4().hex[:8]}'
        docker('run', '-d', '--name', name, *self.mounts, '-e', 'GITSYNC_REPO=file:///remote.git',
               '-e', 'GITSYNC_PERIOD=1s', '-e', 'GITSYNC_EXECHOOK_BACKOFF=1s',
               '-e', 'GITSYNC_STALE_WORKTREE_TIMEOUT=5s', IMAGE)
        self.addCleanup(self.stop, name)
        return name

    def stop(self, name):
        logs = docker('logs', name, check=False)
        if logs.returncode == 0:
            (self.tmp / f'{name}.log').write_text(logs.stdout + logs.stderr)
            docker('rm', '--force', name)

    def ready(self, name):
        return docker('exec', name, '/ha-git-sync/ready', check=False).returncode == 0

    def hook(self, commit, token='ha-token'):
        """Run the hook once in a clone of the deployed branch, as git-sync would after a sync."""
        return docker('run', '--rm', *self.mounts, '-v', f'{self.work}:/work:ro', '-w', '/work',
                      '-e', f'GITSYNC_HASH={commit}', '-e', f'HA_TOKEN_FILE=/etc/git-secret/{token}',
                      '--entrypoint', '/ha-git-sync/hook', IMAGE, check=False)

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

    def recorded(self, name):
        path = self.state / name
        return path.read_text().strip() if path.exists() else None

    def links(self):
        return {path.name: str(path.readlink()) for path in self.config.iterdir() if path.is_symlink()}

    def hacs_era_config(self):
        for name, text in {'configuration.yaml': 'old\n', 'automations.yaml': '[]\n', 'scripts.yaml': '{}\n',
                           'scenes.yaml': '[]\n', 'blueprints/automation/old.yaml': 'x', 'pyscript/old.py': '',
                           'custom_components/hacs/manifest.json': '{}', 'www/community/oldcard/x.js': '',
                           'www/local.png': '', '.storage/core.config': '{}'}.items():
            (self.config / name).parent.mkdir(parents=True, exist_ok=True)
            (self.config / name).write_text(text)

    def test_deploys_migrates_and_reloads(self):
        self.hacs_era_config()
        name = self.start()
        time.sleep(2)
        self.assertFalse(self.ready(name), 'ready before the first sync')

        first = self.deploy({'configuration.yaml': 'v1\n', '.gitignore': '*.log\n', 'README.md': 'readme',
                             'automations/a.yaml': '[]\n', 'pyscript/app.py': 'app',
                             'blueprints/automation/b.yaml': 'b', 'custom_components/retry/manifest.json': 'retry v1',
                             'custom_components/retry/sub/x.py': '1', 'custom_components/nordpool/manifest.json': 'np',
                             'www/community/card/card.js': 'card v1'}, source='a' * 40)
        wait(lambda: self.ready(name), 'ready after the first install')
        self.assertEqual(self.links(), {entry: f'.git-sync/current/{entry}' for entry in
                                        ('README.md', 'automations', 'blueprints', 'configuration.yaml', 'pyscript')})
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v1\n')
        self.assertEqual((self.config / 'pyscript' / 'app.py').read_text(), 'app')
        self.assertEqual((self.config / 'configuration.yaml.pre-git').read_text(), 'old\n')
        self.assertTrue((self.config / 'blueprints.pre-git' / 'automation' / 'old.yaml').is_file())
        for editor in ('automations.yaml', 'scripts.yaml', 'scenes.yaml'):
            self.assertTrue((self.config / f'{editor}.pre-git').is_file(), editor)
            self.assertFalse((self.config / editor).exists(), editor)
        self.assertTrue((self.config / 'custom_components.pre-git' / 'hacs').is_dir())
        self.assertTrue((self.config / 'www' / 'community.pre-git' / 'oldcard').is_dir())
        self.assertTrue((self.config / 'www' / 'local.png').is_file())
        self.assertTrue((self.config / '.storage' / 'core.config').is_file())
        self.assertEqual((self.config / 'custom_components' / 'retry' / 'sub' / 'x.py').read_text(), '1')
        self.assertFalse((self.config / 'custom_components' / 'retry').is_symlink())
        self.assertEqual((self.config / 'www' / 'community' / 'card' / 'card.js').read_text(), 'card v1')
        self.assertEqual(self.ha.posts('/reload_all'), [], 'reloaded while Home Assistant starts')

        mirror = self.tmp / 'mirror'
        layout((self.config / '.git-sync' / 'current').resolve(), mirror)
        self.assertEqual({path.name: str(path.readlink()) for path in mirror.iterdir() if path.is_symlink()},
                         self.links(), 'check_config sees a different layout than the sidecar makes')

        self.ha.state = 'RUNNING'
        wait(lambda: self.recorded('reloaded') == first, 'reload once Home Assistant runs')
        self.assertEqual(len(self.ha.posts('/reload_all')), 1)
        self.assertTrue(all(call[2] == f'Bearer {TOKEN}' for call in self.ha.calls))
        self.assertEqual(self.ha.notifications(), [], 'notified after the first install')

        second = self.deploy({'configuration.yaml': 'v2\n', 'custom_components/retry/manifest.json': 'retry v2'},
                             source='b' * 40, remove=['custom_components/retry/sub', 'pyscript'])
        wait(lambda: self.recorded('reloaded') == second, 'reload of the second deploy')
        wait(lambda: self.ha.notifications('restart'), 'restart notification')
        self.assertIn('bbbbbbb', self.ha.notifications('restart')[0]['message'])
        self.assertEqual((self.config / 'custom_components' / 'retry' / 'manifest.json').read_text(), 'retry v2')
        self.assertFalse((self.config / 'custom_components' / 'retry' / 'sub').exists())
        self.assertEqual(list((self.config / 'custom_components').glob('.retry.*')), [])
        self.assertNotIn('pyscript', self.links(), 'kept the link to a removed folder')

        notes = len(self.ha.notifications())
        third = self.deploy({'configuration.yaml': 'v3\n'}, source='c' * 40)
        wait(lambda: self.recorded('reloaded') == third, 'reload of a configuration-only deploy')
        time.sleep(1)
        self.assertEqual(len(self.ha.notifications()), notes, 'a configuration-only deploy notified')

        stray = self.deploy({'configuration.yaml': 'stray\n'})
        wait(lambda: self.recorded('reloaded') == stray, 'stray commit handled')
        self.assertTrue(self.ha.notifications('deploy'))
        self.assertEqual(len(self.ha.posts('/reload_all')), 3, 'reloaded a commit CI did not build')
        self.assertTrue(self.ready(name), 'a stray commit kept Home Assistant from starting')

        self.ha.reload_status = 500
        fourth = self.deploy({'configuration.yaml': 'v4\n'}, source='d' * 40)
        wait(lambda: self.recorded('reloaded') == fourth, 'failed reload recorded')
        failed = self.ha.notifications('reload')
        self.assertTrue(failed and 'HTTP 500' in failed[0]['message'] and 'not valid' in failed[0]['message'])
        self.ha.reload_status = 200

        (self.config / 'automations.yaml').write_text('- id: ui\n')
        fifth = self.deploy({'configuration.yaml': 'v5\n'}, source='e' * 40, remove=['custom_components/nordpool'])
        wait(lambda: self.recorded('reloaded') == fifth, 'reload of the fifth deploy')
        wait(lambda: self.ha.notifications('editor'), 'editor notification')
        self.assertIn('automations.yaml', self.ha.notifications('editor')[0]['message'])
        self.assertFalse((self.config / 'custom_components' / 'nordpool').exists())
        self.assertTrue((self.config / 'custom_components' / 'retry').exists())
        self.assertTrue((self.config / 'automations.yaml').is_file(), 'moved a later UI file aside')

        status = docker('inspect', '--format', '{{.State.Health.Status}}', name).stdout.strip()
        self.assertEqual(status, 'healthy')

    def test_restarts_quietly_and_retries_failed_hooks(self):
        first = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        self.ha.state = 'RUNNING'
        name = self.start()
        wait(lambda: self.recorded('reloaded') == first, 'first reload')
        self.stop(name)

        calls = len(self.ha.calls)
        name = self.start()
        wait(lambda: self.ready(name), 'ready after a restart', timeout=15)
        time.sleep(3)
        self.assertEqual(len(self.ha.calls), calls, 'a restart called Home Assistant')
        self.stop(name)

        second = self.deploy({'configuration.yaml': 'v2\n'}, source='b' * 40)
        result = self.hook(second, token='bad-token')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('rejected the token', result.stderr)
        self.ha.state = 'NOT_RUNNING'
        result = self.hook(second)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('still starting', result.stderr)
        self.ha.close()
        result = self.hook(second)
        self.assertNotEqual(result.returncode, 0, 'succeeded without Home Assistant')
        self.assertIn('not reachable', result.stderr)
        self.assertNotEqual(self.recorded('reloaded'), second)

    def test_moving_aside_needs_write_access(self):
        self.hacs_era_config()
        www = self.config / 'www'
        www.chmod(0o555)
        self.addCleanup(www.chmod, 0o755)
        commit = self.deploy({'configuration.yaml': 'v1\n', 'www/community/card/card.js': 'js'}, source='a' * 40)
        result = self.hook(commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot move /config/www/community aside', result.stderr)
        self.assertNotEqual(self.recorded('installed'), commit)


if __name__ == '__main__':
    unittest.main()
