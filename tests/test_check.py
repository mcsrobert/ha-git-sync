import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml

from ha_git_sync.__main__ import main
from ha_git_sync.check import PLACEHOLDER, check, layout, parse_check_secrets, secret_names, write_placeholder_secrets
from ha_git_sync.errors import Error


class Deploy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.deploy = self.tmp / 'deploy'
        self.write('configuration.yaml', 'homeassistant:\n  latitude: !secret home_latitude\n'
                   '  packages: !include_dir_named packages\n'
                   'automation: !include_dir_merge_list automations\n'
                   'template: !include includes/template.yaml\n')
        self.write('automations/a.yaml', '- id: a\n  action:\n    - action: notify.x\n      data:\n'
                   '        message: !secret "quoted name"\n')

    def write(self, name, text):
        path = self.deploy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def layout(self):
        config = self.tmp / 'config'
        layout(self.deploy, config)
        return config


class LayoutTest(Deploy):
    def test_links_top_level_entries_and_copies_cards(self):
        self.write('blueprints/automation/b.yaml', 'blueprint:\n')
        self.write('.gitignore', '/*\n')
        self.write('custom_components/x/manifest.json', '{}')
        self.write('www/community/card/card.js', 'js')
        config = self.layout()
        self.assertEqual((config / '.git-sync' / 'current').resolve(), self.deploy.resolve())
        links = {path.name: str(path.readlink()) for path in config.iterdir() if path.is_symlink()}
        self.assertEqual(links, {name: f'.git-sync/current/{name}' for name in
                                 ('automations', 'blueprints', 'configuration.yaml', 'custom_components')})
        self.assertTrue((config / 'blueprints' / 'automation' / 'b.yaml').is_file())
        self.assertTrue((config / 'custom_components' / 'x' / 'manifest.json').is_file())
        self.assertFalse((config / 'www').is_symlink())
        self.assertFalse((config / 'www' / 'community').is_symlink())
        self.assertTrue((config / 'www' / 'community' / 'card' / 'card.js').is_file())

    def test_creates_empty_install_folders(self):
        config = self.layout()
        self.assertTrue((config / 'custom_components').is_dir())
        self.assertTrue((config / 'www' / 'community').is_dir())


