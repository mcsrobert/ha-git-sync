import unittest

from ha_git_sync.errors import Error
from ha_git_sync.sources import Source, load

from helpers import VALID_HACS


class LoadTest(unittest.TestCase):
    def test_valid_file(self):
        self.assertEqual(load(VALID_HACS), [
            Source('integrations', 'amitfin/retry', 'v4.2.0', asset='retry.zip'),
            Source('integrations', 'hacs/integration', '2.0.5', asset='hacs.zip',
                   sha256='97be6b824a4f38e683728cc6dd72367f6b8bad0a43428b1b3b987a3087adf413'),
            Source('integrations', 'custom-components/nordpool', '0.0.18',
                   commit='1eaa2490943bb41a2e322e9349ba2d7c6746a94b'),
            Source('cards', 'RomRider/apexcharts-card', 'v2.2.3', asset='apexcharts-card.js'),
            Source('cards', 'mlamberts78/weather-chart-card', 'V2.4.11',
                   commit='71be08feeced42427ac69954168ff4ba05990dfc', path='dist/weather-chart-card.js'),
        ])

    def test_empty_file_and_lists(self):
        self.assertEqual(load(''), [])
        self.assertEqual(load('integrations:\ncards: []\n'), [])

    def test_versions_stay_strings(self):
        source, = load('integrations:\n  - repository: a/b\n    version: 1.10\n    asset: b.zip\n')
        self.assertEqual(source.version, '1.10')

    def assertInvalid(self, text, message):
        with self.assertRaisesRegex(Error, message):
            load(text)

    def test_rejects_unknown_top_level_keys(self):
        self.assertInvalid('themes: []\n', 'only contain')

    def test_rejects_wrong_empty_collections(self):
        self.assertInvalid('[]', 'only contain')
        self.assertInvalid('integrations: {}', 'integrations must be a list')
        self.assertInvalid('cards: ""', 'cards must be a list')

    def test_rejects_keys_out_of_renovate_order(self):
        self.assertInvalid('integrations:\n  - version: v1\n    repository: a/b\n    asset: b.zip\n', 'in that order')
        self.assertInvalid('integrations:\n  - repository: a/b\n    version: v1\n    asset: b.zip\n    sha256: '
                           + 'a' * 64 + '\n', 'in that order')

    def test_rejects_wrong_shape_per_category(self):
        self.assertInvalid('cards:\n  - repository: a/b\n    version: v1\n    commit: ' + 'a' * 40 + '\n', 'in that order')
        self.assertInvalid('integrations:\n  - repository: a/b\n    version: v1\n    commit: ' + 'a' * 40
                           + '\n    path: x\n', 'in that order')

    def test_rejects_bad_values(self):
        entry = 'integrations:\n  - repository: {}\n    version: {}\n    asset: {}\n'
        self.assertInvalid(entry.format('../x', 'v1', 'b.zip'), 'invalid repository')
        self.assertInvalid(entry.format('a/..', 'v1', 'b.zip'), 'invalid repository')
        self.assertInvalid(entry.format('a/b', '-v1', 'b.zip'), 'invalid version')
        self.assertInvalid(entry.format('a/b', 'v1', 'dir/b.zip'), 'invalid asset')
        self.assertInvalid(entry.format('a/b', 'v1', 'b.tar.gz'), 'must be a .zip')
        self.assertInvalid(entry.format('a/b', '[v1]', 'b.zip'), 'plain strings')
        self.assertInvalid('integrations:\n  - repository: a/b\n    version: v1\n    commit: ABC\n', 'full lowercase SHA')
        pinned = 'cards:\n  - repository: a/b\n    version: v1\n    sha256: {}\n    asset: b.js\n'
        for sha256 in ('A' * 64, 'a' * 63, 'sha256:' + 'a' * 64):
            self.assertInvalid(pinned.format(sha256), 'lowercase hex SHA-256')

    def test_rejects_unsafe_paths(self):
        card = 'cards:\n  - repository: a/b\n    version: v1\n    commit: ' + 'a' * 40 + '\n    path: {}\n'
        for path in ('/etc/passwd', '../x', 'dist/../x', 'dist/'):
            self.assertInvalid(card.format(path), 'invalid path')

    def test_rejects_duplicate_repositories(self):
        self.assertInvalid('integrations:\n  - repository: a/b\n    version: v1\n    asset: b.zip\n'
                           'cards:\n  - repository: A/B\n    version: v1\n    asset: b.js\n', 'more than once: a/b')

    def test_rejects_duplicate_keys(self):
        self.assertInvalid('cards:\n  - repository: a/b\n    version: v1\n    asset: b.js\ncards: []\n',
                           "line 5: duplicate key 'cards'")
        self.assertInvalid('cards:\n  - repository: a/b\n    version: v1\n    version: v2\n    asset: b.js\n',
                           "line 4: duplicate key 'version'")


if __name__ == '__main__':
    unittest.main()
