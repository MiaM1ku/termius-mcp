#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Start the Termius MCP server or a terminal command."""
import logging
import sys

from termius.cli import run as run_cli
from termius.keychain import SecretStoreError
from termius.mcp.server import run_stdio


def _configure_logging():
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format='%(levelname)s %(name)s: %(message)s',
    )
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('paramiko').setLevel(logging.WARNING)


def main(argv=None):
    """Process start from an MCP client or a terminal."""
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)
    _configure_logging()
    try:
        if not argv:
            if sys.stdin.isatty():
                sys.stderr.write(
                    'Termius MCP server. Point your MCP client at this binary '
                    '(no args). To sign in from a terminal, run: termius login\n'
                    'Waiting on stdin.\n'
                )
                sys.stderr.flush()
            run_stdio()
            return 0
        return run_cli(argv)
    except SecretStoreError as exc:
        sys.stderr.write('termius: {}\n'.format(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())
