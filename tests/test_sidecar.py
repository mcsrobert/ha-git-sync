"""Run the image against a local repository and a fake Home Assistant. Needs Docker and HA_GIT_SYNC_IMAGE.

test_hook.py covers the hook's behaviour in-process; these tests cover what only the image shows: git-sync running
the hook, the readiness probe, the health check and a restart of the sidecar.
"""
import os
import subprocess
import time
import unittest
import uuid

from ha_git_sync.layout import layout

from helpers import TOKEN, DeployTest, wait

IMAGE = os.environ.get('HA_GIT_SYNC_IMAGE')


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)


@unittest.skipUnless(IMAGE, 'set HA_GIT_SYNC_IMAGE to the image to test')
class SidecarTest(DeployTest):
    DOCKER = True

    def setUp(self):
        super().setUp()
        self.mounts = ['--user', f'{os.getuid()}:{os.getgid()}', '--add-host', 'host.docker.internal:host-gateway',
                       '-v', f'{self.config}:/config', '-v', f'{self.remote}:/remote.git:ro',
                       '-v', f'{self.secret}:/etc/git-secret:ro']

    def start(self):
        name = f'ha-git-sync-test-{uuid.uuid4().hex[:8]}'
        docker('run', '-d', '--name', name, *self.mounts, '-e', 'GITSYNC_REPO=file:///remote.git',
               '-e', f'HA_URL={self.ha.url}',
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

    def wait_ready(self, name, what, timeout=60):
        try:
            wait(lambda: self.ready(name), what, timeout)
        except AssertionError as error:
            logs = docker('logs', name, check=False)
            raise AssertionError(f'{error}\n{(logs.stdout + logs.stderr)[-4000:]}') from None

    def hook(self, commit, token='ha-token'):
        """Run the hook once in a clone of the deployed branch, as git-sync would after a sync."""
        return docker('run', '--rm', '--cap-drop=DAC_OVERRIDE', *self.mounts, '-v', f'{self.work}:/work:ro', '-w', '/work',
                      '-e', f'HA_URL={self.ha.url}',
                      '-e', f'GITSYNC_HASH={commit}', '-e', f'HA_TOKEN_FILE=/etc/git-secret/{token}',
                      '--entrypoint', '/ha-git-sync/hook', IMAGE, check=False)

    def test_deploys_migrates_and_reloads(self):
        self.hacs_era_config()
        name = self.start()
        time.sleep(2)
        self.assertFalse(self.ready(name), 'ready before the first sync')

        first = self.deploy({'configuration.yaml': 'v1\n', '.gitignore': '*.log\n', 'README.md': 'readme',
                             'automations/a.yaml': '[]\n', 'pyscript/app.py': 'app',
                             'blueprints/automation/b.yaml': 'b', 'custom_components/retry/manifest.json': 'retry v1',
                             'custom_components/retry/sub/x.py': '1', 'custom_components/nordpool/manifest.json': 'np',
                             'www/community/card\n/name.js': 'newline',
                             'www/community/card/card.js': 'card v1'}, source='a' * 40)
        self.wait_ready(name, 'ready after the first install')
        self.assertEqual(self.links(), {entry: f'.git-sync/current/{entry}' for entry in
                                        ('README.md', 'automations', 'blueprints', 'configuration.yaml',
                                         'custom_components', 'pyscript')})
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v1\n')
        self.assertEqual((self.config / 'pyscript' / 'app.py').read_text(), 'app')
        self.assertEqual((self.config / 'configuration.yaml.pre-git').read_text(), 'old\n')
        self.assertTrue((self.config / 'blueprints.pre-git' / 'automation' / 'old.yaml').is_file())
        for editor in ('automations.yaml', 'scripts.yaml', 'scenes.yaml'):
            self.assertTrue((self.config / f'{editor}.pre-git').is_file(), editor)
            self.assertFalse((self.config / editor).exists(), editor)
        self.assertTrue((self.config / 'custom_components.pre-git' / 'hacs').is_dir())
        self.assertTrue((self.config / 'www' / 'community.pre-git' / 'oldcard').is_dir())
        self.assertEqual((self.config / 'www/community/card\n/name.js').read_text(), 'newline')
        self.assertTrue((self.config / 'www' / 'local.png').is_file())
        self.assertTrue((self.config / '.storage' / 'core.config').is_file())
        self.assertEqual((self.config / 'custom_components' / 'retry' / 'sub' / 'x.py').read_text(), '1')
        self.assertEqual((self.config / 'www' / 'community' / 'card' / 'card.js').read_text(), 'card v1')

        mirror = self.tmp / 'mirror'
        layout((self.config / '.git-sync' / 'current').resolve(), mirror)
        self.assertEqual({path.name: str(path.readlink()) for path in mirror.iterdir() if path.is_symlink()},
                         self.links(), 'check_config sees a different layout than the sidecar makes')

        self.ha.start()
        wait(lambda: self.ha.calls, 'a call once Home Assistant answers')
        self.assertEqual(self.ha.posts('/reload_all'), [], 'reloaded while Home Assistant starts')
        self.ha.state = 'RUNNING'
        wait(lambda: self.ha.posts('/restart'), 'restart once Home Assistant runs')
        self.assertEqual(self.ha.posts('/reload_all'), [])
        self.assertEqual(len(self.ha.posts('/restart')), 1, 'lost the restart while the API was unreachable')
        self.assertTrue(all(call[2] == f'Bearer {TOKEN}' for call in self.ha.calls))
        self.assertEqual(self.ha.notifications(), [], 'notified after the first install')

        second = self.deploy({'configuration.yaml': 'v2\n', 'custom_components/retry/manifest.json': 'retry v2'},
                             source='b' * 40, remove=['custom_components/retry/sub', 'pyscript'])
        wait(lambda: len(self.ha.posts('/homeassistant/restart')) == 2, 'restart for the second deploy')
        self.assertEqual(len(self.ha.posts('/reload_all')), 0, 'reloaded instead of restarting')
        self.assertEqual((self.config / 'custom_components' / 'retry' / 'manifest.json').read_text(), 'retry v2')
        self.assertFalse((self.config / 'custom_components' / 'retry' / 'sub').exists())
        self.assertEqual(list((self.config / 'custom_components').glob('.retry.*')), [])
        self.assertNotIn('pyscript', self.links(), 'kept the link to a removed folder')

        third = self.deploy({'configuration.yaml': 'v3\n', 'www/community/card/card.js': 'card v2'}, source='c' * 40)
        wait(lambda: self.ha.posts('/reload_all'), 'reload of a deploy that changes a card')
        self.assertEqual((self.config / 'www' / 'community' / 'card' / 'card.js').read_text(), 'card v2')
        self.assertEqual(len(self.ha.posts('/homeassistant/restart')), 2, 'restarted for a card')
        self.assertEqual(self.ha.notifications(), [], 'notified about a deploy that worked')

        stray = self.deploy({'configuration.yaml': 'stray\n'})
        wait(lambda: self.ha.notifications(), 'stray commit notification')
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v3\n')
        self.assertTrue(self.ha.notifications('deploy'))
        self.assertEqual(len(self.ha.posts('/reload_all')), 1, 'reloaded a commit CI did not build')
        self.assertTrue(self.ready(name), 'a stray commit kept Home Assistant from starting')

        self.ha.status = 500
        self.deploy({'configuration.yaml': 'v4\n'}, source='d' * 40)
        wait(lambda: self.ha.notifications('reload'), 'failed reload notification')
        self.assertEqual(len(self.ha.posts('/reload_all')), 2)
        failed = self.ha.notifications('reload')
        self.assertTrue(failed and 'HTTP 500' in failed[0]['message'] and 'not valid' in failed[0]['message'])

        self.deploy({'configuration.yaml': 'v5\n'}, source='e' * 40, remove=['custom_components/nordpool'])
        wait(lambda: self.ha.notifications('restart'), 'failed restart notification')
        failed = self.ha.notifications('restart')
        self.assertTrue(failed and 'HTTP 500' in failed[0]['message'] and 'not valid' in failed[0]['message'])
        self.ha.status = 200
        self.ha.restart_closes = False

        (self.config / 'automations.yaml').write_text('- id: ui\n')
        self.deploy({'configuration.yaml': 'v6\n'}, source='f' * 40)
        wait(lambda: len(self.ha.posts('/homeassistant/restart')) == 4, 'restart left over from the fifth deploy')
        self.assertEqual(len(self.ha.posts('/reload_all')), 2, 'forgot the restart after it failed')
        wait(lambda: self.ha.notifications('editor'), 'editor notification')
        self.assertIn('automations.yaml', self.ha.notifications('editor')[0]['message'])
        self.assertFalse((self.config / 'custom_components' / 'nordpool').exists())
        self.assertTrue((self.config / 'custom_components' / 'retry').exists())
        self.assertTrue((self.config / 'automations.yaml').is_file(), 'moved a later UI file aside')

        status = docker('inspect', '--format', '{{.State.Health.Status}}', name).stdout.strip()
        self.assertEqual(status, 'healthy')

    def test_cannot_become_root(self):
        result = docker('run', '--rm', '--user', '0:0', '--entrypoint', 'find', IMAGE, '/', '-xdev', '-type', 'f',
                        '(', '-perm', '-4000', '-o', '-perm', '-2000', ')')
        self.assertEqual(result.stdout, '', '/etc/passwd is writable, so a setuid program lets the sidecar user '
                                            'become root')

    def test_restarts_quietly(self):
        self.ha.start()
        first = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        self.ha.state = 'RUNNING'
        result = self.hook(first)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.ha.posts('/reload_all')), 1)

        posts = len(self.ha.posts())
        for start in ('a new clone', 'an existing clone'):
            name = self.start()
            self.wait_ready(name, f'ready after starting with {start}', timeout=15)
            time.sleep(3)
            self.assertEqual(len(self.ha.posts()), posts, f'starting with {start} reloaded or notified')
            self.stop(name)


if __name__ == '__main__':
    unittest.main()
