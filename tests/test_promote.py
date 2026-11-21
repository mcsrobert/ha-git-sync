import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROMOTE = Path(__file__).resolve().parents[1] / 'promote' / 'promote.sh'


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class PromoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.remote = self.tmp / 'remote.git'
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', str(self.remote))
        self.repo = self.tmp / 'repo'
        git(self.tmp, 'clone', '-q', str(self.remote), str(self.repo))
        self.commits = [self.commit(f'change {index}') for index in range(3)]
        git(self.repo, 'push', '-q', 'origin', 'main')

    def commit(self, message):
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty',
            '-m', message)
        return git(self.repo, 'rev-parse', 'HEAD')

    def deploy(self, files):
        path = Path(tempfile.mkdtemp(dir=self.tmp))
        for name, text in files.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(text)
        return path

    def promote(self, files, source):
        result = subprocess.run(['bash', str(PROMOTE), str(self.deploy(files)), source], cwd=self.repo,
                                capture_output=True, text=True, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout + result.stderr

    def deployed(self):
        return git(self.remote, 'rev-parse', 'refs/heads/deployed')

    def message(self, commit):
        return git(self.remote, 'log', '-1', '--format=%B', commit)

    def files(self, commit):
        return git(self.remote, 'ls-tree', '-r', '--name-only', commit).splitlines()

    def test_first_promotion_creates_the_branch_with_every_file(self):
        self.promote({'configuration.yaml': 'a\n', '.gitignore': '/*\n', 'custom_components/x/__init__.py': ''},
                     self.commits[0])
        commit = self.deployed()
        self.assertEqual(self.files(commit), ['.gitignore', 'configuration.yaml', 'custom_components/x/__init__.py'])
        self.assertEqual(git(self.remote, 'rev-list', '--count', commit), '1')
        self.assertIn(f'Source-Commit: {self.commits[0]}', self.message(commit))
        self.assertIn('change 0', self.message(commit))
        self.assertEqual(git(self.remote, 'log', '-1', '--format=%an', commit), 'github-actions[bot]')

    def test_newer_source_fast_forwards(self):
        self.promote({'configuration.yaml': 'a\n'}, self.commits[0])
        first = self.deployed()
        self.promote({'configuration.yaml': 'b\n'}, self.commits[1])
        self.assertEqual(git(self.remote, 'rev-parse', 'deployed^'), first)
        self.assertIn(f'Source-Commit: {self.commits[1]}', self.message(self.deployed()))

    def test_older_source_never_moves_the_branch_back(self):
        self.promote({'configuration.yaml': 'b\n'}, self.commits[1])
        newer = self.deployed()
        log = self.promote({'configuration.yaml': 'a\n'}, self.commits[0])
        self.assertEqual(self.deployed(), newer)
        self.assertIn('already contains', log)

    def test_unchanged_files_still_record_the_newest_source(self):
        self.promote({'configuration.yaml': 'a\n'}, self.commits[0])
        self.promote({'configuration.yaml': 'a\n'}, self.commits[2])
        newest = self.deployed()
        self.assertIn(f'Source-Commit: {self.commits[2]}', self.message(newest))
        self.promote({'configuration.yaml': 'b\n'}, self.commits[1])
        self.assertEqual(self.deployed(), newest)

    def test_replaces_a_branch_ci_did_not_build(self):
        self.promote({'configuration.yaml': 'a\n'}, self.commits[0])
        git(self.repo, 'push', '-q', 'origin', '+main:deployed')
        log = self.promote({'configuration.yaml': 'b\n'}, self.commits[1])
        self.assertEqual(git(self.remote, 'rev-list', '--count', 'deployed'), '1')
        self.assertIn(f'Source-Commit: {self.commits[1]}', self.message(self.deployed()))
        self.assertIn('which CI did not build', log)

    def test_builds_on_the_last_build_below_commits_ci_did_not_make(self):
        self.promote({'configuration.yaml': 'a\n'}, self.commits[0])
        built = self.deployed()
        git(self.repo, 'fetch', '-q', 'origin', 'deployed')
        git(self.repo, 'switch', '-q', '-c', 'stray', 'FETCH_HEAD')
        git(self.repo, 'push', '-q', 'origin', f'{self.commit("manual change")}:refs/heads/deployed')
        git(self.repo, 'switch', '-q', 'main')
        self.promote({'configuration.yaml': 'b\n'}, self.commits[1])
        self.assertEqual(git(self.remote, 'rev-parse', 'deployed^'), built)

    def test_restores_the_last_build_when_nothing_newer_is_on_main(self):
        self.promote({'configuration.yaml': 'b\n'}, self.commits[1])
        built = self.deployed()
        git(self.repo, 'fetch', '-q', 'origin', 'deployed')
        git(self.repo, 'switch', '-q', '-c', 'stray', 'FETCH_HEAD')
        stray = self.commit('manual change')
        git(self.repo, 'push', '-q', 'origin', f'{stray}:refs/heads/deployed')
        self.promote({'configuration.yaml': 'a\n'}, self.commits[0])
        self.assertEqual(self.deployed(), built)

    def test_refuses_a_shallow_clone(self):
        shallow = self.tmp / 'shallow'
        git(self.tmp, 'clone', '-q', '--depth', '1', f'file://{self.remote}', str(shallow))
        result = subprocess.run(['bash', str(PROMOTE), str(self.deploy({'configuration.yaml': 'a\n'})),
                                 self.commits[-1]], cwd=shallow, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fetch-depth: 0', result.stderr)
        self.assertNotIn('deployed', git(self.remote, 'branch', '--list'))

    def test_builds_on_a_rollback(self):
        self.promote({'configuration.yaml': 'a\n'}, self.commits[0])
        old = self.deployed()
        self.promote({'configuration.yaml': 'b\n'}, self.commits[1])
        git(self.remote, 'update-ref', 'refs/heads/deployed', old)
        self.promote({'configuration.yaml': 'c\n'}, self.commits[2])
        self.assertEqual(git(self.remote, 'rev-parse', 'deployed^'), old)


if __name__ == '__main__':
    unittest.main()
