"""The sidecar: put each tree promote built into /config, then reload or restart Home Assistant.

git-sync runs the hook in the new worktree after every sync and when it starts, and retries it with backoff until it
succeeds. Only the standard library is available in the image.
"""
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .errors import Error
from .layout import CARDS, COMPONENTS, CURRENT, EDITOR_FILES, INSTALLED, linked

CONFIG = Path('/config')
MARKER = '.git-sync'
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a redirect as the answer instead of following it, which would send the token to wherever it points."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Sidecar:
    def __init__(self, config, ha_url, token_file):
        self.config = Path(config)
        self.root = self.config / '.git-sync'
        self.state = self.root / 'state'
        self.ha_url = ha_url
        self.token_file = Path(token_file)

    def hook(self, commit, worktree):
        """Install the commit git-sync checked out in worktree, if CI built it, and apply it to Home Assistant."""
        worktree = Path(worktree)
        for name in ('notifications', 'trees', 'components'):
            (self.state / name).mkdir(parents=True, exist_ok=True)
        built_from = git(worktree, 'log', '-1', '--format=%(trailers:key=Source-Commit,valueonly)', commit).strip()
        built_from = built_from.splitlines()[0] if built_from else ''
        if not built_from:
            print(f'{commit[:7]} has no Source-Commit trailer, so CI did not build it; not installing or reloading')
            if self.read('rejected') != commit:
                self.notify('deploy', 'Configuration not deployed',
                            f'The deployed branch is at {commit[:7]}, which CI did not build, so nothing was '
                            'installed or reloaded. The next CI run on the main branch restores it.')
                self.write('rejected', commit)
            self.restore()
            self.send_notifications()
            return
        self.remove('rejected')
        content = git(worktree, 'rev-parse', f'{commit}^{{tree}}').strip()
        components, cards = (git(worktree, 'rev-parse', '--verify', '--quiet', f'{commit}:{name}', check=False).strip()
                             for name in INSTALLED)
        self.publish(commit, content, components, cards, worktree)
        if self.read('reloaded') != commit and (self.read('restart') or self.read('applied') != content):
            self.apply(built_from, content)
        self.write('reloaded', commit)

        orphans = [name for name in EDITOR_FILES if unmanaged_file(self.config / name)]
        if orphans:
            self.notify('editor', 'Changes made in the UI are not loaded',
                        f'Home Assistant saved {" ".join(orphans)} in /config, but it loads its configuration from '
                        'Git. Move the changes to the configuration repository and delete the files.')
        self.send_notifications()

    def restore(self):
        """Finish the install a failed hook left half done, so a commit CI did not build does not leave /config that
        way."""
        installed = self.lines('installed')
        if len(installed) == 4 and (self.state / 'trees' / installed[1]).is_dir():
            self.publish(*installed)

    def publish(self, commit, content, components, cards, worktree=None):
        """Snapshot the tree, take over what it replaces in /config, and switch current to it."""
        store = self.state / 'components' / (components or 'none')
        snapshot = self.snapshot(worktree, content, store)
        names = linked(snapshot)
        self.take_over(commit, names)
        installed = self.lines('installed')
        if components != (installed[2] if len(installed) == 4 else ''):
            self.write('restart', commit)
        self.switch(content)
        self.link(names)
        self.install_cards(snapshot / CARDS, cards, commit)
        self.write('installed', commit, content, components, cards)
        self.prune(snapshot, store)

    def snapshot(self, worktree, content, store):
        """A copy of the worktree that outlives it, so current only ever points at a complete tree. The integrations
        are copied once per version into store and linked from the snapshot: Home Assistant writes files into them
        that this user cannot delete, and a copy in every snapshot would leave those behind with each deploy."""
        snapshot = self.state / 'trees' / content
        staging = self.state / 'staging'
        if not snapshot.is_dir():
            if not store.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
                if (worktree / COMPONENTS).is_dir():
                    copy(worktree / COMPONENTS, staging)
                else:
                    staging.mkdir()
                staging.rename(store)
            shutil.rmtree(staging, ignore_errors=True)
            copy(worktree, staging, ('.git', COMPONENTS))
            (staging / COMPONENTS).symlink_to(os.path.relpath(store, snapshot))
            staging.rename(snapshot)
        for name in INSTALLED:
            (snapshot / name).mkdir(parents=True, exist_ok=True)
        return snapshot

    def take_over(self, commit, names):
        """Move aside what the links and the copied cards replace in /config."""
        www = self.config / 'www'
        if not www.is_dir():
            www.mkdir()
            www.chmod(0o2775)
        community = self.config / CARDS
        if not (community / MARKER).is_file():
            if os.path.lexists(community):
                aside(community)
            community.mkdir()
            (community / MARKER).write_text('')
        if not self.ours(self.config / 'configuration.yaml'):
            for name in EDITOR_FILES:
                if unmanaged_file(self.config / name):
                    aside(self.config / name)
        self.leave_components(commit)
        for name in names:
            path = self.config / name
            if os.path.lexists(path) and not self.ours(path):
                aside(path)

    def leave_components(self, commit):
        """Make room for the link to custom_components. The copies the earlier version made go to the trash.
        Integrations from elsewhere, such as HACS, move aside, and a restart is due: Home Assistant has them loaded."""
        path = self.config / COMPONENTS
        if self.ours(path) or not os.path.lexists(path):
            return
        if (path / MARKER).is_file():
            self.discard(path)
        elif path.is_dir() and any(entry.is_dir() for entry in path.iterdir()):
            self.write('restart', commit)
            aside(path)

    def switch(self, content):
        """Point current at the snapshot in one step, so Home Assistant sees the old tree or the new one."""
        target = f'state/trees/{content}'
        if readlink(self.root / 'current') != target:
            self.remove('reloaded')
            self.remove('applied')
            new = self.root / '.current'
            new.unlink(missing_ok=True)
            new.symlink_to(target)
            os.replace(new, self.root / 'current')

    def link(self, names):
        for name in names:
            path = self.config / name
            if not self.ours(path):
                path.symlink_to(f'{CURRENT}/{name}')
                print(f'Linked {name}')
        for path in list(self.config.iterdir()):
            if self.ours(path) and path.name not in names:
                path.unlink()
                print(f'Removed the link {path.name}')

    def install_cards(self, source, want, commit):
        """Copy the cards into www/community: Home Assistant does not serve files through a link out of www."""
        target = self.config / CARDS
        marker = target / MARKER
        if marker.read_text().strip() == want:
            return
        write_atomic(marker, 'installing')
        for entry in target.iterdir():
            if entry.name != MARKER and (entry.name.startswith('.') or not (source / entry.name).is_dir()):
                remove(entry)
        for entry in sorted(source.iterdir()):
            if not entry.is_dir():
                continue
            new, old = target / f'.{entry.name}.new', target / f'.{entry.name}.old'
            shutil.copytree(entry, new, symlinks=True)
            if (target / entry.name).exists():
                (target / entry.name).rename(old)
            new.rename(target / entry.name)
            remove(old)
        write_atomic(marker, want)
        print(f'Installed {CARDS} from {commit[:7]}')

    def prune(self, snapshot, store):
        """Drop every snapshot and integrations folder but the two in use."""
        for folder, kept in (('trees', snapshot), ('components', store)):
            for entry in (self.state / folder).iterdir():
                if entry != kept:
                    self.discard(entry)
        shutil.rmtree(self.state / 'trash', ignore_errors=True)

    def discard(self, path):
        """Move a folder the sidecar made to the trash, which prune empties. Files Home Assistant wrote into old
        integrations, such as __pycache__, may resist removal, and stay in the trash rather than fail the hook."""
        trash = self.state / 'trash'
        trash.mkdir(exist_ok=True)
        path.rename(trash / f'{path.name}.{time.time_ns()}')

    def apply(self, built_from, content):
        """Reload Home Assistant, or restart it when a restart is due, once it is running."""
        status, body = self.api('config')
        self.answered(status)
        if status != 200:
            raise Error(f'Home Assistant answered HTTP {status}; retrying later')
        try:
            state = json.loads(body).get('state')
        except (ValueError, AttributeError):
            state = None
        if state != 'RUNNING':
            raise Error('Home Assistant is still starting; retrying later')
        self.remove('applied')
        if self.read('restart'):
            status, body = self.api('services/homeassistant/restart', {})
            if status == 'closed':
                status = 200  # Home Assistant may stop before it answers.
            self.answered(status)
            if status == 200:
                print(f'Restarting Home Assistant with {built_from[:7]}, because integrations changed')
                self.remove('restart')
            else:
                self.notify('restart', 'Home Assistant not restarted',
                            f'Home Assistant refused to restart with {built_from[:7]} (HTTP {status}), so it keeps '
                            'running the integrations it had, and the next deploy tries again. It checks the '
                            'configuration with the integrations it runs, so if its answer is about an integration '
                            f'this deploy changed, restart Home Assistant yourself.\n\n{excerpt(body)}')
        else:
            status, body = self.api('services/homeassistant/reload_all', {})
            self.answered(status)
            if status == 200:
                print(f'Reloaded Home Assistant with {built_from[:7]}')
            else:
                self.notify('reload', 'Configuration not reloaded',
                            f'Reloading {built_from[:7]} failed with HTTP {status}: {excerpt(body)}')
        if status == 200:
            self.write('applied', content)

    def api(self, path, data=None):
        """Call Home Assistant's API and return the HTTP status and body. Without a status, it is why there is none:
        down when nothing answers, closed when Home Assistant closed the connection without answering, and failed
        for other errors."""
        headers = {'Authorization': f'Bearer {self.token_file.read_text().strip()}',
                   'Content-Type': 'application/json'}
        body = data if isinstance(data, bytes) or data is None else json.dumps(data).encode()
        request = urllib.request.Request(f'{self.ha_url}/api/{path}', data=body, headers=headers)
        try:
            response = OPENER.open(request, timeout=120)
        except urllib.error.HTTPError as error:
            response = error
        except urllib.error.URLError as error:
            return unanswered(error.reason), b''
        except (OSError, http.client.HTTPException) as error:
            return unanswered(error), b''
        with response:
            try:
                return response.status, response.read()
            except (http.client.HTTPException, OSError):
                return response.status, b'The error response was interrupted.'

    def answered(self, status):
        """Fail, so git-sync retries later, unless Home Assistant answered and accepted the token."""
        if status in ('down', 'closed', 'failed'):
            raise Error(f'Home Assistant is not reachable at {self.ha_url} ({status}); retrying later')
        if status in (401, 403):
            raise Error(f'Home Assistant rejected the token (HTTP {status})')

    def notify(self, name, title, message):
        """Queue a persistent notification, so a failed delivery is retried without repeating what the hook did."""
        message = CONTROL.sub('', message.replace('\t', ' '))
        write_atomic(self.state / 'notifications' / name,
                     json.dumps({'notification_id': f'git_sync_{name}', 'title': title, 'message': message}))

    def send_notifications(self):
        for path in sorted((self.state / 'notifications').iterdir()):
            if path.name.startswith('.') or not path.is_file():
                continue
            status, body = self.api('services/persistent_notification/create', path.read_bytes())
            self.answered(status)
            if status in (408, 429) or (isinstance(status, int) and 500 <= status < 600):
                raise Error(f'Creating notification {path.name} failed with HTTP {status}; retrying later')
            if status != 200:
                print(f'Home Assistant rejected the notification {path.name} (HTTP {status}): {excerpt(body)}',
                      file=sys.stderr)
            path.unlink()

    def ours(self, path):
        return readlink(path) == f'{CURRENT}/{path.name}'

    def read(self, name):
        lines = self.lines(name)
        return lines[0] if lines else ''

    def lines(self, name):
        try:
            return (self.state / name).read_text().splitlines()
        except FileNotFoundError:
            return []

    def write(self, name, *lines):
        write_atomic(self.state / name, *lines)

    def remove(self, name):
        (self.state / name).unlink(missing_ok=True)


