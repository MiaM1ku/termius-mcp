# -*- coding: utf-8 -*-
"""Render an ssh(1) command line from a merged SshConfig."""


def render_command(ssh_config, address, pfrule=None):
    """Generate an ssh command call."""
    identity = ssh_config.get('identity') or {}
    username = identity.get('username', '') or ''
    parts = [
        'ssh',
        _format_port(ssh_config.get('port')),
        _format_pfrule(pfrule),
        _bool_opt('StrictHostKeyChecking', ssh_config.get('strict_host_key_check')),
        _bool_opt('IdentitiesOnly', ssh_config.get('use_ssh_key')),
        _format_timeout(ssh_config.get('timeout')),
        _format_keep_alive(ssh_config.get('keep_alive_packages')),
        _bool_opt('ForwardAgent', ssh_config.get('agent_forwarding')),
        _ssh_auth(username, address),
    ]
    return ' '.join(part for part in parts if part)


def _ssh_auth(username, address):
    if username:
        return '{}@{}'.format(username, address)
    return '{}'.format(address)


def _format_port(port):
    return '-p {}'.format(port) if port else ''


def _format_pfrule(pfrule):
    if not pfrule:
        return ''
    return '-{0.pf_type} {binding}'.format(pfrule, binding=pfrule.binding)


def _format_timeout(timeout):
    return '-o ServerAliveInterval={}'.format(timeout) if timeout else ''


def _format_keep_alive(keep_alive_packages):
    if keep_alive_packages:
        return '-o ServerAliveCountMax={}'.format(keep_alive_packages)
    return ''


def _bool_opt(name, value):
    if value is None:
        return ''
    return '-o {}={}'.format(name, 'yes' if value else 'no')
