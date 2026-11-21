import contextlib
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import warnings

from ha_git_sync.build import build
from ha_git_sync.errors import Error
from ha_git_sync.github import GitHub

from helpers import Server, repository, zip_bytes


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.server = Server()
        self.addCleanup(self.server.close)
        self.github = GitHub(api=self.server.url, git=f'file://{self.tmp}/remote')
        self.source = self.tmp / 'source'
        self.source.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.source, check=True)
        self.write('configuration.yaml', 'homeassistant:\n')
        for domain in ('automation', 'script', 'template'):
            self.write(f'blueprints/{domain}/homeassistant/default.yaml', 'blueprint:\n')
        self.output = self.tmp / 'out' / 'deploy'

    def write(self, name, text):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()) as log:
            build(self.source, self.output, self.github)
        return log.getvalue()

    def files(self):
        return sorted(path.relative_to(self.output).as_posix() for path in self.output.rglob('*') if path.is_file())

    def assertFails(self, message):
        with self.assertRaisesRegex(Error, message):
            self.build()
        self.assertFalse(self.output.exists())
        if self.output.parent.exists():
            self.assertEqual(os.listdir(self.output.parent), [])

    def test_copies_the_repository_except_github_and_ignored_files(self):
        self.write('automations/a.yaml', '[]\n')
        self.write('packages/old.yaml.disable', 'b:\n')
        self.write('.github/workflows/ci.yaml', 'on: push\n')
        self.write('.gitignore', '/ignored.txt\n')
        self.write('ignored.txt', 'x\n')
        self.build()
        self.assertEqual(self.files(), [
            '.gitignore',
            'automations/a.yaml',
            'blueprints/automation/homeassistant/default.yaml',
            'blueprints/script/homeassistant/default.yaml',
            'blueprints/template/homeassistant/default.yaml',
            'configuration.yaml',
            'packages/old.yaml.disable',
        ])
        for name in ('custom_components', 'www/community'):
            self.assertTrue((self.output / name).is_dir(), name)

    def test_includes_staged_and_untracked_files_but_not_deleted_ones(self):
        self.write('scripts/a.yaml', '{}\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.source, check=True)
        (self.source / 'scripts' / 'a.yaml').unlink()
        self.write('scripts/b.yaml', '{}\n')
        self.build()
        self.assertIn('scripts/b.yaml', self.files())
        self.assertNotIn('scripts/a.yaml', self.files())

    def test_installs_integrations_and_cards(self):
        self.server.release('o/zipped', 'v1', {'zipped.zip': zip_bytes({'manifest.json': '{"domain": "zipped"}',
                                                                         'sensor.py': 'x'})})
        self.server.release('o/card', 'v2', {'card.js': b'js'})
        source = repository(self.tmp / 'remote', 'o/source', {'custom_components/src/manifest.json': '{"domain": "src"}',
                                                               'README.md': 'x'}, '1.0')
        dist = repository(self.tmp / 'remote', 'o/dist-card', {'dist/dist-card.js': 'js', 'src/x.ts': 'ts'}, 'V3')
        self.write('hacs.yaml', f"""
integrations:
  - repository: o/zipped
    version: v1
    asset: zipped.zip
  - repository: o/source
    version: '1.0'
    commit: {source}
cards:
  - repository: o/card
    version: v2
    asset: card.js
  - repository: o/dist-card
    version: V3
    commit: {dist}
    path: dist/dist-card.js
""")
        log = self.build()
        installed = [name for name in self.files() if name.startswith(('custom_components/', 'www/'))]
        self.assertEqual(installed, [
            'custom_components/src/manifest.json',
            'custom_components/zipped/manifest.json',
            'custom_components/zipped/sensor.py',
            'www/community/card/card.js',
            'www/community/dist-card/dist-card.js',
        ])
        self.assertIn('o/source 1.0 -> custom_components/src', log)

    def test_installs_assets_by_pinned_sha256(self):
        self.server.release('o/card', 'v1', {'card.js': b'js'}, digests={'card.js': None})
        self.write('hacs.yaml', 'cards:\n  - repository: o/card\n    version: v1\n'
                   f'    sha256: {hashlib.sha256(b"js").hexdigest()}\n    asset: card.js\n')
        self.build()
        self.assertIn('www/community/card/card.js', self.files())
        shutil.rmtree(self.output)
        self.write('hacs.yaml', 'cards:\n  - repository: o/card\n    version: v1\n'
                   f'    sha256: {"0" * 64}\n    asset: card.js\n')
        self.assertFails('does not match its pinned sha256')

    def test_normalizes_permissions(self):
        self.write('pyscript/run.py', '')
        os.chmod(self.source / 'pyscript' / 'run.py', 0o700)
        self.build()
        for path in [self.output, *self.output.rglob('*')]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755 if path.is_dir() else 0o644, path)

    def test_refuses_existing_output(self):
        self.output.mkdir(parents=True)
        with self.assertRaisesRegex(Error, 'already exists'):
            self.build()

    def test_requires_configuration_yaml(self):
        (self.source / 'configuration.yaml').unlink()
        self.assertFails('configuration.yaml is missing')

    def test_blueprints_are_optional(self):
        shutil.rmtree(self.source / 'blueprints')
        self.build()
        self.assertFalse((self.output / 'blueprints').exists())

    def test_requires_a_blueprint_for_every_domain(self):
        for domain in ('automation', 'script', 'template'):
            with self.subTest(domain=domain):
                path = self.source / 'blueprints' / domain / 'homeassistant' / 'default.yaml'
                path.unlink()
                self.assertFails(f'blueprints/{domain}/ must contain a blueprint')
                path.write_text('blueprint:\n')

    def test_rejects_symlinks(self):
        (self.source / 'packages').mkdir()
        (self.source / 'packages' / 'link.yaml').symlink_to('/etc/hosts')
        self.assertFails('packages/link.yaml: symlinks are not allowed')

    def test_rejects_submodules(self):
        repository(self.tmp, 'sub', {'a.yaml': ''}, 'v1')
        subprocess.run(['git', '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(self.tmp / 'sub'),
                        'packages/sub'], cwd=self.source, check=True, capture_output=True)
        self.assertFails('packages/sub: submodules are not allowed')

    def test_rejects_secrets_yaml_anywhere(self):
        for name in ('secrets.yaml', 'includes/secrets.yaml', 'packages/nested/Secrets.yaml'):
            with self.subTest(name=name):
                self.write(name, 'password: real\n')
                self.assertFails('keep secrets.yaml out of the repository')
                (self.source / name).unlink()

    def test_rejects_www(self):
        self.write('www/image.png', 'png')
        self.assertFails('www/ is not deployed')

    def test_keeps_integrations_committed_to_the_repository(self):
        self.write('custom_components/mine/manifest.json', '{"domain": "mine"}')
        self.build()
        self.assertIn('custom_components/mine/manifest.json', self.files())

    def test_rejects_entries_the_sidecar_would_skip(self):
        for name in ('custom_components/shared.py', 'custom_components/.mine/manifest.json'):
            with self.subTest(name=name):
                self.write(name, '')
                self.assertFails('installs only visible directories')
                shutil.rmtree(self.source / 'custom_components')
        self.server.release('o/.card', 'v1', {'card.js': b'js'})
        self.write('hacs.yaml', 'cards:\n  - repository: o/.card\n    version: v1\n    asset: card.js\n')
        self.assertFails('installs only visible directories')

    def test_rejects_a_pinned_integration_the_repository_already_has(self):
        self.write('custom_components/a/manifest.json', '{"domain": "a"}')
        self.install_zip(zip_bytes({'manifest.json': '{"domain": "a"}'}))
        self.assertFails('which the repository or another entry already uses')

    def test_rejects_editor_files_at_the_root(self):
        for name, folder in (('automations.yaml', 'automations/'), ('scripts.yaml', 'scripts/'),
                             ('scenes.yaml', 'scenes/')):
            with self.subTest(name=name):
                self.write(name, '[]\n')
                self.assertFails(f'such as {folder}')
                (self.source / name).unlink()

    def test_rejects_two_integrations_with_one_domain(self):
        for name in ('a', 'b'):
            self.server.release(f'o/{name}', 'v1', {'x.zip': zip_bytes({'manifest.json': '{"domain": "same"}'})})
        self.write('hacs.yaml', 'integrations:\n'
                   '  - repository: o/a\n    version: v1\n    asset: x.zip\n'
                   '  - repository: o/b\n    version: v1\n    asset: x.zip\n')
        self.assertFails('o/b v1 installs into custom_components/same, which the repository or another')

    def test_rejects_integration_without_valid_manifest(self):
        for manifest, message in (({}, 'no manifest.json'), ({'manifest.json': '{"domain": "../x"}'}, 'invalid'),
                                  ({'manifest.json': '[]'}, 'no manifest.json')):
            with self.subTest(manifest=manifest):
                self.server.release('o/a', 'v1', {'x.zip': zip_bytes({'x.py': '', **manifest})})
                self.write('hacs.yaml', 'integrations:\n  - repository: o/a\n    version: v1\n    asset: x.zip\n')
                self.assertFails(message)

    def test_source_integration_needs_one_matching_folder(self):
        cases = {
            'o/none': ({'README.md': ''}, 'exactly one directory'),
            'o/two': ({'custom_components/a/manifest.json': '{"domain": "a"}',
                       'custom_components/b/manifest.json': '{"domain": "b"}'}, 'exactly one directory'),
            'o/mismatch': ({'custom_components/a/manifest.json': '{"domain": "b"}'}, "invalid integration domain: 'b'"),
        }
        for name, (files, message) in cases.items():
            with self.subTest(name=name):
                commit = repository(self.tmp / 'remote', name, files, 'v1')
                self.write('hacs.yaml', f'integrations:\n  - repository: {name}\n    version: v1\n    commit: {commit}\n')
                self.assertFails(message)

    def install_zip(self, files):
        self.server.release('o/a', 'v1', {'x.zip': files})
        self.write('hacs.yaml', 'integrations:\n  - repository: o/a\n    version: v1\n    asset: x.zip\n')

    def test_rejects_file_and_directory_with_one_name(self):
        manifest = '{"domain": "a"}'
        for files in ({'manifest.json': manifest, 'sub': '', 'sub/x.py': ''},
                      {'manifest.json': manifest, 'sub/x.py': '', 'sub': ''},
                      {'manifest.json': manifest, 'Sub': '', 'sub/x.py': ''}):
            with self.subTest(files=list(files)):
                self.install_zip(zip_bytes(files))
                self.assertFails('both a file and a directory')

    def test_rejects_names_differing_only_by_case(self):
        self.install_zip(zip_bytes({'manifest.json': '{"domain": "a"}', 'a.py': '1', 'A.py': '2'}))
        self.assertFails('conflicting paths')

    def test_rejects_duplicate_zip_entries(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            data = zip_bytes([('manifest.json', '{"domain": "a"}'), ('a.py', '1'), ('a.py', '2')])
        self.install_zip(data)
        self.assertFails('more than once')

    def test_reports_write_failures_as_errors(self):
        self.install_zip(zip_bytes({'manifest.json': '{"domain": "a"}', 'x' * 300: ''}))
        self.assertFails('cannot write custom_components/a')


if __name__ == '__main__':
    unittest.main()
