# AGENTS.md —— 推送到 GitHub 的工作约定

> 给所有在本仓库工作的 Agent（DSH / DeepSeek / 其他 CLI Agent）看。
> 目标：一条命令把**已提交**的内容推上 GitHub，不要重复踩下面这些坑。

## 1. 一条命令

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\push-github.ps1
```

也可以直接用包装脚本（参数原样透传）：

```bat
scripts\push-github.cmd
```

成功标志：最后一行输出 `::RESULT:: SUCCESS branch=main head=<sha>`，退出码 0。
脚本自己会定位仓库根目录，从任何工作目录调用都可以。

## 2. 只检查、不上传

| 命令 | 作用 | 退出码 |
| --- | --- | --- |
| `... push-github.ps1 -VerifyOnly` | 只比较远端分支与本地 HEAD | 0 一致 / 5 不一致 / 4 读不到 |
| `... push-github.ps1 -DryRun` | 完整走一遍推送握手（含认证），但不写远端 | 0 可用 |

改完东西先 `-VerifyOnly` 或 `-DryRun`，确认凭据和网络是通的，再正式推。

## 3. 凭据放在哪里

脚本按顺序找凭据，找到就用；**被 GitHub 403 拒绝会自动换下一个**。

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 环境变量 `GITHUB_TOKEN` / `GH_TOKEN` | CI、临时覆盖最方便 |
| 2 | `%USERPROFILE%\.config\github-push\token` | **推荐**。一行 token，ACL 已收紧为仅当前用户可读写 |
| 3 | `%USERPROFILE%\.github-token` | 兼容其他工具 |
| 4 | Windows 凭据管理器 `git:https://github.com` | 直接调用 `git-credential-wincred.exe`，绕过 sh |

- token 权限：fine-grained 要勾 **Contents: Read and write**；classic 要 `repo`。
- 换 token：直接覆盖 `%USERPROFILE%\.config\github-push\token`，仓库里不用改任何文件。
- **不要把 token 写进仓库**。`tools/`、`logs/` 虽已 gitignore，也不要往里放。

## 4. 为什么不能直接 `git push origin main`

Agent 运行在文件沙箱里，`.git` 目录受保护，会出现两类必然失败：

1. `git push origin main` 成功后要更新 `refs/remotes/origin/main`，沙箱里直接报
   `cannot lock ref ... Permission denied`。
2. Git 凭据管理器（GCM）在沙箱里要靠 `sh.exe` 启动，而 sh 创建管道会被拒绝
   （`couldn't create signal pipe, Win32 error 5`），于是认证直接崩掉。

所以约定：

- **推送只走 `scripts\push-github.ps1`**。它用「带凭据的 URL + 显式 refspec」推送，
  不写本地 ref；输出和日志里的 token 一律脱敏成 `******`。
- `git commit` / `git tag` / `git fetch` 同样会写 `.git`，沙箱里会失败。
  需要提交时，请让用户在**非沙箱**终端里执行，或申请更宽的沙箱权限。
- 不要临时改 `.git/config` 里的凭据助手，也不要把 token 塞进 remote URL 长期保存。

## 5. 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-RepoRoot` | 脚本上一级目录 | 仓库根目录 |
| `-Branch` | 当前分支 | 目标分支 |
| `-RemoteUrl` | `origin` 的 URL | 远端地址 |
| `-TokenFile` | 无 | 额外指定一个 token 文件 |
| `-Retries` | 4 | 每个凭据最多尝试几次 |
| `-TimeoutSec` | 90 | 单次 git 操作超时 |
| `-DryRun` | 关 | 只握手，不上传 |
| `-AllTags` | 关 | 推全部 tag；默认只推本次提交可达的附注 tag |
| `-NoVerify` | 关 | 推送后不校验远端 SHA |
| `-VerifyOnly` | 关 | 只校验，不推送 |

## 6. 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功（或 `-VerifyOnly` 一致） |
| 1 | 用法/环境错误（不是 git 仓库、没有提交、git 起不来） |
| 2 | 找不到任何凭据 |
| 3 | 所有凭据都被 GitHub 拒绝 |
| 4 | 网络持续不通 |
| 5 | 推送命令成功但远端 SHA 与本地不一致（或 `-VerifyOnly` 发现不一致） |

## 7. 常见错误对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `403 Permission to ... denied` | token 没有写权限 / 已过期 / 没勾选该仓库 | 换一个 token，覆盖凭据文件 |
| `Could not connect to github.com:443` | 本机到 GitHub 间歇性阻断 | 重跑即可，脚本已内置重试 |
| `couldn't create signal pipe` | 走到了 GCM / sh | 用本脚本，别用裸 `git push` |
| `cannot lock ref` | 沙箱禁止写 `.git` | 用本脚本（URL 推送） |
| `Everything up-to-date` | 内容已经在远端 | 正常，退出码 0 |
| `-VerifyOnly` 退出 5 | 远端比本地新（别人推过，或本地没同步） | 让用户确认后用非沙箱终端 `git pull --ff-only` |

## 8. 网络

- 本机到 `github.com:443` 会间歇性断连，单次失败是常态：脚本默认 4 次 × 90 秒重试。
- 想更稳，可以先跑本项目的加速工具（`github-accel.ps1` / `一键更新hosts.cmd`，需要管理员）。
- **不要因为一次网络失败就改脚本或改 git 配置**，先重跑。

## 9. 安全与遗留问题

- token 只放在用户目录；脚本输出与 `logs\push-github-*.log` 都会脱敏。
- 历史遗留：`tools/token.txt` 里那个 fine-grained token 已被 GitHub 拒绝（403），
  已备份到 `%USERPROFILE%\.config\github-push\token.invalid-20261114`，**不要再用它**。
- `tools/PortableGit/` 是本机便携版 git（不入库），脚本会自动优先使用它。
