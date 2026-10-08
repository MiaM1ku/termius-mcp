# -*- coding: utf-8 -*-
"""Pull Termius Cloud inventory without a CLI command."""
from base64 import b64decode
from datetime import datetime, timezone
import os

from .cloud.client.controllers import ApiController
from .cloud.client.cryptor import UnifiedCryptor
from .cloud.client.keyring import load_keyring
from .core.exceptions import NotSignedIn
from .vault import VaultPasswordRequired, require as require_vault

DEFAULT_SYNC_TTL = 60
SYNC_TTL_ENV = 'TERMIUS_SYNC_TTL'


def is_signed_in(config):
    """True when DeviceToken and username exist."""
    return bool(
        config.get_safe('User', 'username')
        and config.get_safe('User', 'apikey')
    )


def require_signed_in(config):
    """Return decoded vault salts or raise NotSignedIn."""
    username = config.get_safe('User', 'username')
    apikey = config.get_safe('User', 'apikey')
    salt = config.get_safe('User', 'salt')
    hmac_salt = config.get_safe('User', 'hmac_salt')
    if not username or not apikey:
        raise NotSignedIn('Not signed in. Call the login tool.')
    if not salt or not hmac_salt:
        raise NotSignedIn(
            'Login is incomplete (missing vault salts). Call login again.'
        )
    return b64decode(salt), b64decode(hmac_salt)


def sync_ttl():
    """Seconds after last_synced before the next automatic pull."""
    raw = os.environ.get(SYNC_TTL_ENV, str(DEFAULT_SYNC_TTL))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SYNC_TTL


def parse_last_synced(raw):
    """Parse API last_synced into an aware UTC datetime, or None."""
    if raw is None or raw == '':
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    try:
        as_float = float(text)
    except ValueError:
        as_float = None
    if as_float is not None and text.replace('.', '', 1).replace('-', '', 1).isdigit():
        return datetime.fromtimestamp(as_float, tz=timezone.utc)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def last_synced_raw(config):
    """Return the stored last_synced string."""
    return config.get_safe('CloudSynchronization', 'last_synced', default='') or ''


def last_synced_age(config):
    """Seconds since last_synced, or None if missing/unparseable."""
    parsed = parse_last_synced(last_synced_raw(config))
    if parsed is None:
        return None
    now = datetime.now(timezone.utc)
    return (now - parsed).total_seconds()


def is_stale(config, ttl=None):
    """True when inventory has never synced or the TTL has elapsed."""
    if ttl is None:
        ttl = sync_ttl()
    age = last_synced_age(config)
    if age is None:
        return True
    return age >= ttl


def pull(runtime, password):
    """Decrypt and replace the local vault from Termius Cloud."""
    encryption_salt, hmac_salt = require_signed_in(runtime.config)
    if not password:
        raise VaultPasswordRequired(
            'Vault password is required to pull. Pass password or remember it.'
        )
    cryptor = UnifiedCryptor(password, encryption_salt, hmac_salt)
    controller = ApiController(runtime.storage, runtime.config, cryptor)
    pkset = {
        'public_key': runtime.config.get_safe('User', 'public_key'),
        'encrypted_private_key': runtime.config.get_safe(
            'User', 'encrypted_private_key'
        ),
        'encrypted_personal_key': runtime.config.get_safe(
            'User', 'encrypted_personal_key'
        ),
    }
    cryptor.keyring = load_keyring(
        controller.api, runtime.config, cryptor, pkset
    )
    cryptor._sodium = None
    with runtime.storage:
        controller.get_settings()
        controller.get_bulk()
    return {
        'ok': True,
        'last_synced': last_synced_raw(runtime.config),
        'hosts': _count(runtime, 'Host'),
    }


def _count(runtime, name):
    from .core.models.terminal import Group, Host, Identity, SshKey, Snippet
    models = {
        'Host': Host,
        'Group': Group,
        'Identity': Identity,
        'SshKey': SshKey,
        'Snippet': Snippet,
    }
    return len(runtime.storage.get_all(models[name]))


def ensure_fresh(runtime, ttl=None):
    """Pull when signed in, a vault password exists, and the cache is stale.

    ``ttl`` replaces ``TERMIUS_SYNC_TTL`` for this call. The host reads
    pass ``0``, which pulls on every call.
    """
    require_signed_in(runtime.config)
    password = require_vault(runtime)
    if is_stale(runtime.config, ttl):
        pull(runtime, password)
        return {'pulled': True, 'last_synced': last_synced_raw(runtime.config)}
    return {'pulled': False, 'last_synced': last_synced_raw(runtime.config)}


def inventory_counts(runtime):
    """Return host/group/identity/key/snippet counts."""
    from .core.models.terminal import Group, Host, Identity, SshKey, Snippet
    return {
        'hosts': len(runtime.storage.get_all(Host)),
        'groups': len(runtime.storage.get_all(Group)),
        'identities': len(runtime.storage.get_all(Identity)),
        'keys': len(runtime.storage.get_all(SshKey)),
        'snippets': len(runtime.storage.get_all(Snippet)),
    }


def status_payload(runtime):
    """Login and cache metadata. Does not pull."""
    from .vault import is_available
    config = runtime.config
    username = config.get_safe('User', 'username', default='') or ''
    counts = inventory_counts(runtime)
    last_synced = last_synced_raw(config)
    payload = {
        'logged_in': is_signed_in(config),
        'username': username,
        'encryption_schema': config.get_safe(
            'User', 'encryption_schema', default=''
        ) or '',
        'is_team': config.get_safe('User', 'is_team', default='no') == 'yes',
        'has_keypair': bool(config.get_safe('User', 'private_key', default='')),
        'last_synced': last_synced,
        'stale': is_stale(config),
        'vault_remembered': is_available(runtime),
        'sync_ttl': sync_ttl(),
    }
    payload.update(counts)
    return payload