def ready(config=CONFIG):
    """Whether /config holds a complete deploy and git-sync's tip is that deploy, or a commit the hook rejected."""
    sidecar = Sidecar(config, None, '')
    tip = readlink(sidecar.root / 'incoming')
    installed = sidecar.read('installed')
    return bool(tip and installed and (readlink(sidecar.root / 'current') or '').startswith('state/trees/')
                and (sidecar.root / 'current' / 'configuration.yaml').is_file()
                and Path(tip).name in (installed, sidecar.read('rejected')))


def excerpt(body):
    """Home Assistant's answer, cut to 1000 bytes without ending in part of a character."""
    return body[:1000].decode(errors='ignore').replace('\0', ' ')


def unanswered(error):
    if isinstance(error, ConnectionResetError):
        return 'closed'
    if isinstance(error, OSError) and not isinstance(error, TimeoutError):
        return 'down'
    return 'failed'


def aside(path):
    """Move what the sidecar replaces out of the way, keeping it next to where it was."""
    backup = path.with_name(f'{path.name}.pre-git')
    if os.path.lexists(backup):
        backup = path.with_name(f'{path.name}.pre-git.{time.strftime("%Y%m%d%H%M%S")}')
    try:
        path.rename(backup)
    except OSError:
        raise Error(f'Cannot move {path} aside; give this container write access to {path.parent}, '
                    'or move it yourself') from None
    print(f'Moved {path} to {backup}')


