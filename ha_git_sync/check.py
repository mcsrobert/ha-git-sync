"""Run Home Assistant's check_config on a deploy tree, laid out the way the sidecar links it into /config."""
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from . import sources
from .errors import Error
from .layout import CARDS, COMPONENTS, CURRENT, layout

INSTALLED_ROOTS = (COMPONENTS, 'www')
PLACEHOLDER = 'check-config-placeholder'
CARD_URL = '/local/community/'
# Fallback for files PyYAML cannot parse; check_config reports those files itself.
SECRET = re.compile(r'''!secret[ \t]+(?:"([^"\n]+)"|'([^'\n]+)'|([^\s#,\]}]+))''')


def check(deploy, placeholders=None):
    from homeassistant.scripts import check_config

    deploy = Path(deploy).absolute()
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / 'config'
        layout(deploy, config)
        write_placeholder_secrets(config, placeholders)
        result = check_config.check(str(config))
        lovelace = (result.get('components') or {}).get('lovelace')
        frontend = lovelace_problems(config, deploy, lovelace) if lovelace else []
    problems = {}
    for kind, key in (('errors', 'except'), ('warnings', 'warn')):
        problems[kind] = {domain: [relative(message, config, deploy) for message in messages if isinstance(message, str)]
                          for domain, messages in result[key].items()}
    if frontend:
        problems['errors'].setdefault('lovelace', []).extend(relative(message, config, deploy) for message in frontend)
    print(json.dumps(problems, indent=2))
    if any(problems.values()):
        raise Error('Home Assistant check_config reported problems')
    print('Home Assistant check_config passed')


def lovelace_problems(config, deploy, lovelace):
    """What check_config leaves out of the lovelace block: whether the YAML dashboards load, and whether the card
    resources match the cards in hacs.yaml."""
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util.yaml import Secrets, load_yaml_dict

    problems = []
    dashboards = dict(lovelace.get('dashboards') or {})
    if lovelace.get('mode') == 'yaml' and 'lovelace' not in dashboards:
        dashboards['lovelace'] = {'filename': 'ui-lovelace.yaml'}  # Home Assistant's deprecated YAML Overview.
    for url_path, dashboard in dashboards.items():
        filename = dashboard['filename']
        path = PurePosixPath(filename)
        if path.is_relative_to('/config'):
            path = path.relative_to('/config')
        try:
            load_yaml_dict(config / path, Secrets(config))
        except FileNotFoundError:
            problems.append(f'Dashboard {url_path}: {filename} does not exist')
        except HomeAssistantError as error:
            problems.append(f'Dashboard {url_path}: {filename} does not load: {error}')
    if lovelace.get('resource_mode', lovelace.get('mode')) == 'yaml':
        problems += resource_problems(deploy, lovelace.get('resources') or [])
    return problems


def resource_problems(deploy, resources):
    """Every card hacs.yaml installs needs a resource at one of its files, ending in ?v= and the card's version:
    browsers cache /local for a month, so a URL that does not change with the card keeps the old one."""
    hacs = deploy / 'hacs.yaml'
    cards = {item.name: item.version for item in (sources.load(hacs.read_text()) if hacs.is_file() else [])
             if item.category == 'cards'}
    problems = []
    loaded = set()
    for resource in resources:
        url = resource.get('url') or ''
        parts = urlsplit(url)
        path = posixpath.normpath(unquote(parts.path))
        if parts.scheme or parts.netloc or not path.startswith(CARD_URL):
            continue
        name, _, file = path.removeprefix(CARD_URL).partition('/')
        if name not in cards:
            problems.append(f'Resource {url}: no card in hacs.yaml installs {CARDS}/{name}')
            continue
        loaded.add(name)
        if not (deploy / CARDS / name / file).is_file():
            problems.append(f'Resource {url}: {name} installs no file {file!r}')
        if parse_qs(parts.query.replace('+', '%2B')).get('v') != [cards[name]]:
            problems.append(f'Resource {url}: end it in ?v={cards[name]}, the version of {name} in hacs.yaml, '
                            'so browsers load the new file when the card changes')
    for name in sorted(set(cards) - loaded):
        problems.append(f'Card {name} in hacs.yaml has no resource under lovelace: resources:')
    return problems


def relative(message, config, deploy):
    for prefix in sorted({str(config), str(config.resolve()), str(deploy), str(deploy.resolve())}, key=len, reverse=True):
        message = message.replace(f'{prefix}/', '')
    return message


def parse_check_secrets(text):
    """Read typed stand-ins for secrets that must not be strings."""
    try:
        values = yaml.safe_load(text)
    except yaml.YAMLError:
        raise Error('the secrets file must be valid YAML') from None
    if values is None:
        return {}
    if not isinstance(values, dict) or any(not isinstance(name, str) or not name for name in values):
        raise Error('the secrets file must be a YAML mapping with nonempty string keys')
    return values


def write_placeholder_secrets(config, placeholders=None):
    """Write secrets.yaml with a placeholder for every secret the configuration references.

    The real secrets.yaml stays on the Home Assistant volume and never reaches Git,
    so check_config would otherwise fail on every !secret. Only call this on a throwaway layout.
    """
    config = Path(config)
    names = sorted(secret_names(config))
    if names:
        placeholders = placeholders or {}
        values = {name: placeholders.get(name, PLACEHOLDER) for name in names}
        (config / 'secrets.yaml').write_text(yaml.safe_dump(values, sort_keys=True))
    return names


def secret_names(config):
    """Find !secret references in YAML files and their explicit includes.

    Includes resolve against the path Home Assistant opened, not its symlink target, so the walk keeps
    unresolved paths. Home Assistant treats an explicit !include as YAML regardless of its extension.
    """
    config = Path(config)
    roots = (config.resolve(), (config / CURRENT).resolve())
    pending = []
    for directory, dirs, files in os.walk(config, followlinks=True):
        if Path(directory) == config:
            dirs[:] = [name for name in dirs if not name.startswith('.') and name not in INSTALLED_ROOTS]
        pending.extend(Path(directory, name) for name in files if name.endswith(('.yaml', '.yml')))
    names = set()
    seen = set()
    while pending:
        path = pending.pop()
        real = path.resolve()
        if (real in seen or not any(real.is_relative_to(root) for root in roots)
                or path.name == 'secrets.yaml' or not real.is_file()):
            continue
        seen.add(real)
        secrets, includes = file_references(real.read_text(errors='replace'))
        names |= secrets
        pending.extend(path.parent / include for include in includes)
    return names


def file_references(text):
    """Return secret names and explicit include paths without constructing YAML tags."""
    try:
        nodes = [node for node in yaml.compose_all(text, Loader=yaml.SafeLoader) if node is not None]
    except yaml.YAMLError:
        return {next(group for group in match.groups() if group) for match in SECRET.finditer(text)}, set()
    names = set()
    includes = set()
    seen = set()
    while nodes:
        node = nodes.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            if node.tag == '!secret' and node.value:
                names.add(node.value)
            elif node.tag == '!include' and node.value:
                includes.add(node.value)
        elif isinstance(node, yaml.SequenceNode):
            nodes.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                nodes.extend((key, value))
    return names, includes
