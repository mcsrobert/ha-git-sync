"""Command line: python -m ha_git_sync build SOURCE OUTPUT, python -m ha_git_sync check DEPLOY."""
import argparse
import os
from pathlib import Path
import sys

from .build import build
from .check import check, parse_check_secrets
from .errors import Error
from .github import GitHub


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ha_git_sync', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build_command = commands.add_parser('build', help='assemble the repository and pinned downloads')
    build_command.add_argument('source', type=Path, help='configuration repository directory')
    build_command.add_argument('output', type=Path, help='new directory for the deploy tree')
    check_command = commands.add_parser('check', help="run Home Assistant's check_config")
    check_command.add_argument('deploy', type=Path, help='directory made by build')
    check_command.add_argument('--secrets', type=Path, help='YAML file of typed values for secrets that are not strings')
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            build(args.source, args.output, GitHub(token=os.environ.get('GITHUB_TOKEN')))
        else:
            try:
                secrets = args.secrets.read_text() if args.secrets else '{}'
            except OSError as error:
                raise Error(f'cannot read {args.secrets}: {error.strerror or error}') from None
            check(args.deploy, parse_check_secrets(secrets))
    except Error as error:
        sys.exit(f'error: {error}')


if __name__ == '__main__':
    main()