def unmanaged_file(path):
    return path.is_file() and not path.is_symlink()


def write_atomic(path, *lines):
    """Write lines through a hidden temporary file in the same directory, so readers never see part of it."""
    temporary = path.with_name(f'.{path.name.lstrip(".")}.tmp')
    temporary.write_text(''.join(f'{line}\n' for line in lines))
    os.replace(temporary, path)


def readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def copy(source, target, ignored=()):
    """Copy a folder, naming the file that failed, and leave out the entries of source listed in ignored."""
    try:
        shutil.copytree(source, target, symlinks=True,
                        ignore=lambda directory, names: ignored if Path(directory) == source else [])
    except shutil.Error as error:
        problems = error.args[0]
        raise Error(f'Cannot copy the checkout: {problems[0][2] if isinstance(problems, list) else problems}')


def git(worktree, *args, check=True):
    return subprocess.run(['git', '-C', str(worktree), *args], check=check, capture_output=True, text=True).stdout


def hook(config=CONFIG, worktree=None, environ=os.environ):
    """git-sync's exechook: it runs in the new worktree, with the commit in GITSYNC_HASH."""
    sidecar = Sidecar(config, environ.get('HA_URL') or 'http://localhost:8123',
                      environ.get('HA_TOKEN_FILE') or '/etc/git-secret/ha-token')
    commit = environ.get('GITSYNC_HASH')
    if not commit:
        sys.exit('GITSYNC_HASH is not set; git-sync runs this hook')
    try:
        sidecar.hook(commit, worktree or Path.cwd())
    except (Error, OSError) as error:
        sys.exit(str(error))
    except subprocess.CalledProcessError as error:
        sys.exit(f'git: {error.stderr.strip() or f"exit status {error.returncode}"}')
