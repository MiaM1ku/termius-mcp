# Termius MCP

[English](README.md) · [简体中文](README.zh-CN.md)

stdio MCP server for [Termius](https://termius.com/) Cloud.

Repository: [MiaM1ku/termius-mcp](https://github.com/MiaM1ku/termius-mcp).

`termius` with no arguments is the MCP server. An MCP client starts that
binary with no args. The process speaks newline-delimited JSON-RPC on
stdin/stdout (MCP stdio). It negotiates `protocolVersion` `2025-11-25` or
`2025-06-18` (echoes the client when supported). Login, vault sync, host
lookup, SSH exec, and SFTP file transfer are tools.

`termius login` signs in from a terminal. Use it for Google SSO, email and
password, and OTP.

This tree talks to **Termius desktop 10.0.6** APIs (DeviceToken, SRP / gRPC
login, RNCryptor v3 and Sodium v4/v5, `v4/terminal/sync/`).

## Install

Python 3.9+ is required.
The PyPI name is `termius-mcp`. The official Termius CLI already uses `termius`.
After install, the command is still `termius`.

```bash
pip install termius-mcp
```

On Debian/Ubuntu (PEP 668) use a venv or `pipx`:

```bash
pipx install termius-mcp
```

From a git clone:

```bash
python3 -m venv ~/.local/share/termius-mcp
~/.local/share/termius-mcp/bin/pip install -U pip
~/.local/share/termius-mcp/bin/pip install -e .
ln -sf ~/.local/share/termius-mcp/bin/termius ~/.local/bin/termius
```

Point the MCP client at that binary. Do not pass `mcp` or other args.
Do not pass `login` in the MCP client `args` list.

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
handshake. It becomes connected when `initialize` succeeds. Sign in with
`termius login` before that, or use the login tools after connect.

Optional environment variables:

| Variable | Purpose |
| --- | --- |
| `TERMIUS_VAULT_PASSWORD` | Vault encryption password (preferred over the remember file) |
| `TERMIUS_SYNC_TTL` | Seconds before an automatic pull in `exec`, `files`, and `inventory`. Default `60`. `0` pulls on every read. `hosts` and `host` always pull, then fall back to the local cache if the pull fails. |
| `TERMIUS_KEYRING` | `1` forces the OS keychain, `0` forces the local secrets file. Default: use the backend saved in `config`. With no saved choice, pick automatically. `PYTHON_KEYRING_BACKEND` selects the keychain only before a choice is saved. |
| `TERMIUS_SECRETS_KEY` | Passphrase that seals `~/.termius/secrets` instead of the machine id. Set this in a container so a new container can read the same file. |

## First-time setup

Sign in from a terminal, then start the MCP client.

### Terminal login

```bash
termius login
```

The command prompts for `google` or `email` when stdin is a TTY.
You can also pass the method:

```bash
termius login google
termius login email -u you@example.com
```

Google:

1. Open the printed `https://account.termius.com/sso/desktop?...` URL.
2. Sign in with Google.
3. When the browser asks to open Termius, decline. Get the
   `termius://app/continue-sso?...` URL with the script in
   [Get the callback URL](#get-the-callback-url).
4. Paste that URL.
5. Enter the vault encryption password from the Termius app. This is not
   the Google password.
6. If 2FA is on, enter the OTP from your authenticator app. Termius does not
   send it by email.

Email:

1. Enter the Termius email if you did not pass `-u`.
2. Enter the vault / account password.
3. If 2FA is on, enter the OTP from your authenticator app.

#### Get the callback URL

The page sets `window.location` to the `termius://` URL. It has no link to
copy. Build the URL from the page state instead:

1. Stay on the "Redirecting to Termius" page.
2. Open the JavaScript console. In Safari, turn on Settings > Advanced >
   "Show features for web developers", then press Option-Command-C.
3. Paste this script and press Return:

   ```js
   (async () => {
     const requestId = new URLSearchParams(location.search).get('request');
     const db = await new Promise((resolve, reject) => {
       const req = indexedDB.open('firebaseLocalStorageDb');
       req.onsuccess = () => resolve(req.result);
       req.onerror = () => reject(req.error);
     });
     const rows = await new Promise((resolve, reject) => {
       const req = db.transaction('firebaseLocalStorage').objectStore('firebaseLocalStorage').getAll();
       req.onsuccess = () => resolve(req.result);
       req.onerror = () => reject(req.error);
     });
     const user = rows.map((row) => row.value).find((value) => value && value.stsTokenManager);
     if (!user || !requestId) throw new Error('Finish Google sign-in on this page first.');
     window.termiusSsoUrl = 'termius://app/continue-sso?email=' + encodeURIComponent(user.email)
       + '&firebaseToken=' + user.stsTokenManager.accessToken
       + '&requestId=' + requestId;
     prompt('Copy this URL into termius login', window.termiusSsoUrl);
   })()
   ```

4. Copy the URL from the dialog.

The URL contains a Firebase ID token. The token expires after about one
hour. Do not share the URL.

`TERMIUS_VAULT_PASSWORD` supplies the vault password and skips the prompt.
Default remember stores the vault password in the secret store: the OS
keychain, or `~/.termius/secrets` on a machine without one. Pass
`--no-remember` to skip that.

Check the session:

```bash
termius status
```

Sign out:

```bash
termius logout
```

### MCP tools

After the server is connected, you can also use the tools.

If a previous login already stored a DeviceToken:

1. Call `status`. Expect `logged_in: true` and often `vault_remembered: false`.
2. Call `sync` with the **vault encryption password** from the Termius app
   (not the Google password). Default `remember=true` stores it in the
   secret store.
3. Call `hosts`. It pulls Termius Cloud on every call, so the list is
   always current.

If this machine has never signed in and you are not using `termius login`:

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
| `sync` | Force a pull now; also how you pass and remember the vault password |
| `hosts` | List hosts (optional `query`). Pulls on every call. Returns the local cache if the pull fails |
| `host` | One host + merged SSH settings + `ssh_command`. Pulls on every call. Returns the local cache if the pull fails |
| `exec` | Run a remote command over SSH |
| `files` | SFTP list / stat / read / write / get / put / mkdir / rm / rename |
| `inventory` | `kind=groups\|identities\|keys\|snippets` |

`hosts` and `host` pull on every call. If that pull fails, they return the
local cache and set `stale` to true. `sync_error` is the pull error. A missing
sign-in or vault password is still an error. `exec`, `files`, and `inventory`
pull when the local cache is older than `TERMIUS_SYNC_TTL` and a vault password
is available. Those three tools still fail the call when the pull fails.

`files` uses SFTP on the same SSH credentials as `exec`. `get` and `put` copy
between the MCP host filesystem and the remote host. `read` and `write` move
file content through the tool result (max 200000 bytes). `get` and `put` allow
up to 50 MiB. `list` defaults `path` to the SSH login directory.

## Local data

A desktop machine keeps secrets in the OS keychain through
[`keyring`](https://pypi.org/project/keyring/): the macOS Keychain, the
Windows Credential Manager, or the Linux Secret Service (GNOME Keyring,
KWallet). Entries use the service name `termius-mcp:<directory>`.

A machine with no usable keychain (a server, a container, or a Linux box with
no desktop session) keeps the same names in `~/.termius/secrets` instead:
one Fernet token, mode `0600`. The first start writes the choice to `config`
as `[Secrets] backend`. Later starts keep that choice, so a desktop session
and an SSH session on the same machine use the same store. `TERMIUS_KEYRING`
overrides the saved choice and saves the new one.

The file key comes from the machine id and the user id. The file is not
readable text, and a copy of it does not open on another machine or account.
It does not hide anything from somebody who is already this user on this
machine. Set `TERMIUS_SECRETS_KEY` to seal the file with that passphrase
instead. A container should set it: a slim image has no `/etc/machine-id`,
and Docker assigns a new MAC address on each `docker run`, so a machine-bound
file will not open in the next container.

On the first start that selects the file, an empty `secrets` file copies
`vault_password`, `User.apikey`, `User.private_key`, `User.personal_v4_key`,
and `storage_key` out of the OS keychain when those entries exist. The
keychain entries stay in place. `PYTHON_KEYRING_BACKEND` selects the keychain
when no choice is saved yet, which keeps a headless install that followed the
older instructions on the keychain.

Names in the store:

- `vault_password` — remembered vault password, if you chose `remember`
- `User.apikey` — DeviceToken
- `User.private_key`, `User.personal_v4_key` — unwrapped personal keys
- `storage_key` — key that encrypts `~/.termius/storage`

Files in `~/.termius/`:

- `config` — username, salts, `last_synced`
- `storage` — hosts, groups, identities, keys, snippets, encrypted with
  `storage_key` (Fernet), mode `0600`
- `secrets` — only on a machine with no keychain, see above

Private keys stay inside `storage`. `exec` and `files` load them in memory,
so `ssh_command` from `host` has no `-i` option.

On first start, plaintext data from older versions moves into the secret
store: the `vault` file and the secrets in `config` are moved, `storage` is
encrypted, and `ssh_keys/` is deleted.

If the secrets file cannot be decrypted, the server renames it to
`secrets.bad-<UTC timestamp>` and starts with an empty store. `status` then
reports `logged_in: false`. Sign in again. The renamed file is kept. Losing
the store costs one cloud pull and one sign-in, not the vault itself. This
happens when `/etc/machine-id` changes (a reinstall or a cloned image), when
the file moves to another host, or when `TERMIUS_SECRETS_KEY` is missing for
a file that was sealed with it.

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
