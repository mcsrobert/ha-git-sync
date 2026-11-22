"""Run the sidecar's hook in-process, as git-sync would after a sync, against a fake Home Assistant."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest

from ha_git_sync import sidecar

from helpers import DeployTest, Server, git

AS_ROOT = os.geteuid() == 0
ROOT = Path(__file__).resolve().parent.parent


class ImageTest(unittest.TestCase):
    def test_the_sidecar_needs_only_the_standard_library_and_the_modules_the_image_copies(self):
        script = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); import ha_git_sync.sidecar; '
                  'print(*sorted(name for name in sys.modules if name.startswith("ha_git_sync.")))')
        result = subprocess.run([sys.executable, '-I', '-S', '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        dockerfile = (ROOT / 'Dockerfile').read_text()
        for module in result.stdout.split():
            self.assertIn(f'ha_git_sync/{module.split(".")[1]}.py', dockerfile)


class HookTest(DeployTest):
    def hook(self, commit, token='ha-token'):
        """Run the hook once in the work clone, checked out at commit, and return its exit code and output."""
        environ = {'GITSYNC_HASH': commit, 'HA_URL': self.ha.url, 'HA_TOKEN_FILE': str(self.secret / token)}
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                sidecar.hook(self.config, self.work, environ)
        except SystemExit as error:
            return types.SimpleNamespace(returncode=1, stdout=stdout.getvalue(), stderr=str(error.code))
        return types.SimpleNamespace(returncode=0, stdout=stdout.getvalue(), stderr='')

    def test_first_valid_deploy_migrates_after_a_rejected_tip(self):
        self.hacs_era_config()
        self.ha.start()
        self.ha.state = 'RUNNING'
        stray = self.deploy({'configuration.yaml': 'stray\n'})
        self.assertEqual(self.hook(stray).returncode, 0)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'old\n')

        first = self.deploy({'configuration.yaml': 'valid\n'}, source='a' * 40)
        self.assertEqual(self.hook(first).returncode, 0)
        self.assertTrue((self.config / 'automations.yaml.pre-git').is_file())
        self.assertTrue((self.config / 'custom_components.pre-git/hacs').is_dir())
        self.assertEqual(len(self.ha.posts('/restart')), 1)

        same_tree = self.deploy({}, source='b' * 40)
        self.assertEqual(self.hook(same_tree).returncode, 0)
        self.assertEqual(len(self.ha.posts()), 2)  # The rejection notification and the migration restart.

    @unittest.skipIf(AS_ROOT, 'root reads unreadable files')
    def test_a_failed_install_changes_nothing_live(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        path = 'custom_components/demo/version'
        original = self.deploy({'configuration.yaml': 'v1', path: 'old',
                                'custom_components/obsolete/version': 'old'}, source='a' * 40)
        self.assertEqual(self.hook(original).returncode, 0)
        self.assertEqual(len(self.ha.posts('/restart')), 1)

        update = self.deploy({'configuration.yaml': 'v2', path: 'new'}, remove=['custom_components/obsolete'],
                             source='b' * 40)
        unreadable = self.work / path
        unreadable.chmod(0)
        try:
            result = self.hook(update)
        finally:
            unreadable.chmod(0o644)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot copy the checkout: [Errno 13] Permission denied', result.stderr)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v1')
        self.assertEqual((self.config / path).read_text(), 'old')
        self.assertEqual((self.config / 'custom_components/obsolete/version').read_text(), 'old')
        stray = self.deploy({'configuration.yaml': 'stray'})
        self.assertEqual(self.hook(stray).returncode, 0)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v1')
        git(self.work, 'checkout', '-q', original)
        self.assertEqual(self.hook(original).returncode, 0)
        self.assertEqual(len(self.ha.posts('/restart')), 1, 'restarted although no integration changed')
        self.assertEqual(self.ha.posts('/reload_all'), [])

    def test_rollback_after_startup_reloads_the_restored_configuration(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        original = self.deploy({'configuration.yaml': 'v1'}, source='a' * 40)
        self.assertEqual(self.hook(original).returncode, 0)
        self.ha.state = 'STARTING'
        update = self.deploy({'configuration.yaml': 'v2'}, source='b' * 40)
        self.assertNotEqual(self.hook(update).returncode, 0)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v2')
        self.ha.state = 'RUNNING'
        git(self.work, 'checkout', '-q', original)
        self.assertEqual(self.hook(original).returncode, 0)
        self.assertEqual(len(self.ha.posts('/reload_all')), 2)

    def test_preserves_required_restarts_across_outages_and_install_failures(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        plain = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        self.assertEqual(self.hook(plain).returncode, 0)
        self.assertEqual(len(self.ha.posts('/reload_all')), 1)

        first = self.deploy({'custom_components/retry/manifest.json': 'v1'}, source='b' * 40)
        self.assertEqual(self.hook(first).returncode, 0)
        self.assertEqual(len(self.ha.posts('/restart')), 1, 'adding the first integration did not restart')

        self.ha.stop()
        down = self.deploy({'custom_components/retry/manifest.json': 'v2'}, source='c' * 40)
        self.assertIn('not reachable', self.hook(down).stderr)
        self.assertEqual((self.config / 'custom_components' / 'retry' / 'manifest.json').read_text(), 'v2')
        self.ha.state = 'STARTING'
        self.ha.start()
        posts = len(self.ha.posts())
        starting = self.hook(down)
        self.assertNotEqual(starting.returncode, 0)
        self.assertIn('still starting', starting.stderr)
        self.assertEqual(len(self.ha.posts()), posts)

        self.ha.state = 'RUNNING'
        self.assertEqual(self.hook(down).returncode, 0)
        self.assertEqual(len(self.ha.posts('/reload_all')), 1)
        self.assertEqual(len(self.ha.posts('/restart')), 2, 'lost the restart while the API was unreachable')

        self.ha.status = 500
        self.ha.truncated_error_path = '/api/services/homeassistant/restart'
        refused = self.deploy({'custom_components/retry/manifest.json': 'v3'}, source='d' * 40)
        self.assertEqual(self.hook(refused).returncode, 0)
        message = self.ha.notifications('restart')[0]['message']
        self.assertIn('HTTP 503', message)
        self.assertIn('response was interrupted', message)
        self.ha.truncated_error_path = None
        self.ha.stop()
        self.assertEqual(self.hook(refused).returncode, 0)
        self.ha.start()
        self.ha.status = 200
        config_only = self.deploy({'configuration.yaml': 'v2\n'}, source='e' * 40)
        self.assertEqual(self.hook(config_only).returncode, 0)
        self.assertEqual(len(self.ha.posts('/restart')), 4, 'lost a refused restart during an API outage')
        self.assertEqual(len(self.ha.posts('/reload_all')), 1)

        community = self.config / 'www' / 'community'
        halfway = self.deploy({'custom_components/retry/manifest.json': 'v4', 'www/community/card/card.js': 'js'},
                              source='f' * 40)
        community.chmod(0o555)
        self.addCleanup(community.chmod, 0o755)
        self.assertNotEqual(self.hook(halfway).returncode, 0)
        self.assertEqual((self.config / 'custom_components' / 'retry' / 'manifest.json').read_text(), 'v4')
        community.chmod(0o755)
        self.assertEqual(self.hook(halfway).returncode, 0)
        self.assertEqual(len(self.ha.posts('/restart')), 5, 'lost the restart when the hook failed halfway')

    def test_retries_notification_after_interrupted_error_response(self):
        self.ha.start()
        stray = self.deploy({'configuration.yaml': 'v1'})
        self.ha.truncated_error_path = '/api/services/persistent_notification/create'
        self.assertNotEqual(self.hook(stray).returncode, 0)
        self.assertEqual(self.ha.notifications('deploy'), [])
        self.ha.truncated_error_path = None
        self.assertEqual(self.hook(stray).returncode, 0)
        self.assertEqual(len(self.ha.notifications('deploy')), 1)

    def test_retries_notifications_without_repeating_the_deploy(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        plain = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        self.assertEqual(self.hook(plain).returncode, 0)

        self.ha.status = 502
        self.ha.drop_notifications = True
        refused = self.deploy({'custom_components/retry/manifest.json': 'v1'}, source='b' * 40)
        for _ in range(2):
            result = self.hook(refused)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not reachable', result.stderr)
        self.assertEqual(len(self.ha.posts('/restart')), 1, 'repeated the restart to retry a notification')
        self.ha.drop_notifications = False
        for status in (429, 503):
            self.ha.notification_status = status
            self.assertNotEqual(self.hook(refused).returncode, 0)
            self.assertEqual(self.ha.notifications('restart'), [])
        self.assertEqual(len(self.ha.posts('/restart')), 1)
        self.ha.notification_status = 200
        self.assertEqual(self.hook(refused).returncode, 0)
        self.assertIn('HTTP 502', self.ha.notifications('restart')[0]['message'])

        self.ha.status = 500
        self.ha.message = '錯誤' * 400
        cut = self.deploy({'configuration.yaml': 'v2\n'}, source='c' * 40)
        self.assertEqual(self.hook(cut).returncode, 0)
        self.assertEqual(len(self.ha.notifications('restart')), 2, 'a notification cut in a character was rejected')
        expected = json.dumps({'message': self.ha.message}, ensure_ascii=False).encode()[:1000]
        self.assertTrue(self.ha.notifications('restart')[1]['message'].endswith(expected.decode(errors='ignore')))

        self.ha.message = b'a' * 1000 + b'\0tail'
        binary = self.deploy({'configuration.yaml': 'v3\n'}, source='d' * 40)
        self.assertEqual(self.hook(binary).returncode, 0)
        self.assertTrue(self.ha.notifications('restart')[-1]['message'].endswith('a' * 1000))

    def test_retries_failed_hooks(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        first = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        self.assertEqual(self.hook(first).returncode, 0)

        second = self.deploy({'configuration.yaml': 'v2\n'}, source='b' * 40)
        result = self.hook(second, token='bad-token')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('rejected the token', result.stderr)
        self.ha.state = 'NOT_RUNNING'
        result = self.hook(second)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('still starting', result.stderr)
        self.ha.stop()
        result = self.hook(second)
        self.assertNotEqual(result.returncode, 0, 'succeeded without Home Assistant')
        self.assertIn('not reachable', result.stderr)
        self.ha.state = 'RUNNING'
        self.ha.start()
        self.assertEqual(self.hook(second).returncode, 0)
        self.assertEqual(len(self.ha.posts('/reload_all')), 2, 'a failed hook marked the deploy as reloaded')

    @unittest.skipIf(AS_ROOT, 'root writes anywhere')
    def test_moving_aside_needs_write_access(self):
        self.hacs_era_config()
        www = self.config / 'www'
        www.chmod(0o555)
        self.addCleanup(www.chmod, 0o755)
        commit = self.deploy({'configuration.yaml': 'v1\n', 'www/community/card/card.js': 'js'}, source='a' * 40)
        result = self.hook(commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'Cannot move {self.config}/www/community aside', result.stderr)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'old\n')

    def test_replaces_the_copies_an_earlier_version_made_without_restarting(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        commit = self.deploy({'configuration.yaml': 'v1\n', 'custom_components/retry/manifest.json': 'v1'},
                             source='a' * 40)
        components = git(self.work, 'rev-parse', f'{commit}:custom_components')
        copies = self.config / 'custom_components'
        (copies / 'retry').mkdir(parents=True)
        (copies / 'retry' / 'manifest.json').write_text('v1')
        (copies / '.git-sync').write_text(f'{components}\n')
        state = self.config / '.git-sync' / 'state'
        state.mkdir(parents=True)
        (state / 'installed').write_text(f'{"0" * 40}\n{"1" * 40}\n{components}\n\n')

        self.assertEqual(self.hook(commit).returncode, 0)
        self.assertEqual(self.links()['custom_components'], '.git-sync/current/custom_components')
        self.assertFalse(os.path.lexists(self.config / 'custom_components.pre-git'))
        self.assertEqual(self.ha.posts('/restart'), [])
        self.assertEqual(len(self.ha.posts('/reload_all')), 1)

    @unittest.skipIf(AS_ROOT, 'root removes anything')
    def test_keeps_deploying_when_old_integrations_resist_removal(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        first = self.deploy({'configuration.yaml': 'v1\n', 'custom_components/retry/__init__.py': 'v1'},
                            source='a' * 40)
        self.assertEqual(self.hook(first).returncode, 0)
        cache = self.config / 'custom_components' / 'retry' / '__pycache__'
        cache.mkdir()
        (cache / '__init__.pyc').write_bytes(b'')
        cache.chmod(0o555)
        state = self.config / '.git-sync' / 'state'
        trash = state / 'trash'
        self.addCleanup(lambda: [path.chmod(0o755) for path in [cache, *trash.glob('*/retry/__pycache__')]
                                 if path.exists()])

        second = self.deploy({'configuration.yaml': 'v2\n'}, source='b' * 40)
        self.assertEqual(self.hook(second).returncode, 0)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v2\n')
        self.assertTrue(cache.is_dir(), 'copied the integrations again for a change to the configuration')
        self.assertFalse(trash.exists(), 'a change to the configuration left something in the trash')
        self.assertEqual(len(list((state / 'trees').iterdir())), 1)

        third = self.deploy({'custom_components/retry/__init__.py': 'v2'}, source='c' * 40)
        self.assertEqual(self.hook(third).returncode, 0)
        self.assertEqual((self.config / 'custom_components' / 'retry' / '__init__.py').read_text(), 'v2')
        self.assertEqual(len(list((state / 'trees').iterdir())), 1)
        self.assertEqual(len(list((state / 'components').iterdir())), 1)
        self.assertTrue(list(trash.glob('*/retry/__pycache__')), 'expected the leftover in trash')

    @unittest.skipIf(AS_ROOT, 'root writes anywhere')
    def test_a_commit_ci_did_not_build_restores_an_install_that_failed_halfway(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        first = self.deploy({'configuration.yaml': 'v1\n', 'www/community/card/card.js': 'v1'}, source='a' * 40)
        self.assertEqual(self.hook(first).returncode, 0)
        community = self.config / 'www' / 'community'
        community.chmod(0o555)
        self.addCleanup(community.chmod, 0o755)
        second = self.deploy({'configuration.yaml': 'v2\n', 'www/community/card/card.js': 'v2'}, source='b' * 40)
        self.assertNotEqual(self.hook(second).returncode, 0)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v2\n', 'the install did not fail halfway')

        stray = self.deploy({'configuration.yaml': 'stray\n'})
        self.assertEqual(self.hook(stray).returncode, 0)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), 'v1\n')
        self.assertEqual((community / 'card' / 'card.js').read_text(), 'v1')
        self.assertTrue(self.ha.notifications('deploy'))

    def test_adopts_a_snapshot_without_integrations_that_the_earlier_version_made(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        commit = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        content = git(self.work, 'rev-parse', f'{commit}^{{tree}}')
        state = self.config / '.git-sync' / 'state'
        (state / 'trees' / content).mkdir(parents=True)
        (state / 'trees' / content / 'configuration.yaml').write_text('v1\n')
        (state / 'installed').write_text(f'{commit}\n{content}\n\n\n')
        (self.config / '.git-sync' / 'current').symlink_to(f'state/trees/{content}')
        (self.config / 'configuration.yaml').symlink_to('.git-sync/current/configuration.yaml')
        (self.config / 'custom_components').mkdir()
        (self.config / 'custom_components' / '.git-sync').write_text('\n')
        (self.config / 'www' / 'community').mkdir(parents=True)
        (self.config / 'www' / 'community' / '.git-sync').write_text('installing\n')

        self.assertEqual(self.hook(commit).returncode, 0)
        self.assertEqual(self.links()['custom_components'], '.git-sync/current/custom_components')
        self.assertFalse(os.path.lexists(self.config / 'custom_components.pre-git'))
        self.assertEqual(self.ha.posts('/restart'), [])
        self.assertEqual((self.config / 'www' / 'community' / '.git-sync').read_text().strip(), '')

    def test_does_not_send_the_token_where_a_redirect_points(self):
        elsewhere = Server()
        self.addCleanup(elsewhere.close)
        self.ha.redirect = f'{elsewhere.url}/api/config'
        self.ha.start()
        commit = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        result = self.hook(commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('HTTP 302', result.stderr)
        self.assertEqual(elsewhere.requests, [], 'followed the redirect with the token')

    def test_ready_once_the_tip_is_installed_or_rejected(self):
        self.ha.start()
        self.ha.state = 'RUNNING'
        incoming = self.config / '.git-sync' / 'incoming'

        def tip(commit):
            incoming.unlink(missing_ok=True)
            incoming.symlink_to(f'repo/.worktrees/{commit}')

        self.assertFalse(sidecar.ready(self.config))
        first = self.deploy({'configuration.yaml': 'v1\n'}, source='a' * 40)
        (self.config / '.git-sync').mkdir()
        tip(first)
        self.assertFalse(sidecar.ready(self.config), 'ready before the first install')
        self.assertEqual(self.hook(first).returncode, 0)
        self.assertTrue(sidecar.ready(self.config))

        second = self.deploy({'configuration.yaml': 'v2\n'}, source='b' * 40)
        tip(second)
        self.assertFalse(sidecar.ready(self.config), 'ready before installing the new tip')
        stray = self.deploy({'configuration.yaml': 'stray\n'})
        tip(stray)
        self.assertEqual(self.hook(stray).returncode, 0)
        self.assertTrue(sidecar.ready(self.config), 'a rejected tip kept Home Assistant from starting')


if __name__ == '__main__':
    unittest.main()
