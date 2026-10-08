# Termius MCP

[English](README.md) · [简体中文](README.zh-CN.md)

[Termius](https://termius.com/) Cloud 的 stdio MCP 服务器。

仓库：[MiaM1ku/termius-mcp](https://github.com/MiaM1ku/termius-mcp)。

不带参数时，`termius` 是 MCP 服务器。MCP 客户端以无参数方式启动该二进制文件。进程在 stdin/stdout 上使用换行分隔的 JSON-RPC（MCP stdio）。它协商 `protocolVersion` `2025-11-25` 或 `2025-06-18`（客户端版本受支持时回显该版本）。登录、保险库同步、主机查询、SSH 执行和 SFTP 文件传输是工具。

`termius login` 从终端登录。它支持 Google SSO、邮箱加密码，以及 OTP。

本仓库对接 **Termius desktop 10.0.6** API（DeviceToken、SRP / gRPC 登录、RNCryptor v3 和 Sodium v4/v5、`v4/terminal/sync/`）。

## 安装

需要 Python 3.9+。
PyPI 包名是 `termius-mcp`。官方 Termius CLI 已经占用 `termius`。
安装后，命令仍是 `termius`。

```bash
pip install termius-mcp
```

在 Debian/Ubuntu（PEP 668）上使用 venv 或 `pipx`：

```bash
pipx install termius-mcp
```

从 git clone 安装：

```bash
python3 -m venv ~/.local/share/termius-mcp
~/.local/share/termius-mcp/bin/pip install -U pip
~/.local/share/termius-mcp/bin/pip install -e .
ln -sf ~/.local/share/termius-mcp/bin/termius ~/.local/bin/termius
```

把 MCP 客户端指向该二进制文件。不要传入 `mcp` 或其他参数。不要在 MCP 客户端的 `args` 列表中传入 `login`。

Claude / 通用配置（`contrib/mcp/termius.mcp.json`）：

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

Codex（`contrib/mcp/codex.toml`，合并到 `~/.codex/config.toml`）：

```toml
[mcp_servers.termius]
command = "termius"
args = []
startup_timeout_sec = 30.0
tool_timeout_sec = 60.0
```

Pi / OMP（`~/.omp/agent/mcp.json`）：

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

编辑配置后重启 MCP 客户端。`connecting [stdio]` 表示握手。`initialize` 成功后进入已连接状态。在此之前用 `termius login` 登录，或在连接后使用登录工具。

可选环境变量：

| 变量 | 用途 |
| --- | --- |
| `TERMIUS_VAULT_PASSWORD` | 保险库加密密码（优先于记住文件） |
| `TERMIUS_SYNC_TTL` | 下次自动拉取前的秒数。默认 `60`。`0` 表示每次读取都拉取。 |

## 首次设置

先从终端登录，再启动 MCP 客户端。

### 终端登录

```bash
termius login
```

当 stdin 是 TTY 时，命令会提示选择 `google` 或 `email`。也可以直接传入方式：

```bash
termius login google
termius login email -u you@example.com
```

Google：

1. 打开打印的 `https://account.termius.com/sso/desktop?...` URL。
2. 用 Google 登录。
3. 当页面尝试打开 Termius 时，复制 `termius://app/continue-sso?...`。
4. 粘贴该 URL。
5. 输入 Termius 应用中的保险库加密密码。这不是 Google 密码。
6. 如果开启了 2FA，输入 OTP。

邮箱：

1. 如果未传入 `-u`，输入 Termius 邮箱。
2. 输入保险库 / 账户密码。
3. 如果开启了 2FA，输入 OTP。

`TERMIUS_VAULT_PASSWORD` 提供保险库密码，并跳过提示。默认记住会把密码存入系统钥匙串。传入 `--no-remember` 可跳过这一步。

查看会话：

```bash
termius status
```

退出登录：

```bash
termius logout
```

### MCP 工具

服务器连接后，也可以使用这些工具。

如果以前登录过，钥匙串中已经有 DeviceToken：

1. 调用 `status`。预期 `logged_in: true`，并且经常是 `vault_remembered: false`。
2. 调用 `sync`，并传入 Termius 应用中的**保险库加密密码**（不是 Google 密码）。默认 `remember=true` 会把密码存入系统钥匙串。
3. 调用 `hosts`。之后的读取会在缓存早于 `TERMIUS_SYNC_TTL` 时自动拉取。

如果这台机器从未登录，并且你不使用 `termius login`：

1. 调用 `status`。预期 `logged_in: false`。
2. Google：用 `method=google` 调用 `login`。打开返回的 URL。登录。当页面尝试打开 Termius 时，复制 `termius://app/continue-sso?...`。用该 URL 和保险库加密密码调用 `login_complete`。
3. 邮箱：用 `method=email`、用户名和保险库密码调用 `login`。如果开启了 2FA，加上 `otp`。
4. 调用 `hosts`。

进程不会在工具结果中返回保险库密码。

## 工具

先调用 `status`。

| 工具 | 用途 |
| --- | --- |
| `status` | 登录状态、上次同步、过期标志、是否已记住保险库、计数。不拉取。 |
| `login` | `method=email` 需要用户名和密码，或 `method=google` 获取 SSO URL |
| `login_complete` | 用 `callback_url` 和保险库密码完成 Google SSO |
| `logout` | 清除会话、已记住的密码和本地清单 |
| `sync` | 立即从云端强制拉取 |
| `hosts` | 列出主机（可选 `query`） |
| `host` | 一台主机 + 合并后的 SSH 设置 + `ssh_command` |
| `exec` | 通过 SSH 运行远程命令 |
| `files` | SFTP list / stat / read / write / get / put / mkdir / rm / rename |
| `inventory` | `kind=groups\|identities\|keys\|snippets` |

当本地缓存早于 `TERMIUS_SYNC_TTL`，并且保险库密码可用时，`hosts`、`host`、`exec`、`files` 和 `inventory` 会自动拉取。

`files` 使用与 `exec` 相同的 SSH 凭据，通过 SFTP 工作。`get` 和 `put` 在 MCP 主机文件系统与远程主机之间复制。`read` 和 `write` 通过工具结果传输文件内容（最大 200000 字节）。`get` 和 `put` 允许最大 50 MiB。`list` 默认把 `path` 设为 SSH 登录目录。

## 本地数据

机密通过 [`keyring`](https://pypi.org/project/keyring/) 存入系统钥匙串：macOS 钥匙串、Windows 凭据管理器，或 Linux Secret Service（GNOME Keyring、KWallet）。条目的服务名为 `termius-mcp:<目录>`：

- `vault_password` — 已记住的保险库密码（如果你选择了 `remember`）
- `User.apikey` — DeviceToken
- `User.private_key`、`User.personal_v4_key` — 解开后的个人密钥
- `storage_key` — 加密 `~/.termius/storage` 的密钥

`~/.termius/` 中的文件：

- `config` — 用户名、salt、`last_synced`
- `storage` — 主机、分组、身份、密钥、代码片段，用 `storage_key` 加密（Fernet），权限为 `0600`

私钥只保存在 `storage` 中。`exec` 和 `files` 在内存中加载私钥，因此 `host` 返回的 `ssh_command` 不带 `-i` 选项。

首次启动时，旧版本留下的明文数据会迁入钥匙串：移走 `vault` 文件和 `config` 中的机密，加密 `storage`，删除 `ssh_keys/`。

没有 Secret Service 的无头 Linux 没有默认后端。请在该机器上运行 Secret Service，或把 `PYTHON_KEYRING_BACKEND` 设为其他 `keyring` 后端。

## 加密说明

Termius Cloud 目前有两种个人加密方案：

- **v3** — 按字段的 RNCryptor（AES-CBC + HMAC）。REST 登录即可。
- **v5** — 实体 `content` blob 用 Argon2id + XChaCha20-Poly1305 密封，并使用 SRP 登录。

服务器自动检测密文版本（`A…` = v3，`B…` = v5）。登录先使用 gRPC/SRP（桌面端 `login_v2`）。REST 只作为未迁移账户（`NOT_MIGRATED`）的回退。对于 v5 SRP，保险库密码先按 Argon2id 哈希（libsodium interactive，16 字节 salt），再做 base64 编码，然后用 Botan SRP-6a ``modp/srp/8192`` + **Blake2b-512** 完成证明。``public_data`` / ``proof`` 是不带 ``0x`` 前缀的大写十六进制（Android ``libtermius`` 在写入 gRPC/Socket.IO 载荷前去掉 Botan 的前缀）。

团队保险库：`sync` / 自动拉取会加载 `/api/v4/team/vault/keys/`。程序用个人 X25519 密钥对解开每个 `encrypted_with` 密钥（ECDH + HChaCha20 + XChaCha20-Poly1305）。然后解密共享的主机、密钥和身份。如果某个实体的保险库密钥缺失，程序跳过该实体，不删除它。

## 许可证

见 [LICENSE](LICENSE)。
