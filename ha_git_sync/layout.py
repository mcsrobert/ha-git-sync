"""How a deploy tree appears in /config, shared by the check action and the sidecar."""
import shutil

CURRENT = '.git-sync/current'
COMPONENTS = 'custom_components'
CARDS = 'www/community'
INSTALLED = (COMPONENTS, CARDS)
EDITOR_FILES = ('automations.yaml', 'scripts.yaml', 'scenes.yaml')


def linked(tree):
    """The top-level entries linked into /config: all visible ones except www, which stays Home Assistant's."""
    return sorted(entry.name for entry in tree.iterdir() if not entry.name.startswith('.') and entry.name != 'www')


def layout(deploy, config):
    """Lay out a deploy tree the way the sidecar does: entries linked through current, cards copied into www."""
    (config / CURRENT).parent.mkdir(parents=True)
    (config / CURRENT).symlink_to(deploy)
    for name in linked(deploy):
        (config / name).symlink_to(f'{CURRENT}/{name}')
    if not (deploy / COMPONENTS).is_dir():
        (config / COMPONENTS).mkdir()
    if (deploy / CARDS).is_dir():
        shutil.copytree(deploy / CARDS, config / CARDS)
    else:
        (config / CARDS).mkdir(parents=True)
