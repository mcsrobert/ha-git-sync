import json
from pathlib import Path
import re
import unittest

from helpers import VALID_HACS

PRESET = json.loads((Path(__file__).parent.parent / 'default.json').read_text())


def matches(text):
    """What the preset's hacs.yaml managers extract from text, translated from JavaScript to Python regex."""
    found = []
    for manager in PRESET['customManagers']:
        if not any('hacs' in pattern for pattern in manager['managerFilePatterns']):
            continue
        for pattern in manager['matchStrings']:
            for match in re.finditer(pattern.replace('(?<', '(?P<'), text):
                found.append((manager['datasourceTemplate'], match.groupdict()))
    return found


class HacsManagersTest(unittest.TestCase):
    def test_every_entry_matches_exactly_one_manager(self):
        self.assertEqual(sorted(groups['depName'] for _, groups in matches(VALID_HACS)),
                         sorted(re.findall(r'repository: (\S+)', VALID_HACS)))

    def test_captures_quoted_and_unquoted_values(self):
        for quote in ('', '"', "'"):
            with self.subTest(quote=quote):
                text = (f'integrations:\n  - repository: hacs/integration\n    version: {quote}2.0.5{quote}\n'
                        f'    sha256: {quote}{"a" * 64}{quote}\n    asset: hacs.zip\n'
                        f'  - repository: o/r\n    version: {quote}v1{quote}\n'
                        f'    commit: {quote}{"b" * 40}{quote}\n')
                self.assertEqual(matches(text), [
                    ('github-release-attachments',
                     {'depName': 'hacs/integration', 'currentValue': '2.0.5', 'currentDigest': 'a' * 64}),
                    ('github-tags', {'depName': 'o/r', 'currentValue': 'v1', 'currentDigest': 'b' * 40}),
                ])


class ResourceManagerTest(unittest.TestCase):
    MANAGER = next(manager for manager in PRESET['customManagers'] if 'dashboard resources' in manager['description'])

    def extract(self, text):
        pattern = self.MANAGER['matchStrings'][0].replace('(?<', '(?P<')
        return [{key: value for key, value in match.groupdict().items() if value}
                for match in re.finditer(pattern, text)]

    def test_captures_the_repository_and_version_of_each_resource(self):
        text = ('lovelace:\n  resource_mode: yaml\n  resources:\n'
                '    # renovate: repository=RomRider/apexcharts-card\n'
                '    - url: /local/community/apexcharts-card/apexcharts-card.js?v=v2.2.3\n      type: module\n'
                '    # renovate: datasource=github-tags repository=o/card\n'
                '    - type: module\n      url: "/local/community/card/card.js?v=V2.4.11"\n'
                '    - url: /local/community/other/other.js?v=1.0.0\n')
        self.assertEqual(self.extract(text), [
            {'depName': 'RomRider/apexcharts-card', 'currentValue': 'v2.2.3'},
        ])
        text = text.replace('    - type: module\n      url:', '    - url:')
        self.assertEqual(self.extract(text)[1], {'depName': 'o/card', 'datasource': 'github-tags',
                                                 'currentValue': 'V2.4.11'})

    def test_takes_the_datasource_before_the_repository(self):
        text = '    # renovate: datasource=github-tags repository=o/card\n    - url: /local/community/card/card.js?v=v1.0.0\n'
        self.assertEqual(self.extract(text), [
            {'depName': 'o/card', 'datasource': 'github-tags', 'currentValue': 'v1.0.0'},
        ])

    def test_matches_files_with_windows_line_endings(self):
        text = ('    # renovate: repository=o/card\r\n'
                '    - url: /local/community/card/card.js?v=v1.0.0\r\n      type: module\r\n')
        self.assertEqual(self.extract(text), [{'depName': 'o/card', 'currentValue': 'v1.0.0'}])

    def test_uses_the_versioning_and_a_release_datasource_of_the_hacs_yaml_managers(self):
        hacs = next(manager for manager in PRESET['customManagers'] if 'hacs' in manager['managerFilePatterns'][0])
        self.assertEqual(self.MANAGER['versioningTemplate'], hacs['versioningTemplate'])
        self.assertIn('{{else}}github-releases{{/if}}', self.MANAGER['datasourceTemplate'])