class SecretsTest(Deploy):
    def test_finds_secrets_through_the_links_and_every_included_file(self):
        self.write('includes/template.yaml', '- sensor:\n    - name: x\n      state: !secret template_state\n')
        self.write('packages/deep/nested.yml', 'mqtt:\n  password: !secret mqtt_password\n'
                   'flow: [!secret flow_one, {key: !secret flow_two}]\n---\nsecond: !secret second_document\n')
        self.write('blueprints/automation/b.yaml', 'blueprint:\n  input: {}\ntrigger: !input t\nkey: !secret blueprint_key\n')
        self.assertEqual(secret_names(self.layout()), {'home_latitude', 'quoted name', 'template_state',
                                                       'mqtt_password', 'flow_one', 'flow_two', 'second_document',
                                                       'blueprint_key'})

    def test_ignores_secret_text_that_is_not_a_tag(self):
        self.write('includes/template.yaml', '# password: !secret commented\nvalue: "!secret quoted_text"\n')
        self.assertEqual(secret_names(self.layout()), {'home_latitude', 'quoted name'})

    def test_resolves_includes_from_the_path_home_assistant_opened(self):
        self.write('includes/template.yaml', 'sensor: !include "nested/sensor.inc"\n')
        self.write('includes/nested/sensor.inc', 'url: !secret sensor_url\nvalue: !include ../value\n')
        self.write('includes/value', 'state: !secret sensor_state\n')
        self.write('includes/unreferenced.inc', 'ignored: !secret not_loaded\n')
        self.assertEqual(secret_names(self.layout()), {'home_latitude', 'quoted name', 'sensor_url', 'sensor_state'})

    def test_skips_installed_integrations(self):
        self.write('custom_components/x/services.yaml', 'x: !secret from_integration\n')
        self.assertEqual(secret_names(self.layout()), {'home_latitude', 'quoted name'})

    def test_include_cycles_and_recursive_yaml_aliases_terminate(self):
        self.write('includes/template.yaml', 'a: !include loop.inc\n')
        self.write('includes/loop.inc', 'a: !include ./nested/../loop.inc\nb: &loop [*loop, !secret cycle_secret]\n')
        self.assertEqual(secret_names(self.layout()), {'home_latitude', 'quoted name', 'cycle_secret'})

    def test_ignores_missing_files_and_paths_outside_the_config(self):
        self.write('includes/template.yaml', 'missing: !include missing.inc\noutside: !include ../../../outside.inc\n')
        (self.tmp / 'outside.inc').write_text('x: !secret outside\n')
        self.assertEqual(secret_names(self.layout()), {'home_latitude', 'quoted name'})

    def test_falls_back_to_text_for_files_yaml_cannot_parse(self):
        self.write('includes/template.yaml', 'a: !secret broken_one\n  b: [\nc: !secret "broken two" # x\n')
        self.assertEqual(secret_names(self.layout()), {'home_latitude', 'quoted name', 'broken_one', 'broken two'})

    def test_writes_a_placeholder_for_every_secret_next_to_configuration_yaml(self):
        config = self.layout()
        self.assertEqual(write_placeholder_secrets(config), ['home_latitude', 'quoted name'])
        self.assertEqual(yaml.safe_load((config / 'secrets.yaml').read_text()),
                         {'home_latitude': PLACEHOLDER, 'quoted name': PLACEHOLDER})
        self.assertFalse((self.deploy / 'secrets.yaml').exists())

    def test_writes_nothing_without_secrets(self):
        self.write('configuration.yaml', 'homeassistant:\n')
        shutil.rmtree(self.deploy / 'automations')
        config = self.layout()
        self.assertEqual(write_placeholder_secrets(config), [])
        self.assertFalse((config / 'secrets.yaml').exists())

    def test_rejects_invalid_placeholder_overrides(self):
        for text in ('broken: [', '[]', 'value', 'false', '1: value', "'': value", 'x: !secret real'):
            with self.subTest(text=text), self.assertRaisesRegex(Error, 'the secrets file must be'):
                parse_check_secrets(text)
        self.assertEqual(parse_check_secrets(''), {})


