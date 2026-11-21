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
