"""Exercise the Python resolver the check action runs before setup-python, including failed requests."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

CHECK = Path(__file__).resolve().parents[1] / 'check'


def run(script, *args, response='', exit_code=0, env=None):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        curl = root / 'curl'
        curl.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CURL_ARGS"\n'
                        'printf "%s" "$CURL_RESPONSE"\nexit "$CURL_EXIT"\n')
        curl.chmod(0o755)
        calls = root / 'args'
        result = subprocess.run(['bash', str(CHECK / script), *args], capture_output=True, text=True,
                                env={**os.environ, 'PATH': f'{root}:{os.environ["PATH"]}', 'CURL_ARGS': str(calls),
                                     'CURL_RESPONSE': response, 'CURL_EXIT': str(exit_code), **(env or {})})
        return result, calls.read_text().splitlines() if calls.exists() else []


class PythonVersionTest(unittest.TestCase):
    def test_exact_release_tag_and_patch_version(self):
        result, args = run('resolve-python.sh', '2026.10.4', response='3.14.9\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '3.14.9\n')
        self.assertEqual(args[-1], 'https://raw.githubusercontent.com/home-assistant/core/2026.10.4/.python-version')

    def test_invalid_release_is_rejected_before_fetch(self):
        for release in ('dev', 'latest', '2026.10', '../dev', '2026.10.4\n'):
            with self.subTest(release=release):
                result, args = run('resolve-python.sh', release, response='3.14.9\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(args, [])

    def test_invalid_pin_is_not_passed_to_setup_python(self):
        for response in ('', '3.14', '<html>Not found</html>', '3.14.9\n3.15.0', '3.15.0rc1'):
            with self.subTest(response=response):
                result, _ = run('resolve-python.sh', '2026.10.4', response=response)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_failed_download_is_not_silently_replaced(self):
        result, _ = run('resolve-python.sh', '2026.10.4', response='3.14.9', exit_code=22)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
