"""Assemble the deploy tree: the configuration repository plus the integrations and cards pinned in hacs.yaml."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from . import sources
from .errors import Error
from .github import unzip

EXCLUDED = ('.github',)
INSTALLED = ('custom_components', 'www/community')
EDITOR_FILES = ('automations.yaml', 'scripts.yaml', 'scenes.yaml')
BLUEPRINT_DOMAINS = ('automation', 'script', 'template')
DOMAIN = re.compile(r'[a-z0-9_]+')


def build(source, output, github):
    source = Path(source)
    output = Path(output).absolute()
    if output.exists():
        raise Error(f'{output} already exists')
    files = repository_files(source)
    check_rules(files)
    pinned = sources.load((source / 'hacs.yaml').read_text()) if 'hacs.yaml' in files else []
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        root = Path(staging) / 'deploy'
        root.mkdir()
        for name in files:
            if PurePosixPath(name).parts[0] in EXCLUDED:
                continue
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, root / name)
        for name in INSTALLED:
            (root / name).mkdir(parents=True, exist_ok=True)
        for item in pinned:
            print(f'{item} -> {install(item, root, github)}')
        for name in INSTALLED:
            for entry in (root / name).iterdir():
                if entry.name.startswith('.') or not entry.is_dir():
                    raise Error(f'{entry.relative_to(root)}: the sidecar installs only visible directories in {name}')
        normalize(root)
        root.rename(output)


def repository_files(source):
    """Tracked and untracked, not ignored files, so a local check sees uncommitted work too."""
    try:
        listing = subprocess.run(['git', '-C', str(source), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                 check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as error:
        raise Error(f'{source} is not a Git repository: {error.stderr.decode(errors="replace").strip()}') from None
    files = []
    for name in sorted(set(os.fsdecode(entry) for entry in listing.split(b'\0') if entry)):
        path = source / name
        if path.is_symlink():
            raise Error(f'{name}: symlinks are not allowed')
        if path.is_dir():
            raise Error(f'{name}: submodules are not allowed')
        if path.exists():
            files.append(name)
    return files


def check_rules(files):
    if 'configuration.yaml' not in files:
        raise Error('configuration.yaml is missing')
    for name in files:
        parts = PurePosixPath(name).parts
        if any(part.casefold() == 'secrets.yaml' for part in parts):
            raise Error(f'{name}: keep secrets.yaml out of the repository; Home Assistant reads it from /config')
        if parts[0] == 'www':
            raise Error(f'{name}: www/ is not deployed; pin dashboard cards in hacs.yaml')
        if name in EDITOR_FILES:
            raise Error(f"{name}: Home Assistant's UI editor writes this file in /config; "
                        f'keep the configuration in another file or folder, such as {name.removesuffix(".yaml")}/')
    if any(name.startswith('blueprints/') for name in files):
        for domain in BLUEPRINT_DOMAINS:
            if not any(name.startswith(f'blueprints/{domain}/') for name in files):
                raise Error(f'blueprints/{domain}/ must contain a blueprint, or Home Assistant tries to write '
                            'its default blueprints into the read-only checkout')


def install(item, root, github):
    folder = None
    if item.asset:
        data = github.release_asset(item.repository, item.version, item.asset)
        files = unzip(data) if item.category == 'integrations' else {item.asset: data}
    else:
        with github.checkout(item.repository, item.version, item.commit) as tree:
            if item.category == 'cards':
                files = tree.files(item.path)
            else:
                folders = tree.dirs('custom_components')
                if len(folders) != 1:
                    raise Error(f'{item} must have exactly one directory in custom_components')
                folder = folders[0]
                files = tree.files(f'custom_components/{folder}')
    if not files:
        raise Error(f'{item} contains no files')
    if item.category == 'integrations':
        try:
            domain = json.loads(files['manifest.json'])['domain']
        except (KeyError, TypeError, ValueError):
            raise Error(f'{item} has no manifest.json with a domain') from None
        if not isinstance(domain, str) or not DOMAIN.fullmatch(domain) or folder not in (None, domain):
            raise Error(f'{item} has an invalid integration domain: {domain!r}')
        target = Path('custom_components', domain)
    else:
        target = Path('www', 'community', item.name)
    if (root / target).exists():
        raise Error(f'{item} installs into {target.as_posix()}, which the repository or another entry already uses')
    check_paths(item, files)
    try:
        for name, data in files.items():
            path = root / target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    except OSError as error:
        raise Error(f'{item}: cannot write {target.as_posix()}: {error.strerror or error}') from None
    return target.as_posix()


def check_paths(item, files):
    """Reject duplicate, case-colliding and file-versus-directory paths before writing."""
    seen = {}
    for name in files:
        key = tuple(part.casefold() for part in name.split('/'))
        if key in seen:
            raise Error(f'{item} contains conflicting paths {seen[key]!r} and {name!r}')
        seen[key] = name
    for key, name in seen.items():
        for end in range(1, len(key)):
            if key[:end] in seen:
                raise Error(f'{item} contains {seen[key[:end]]!r} as both a file and a directory ({name!r})')


def normalize(root):
    for directory, _, files in os.walk(root):
        os.chmod(directory, 0o755)
        for name in files:
            os.chmod(os.path.join(directory, name), 0o644)
