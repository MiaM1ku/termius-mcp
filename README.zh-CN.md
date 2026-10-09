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

超时和取消：

- `exec` 和 `files` 的 `timeout` 参数限制整个调用的时长，包括建立连接。默认 60 秒。
- `exec` 超时后，服务器关闭 SSH 连接。结果里 `timed_out` 为 `true`，`exit_code` 为 `null`，并带上已经读到的输出。不再输出的远程进程可能继续运行。
- `files` 超时后，调用失败。大文件 `get` 或 `put` 要传更大的 `timeout`。
- 每个工具调用在单独的线程里运行。长时间的 `exec` 不会阻塞 `status` 和其他调用。
- 客户端发送 `notifications/cancelled` 后，服务器关闭这个调用的 SSH 连接，并且不发送响应。
- 客户端的工具超时要大于你传的最大 `timeout`，否则客户端会先取消调用。例如 Codex 的 `tool_timeout_sec = 60.0` 会在 60 秒时取消 `timeout: 180` 的 `exec`。

可选环境变量：

| 变量 | 用途 |
| --- | --- |
| `TERMIUS_VAULT_PASSWORD` | 保险库加密密码（优先于记住文件） |
| `TERMIUS_SYNC_TTL` | `exec`、`files`、`inventory` 下次自动拉取前的秒数。默认 `60`。`0` 表示每次读取都拉取。`hosts` 和 `host` 在缓存超过 600 秒时拉取，拉取失败则返回本地缓存。 |
| `TERMIUS_KEYRING` | `1` 强制用系统钥匙串，`0` 强制用本地机密文件。默认用 `config` 里记下的后端。还没有记录时自动判断。`PYTHON_KEYRING_BACKEND` 只在还没有记录时选择钥匙串。 |
| `TERMIUS_SECRETS_KEY` | 用来封装 `~/.termius/secrets` 的口令，代替机器标识。容器里要设置它，这样新容器才能读同一个文件。 |

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
3. 浏览器询问是否打开 Termius 时，选择拒绝。用[获取回调 URL](#获取回调-url) 中的脚本获取 `termius://app/continue-sso?...`。
4. 粘贴该 URL。
5. 输入 Termius 应用中的保险库加密密码。这不是 Google 密码。
6. 如果开启了 2FA，输入验证器应用中的 OTP。Termius 不通过邮件发送 OTP。

邮箱：

1. 如果未传入 `-u`，输入 Termius 邮箱。
2. 输入保险库 / 账户密码。
3. 如果开启了 2FA，输入验证器应用中的 OTP。

#### 获取回调 URL

页面通过设置 `window.location` 跳转到 `termius://` URL，没有可复制的链接。改用页面状态拼出该 URL：

1. 停留在 "Redirecting to Termius" 页面。
2. 打开 JavaScript 控制台。在 Safari 中，先开启「设置 > 高级 > 显示网页开发者功能」，再按 Option-Command-C。
3. 粘贴以下脚本并按回车：

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

4. 从对话框中复制该 URL。

该 URL 包含 Firebase ID token，约一小时后过期。不要分享该 URL。

`TERMIUS_VAULT_PASSWORD` 提供保险库密码，并跳过提示。默认记住会把密码存进机密存储：系统钥匙串，或没有钥匙串的机器上的 `~/.termius/secrets`。传入 `--no-remember` 可跳过这一步。

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
2. 调用 `sync`，并传入 Termius 应用中的**保险库加密密码**（不是 Google 密码）。默认 `remember=true` 会把密码存进机密存储。
3. 调用 `hosts`。本地缓存超过 600 秒时，它会拉取云端。

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
| `sync` | 立即强制拉取，也是传入并记住保险库密码的入口 |
| `hosts` | 列出主机（可选 `query`）。缓存超过 600 秒时拉取。拉取失败则返回本地缓存 |
| `host` | 一台主机 + 合并后的 SSH 设置 + `ssh_command`。缓存超过 600 秒时拉取。拉取失败则返回本地缓存 |
| `exec` | 通过 SSH 运行远程命令 |
| `files` | SFTP list / stat / read / write / get / put / mkdir / rm / rename |
| `inventory` | `kind=groups\|identities\|keys\|snippets` |

`hosts` 和 `host` 在本地缓存超过 600 秒时拉取。拉取失败时，它们返回本地缓存，并把 `stale` 设为 true。`sync_error` 是这次拉取的错误。没有登录或没有保险库密码时仍然报错。`exec`、`files` 和 `inventory` 在本地缓存早于 `TERMIUS_SYNC_TTL` 时拉取，前提是保险库密码可用。这三个工具在拉取失败时仍然让这次调用失败。

`files` 使用与 `exec` 相同的 SSH 凭据，通过 SFTP 工作。`get` 和 `put` 在 MCP 主机文件系统与远程主机之间复制。`read` 和 `write` 通过工具结果传输文件内容（最大 200000 字节）。`get` 和 `put` 允许最大 50 MiB。`list` 默认把 `path` 设为 SSH 登录目录。

## 本地数据

桌面机器通过 [`keyring`](https://pypi.org/project/keyring/) 把机密存进系统钥匙串：macOS 钥匙串、Windows 凭据管理器，或 Linux Secret Service（GNOME Keyring、KWallet）。条目的服务名为 `termius-mcp:<目录>`。

没有可用钥匙串的机器（服务器、容器、没有桌面会话的 Linux）把同样的名字存进 `~/.termius/secrets`：一个 Fernet 令牌，权限 `0600`。第一次启动把选择写进 `config` 的 `[Secrets] backend`。之后的启动沿用这个选择，所以同一台机器上的桌面会话和 SSH 会话用同一个存储。`TERMIUS_KEYRING` 可以改掉已保存的选择，并把新选择写回去。

文件密钥由机器标识和用户 id 派生。这个文件不是可读文本，复制到别的机器或别的账号上也解不开。但它挡不住已经以这个用户身份登录这台机器的人。设置 `TERMIUS_SECRETS_KEY` 后，文件改用这个口令封装。容器里应该设置它。slim 镜像没有 `/etc/machine-id`，程序也不会回退到 MAC 地址。两者都没有时，这个文件的保密程度只和它所在的目录一样。

第一次选择文件存储、并且 `secrets` 还是空的时候，如果系统钥匙串里已经有 `vault_password`、`User.apikey`、`User.private_key`、`User.personal_v4_key` 和 `storage_key`，这些条目会复制进文件。钥匙串里的原条目保留。还没有保存选择时，`PYTHON_KEYRING_BACKEND` 会选钥匙串。这样，按旧说明在无头机器上设置了这个变量的安装会继续用钥匙串。

机密名：

- `vault_password` — 已记住的保险库密码（如果你选择了 `remember`）
- `User.apikey` — DeviceToken
- `User.private_key`、`User.personal_v4_key` — 解开后的个人密钥
- `storage_key` — 加密 `~/.termius/storage` 的密钥

`~/.termius/` 中的文件：

- `config` — 用户名、salt、`last_synced`
- `storage` — 主机、分组、身份、密钥、代码片段，用 `storage_key` 加密（Fernet），权限为 `0600`
- `secrets` — 只在没有钥匙串的机器上出现，见上

私钥只保存在 `storage` 中。`exec` 和 `files` 在内存中加载私钥，因此 `host` 返回的 `ssh_command` 不带 `-i` 选项。

首次启动时，旧版本留下的明文数据会迁入机密存储：移走 `vault` 文件和 `config` 中的机密，加密 `storage`，删除 `ssh_keys/`。

机密文件格式损坏，或者是别的版本写的，服务器把它改名为 `secrets.bad-<UTC 时间>`，然后用空存储启动。这时 `status` 报告 `logged_in: false`。重新登录即可。改名后的文件会留下来。

用 `TERMIUS_SECRETS_KEY` 封装的文件，在这个变量没设或者对不上时，服务器不会启动。文件字节保持原样。把同一个口令设回去再启动。

文件绑在另一台机器的标识上时，服务器也不会启动。文件字节保持原样。`/etc/machine-id` 变了（重装系统、克隆镜像），或者文件被搬到别的机器，都会这样。把 `secrets` 移到旁边，再启动，然后重新登录，重新输入保险库密码。进程不会自己换掉这个文件。换一个空存储会丢掉已保存的登录。

## 回退到 PyPI 3.0.0

需要 Termius 账号和保险库密码。如果密码只在 `secrets` 里，而你不记得它，就不要回退。

1. 停掉所有 `termius-mcp` 进程。运行 `pgrep -af termius`，确认没有进程。
2. 备份目录：`cp -a ~/.termius ~/.termius.bak-$(date +%Y%m%dT%H%M%S)`。
3. 如果留着升级前的 `~/.termius` 备份，恢复那个备份，然后停在这里。
4. 没有备份时，把 `~/.termius/storage` 和 `~/.termius/secrets` 移到旁边。不要删除。
5. 运行 `pip install termius-mcp==3.0.0`。
6. 启动服务器，重新登录，然后调用 `hosts`。

3.0.0 打不开这个版本写的 `storage`。它会抛出 `ValueError: File not in a supported format`，并且启动不了。移走 `storage` 和 `secrets` 之后，3.0.0 可以启动，主机数为 0，apikey 为空。`config` 里的 `[Secrets]` 段不影响 3.0.0。3.0.0 会把 apikey 以明文写回 `config`。

## 加密说明

Termius Cloud 目前有两种个人加密方案：

- **v3** — 按字段的 RNCryptor（AES-CBC + HMAC）。REST 登录即可。
- **v5** — 实体 `content` blob 用 Argon2id + XChaCha20-Poly1305 密封，并使用 SRP 登录。

服务器自动检测密文版本（`A…` = v3，`B…` = v5）。登录先使用 gRPC/SRP（桌面端 `login_v2`）。REST 只作为未迁移账户（`NOT_MIGRATED`）的回退。对于 v5 SRP，保险库密码先按 Argon2id 哈希（libsodium interactive，16 字节 salt），再做 base64 编码，然后用 Botan SRP-6a ``modp/srp/8192`` + **Blake2b-512** 完成证明。``public_data`` / ``proof`` 是不带 ``0x`` 前缀的大写十六进制（Android ``libtermius`` 在写入 gRPC/Socket.IO 载荷前去掉 Botan 的前缀）。

团队保险库：`sync` / 自动拉取会加载 `/api/v4/team/vault/keys/`。程序用个人 X25519 密钥对解开每个 `encrypted_with` 密钥（ECDH + HChaCha20 + XChaCha20-Poly1305）。然后解密共享的主机、密钥和身份。如果某个实体的保险库密钥缺失，程序跳过该实体，不删除它。

## 许可证

见 [LICENSE](LICENSE)。
