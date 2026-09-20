# Termius MCP

stdio MCP server for [Termius](https://termius.com/) Cloud.

Repository: [MiaM1ku/termius-mcp](https://github.com/MiaM1ku/termius-mcp).

`termius` is not a human CLI. An MCP client starts the binary with no args.
The process speaks newline-delimited JSON-RPC on stdin/stdout (MCP stdio).
It negotiates `protocolVersion` `2025-11-25` or `2025-06-18` (echoes the
client when supported). Login, vault sync, host lookup, SSH exec, and SFTP
file transfer are tools.

This tree talks to **Termius desktop 10.0.6** APIs (DeviceToken, SRP / gRPC
login, RNCryptor v3 and Sodium v4/v5, `v4/terminal/sync/`).

## Install

Python 3.9+ is required. On Debian/Ubuntu (PEP 668) use a venv or `pipx`.

```bash
python3 -m venv ~/.local/share/termius-mcp
~/.local/share/termius-mcp/bin/pip install -U pip
~/.local/share/termius-mcp/bin/pip install -e .
ln -sf ~/.local/share/termius-mcp/bin/termius ~/.local/bin/termius
```

Point the MCP client at that binary. Do not pass `mcp` or other args.

Claude / generic (`contrib/mcp/termius.mcp.json`):

```json
{
  "mcpServers": {
    "termius": {
      "command": "termius",
      "args": []
    }
  }
}
```

Codex (`contrib/mcp/codex.toml`, merge into `~/.codex/config.toml`):

```toml
[mcp_servers.termius]
command = "termius"
args = []
startup_timeout_sec = 30.0
tool_timeout_sec = 60.0
```

Pi / OMP (`~/.omp/agent/mcp.json`):

```json
{
  "mcpServers": {
    "termius": {
      "type": "stdio",
      "command": "termius",
      "args": []
    }
  }
}
```

Restart the MCP client after you edit the config. `connecting [stdio]` is the
handshake. It becomes connected when `initialize` succeeds. Login happens
after that, through tools.

Optional environment variables:

| Variable | Purpose |
| --- | --- |
| `TERMIUS_VAULT_PASSWORD` | Vault encryption password (preferred over the remember file) |
| `TERMIUS_SYNC_TTL` | Seconds before the next automatic pull. Default `60`. `0` pulls on every read. |

## First-time setup

There is no setup wizard. After the server is connected, use the tools.

If `~/.termius/config` already has a DeviceToken (a previous login):

1. Call `status`. Expect `logged_in: true` and often `vault_remembered: false`.
2. Call `sync` with the **vault encryption password** from the Termius app
   (not the Google password). Default `remember=true` writes `~/.termius/vault`
   mode `0600`.
3. Call `hosts`. Later reads auto-pull when the cache is older than
   `TERMIUS_SYNC_TTL`.

If this machine has never signed in:

1. Call `status`. Expect `logged_in: false`.
2. Google: call `login` with `method=google`. Open the returned URL. Sign in.
   When the page tries to open Termius, copy
   `termius://app/continue-sso?...`. Call `login_complete` with that URL and
   the vault encryption password.
3. Email: call `login` with `method=email`, username, and the vault password.
   Add `otp` if 2FA is on.
4. Call `hosts`.

The process never returns the vault password in a tool result.

## Tools

Call `status` first.

| Tool | Purpose |
| --- | --- |
| `status` | Login state, last sync, stale flag, vault remembered, counts. Does not pull. |
| `login` | `method=email` with username + password, or `method=google` to get an SSO URL |
| `login_complete` | Finish Google SSO with `callback_url` + vault password |
| `logout` | Clear the session, remembered password, and local inventory |
| `sync` | Force a cloud pull now |
| `hosts` | List hosts (optional `query`) |
| `host` | One host + merged SSH settings + `ssh_command` |
| `exec` | Run a remote command over SSH |
| `files` | SFTP list / stat / read / write / get / put / mkdir / rm / rename |
| `inventory` | `kind=groups\|identities\|keys\|snippets` |

`hosts`, `host`, `exec`, `files`, and `inventory` pull automatically when the
local cache is older than `TERMIUS_SYNC_TTL` and a vault password is available.

`files` uses SFTP on the same SSH credentials as `exec`. `get` and `put` copy
between the MCP host filesystem and the remote host. `read` and `write` move
file content through the tool result (max 200000 bytes). `get` and `put` allow
up to 50 MiB. `list` defaults `path` to the SSH login directory.

## Local data

After a successful pull, decrypted inventory lives in:

- `~/.termius/config` — DeviceToken, salts, `last_synced`
- `~/.termius/storage` — hosts, groups, identities, keys, snippets (plaintext JSON)
- `~/.termius/ssh_keys/` — private key files
- `~/.termius/vault` — remembered vault password, if you chose `remember`

Treat that directory as secret.

## Encryption notes

Termius Cloud currently has two personal encryption schemas:

- **v3** — per-field RNCryptor (AES-CBC + HMAC). REST login is enough.
- **v5** — entity `content` blobs sealed with Argon2id + XChaCha20-Poly1305, plus SRP login.

The server auto-detects ciphertext version (`A…` = v3, `B…` = v5). Login uses
gRPC/SRP first (desktop `login_v2`); REST is only the fallback for accounts
that are not migrated (`NOT_MIGRATED`). For v5 SRP the vault password is Argon2id-hashed (libsodium interactive,
16-byte salt) and base64-encoded, then proven with Botan SRP-6a
``modp/srp/8192`` + **Blake2b-512**. ``public_data`` / ``proof`` are
uppercase hex **without** a ``0x`` prefix (Android ``libtermius`` strips
Botan's prefix before the gRPC/Socket.IO payload).

Team vaults: `sync` / auto-pull loads `/api/v4/team/vault/keys/`, unwraps each `encrypted_with` key with the personal X25519 keypair (ECDH + HChaCha20 + XChaCha20-Poly1305), and decrypts shared hosts/keys/identities. Entities whose vault key is missing are skipped, not deleted.

## License

See [LICENSE](LICENSE).