class CheckTest(Deploy):
    def fake_check_config(self, result):
        def check(path):
            path = Path(path)
            secrets = path / 'secrets.yaml'
            self.seen['secrets'] = yaml.safe_load(secrets.read_text()) if secrets.exists() else None
            self.seen['configuration'] = (path / 'configuration.yaml').read_text()
            return result(path) if callable(result) else result

        class HomeAssistantError(Exception):
            pass

        def load_yaml_dict(path, secrets):
            try:
                return yaml.safe_load(Path(path).read_text()) or {}
            except yaml.YAMLError as error:
                raise HomeAssistantError('invalid YAML') from error

        self.seen = {}
        scripts = types.ModuleType('homeassistant.scripts')
        scripts.check_config = types.SimpleNamespace(check=check)
        exceptions = types.SimpleNamespace(HomeAssistantError=HomeAssistantError)
        util_yaml = types.SimpleNamespace(Secrets=lambda path: None, load_yaml_dict=load_yaml_dict)
        homeassistant = types.ModuleType('homeassistant')
        homeassistant.scripts = scripts
        return mock.patch.dict(sys.modules, {'homeassistant': homeassistant, 'homeassistant.scripts': scripts,
                                             'homeassistant.exceptions': exceptions,
                                             'homeassistant.util.yaml': util_yaml})

    def test_checks_the_layout_with_placeholder_secrets_and_leaves_the_deploy_tree_untouched(self):
        before = sorted(path.relative_to(self.deploy) for path in self.deploy.rglob('*'))
        with self.fake_check_config({'except': {}, 'warn': {}}), contextlib.redirect_stdout(io.StringIO()) as log:
            check(self.deploy)
        self.assertEqual(self.seen['secrets'], {'home_latitude': PLACEHOLDER, 'quoted name': PLACEHOLDER})
        self.assertIn('!include_dir_merge_list automations', self.seen['configuration'])
        self.assertEqual(sorted(path.relative_to(self.deploy) for path in self.deploy.rglob('*')), before)
        self.assertIn('passed', log.getvalue())

    def test_reports_problems_with_paths_relative_to_the_config(self):
        def result(config):
            return {'except': {'automation': [f'{config}/automations/a.yaml is bad']},
                    'warn': {'template': [f'{self.deploy.resolve()}/includes/template.yaml is odd']}}

        with self.fake_check_config(result), contextlib.redirect_stdout(io.StringIO()) as log:
            with self.assertRaisesRegex(Error, 'reported problems'):
                check(self.deploy)
        self.assertIn('"automations/a.yaml is bad"', log.getvalue())
        self.assertIn('"includes/template.yaml is odd"', log.getvalue())

    def lovelace(self, **lovelace):
        self.write('hacs.yaml', 'cards:\n  - repository: RomRider/apexcharts-card\n    version: v2.2.3\n'
                   '    asset: apexcharts-card.js\n  - repository: madmicio/LG-WebOS-Remote-Control\n'
                   '    version: 2.1.0\n    asset: lg-remote-control.js\n')
        self.write('www/community/apexcharts-card/apexcharts-card.js', 'js')
        self.write('www/community/LG-WebOS-Remote-Control/lg-remote-control.js', 'js')
        self.write('dashboards/home.yaml', 'views: []\n')
        defaults = {'mode': 'storage', 'resource_mode': 'yaml',
                    'dashboards': {'dashboard-home': {'mode': 'yaml', 'filename': 'dashboards/home.yaml'}},
                    'resources': [
                        {'url': '/local/community/apexcharts-card/apexcharts-card.js?v=v2.2.3', 'type': 'module'},
                        {'url': '/local/community/LG-WebOS-Remote-Control/lg-remote-control.js?v=2.1.0',
                         'type': 'module'},
                        {'url': 'https://cdn.example.com/card.js', 'type': 'module'},
                        {'url': '/local/my-card.js', 'type': 'module'},
                    ]}
        return {'except': {}, 'warn': {}, 'components': {'lovelace': {**defaults, **lovelace}}}

    def lovelace_errors(self, result):
        with self.fake_check_config(result), contextlib.redirect_stdout(io.StringIO()) as log:
            try:
                check(self.deploy)
            except Error:
                return json.loads(log.getvalue())['errors']['lovelace']
        return []

    def test_passes_dashboards_that_load_and_a_resource_per_card_at_its_version(self):
        self.assertEqual(self.lovelace_errors(self.lovelace()), [])

    def test_reports_dashboards_that_are_missing_or_do_not_load(self):
        self.write('dashboards/broken.yaml', 'views: [\n')
        result = self.lovelace(dashboards={
            'dashboard-home': {'mode': 'yaml', 'filename': 'dashboards/home.yaml'},
            'dashboard-gone': {'mode': 'yaml', 'filename': 'dashboards/gone.yaml'},
            'dashboard-broken': {'mode': 'yaml', 'filename': 'dashboards/broken.yaml'},
        })
        self.assertEqual(self.lovelace_errors(result), [
            'Dashboard dashboard-gone: dashboards/gone.yaml does not exist',
            'Dashboard dashboard-broken: dashboards/broken.yaml does not load: invalid YAML',
        ])

    def test_reports_resources_that_do_not_match_the_cards(self):
        result = self.lovelace(resources=[
            {'url': '/local/community/apexcharts-card/apexcharts-card.js?v=v2.2.2', 'type': 'module'},
            {'url': '/local/community/apexcharts-card/missing.js?v=v2.2.3', 'type': 'module'},
            {'url': '/local/community/weather-chart-card/weather-chart-card.js?v=1', 'type': 'module'},
        ])
        self.assertEqual(self.lovelace_errors(result), [
            'Resource /local/community/apexcharts-card/apexcharts-card.js?v=v2.2.2: end it in ?v=v2.2.3, the version '
            'of apexcharts-card in hacs.yaml, so browsers load the new file when the card changes',
            "Resource /local/community/apexcharts-card/missing.js?v=v2.2.3: apexcharts-card installs no file "
            "'missing.js'",
            'Resource /local/community/weather-chart-card/weather-chart-card.js?v=1: no card in hacs.yaml installs '
            'www/community/weather-chart-card',
            'Card LG-WebOS-Remote-Control in hacs.yaml has no resource under lovelace: resources:',
        ])

    def test_resolves_dashboards_like_home_assistant(self):
        self.write('ui-lovelace.yaml', 'views: []\n')
        absolute = self.lovelace(dashboards={'dashboard-home': {'mode': 'yaml', 'filename': '/config/dashboards/home.yaml'}})
        self.assertEqual(self.lovelace_errors(absolute), [])
        (self.deploy / 'ui-lovelace.yaml').unlink()
        self.assertEqual(self.lovelace_errors(self.lovelace(mode='yaml')),
                         ['Dashboard lovelace: ui-lovelace.yaml does not exist'])

    def test_counts_a_resource_for_the_card_its_path_resolves_to(self):
        result = self.lovelace(dashboards={}, resources=[
            {'url': '/local/community/card/card.js?v=1.0+build', 'type': 'module'},
            {'url': '/local/community/card/../other/other.js?v=v1', 'type': 'module'},
        ])
        self.write('hacs.yaml', 'cards:\n  - repository: o/card\n    version: 1.0+build\n    asset: card.js\n'
                   '  - repository: o/other\n    version: v1\n    asset: other.js\n')
        self.write('www/community/card/card.js', 'js')
        self.write('www/community/other/other.js', 'js')
        self.assertEqual(self.lovelace_errors(result), [])
        result['components']['lovelace']['resources'][1]['url'] = '/local/community/other/../card/card.js?v=1.0+build'
        self.assertEqual(self.lovelace_errors(result),
                         ['Card other in hacs.yaml has no resource under lovelace: resources:'])

    def test_leaves_resources_in_storage_mode_to_the_ui(self):
        self.assertEqual(self.lovelace_errors(self.lovelace(resource_mode='storage', resources=[])), [])

    def test_cli_uses_typed_overrides_from_a_file(self):
        self.write('includes/template.yaml', 'port: !secret server_port\nenabled: !secret enabled\n')
        overrides = self.tmp / 'check-secrets.yaml'
        overrides.write_text('server_port: 8123\nenabled: false\nunused: dummy\n')
        with self.fake_check_config({'except': {}, 'warn': {}}), contextlib.redirect_stdout(io.StringIO()):
            main(['check', str(self.deploy), '--secrets', str(overrides)])
        self.assertEqual(self.seen['secrets'], {'home_latitude': PLACEHOLDER, 'quoted name': PLACEHOLDER,
                                              'server_port': 8123, 'enabled': False})

    def test_cli_runs_without_a_secrets_file(self):
        with self.fake_check_config({'except': {}, 'warn': {}}), contextlib.redirect_stdout(io.StringIO()):
            main(['check', str(self.deploy)])
        self.assertEqual(self.seen['secrets'], {'home_latitude': PLACEHOLDER, 'quoted name': PLACEHOLDER})

    def test_cli_reports_a_missing_secrets_file(self):
        with self.fake_check_config({'except': {}, 'warn': {}}):
            with self.assertRaisesRegex(SystemExit, 'error: cannot read'):
                main(['check', str(self.deploy), '--secrets', str(self.tmp / 'missing.yaml')])
        self.assertEqual(self.seen, {})

    def test_cli_reports_invalid_overrides_before_running_the_check(self):
        overrides = self.tmp / 'check-secrets.yaml'
        overrides.write_text('broken: [')
        with self.fake_check_config({'except': {}, 'warn': {}}):
            with self.assertRaisesRegex(SystemExit, 'error: the secrets file must be valid YAML'):
                main(['check', str(self.deploy), '--secrets', str(overrides)])
        self.assertEqual(self.seen, {})


if __name__ == '__main__':
    unittest.main()
