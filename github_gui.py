#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 加速控制台 —— 图形界面（tkinter，Python 自带，无需安装任何第三方库）

功能：
  * 把 github520.py（Python 版）和 github-accel.ps1（PowerShell 版）的所有常用动作
    做成可勾选的任务清单，支持「选择性运行」与顺序批量执行。
  * 实时流式呈现子进程输出（Python 的 print / PowerShell 的 Write-Host 都会被捕获），
    并按 [OK] / [警告] / [错误] / -> / ===== 自动着色。
  * 三个视图：
      运行输出   本次/历史的命令输出，带时间戳、退出码、耗时
      日志文件   直接浏览 logs\\ 下的 accel-*.log、github520-*.log（可自动刷新）
      当前 hosts 解析当前 hosts 标记块的每一条记录，并可一键验证真实解析结果
  * 权限不足时一键「以管理员身份重启」（写 hosts / 注册计划任务需要）。
  * 浏览器直连：勾选要看的页面后一键打开浏览器，并在浏览器进程内直接锁定 hosts 里的
    优选 IP（--host-resolver-rules）、禁用浏览器代理（--no-proxy-server），
    即使系统 DNS 被污染 / 浏览器开了 DoH 也能直连 GitHub。

用法：
    python github_gui.py          （或 py -3 github_gui.py）
    pythonw github_gui.py         不显示控制台窗口
"""

from __future__ import annotations

import codecs
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Sequence, Tuple

APP_TITLE = "GitHub 加速控制台"
APP_VERSION = "1.1.0"
HERE = Path(__file__).resolve().parent
PY_SCRIPT = HERE / "github520.py"
PS_SCRIPT = HERE / "github-accel.ps1"
HOSTS_PATH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
BACKUP_DIR = HERE / "backup"
LOG_DIR = HERE / "logs"
TASK_NAME = "GitHub520-HostsUpdate"
MARKER_START = "# >>> github-accel >>>"
MARKER_END = "# <<< github-accel <<<"
BLOCK_RE = re.compile(re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END), re.S)
HOSTS_LINE_RE = re.compile(r"^\s*([\d.]+)\s+([A-Za-z0-9][A-Za-z0-9._-]*)")
# 「域名 IP」写反的行：Windows 只认「IP 域名」，写反了整行等于没生效
HOSTS_LINE_SWAPPED_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s+([\d.]+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"


# --------------------------------------------------------------------------
# 环境辅助
# --------------------------------------------------------------------------

def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def enable_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def python_exe() -> str:
    """返回可用的 python.exe（若本程序跑在 pythonw 下，换成同目录的 python.exe 才能拿到输出）。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.is_file():
            return str(cand)
    return str(exe)


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def fmt_size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


# --------------------------------------------------------------------------
# 任务定义
# --------------------------------------------------------------------------

@dataclass
class Task:
    key: str
    group: str
    title: str
    desc: str
    argv: List[str]
    needs_admin: bool = False
    long_running: bool = False
    available: bool = True
    kind: str = "cmd"                       # cmd = 起子进程；web = 打开浏览器
    urls: List[str] = field(default_factory=list)
    opts: Dict[str, object] = field(default_factory=dict)


def ps_task(key: str, group: str, title: str, desc: str, ps_args: str,
            needs_admin: bool = False) -> Task:
    inner = str(PS_SCRIPT).replace("'", "''")
    cmd = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
           f"& '{inner}' {ps_args}")
    return Task(key, group, title, desc,
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                needs_admin=needs_admin, available=PS_SCRIPT.is_file())


def web_task(key: str, title: str, desc: str, url: str = "") -> Task:
    return Task(key, WEB_GROUP, title, desc, [], kind="web", urls=[url] if url else [])


def build_tasks() -> List[Task]:
    py = python_exe()
    pyw = [py, "-u", str(PY_SCRIPT)]
    g_py = "Python 版 · github520.py"
    g_ps = "PowerShell 版 · github-accel.ps1"
    g_sys = "系统工具"

    tasks: List[Task] = [
        # ---- 浏览器直连：勾选后点「一键打开并直连 GitHub」即可逐个打开 ----
        web_task("web_doctor", "⓪ 直连自检（不打开浏览器）",
                 "浏览器 / hosts / DoH / 代理 一次体检"),
        web_task("web_home", "① 打开 GitHub 首页",
                 "页面与静态资源都走优选 IP", "https://github.com/"),
        web_task("web_trending", "② 打开 GitHub 今日趋势",
                 "热门仓库页，图片走 CDN", "https://github.com/trending"),
        web_task("web_search", "③ 打开 GitHub 搜索页",
                 "搜索/列表页，验证动态请求", "https://github.com/search"),
        web_task("web_gist", "④ 打开 Gist",
                 "最容易被 DNS 污染，验证锁定 IP", "https://gist.github.com/"),
        web_task("web_raw", "⑤ 打开 raw 文件（验证 CDN）",
                 "能显示文件内容就说明直连成功",
                 "https://raw.githubusercontent.com/github/gitignore/main/Python.gitignore"),

        Task("py_check", g_py, "① 体检：浏览器到 GitHub 连接状态",
             "浏览器/代理/DoH/hosts/DNS/连通性（不需要管理员）", [*pyw, "check"],
             available=PY_SCRIPT.is_file()),
        Task("py_check_json", g_py, "① 体检（JSON 结果）",
             "同上，附机器可读 JSON", [*pyw, "check", "--json"], available=PY_SCRIPT.is_file()),
        Task("py_update_dry", g_py, "② 预览更新（DryRun）",
             "只拉取+测速并显示将写入的 IP，不改系统", [*pyw, "update", "--dry-run"],
             available=PY_SCRIPT.is_file()),
        Task("py_update", g_py, "③ 更新 hosts（优选 IP）",
             "拉取 + DoH + TLS/HTTP 三层优选后写入 hosts", [*pyw, "update"],
             needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_update_fast", g_py, "③ 更新 hosts（快速，不测速）",
             "直接用 GitHub520 源里的 IP", [*pyw, "update", "--no-optimize"],
             needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_watch_once", g_py, "③ 单轮自检并更新",
             "等同计划任务里执行的那一条", [*pyw, "watch", "--once"],
             needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_watch", g_py, "③ 常驻监控（每 30 分钟）",
             "持续运行，直到点「停止」（需管理员）", [*pyw, "watch", "--interval", "1800"],
             needs_admin=True, long_running=True, available=PY_SCRIPT.is_file()),
        Task("py_status", g_py, "④ 查看状态",
             "加速条目 / 备份 / 计划任务 / 数据源可达性", [*pyw, "status"],
             available=PY_SCRIPT.is_file()),
        Task("py_backup_list", g_py, "④ 列出备份", "列出 backup 目录下的 hosts 备份",
             [*pyw, "restore", "--list"], available=PY_SCRIPT.is_file()),
        Task("py_restore", g_py, "④ 从备份还原 hosts", "用最近一次备份覆盖 hosts",
             [*pyw, "restore"], needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_task_install", g_py, "⑤ 注册计划任务（每小时）",
             "开机后每小时自动更新 hosts", [*pyw, "task", "install"],
             needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_task_status", g_py, "⑤ 计划任务状态", "查询 GitHub520-HostsUpdate",
             [*pyw, "task", "status"], available=PY_SCRIPT.is_file()),
        Task("py_task_uninstall", g_py, "⑤ 删除计划任务", "卸载 GitHub520-HostsUpdate",
             [*pyw, "task", "uninstall"], needs_admin=True, available=PY_SCRIPT.is_file()),

        ps_task("ps_diagnose", g_ps, "① 诊断：DNS/TCP/TLS/代理",
                "分层诊断并给出结论", "-Action diagnose"),
        ps_task("ps_hosts_dry", g_ps, "② 预览 hosts 更新", "只显示将写入的条目",
                "-Action hosts -DryRun"),
        ps_task("ps_hosts", g_ps, "③ 更新 hosts", "GitHub520 多源更新 hosts",
                "-Action hosts", needs_admin=True),
        ps_task("ps_hosts_restore", g_ps, "④ 还原 hosts", "从 PS 版备份恢复",
                "-Action hosts -Restore", needs_admin=True),
        ps_task("ps_proxy", g_ps, "git 代理：自动配置", "探测本地代理端口并写入 git 全局配置",
                "-Action proxy"),
        ps_task("ps_proxy_off", g_ps, "git 代理：取消", "移除脚本写入的 git 代理设置",
                "-Action proxy -Off"),
        ps_task("ps_mirror", g_ps, "镜像重写：启用", "只读加速，push 会失败",
                "-Action mirror"),
        ps_task("ps_mirror_off", g_ps, "镜像重写：关闭", "移除 insteadOf 规则",
                "-Action mirror -Off"),
        ps_task("ps_status", g_ps, "查看状态", "hosts / git 代理 / 计划任务",
                "-Action status"),
        ps_task("ps_off", g_ps, "一键还原（全部）", "移除 hosts 条目 + 取消 git 代理 + 移除镜像",
                "-Action off", needs_admin=True),

        Task("sys_hosts_tail", g_sys, "查看 hosts 文件（末尾 80 行）", str(HOSTS_PATH),
             ["powershell.exe", "-NoProfile", "-Command",
              "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
              f"Get-Content -LiteralPath '{str(HOSTS_PATH).replace(chr(39), chr(39) * 2)}' "
              "-Encoding UTF8 -Tail 80"]),
        Task("sys_flush", g_sys, "刷新 DNS 缓存", "等同于 ipconfig /flushdns",
             ["cmd.exe", "/c", "chcp 65001>nul && ipconfig /flushdns"]),
        Task("sys_restart_dns", g_sys, "重启 DNS 客户端服务（让 hosts 立刻生效）",
             "Restart-Service Dnscache -Force：改完 hosts 却不生效时先试这个",
             ["powershell.exe", "-NoProfile", "-Command",
              "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
              "Restart-Service Dnscache -Force -ErrorAction Stop; "
              "Write-Host '[OK] DNS 客户端服务已重启，hosts 会被重新读取'"],
             needs_admin=True),
        Task("sys_ping", g_sys, "测试 github.com 延迟", "ping 3 次",
             ["cmd.exe", "/c", "chcp 65001>nul && ping -n 3 github.com"]),
        Task("sys_ping_raw", g_sys, "测试 raw.githubusercontent.com 延迟", "ping 3 次",
             ["cmd.exe", "/c", "chcp 65001>nul && ping -n 3 raw.githubusercontent.com"]),
    ]
    return tasks


# --------------------------------------------------------------------------
# 浏览器直连（一键打开 GitHub）
# --------------------------------------------------------------------------
# 思路：hosts 只改「系统解析」，一旦浏览器开了安全 DNS(DoH)、或 DNS 客户端没重载
# hosts、或上游 DNS 继续投毒，hosts 就白改了。所以这里在启动浏览器时直接把优选 IP
# 塞进浏览器进程：
#   --host-resolver-rules="MAP github.com 20.27.177.113, MAP ..."  绕过 DNS/DoH
#   --no-proxy-server                                              绕过系统代理/PAC
# 这两个开关只影响本次启动的浏览器实例，不改注册表、不改 hosts，关掉即失效。

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CHROMIUM_EXES = {
    "msedge.exe", "chrome.exe", "brave.exe", "chromium.exe", "vivaldi.exe", "opera.exe",
    "360se.exe", "360chrome.exe", "qqbrowser.exe", "sogouexplorer.exe", "maxthon.exe",
    "liebao.exe", "2345explorer.exe", "ucbrowser.exe",
}
CHROMIUM_STATE = (
    ("Microsoft Edge", r"Microsoft\Edge\User Data"),
    ("Google Chrome", r"Google\Chrome\User Data"),
    ("Brave", r"BraveSoftware\Brave-Browser\User Data"),
)
DOH_MODE_TEXT = {
    "off": "关闭（会读 hosts）",
    "secure": "强制开启（一定绕过 hosts）",
    "automatic": "自动（可能绕过 hosts）",
}
WEB_HOME = "https://github.com/"
WEB_GROUP = "浏览器直连（勾选后一键打开）"
BROWSER_AUTO = "自动（跟随系统默认浏览器）"
# 这些域名最能反映「hosts 到底有没有生效」
KEY_WATCH_DOMAINS = ("github.com", "gist.github.com", "raw.githubusercontent.com",
                     "api.github.com", "codeload.github.com", "objects.githubusercontent.com")


def _reg_query(root, path: str, name: Optional[str] = None):
    if not IS_WINDOWS:
        return None
    import winreg
    try:
        with winreg.OpenKey(root, path) as key:
            if name is None:
                return winreg.QueryValue(key, None)
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _reg_subkeys(root, path: str) -> List[str]:
    if not IS_WINDOWS:
        return []
    import winreg
    out: List[str] = []
    try:
        with winreg.OpenKey(root, path) as key:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out


def _extract_exe(cmd) -> str:
    """从注册表的命令行里取出 exe 路径（兼容带引号 / 不带引号 / 带参数）。"""
    if not cmd:
        return ""
    text = str(cmd).strip()
    m = re.match(r'"([^"]+)"', text)
    if m:
        return m.group(1)
    parts = text.split()
    for i in range(len(parts), 0, -1):
        cand = " ".join(parts[:i])
        if Path(cand).is_file():
            return cand
    return parts[0] if parts else ""


def installed_browsers() -> List[Tuple[str, str]]:
    """列出已安装浏览器 -> [(显示名, exe路径)]。"""
    if not IS_WINDOWS:
        return []
    import winreg
    found: Dict[str, str] = {}
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        base = r"SOFTWARE\Clients\StartMenuInternet"
        for sub in _reg_subkeys(root, base):
            name = _reg_query(root, rf"{base}\{sub}", "") or sub
            cmd = _reg_query(root, rf"{base}\{sub}\shell\open\command", "")
            exe = _extract_exe(cmd)
            if exe:
                found.setdefault(str(name), exe)
    well_known = {
        "Microsoft Edge": Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        "Google Chrome": Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        "Mozilla Firefox": Path(os.environ.get("ProgramFiles", "")) / "Mozilla Firefox/firefox.exe",
    }
    for name, path in well_known.items():
        if path.is_file():
            found.setdefault(name, str(path))
    return sorted((n, p) for n, p in found.items() if Path(p).is_file())


def default_browser() -> Tuple[str, str]:
    """返回系统默认浏览器 (显示名, exe)。"""
    if not IS_WINDOWS:
        return ("", "")
    import winreg
    prog = _reg_query(winreg.HKEY_CURRENT_USER,
                      r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
                      "ProgId")
    if not prog:
        return ("", "")
    exe = _extract_exe(_reg_query(winreg.HKEY_CLASSES_ROOT, rf"{prog}\shell\open\command", ""))
    name = str(prog)
    for label, path in installed_browsers():
        if exe and os.path.normcase(path) == os.path.normcase(exe):
            name = label
            break
    return (name, exe if exe and Path(exe).is_file() else "")


def block_rows(text: str) -> Tuple[List[Tuple[str, str]], str, int]:
    """从 hosts 文本里取出标记块 -> ([(ip, 域名)], 生成时间, 写反的行数)。

    Windows 只认「IP 域名」；「域名 IP」的行会被整行忽略，所以单独统计出来报警。
    """
    match = BLOCK_RE.search(text)
    if not match:
        return [], "", 0
    body = match.group(0)
    ts = re.search(r"Generated by [\w.\-]+ at ([\d\-: ]+)", body)
    rows: List[Tuple[str, str]] = []
    swapped = 0
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = HOSTS_LINE_RE.match(line)
        if m:
            rows.append((m.group(1), m.group(2).lower()))
            continue
        m = HOSTS_LINE_SWAPPED_RE.match(line)
        if m:
            swapped += 1
            rows.append((m.group(2), m.group(1).lower()))
    return rows, (ts.group(1).strip() if ts else "未知"), swapped


def read_hosts_text() -> Tuple[str, str]:
    """读取 hosts 文本 -> (文本, 编码)。"""
    raw = HOSTS_PATH.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def hosts_entries() -> Tuple[List[Tuple[str, str]], str, int]:
    """读取 hosts 标记块 -> ([(ip, 域名)], 生成时间, 写反的行数)。"""
    try:
        text, _enc = read_hosts_text()
    except OSError:
        return [], "", 0
    return block_rows(text)


def system_proxy_summary() -> str:
    if not IS_WINDOWS:
        return "非 Windows 系统"
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    pac = _reg_query(winreg.HKEY_CURRENT_USER, base, "AutoConfigURL") or ""
    if pac:
        return f"PAC 自动配置：{pac}"
    if _reg_query(winreg.HKEY_CURRENT_USER, base, "ProxyEnable"):
        return f"已启用：{_reg_query(winreg.HKEY_CURRENT_USER, base, 'ProxyServer') or '（未填地址）'}"
    return "未启用（系统直连）"


def browser_doh_summary() -> List[str]:
    """Chrome/Edge/Brave 的「安全 DNS(DoH)」——开启后会绕过 hosts。"""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    out: List[str] = []
    for label, rel in CHROMIUM_STATE:
        state_file = local / rel / "Local State"
        if not state_file.is_file():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        doh = data.get("dns_over_https") or {}
        mode = str(doh.get("mode", "") or "off").lower() if isinstance(doh, dict) else "?"
        out.append(f"{label} 安全 DNS(DoH)：{DOH_MODE_TEXT.get(mode, mode)}")
    return out


def is_chromium(exe: str) -> bool:
    return Path(exe).name.lower() in CHROMIUM_EXES


def build_browser_argv(exe: str, urls: Sequence[str], entries: Sequence[Tuple[str, str]],
                       pin: bool, no_proxy: bool) -> Tuple[List[str], List[str]]:
    """拼出浏览器命令行 -> (argv, 说明/警告)。"""
    argv: List[str] = [exe]
    notes: List[str] = []
    stem = Path(exe).name.lower()
    if stem in CHROMIUM_EXES:
        if no_proxy:
            argv.append("--no-proxy-server")
            notes.append("-> 已禁用浏览器代理（--no-proxy-server），忽略系统代理/PAC")
        if pin and entries:
            rules = ", ".join(f"MAP {d} {ip}" for ip, d in entries)
            argv.append("--host-resolver-rules=" + rules)
            notes.append(f"-> 已在浏览器内锁定 {len(entries)} 个域名的 IP"
                         "（--host-resolver-rules，直接绕过 DNS 与 DoH）")
        argv.extend(urls)
    elif stem == "firefox.exe":
        for u in urls:
            argv.extend(["-new-tab", u])
        if pin and entries:
            notes.append("[警告] Firefox 不支持命令行锁定 IP，只能靠 hosts 生效；"
                         "若开了 DoH 请到「设置 → 隐私与安全 → 基于 HTTPS 的 DNS」里关闭")
        if no_proxy:
            notes.append("[警告] Firefox 不跟随系统代理，如单独配了代理请自行关闭")
    else:
        argv.extend(urls)
        if pin or no_proxy:
            notes.append(f"[警告] 未识别的浏览器（{stem}）：无法锁定 IP / 禁用代理，"
                         "已按普通方式打开")
    return argv, notes


def browser_running(exe: str) -> bool:
    name = Path(exe).name
    if not name:
        return False
    try:
        proc = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                              capture_output=True, text=True, errors="replace",
                              timeout=20, creationflags=CREATE_NO_WINDOW)
    except Exception:
        return False
    return name.lower() in (proc.stdout or "").lower()


def short_cmdline(argv: Sequence[str]) -> str:
    """打印用命令行：把超长的 IP 锁定规则压缩成前几条。"""
    out: List[str] = []
    for a in argv:
        if a.startswith("--host-resolver-rules="):
            body = a.split("=", 1)[1]
            head = ", ".join(body.split(", ")[:3])
            out.append(f"--host-resolver-rules={head}, … 共 {body.count('MAP ')} 条")
        else:
            out.append(a)
    return subprocess.list2cmdline(out)


def resolver_check(entries: Sequence[Tuple[str, str]],
                   domains: Sequence[str] = KEY_WATCH_DOMAINS) -> List[str]:
    """对比「系统解析结果」与「hosts 写的 IP」，判断 hosts 到底有没有生效。"""
    import socket
    table = {d: ip for ip, d in entries}
    hit = miss = dead = 0
    details: List[str] = []
    for d in domains:
        want = table.get(d)
        if not want:
            continue
        try:
            got = sorted({i[4][0] for i in socket.getaddrinfo(d, 443, socket.AF_INET, socket.SOCK_STREAM)})
        except Exception:
            got = []
        if want in got:
            hit += 1
        elif got:
            miss += 1
            details.append(f"[警告] {d} 系统解析到 {', '.join(got[:3])}，hosts 写的是 {want}"
                           " —— 解析器没按 hosts 返回（DNS 劫持 / 缓存 / 服务未重载）")
        else:
            dead += 1
            details.append(f"[警告] {d} 系统解析失败，hosts 写的是 {want}"
                           " —— 解析器没按 hosts 返回（DNS 劫持 / 缓存 / 服务未重载）")
    total = hit + miss + dead
    head = []
    if hit and not (miss or dead):
        head.append(f"[OK] 系统解析已按 hosts 生效（{hit}/{total} 个关键域名一致）")
    elif hit:
        head.append(f"[警告] 系统解析只有 {hit}/{total} 个关键域名按 hosts 返回"
                    "（其余被 DNS 缓存/劫持覆盖）")
    elif total:
        head.append(f"[警告] 系统解析 0/{total} 个关键域名按 hosts 返回——"
                    "hosts 目前等于没生效")
    return head + details[:4]


def self_check_report() -> List[str]:
    """浏览器直连自检：不开浏览器，只报告现状与可行性。"""
    lines: List[str] = []
    dname, dexe = default_browser()
    browsers = installed_browsers()
    lines.append(f"[OK] 默认浏览器：{dname or '未识别'}")
    if dexe:
        lines.append(f"-> 路径：{dexe}")
    lines.append("-> 已安装：" + ("、".join(n for n, _ in browsers) if browsers else "未检测到"))
    entries, gen, swapped = hosts_entries()
    if entries:
        lines.append(f"[OK] hosts 加速条目：{len(entries)} 条（生成于 {gen}）")
        if swapped:
            lines.append(f"[错误] 其中 {swapped} 行写成了「域名 IP」顺序——Windows 只认「IP 域名」，"
                         "这些行当前完全没生效！请运行一次「③ 更新 hosts（优选 IP）」覆写该区块。")
            lines.append("-> 下面的解析对比仅供参考（当前区块无效，个别域名一致只是巧合）")
        lines += resolver_check(entries)
    else:
        lines.append("[警告] hosts 里没有 github-accel 标记块：先跑一次"
                     "「更新 hosts（优选 IP）」，否则没有 IP 可锁定")
    lines.append(f"-> 系统代理：{system_proxy_summary()}")
    doh = browser_doh_summary()
    lines += ["-> " + s for s in doh] or ["-> 未检测到 Chromium 系浏览器的 DoH 配置"]
    target = dexe or (browsers[0][1] if browsers else "")
    if target and is_chromium(target):
        lines.append("[OK] 支持 IP 锁定：Chromium 系浏览器可用 --host-resolver-rules"
                     " 直接指定 IP，绕过 DNS/DoH，hosts 生效不生效都不影响")
    elif Path(target).name.lower() == "firefox.exe":
        lines.append("[警告] Firefox 不支持命令行锁 IP：需要靠 hosts 生效，且要确认 DoH 已关")
    lines.append("-> 结论：勾选要看的页面 → 点「一键打开并直连 GitHub」即可")
    return lines


def open_websites(urls: Sequence[str], exe: str, pin: bool, no_proxy: bool,
                  flush: bool = True) -> List[str]:
    """在（可选的）浏览器里打开若干 URL，返回给人看的日志行。"""
    lines: List[str] = []
    if flush:
        try:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True,
                           timeout=25, creationflags=CREATE_NO_WINDOW)
            lines.append("-> 已刷新 DNS 缓存（ipconfig /flushdns）")
        except Exception as exc:
            lines.append(f"[警告] 刷新 DNS 缓存失败：{exc}")
    entries, _gen, swapped = hosts_entries()
    if entries:
        lines.append(f"-> hosts 加速条目 {len(entries)} 条，可用于锁定 IP")
        if swapped:
            lines.append(f"[警告] 但其中 {swapped} 行是「域名 IP」顺序，系统根本没生效；"
                         "本次仍会用这些 IP 在浏览器内锁定，建议之后跑一次更新覆写 hosts")
    if pin and not entries:
        lines.append("[警告] hosts 里没有加速条目，本次无法锁定 IP：将使用系统 DNS 解析")
    if pin and entries:
        lines += resolver_check(entries, domains=("github.com", "gist.github.com"))

    if not exe:
        lines.append("[警告] 没识别出浏览器 exe，改用系统默认方式打开")
        for u in urls:
            try:
                os.startfile(u)  # type: ignore[attr-defined]
            except Exception as exc:
                lines.append(f"[错误] 打开 {u} 失败：{exc}")
                return lines
        lines.append(f"[OK] 已交给默认程序打开 {len(urls)} 个页面：" + "、".join(urls))
        return lines

    already = browser_running(exe)
    if already and (pin or no_proxy):
        lines.append(f"[警告] {Path(exe).name} 正在运行：已运行的实例会接管标签页，"
                     "本次的「锁定 IP / 禁用代理」参数可能被忽略；"
                     "想确保生效请先完全退出该浏览器再打开")

    argv, notes = build_browser_argv(exe, urls, entries, pin, no_proxy)
    lines += notes
    lines.append("-> 命令行：" + short_cmdline(argv))
    try:
        subprocess.Popen(argv, cwd=str(HERE), close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except Exception as exc:
        lines.append(f"[错误] 启动浏览器失败：{type(exc).__name__}: {exc}")
        return lines
    lines.append(f"[OK] 已打开 {len(urls)} 个页面：" + "、".join(urls))
    lines.append("-> 若页面能正常打开且不再卡在「正在解析主机」，说明直连已生效")
    return lines


# --------------------------------------------------------------------------
# 可滚动区域
# --------------------------------------------------------------------------

class ScrollFrame(ttk.Frame):
    def __init__(self, master, width: int = 330, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, width=width)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        for widget in (self.canvas, self.inner):
            widget.bind("<Enter>", self._bind_wheel)
            widget.bind("<Leave>", self._unbind_wheel)

    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        # 自适应屏幕：小屏上不要把窗口顶出可视区域，并居中显示
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        win_w = min(1240, max(900, screen_w - 100))
        win_h = min(820, max(600, screen_h - 120))
        pos_x = max(0, (screen_w - win_w) // 2)
        pos_y = max(0, (screen_h - win_h) // 3)
        self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.minsize(980, 620)

        self.tasks = build_tasks()
        self.vars: Dict[str, tk.BooleanVar] = {t.key: tk.BooleanVar(value=False) for t in self.tasks}
        self.queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.proc: Optional[subprocess.Popen] = None
        self.running = False
        self.log_follow = tk.BooleanVar(value=True)
        self._last_log_sig: Tuple[str, int] = ("", -1)
        # 浏览器直连选项
        self.opt_pin = tk.BooleanVar(value=True)        # 锁定 hosts 优选 IP
        self.opt_noproxy = tk.BooleanVar(value=True)    # 禁用浏览器代理
        self.opt_flush = tk.BooleanVar(value=True)      # 打开前刷新 DNS 缓存
        self.browsers: List[Tuple[str, str]] = installed_browsers()
        self._default_browser: Tuple[str, str] = default_browser()
        self.cmb_browser: Optional[ttk.Combobox] = None

        self._build_style()
        self._build_toolbar()
        # 顺序很重要：先占好底部状态栏，再让主体填充剩余空间，否则状态栏会被挤没
        self._build_statusbar()
        self._build_body()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._pump)
        self.after(200, self._refresh_all)

    # ---------------------------------------------------------------- 样式
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=(UI_FONT, 10))
        style.configure("Treeview", font=(MONO_FONT, 10), rowheight=24)
        style.configure("Treeview.Heading", font=(UI_FONT, 10, "bold"))
        style.configure("Group.TLabelframe.Label", font=(UI_FONT, 11, "bold"), foreground="#0b5394")
        style.configure("Title.TLabel", font=(UI_FONT, 12, "bold"))
        style.configure("Desc.TLabel", font=(UI_FONT, 9), foreground="#666666")
        style.configure("Badge.TLabel", font=(UI_FONT, 10, "bold"))
        self.configure(bg="#f4f6f8")

    # -------------------------------------------------------------- 工具栏
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8, 10, 4))
        bar.pack(fill="x")

        self.btn_run = ttk.Button(bar, text="▶ 运行选中", command=self._run_selected, width=14)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■ 停止", command=self._stop, width=10, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="全选", command=lambda: self._set_all(True), width=8).pack(side="left")
        ttk.Button(bar, text="全不选", command=lambda: self._set_all(False), width=8).pack(side="left", padx=6)
        ttk.Button(bar, text="常用组合", command=self._preset_common, width=10).pack(side="left")
        ttk.Button(bar, text="清空输出", command=self._clear_output, width=10).pack(side="left", padx=6)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        self.btn_direct = ttk.Button(bar, text="▶ 一键直连 GitHub", command=self._one_click_direct,
                                     width=18)
        self.btn_direct.pack(side="left")

        self.lbl_admin = ttk.Label(bar, text="", style="Badge.TLabel")
        self.lbl_admin.pack(side="right")
        self.btn_elevate = ttk.Button(bar, text="提权重启", command=self._restart_as_admin, width=10)
        self.btn_elevate.pack(side="right", padx=8)

        bar2 = ttk.Frame(self, padding=(10, 0, 10, 6))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="自定义命令：").pack(side="left")
        self.ent_cmd = ttk.Entry(bar2, font=(MONO_FONT, 10))
        self.ent_cmd.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.ent_cmd.insert(0, "py -3 github520.py status")
        self.ent_cmd.bind("<Return>", lambda e: self._run_custom())
        ttk.Button(bar2, text="运行", command=self._run_custom, width=8).pack(side="left")
        ttk.Label(bar2, text="（在 PowerShell 中执行，输出同样会显示在下方）",
                  style="Desc.TLabel").pack(side="left", padx=8)

    # ---------------------------------------------------------------- 主体
    def _build_body(self) -> None:
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        # 左：任务清单
        left = ttk.Frame(paned)
        paned.add(left, weight=0)
        head = ttk.Frame(left)
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="任务清单（可多选，按顺序执行）", style="Title.TLabel").pack(side="left")

        self.scroll = ScrollFrame(left, width=385)
        self.scroll.pack(fill="both", expand=True)
        self._build_web_panel(self.scroll.inner)
        self._build_task_list(self.scroll.inner)

        # 右：视图
        right = ttk.Frame(paned)
        paned.add(right, weight=1)
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)

        self._build_output_tab()
        self._build_log_tab()
        self._build_hosts_tab()
        self._build_help_tab()

    def _build_task_list(self, parent) -> None:
        groups: Dict[str, List[Task]] = {}
        for task in self.tasks:
            if task.group == WEB_GROUP:      # 由 _build_web_panel 单独渲染
                continue
            groups.setdefault(task.group, []).append(task)

        for group, items in groups.items():
            box = ttk.Labelframe(parent, text=group, style="Group.TLabelframe", padding=(8, 4, 8, 8))
            box.pack(fill="x", expand=False, padx=4, pady=(4, 8))
            for task in items:
                row = ttk.Frame(box)
                row.pack(fill="x", pady=1)
                text = task.title + ("（需管理员）" if task.needs_admin else "")
                cb = ttk.Checkbutton(row, text=text, variable=self.vars[task.key])
                cb.pack(anchor="w")
                if not task.available:
                    cb.state(["disabled"])
                    self.vars[task.key].set(False)
                if task.desc:
                    ttk.Label(row, text="      " + task.desc, style="Desc.TLabel",
                              wraplength=350, justify="left").pack(anchor="w")

    # ------------------------------------------------------- 浏览器直连面板
    def _browser_labels(self) -> List[str]:
        labels = [BROWSER_AUTO]
        if self._default_browser[1]:
            labels.append(f"系统默认：{self._default_browser[0]}")
        labels += [f"{name}" for name, _exe in self.browsers]
        seen, out = set(), []
        for x in labels:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _selected_browser_exe(self) -> Tuple[str, str]:
        """返回本次要用的 (显示名, exe)。空 exe 表示交给系统默认方式打开。"""
        pick = self.cmb_browser.get() if self.cmb_browser is not None else BROWSER_AUTO
        if pick == BROWSER_AUTO or not pick:
            return self._default_browser
        if pick.startswith("系统默认："):
            return self._default_browser
        for name, exe in self.browsers:
            if name == pick:
                return (name, exe)
        return self._default_browser

    def _build_web_panel(self, parent) -> None:
        box = ttk.Labelframe(parent, text=WEB_GROUP, style="Group.TLabelframe", padding=(8, 4, 8, 8))
        box.pack(fill="x", expand=False, padx=4, pady=(4, 4))
        for task in [t for t in self.tasks if t.group == WEB_GROUP]:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=1)
            ttk.Checkbutton(row, text=task.title, variable=self.vars[task.key]).pack(anchor="w")
            if task.desc:
                ttk.Label(row, text="      " + task.desc, style="Desc.TLabel",
                          wraplength=350, justify="left").pack(anchor="w")

        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=6)

        brow = ttk.Frame(box)
        brow.pack(fill="x")
        ttk.Label(brow, text="浏览器：").pack(side="left")
        self.cmb_browser = ttk.Combobox(brow, state="readonly", width=22, values=self._browser_labels())
        self.cmb_browser.pack(side="left", fill="x", expand=True)
        self.cmb_browser.set(BROWSER_AUTO)

        self.btn_web = ttk.Button(box, text="▶ 一键打开并直连 GitHub",
                                  command=self._one_click_direct)
        self.btn_web.pack(fill="x", pady=(6, 2))
        ttk.Label(box, text="      勾选上面的页面后点按钮；没勾选则打开首页。",
                  style="Desc.TLabel", wraplength=350, justify="left").pack(anchor="w")

        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=6)
        ttk.Checkbutton(box, text="锁定优选 IP（绕过 DNS/DoH）",
                        variable=self.opt_pin).pack(anchor="w")
        ttk.Checkbutton(box, text="禁用浏览器代理（强制直连）",
                        variable=self.opt_noproxy).pack(anchor="w")
        ttk.Checkbutton(box, text="打开前刷新 DNS 缓存",
                        variable=self.opt_flush).pack(anchor="w")

    # ------------------------------------------------------------ 输出视图
    def _build_output_tab(self) -> None:
        frame = ttk.Frame(self.nb, padding=6)
        self.nb.add(frame, text="  运行输出  ")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="实时输出（stdout + stderr）",
                  style="Desc.TLabel").pack(side="left")
        self.lbl_run_state = ttk.Label(bar, text="空闲", style="Badge.TLabel")
        self.lbl_run_state.pack(side="right")

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        self.txt_out = tk.Text(wrap, wrap="none", bg="#1e1e1e", fg="#d4d4d4",
                               insertbackground="#d4d4d4", font=(MONO_FONT, 10),
                               relief="flat", padx=10, pady=8, state="disabled")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_out.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.txt_out.xview)
        self.txt_out.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.txt_out.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        hsb.pack(fill="x")
        for tag, color, bold in (("info", "#d4d4d4", False), ("ok", "#6ac36a", False),
                                 ("warn", "#e0b040", False), ("err", "#f06a6a", True),
                                 ("title", "#4fc3f7", True), ("step", "#9aa0a6", False),
                                 ("cmd", "#c586c0", False), ("head", "#ffffff", True)):
            self.txt_out.tag_configure(tag, foreground=color,
                                       font=(MONO_FONT, 10, "bold") if bold else (MONO_FONT, 10))

    # ------------------------------------------------------------ 日志视图
    def _build_log_tab(self) -> None:
        frame = ttk.Frame(self.nb, padding=6)
        self.nb.add(frame, text="  日志文件  ")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 2))
        ttk.Label(bar, text="日志文件：").pack(side="left")
        self.cmb_log = ttk.Combobox(bar, state="readonly", width=34, font=(MONO_FONT, 10))
        self.cmb_log.pack(side="left")
        self.cmb_log.bind("<<ComboboxSelected>>", lambda e: self._load_log(force=True))
        ttk.Button(bar, text="刷新列表", command=self._refresh_logs, width=9).pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="自动刷新(2s)", variable=self.log_follow).pack(side="left")

        bar2 = ttk.Frame(frame)
        bar2.pack(fill="x", pady=(0, 4))
        ttk.Button(bar2, text="打开日志目录", command=lambda: self._open_path(LOG_DIR),
                   width=12).pack(side="left")
        ttk.Button(bar2, text="打开工作目录", command=lambda: self._open_path(HERE),
                   width=12).pack(side="left", padx=6)
        ttk.Label(bar2, text="日志是脚本运行过程中写入的，和「运行输出」内容一致，便于事后排查。",
                  style="Desc.TLabel").pack(side="left", padx=6)

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        self.txt_log = tk.Text(wrap, wrap="word", bg="#fbfbfb", fg="#222222",
                               font=(MONO_FONT, 10), relief="flat", padx=10, pady=8, state="disabled")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=vsb.set)
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.txt_log.tag_configure("errline", foreground="#c0392b")
        self.txt_log.tag_configure("warnline", foreground="#b9770e")
        self.txt_log.tag_configure("okline", foreground="#1e8449")

    # ----------------------------------------------------------- hosts 视图
    def _build_hosts_tab(self) -> None:
        frame = ttk.Frame(self.nb, padding=6)
        self.nb.add(frame, text="  当前 hosts  ")

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Button(bar, text="刷新", command=self._refresh_hosts, width=8).pack(side="left")
        ttk.Button(bar, text="验证解析结果", command=self._verify_resolution,
                   width=14).pack(side="left", padx=6)
        ttk.Button(bar, text="用记事本打开", command=self._open_hosts_notepad,
                   width=14).pack(side="left")
        ttk.Button(bar, text="复制全部条目", command=self._copy_hosts, width=12).pack(side="left", padx=6)
        self.lbl_hosts_info = ttk.Label(frame, text="", style="Desc.TLabel", justify="left")
        self.lbl_hosts_info.pack(fill="x", pady=(0, 4))

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        cols = ("ip", "domain", "resolved")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        self.tree.heading("ip", text="IP")
        self.tree.heading("domain", text="域名")
        self.tree.heading("resolved", text="当前实际解析（点上方按钮验证）")
        self.tree.column("ip", width=175, anchor="w", stretch=False)
        self.tree.column("domain", width=360, anchor="w")
        self.tree.column("resolved", width=250, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.tag_configure("bad", foreground="#c0392b")
        self.tree.tag_configure("good", foreground="#1e8449")

    # ------------------------------------------------------------- 帮助视图
    def _build_help_tab(self) -> None:
        frame = ttk.Frame(self.nb, padding=6)
        self.nb.add(frame, text="  帮助  ")
        txt = tk.Text(frame, wrap="word", font=(UI_FONT, 10), relief="flat",
                      padx=12, pady=10, bg="#fbfbfb")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")

    # ---------------------------------------------------------------- 状态栏
    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 4))
        bar.pack(fill="x", side="bottom")
        self.lbl_status = ttk.Label(bar, text="就绪", style="Desc.TLabel")
        self.lbl_status.pack(side="left")
        self.lbl_state = ttk.Label(bar, text="", style="Desc.TLabel")
        self.lbl_state.pack(side="right")

    # ============================================================== 运行逻辑
    def _set_all(self, value: bool) -> None:
        for task in self.tasks:
            if task.available:
                self.vars[task.key].set(value)

    def _preset_common(self) -> None:
        """常用组合：体检 + 预览 + 更新（管理员）+ 状态。"""
        keep = {"py_check", "py_update_dry", "py_update", "py_status"}
        for task in self.tasks:
            self.vars[task.key].set(task.key in keep and task.available)
        self._append("已勾选常用组合：体检 -> 预览 -> 更新 hosts -> 查看状态", "step")

    def _selected(self) -> List[Task]:
        return [t for t in self.tasks if t.available and self.vars[t.key].get()]

    def _web_opts(self) -> Dict[str, object]:
        """把浏览器直连选项打成快照，交给后台线程用（避免跨线程读 tk 变量）。"""
        _name, exe = self._selected_browser_exe()
        return {"pin": bool(self.opt_pin.get()), "noproxy": bool(self.opt_noproxy.get()),
                "flush": bool(self.opt_flush.get()), "browser": exe}

    def _run_selected(self) -> None:
        if self.running:
            messagebox.showinfo(APP_TITLE, "已有任务在运行，请先停止或等待完成。")
            return
        chosen = self._selected()
        if not chosen:
            messagebox.showinfo(APP_TITLE, "请先在左侧勾选要运行的任务。")
            return
        self._launch_tasks(chosen)

    def _one_click_direct(self) -> None:
        """一键：可选先更新 hosts，然后用浏览器直连打开勾选的页面。"""
        if self.running:
            messagebox.showinfo(APP_TITLE, "已有任务在运行，请先停止或等待完成。")
            return
        sites = [t for t in self.tasks
                 if t.group == WEB_GROUP and t.kind == "web" and t.urls and self.vars[t.key].get()]
        urls = [t.urls[0] for t in sites] or [WEB_HOME]
        pin = bool(self.opt_pin.get())
        entries, gen, swapped = hosts_entries()

        if pin and not entries:
            answer = messagebox.askyesno(
                APP_TITLE,
                "hosts 里还没有加速条目，没有 IP 可以锁定。\n\n"
                "要现在先跑一次「更新 hosts（优选 IP）」吗？\n"
                "（需要管理员权限，会弹 UAC；更新完自动继续打开浏览器）")
            if not answer:
                return
            update = next((t for t in self.tasks if t.key == "py_update"), None)
            if update is None:
                messagebox.showerror(APP_TITLE, "找不到 github520.py 的更新任务。")
                return
            plan = [update] + (sites or [Task("web_open", WEB_GROUP, "打开 GitHub 首页", "",
                                              [], kind="web", urls=urls)])
            self._launch_tasks(plan)
            return

        if entries:
            self._append(f"-> 将使用 hosts 里的 {len(entries)} 条优选 IP"
                         f"（生成于 {gen}）打开 {len(urls)} 个页面", "step")
        self._launch_tasks(sites or [Task("web_open", WEB_GROUP, "打开 GitHub 首页", "",
                                          [], kind="web", urls=urls)])

    def _launch_tasks(self, chosen: Sequence[Task]) -> None:
        chosen = [replace(t, opts=dict(t.opts) or self._web_opts()) if t.kind == "web" else t
                  for t in chosen]

        admin_tasks = [t for t in chosen if t.needs_admin]
        if admin_tasks and not is_admin():
            names = "\n".join(f"  · {t.title}" for t in admin_tasks)
            answer = messagebox.askyesnocancel(
                APP_TITLE,
                "以下任务需要管理员权限：\n\n" + names +
                "\n\n是：以管理员身份重启本程序（推荐，之后所有任务都能跑）\n"
                "否：只运行不需要管理员的任务\n"
                "取消：什么都不做")
            if answer is None:
                return
            if answer:
                self._restart_as_admin()
                return
            chosen = [t for t in chosen if not t.needs_admin]
            if not chosen:
                return

        long_running = [t for t in chosen if t.long_running]
        if long_running and not messagebox.askyesno(
                APP_TITLE, "选了常驻任务（如 watch），它会一直运行到点「停止」。继续吗？"):
            return

        self._start_worker(chosen)

    def _run_custom(self) -> None:
        if self.running:
            messagebox.showinfo(APP_TITLE, "已有任务在运行。")
            return
        text = self.ent_cmd.get().strip()
        if not text:
            return
        cmd = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + text)
        task = Task("custom", "自定义", f"自定义命令：{text}", "", [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd])
        self._start_worker([task])

    def _start_worker(self, tasks: Sequence[Task]) -> None:
        self.running = True
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_run_state.configure(text="运行中…")
        self.nb.select(0)
        self._append("", "info")
        self._append(f"════════ 开始执行 {len(tasks)} 个任务  {datetime.now():%Y-%m-%d %H:%M:%S} ════════", "head")
        threading.Thread(target=self._worker, args=(list(tasks),), daemon=True).start()

    def _worker(self, tasks: List[Task]) -> None:
        summary: List[Tuple[str, int, float]] = []
        for task in tasks:
            self.queue.put(("task_start", task))
            start = time.time()
            code = -1
            if task.kind == "web":
                try:
                    lines, code = self._run_web(task)
                except Exception as exc:      # pragma: no cover
                    lines, code = [f"[错误] 打开浏览器失败：{type(exc).__name__}: {exc}"], 1
                for line in lines:
                    self.queue.put(("line", line))
                elapsed = time.time() - start
                summary.append((task.title, code, elapsed))
                self.queue.put(("task_end", (task, code, elapsed)))
                continue
            try:
                self.proc = subprocess.Popen(
                    task.argv, cwd=str(HERE), env=child_env(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=CREATE_NO_WINDOW)
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    self.queue.put(("line", line.rstrip("\r\n")))
                self.proc.wait()
                code = self.proc.returncode
            except FileNotFoundError as exc:
                self.queue.put(("line", f"[错误] 找不到可执行文件：{exc.filename}"))
            except Exception as exc:  # pragma: no cover
                self.queue.put(("line", f"[错误] 启动失败：{type(exc).__name__}: {exc}"))
            finally:
                self.proc = None
            elapsed = time.time() - start
            summary.append((task.title, code, elapsed))
            self.queue.put(("task_end", (task, code, elapsed)))
        self.queue.put(("all_done", summary))

    def _run_web(self, task: Task) -> Tuple[List[str], int]:
        """浏览器直连任务：在后台线程里执行，只返回文本，不碰任何 tk 控件。"""
        if task.key == "web_doctor":
            return self_check_report(), 0
        exe = str(task.opts.get("browser") or "")
        lines = open_websites(task.urls, exe, bool(task.opts.get("pin")),
                              bool(task.opts.get("noproxy")), bool(task.opts.get("flush")))
        code = 1 if any(ln.startswith("[错误]") for ln in lines) else 0
        return lines, code

    def _stop(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            self._append("没有正在运行的任务。", "step")
            return
        self._append("正在停止（含子进程）…", "warn")
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ============================================================== 消息泵
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    self._append(str(payload), classify(str(payload)))
                elif kind == "task_start":
                    task = payload  # type: Task
                    self._append("", "info")
                    self._append(f"▶ 运行：{task.title}", "head")
                    if task.argv:
                        self._append("$ " + " ".join(task.argv), "cmd")
                    elif task.kind == "web":
                        self._append("$ 浏览器直连：" + ("、".join(task.urls) or "环境自检"), "cmd")
                    self.lbl_run_state.configure(text=f"运行中：{task.title[:16]}")
                elif kind == "task_end":
                    task, code, elapsed = payload  # type: ignore[misc]
                    ok = code == 0
                    self._append(f"◀ 结束：{task.title}  退出码 {code}  耗时 {elapsed:.1f}s",
                                 "ok" if ok else "err")
                elif kind == "all_done":
                    summary = payload  # type: ignore[assignment]
                    failed = [s for s in summary if s[1] != 0]
                    self.running = False
                    self.btn_run.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.lbl_run_state.configure(text="空闲")
                    self._append("════════ 全部完成：%d 个任务，失败 %d 个 ════════"
                                 % (len(summary), len(failed)), "head" if not failed else "err")
                    self._refresh_all()
                    self.lbl_state.configure(
                        text=f"上次运行：{datetime.now():%H:%M:%S}  失败 {len(failed)} 个")
                elif kind == "resolved":
                    self._apply_resolution(payload)  # type: ignore[arg-type]
                elif kind == "task_state":
                    self.lbl_state.configure(text=str(payload))
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _append(self, text: str, tag: str = "info") -> None:
        widget = self.txt_out
        at_bottom = widget.yview()[1] > 0.98
        widget.configure(state="normal")
        widget.insert("end", ANSI_RE.sub("", text) + "\n", tag)
        if int(widget.index("end-1c").split(".")[0]) > 4000:      # 限制行数，避免越来越卡
            widget.delete("1.0", "500.0")
        widget.configure(state="disabled")
        if at_bottom:
            widget.see("end")

    def _clear_output(self) -> None:
        self.txt_out.configure(state="normal")
        self.txt_out.delete("1.0", "end")
        self.txt_out.configure(state="disabled")

    # ============================================================ 刷新各视图
    def _refresh_all(self) -> None:
        self._refresh_admin_badge()
        self._refresh_hosts()
        self._refresh_logs()
        self._refresh_task_state()

    def _refresh_admin_badge(self) -> None:
        if is_admin():
            self.lbl_admin.configure(text="权限：管理员 ✓", foreground="#1e8449")
            self.btn_elevate.state(["disabled"])
        else:
            self.lbl_admin.configure(text="权限：普通用户", foreground="#b9770e")
            self.btn_elevate.state(["!disabled"])
            self.lbl_state.configure(text="写 hosts / 注册计划任务需要管理员权限")

    def _refresh_hosts(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            raw = HOSTS_PATH.read_bytes()
            text, enc = read_hosts_text()
        except OSError as exc:
            self.lbl_hosts_info.configure(text=f"无法读取 hosts：{exc}",
                                          foreground="#c0392b")
            return
        bom = "带 BOM" if raw.startswith(b"\xef\xbb\xbf") else "无 BOM"
        rows, ts, swapped = block_rows(text)
        if not rows and swapped == 0 and MARKER_START not in text:
            self.lbl_hosts_info.configure(
                text=f"{HOSTS_PATH}\n状态：未启用加速条目（没有 {MARKER_START} 标记块）",
                foreground="#666666")
            return
        for ip, domain in rows:
            self.tree.insert("", "end", values=(ip, domain, "—"))
        info = (f"{HOSTS_PATH}\n状态：已启用　条数：{len(rows)}　生成：{ts}　"
                f"编码：{enc}　{bom}　大小：{fmt_size(len(raw))}")
        if swapped:
            info += (f"\n[！] {swapped} 行写成了「域名 IP」顺序（Windows 只认「IP 域名」），"
                     "当前完全没生效 → 请跑一次「③ 更新 hosts」覆写")
            self.lbl_hosts_info.configure(text=info, foreground="#c0392b")
        else:
            self.lbl_hosts_info.configure(text=info, foreground="#666666")

    def _verify_resolution(self) -> None:
        domains = [self.tree.set(i, "domain") for i in self.tree.get_children()]
        if not domains:
            messagebox.showinfo(APP_TITLE, "当前 hosts 没有加速条目可验证。")
            return
        self.lbl_hosts_info.configure(text="正在解析验证（走系统解析器，会读 hosts）…")

        def work() -> None:
            import socket
            out: Dict[str, str] = {}
            for domain in domains:
                try:
                    infos = socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
                    out[domain] = ", ".join(sorted({i[4][0] for i in infos})) or "无结果"
                except Exception as exc:
                    out[domain] = f"解析失败（{type(exc).__name__}）"
            self.queue.put(("resolved", out))

        threading.Thread(target=work, daemon=True).start()

    def _apply_resolution(self, mapping: Dict[str, str]) -> None:
        good = bad = 0
        for item in self.tree.get_children():
            ip = self.tree.set(item, "ip")
            domain = self.tree.set(item, "domain")
            got = mapping.get(domain, "—")
            hit = ip in got
            self.tree.set(item, "resolved", got)
            self.tree.item(item, tags=("good",) if hit else ("bad",))
            good += 1 if hit else 0
            bad += 0 if hit else 1
        self.lbl_hosts_info.configure(
            text=f"{HOSTS_PATH}\n解析验证完成：命中 {good} 条，未命中 {bad} 条"
                 f"（未命中的可能被上游 DNS 覆盖，或浏览器 DoH 绕过 hosts）")

    def _copy_hosts(self) -> None:
        rows = [(self.tree.set(i, "ip"), self.tree.set(i, "domain")) for i in self.tree.get_children()]
        if not rows:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(f"{ip}\t{d}" for ip, d in rows))
        self._append(f"已复制 {len(rows)} 条 hosts 记录到剪贴板。", "step")

    def _open_hosts_notepad(self) -> None:
        try:
            subprocess.Popen(["notepad.exe", str(HOSTS_PATH)], creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"打开失败：{exc}")

    def _open_path(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"打开失败：{exc}")

    def _refresh_logs(self) -> None:
        files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if LOG_DIR.is_dir() else []
        names = [f.name for f in files]
        current = self.cmb_log.get()
        self.cmb_log.configure(values=names)
        if names and current not in names:
            self.cmb_log.set(names[0])
        elif not names:
            self.cmb_log.set("")
        self._load_log()

    def _load_log(self, force: bool = False) -> None:
        name = self.cmb_log.get()
        if not name:
            self._set_log_text("（logs 目录下还没有日志文件：先跑一个任务）")
            return
        path = LOG_DIR / name
        try:
            size = path.stat().st_size
        except OSError:
            return
        if not force and self.log_follow.get() and (name, size) == self._last_log_sig:
            self.after(2000, self._load_log)
            return
        self._last_log_sig = (name, size)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            self._set_log_text(f"读取失败：{exc}")
            return
        tail = lines[-800:]
        self._set_log_text("\n".join(tail), colorize=True,
                           prefix=f"{path}　共 {len(lines)} 行，显示末尾 {len(tail)} 行\n" + "─" * 90)
        if self.log_follow.get():
            self.after(2000, self._load_log)

    def _set_log_text(self, text: str, colorize: bool = False, prefix: str = "") -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        if prefix:
            self.txt_log.insert("end", prefix + "\n")
        for line in text.splitlines():
            tag = ""
            if colorize:
                if "[ERROR]" in line or "[错误]" in line:
                    tag = "errline"
                elif "[WARN]" in line or "[警告]" in line:
                    tag = "warnline"
                elif "[OK]" in line:
                    tag = "okline"
            self.txt_log.insert("end", line + "\n", tag)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _refresh_task_state(self) -> None:
        # 注意：tkinter 只能在主线程里操作，后台线程一律通过队列把结果交回 _pump
        def work() -> None:
            try:
                proc = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                                      capture_output=True, text=True, errors="replace",
                                      timeout=15, creationflags=CREATE_NO_WINDOW)
                text = f"计划任务：{'已注册' if proc.returncode == 0 else '未注册'}"
            except Exception:
                text = "计划任务：查询失败"
            self.queue.put(("task_state", text))

        threading.Thread(target=work, daemon=True).start()

    # ============================================================== 提权重启
    def _restart_as_admin(self) -> None:
        if is_admin():
            messagebox.showinfo(APP_TITLE, "当前已经是管理员权限。")
            return
        script = str(Path(__file__).resolve())
        params = f'"{script}"'
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", python_exe(), params, str(HERE), 1)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"提权失败：{exc}")
            return
        if rc > 32:
            self.destroy()
        else:
            messagebox.showerror(APP_TITLE, f"提权被取消或失败（返回码 {rc}）。")

    def _on_close(self) -> None:
        if self.running and not messagebox.askyesno(
                APP_TITLE, "还有任务在运行，确定要退出吗？（会一起终止子进程）"):
            return
        if self.running:
            self._stop()
        self.destroy()


def classify(line: str) -> str:
    if "=====" in line or "════" in line:
        return "title" if "=====" in line else "head"
    if line.startswith("$ ") or line.startswith(">"):
        return "cmd"
    if "[OK]" in line:
        return "ok"
    if "[警告]" in line or "[WARN]" in line:
        return "warn"
    if "[错误]" in line or "[ERROR]" in line or "Traceback" in line or "FAIL" in line:
        return "err"
    if line.strip().startswith("->") or "[DryRun]" in line:
        return "step"
    if line.startswith("▶") or line.startswith("◀"):
        return "head"
    return "info"


HELP_TEXT = """\
GitHub 加速控制台 —— 使用说明

一、这个界面在做什么
   它本身不修改系统，只是把你已经有的两个脚本包成可勾选的任务，并把它们的输出实时显示出来：
     · github520.py       Python 版：浏览器连通体检 + DoH/TLS/HTTP 三层优选 IP + 自动更新 hosts
     · github-accel.ps1   PowerShell 版：诊断 + hosts 更新 + git 代理 + 镜像重写

二、推荐流程
   1. 勾选「① 体检：浏览器到 GitHub 连接状态」→ 运行，先看清问题是 DNS、TLS 还是代理。
   2. 勾选「② 预览更新（DryRun）」→ 运行，看看会写入哪些 IP、延迟多少、是否通过 HTTP 校验。
   3. 勾选「③ 更新 hosts（优选 IP）」→ 运行。没有管理员权限会弹窗问你是否提权重启。
   4. 勾选「⑤ 注册计划任务（每小时）」→ 之后就不用管了，系统会定期自动更新。
   5. 「当前 hosts」标签页里点「验证解析结果」，确认每条都真的按 hosts 解析成功。
   6. 回到「浏览器直连」面板，勾上要看的页面，点「▶ 一键打开并直连 GitHub」实际访问一次
      （细节见第七章；如果体检显示 DNS 没按 hosts 返回，这一步尤其有用）。

三、权限说明
   · 写系统 hosts、注册计划任务必须管理员。界面右上角会显示当前权限。
   · 点「以管理员身份重启」会弹 UAC；提权后所有任务都能跑，输出照常显示。
   · 不想提权也可以：在弹窗里选「否」，只跑不需要管理员的任务（体检、预览、状态等）。

四、四个标签页
   · 运行输出：本次执行的全部输出，按 [OK]/[警告]/[错误] 着色，带命令、退出码与耗时。
   · 日志文件：直接浏览 logs\\accel-*.log（PS 版）和 logs\\github520-*.log（Python 版），
              默认每 2 秒自动刷新，方便一边跑一边看。
   · 当前 hosts：解析 hosts 里的加速标记块，逐条列出 IP / 域名 / 实际解析结果。

五、出问题时
   · 输出里出现 Traceback 或「[错误]」，把那一屏内容发出来即可。
   · 想彻底恢复：先跑「④ 从备份还原 hosts」，再跑「⑤ 删除计划任务」，最后手动清掉
     git 代理与镜像重写（PS 版「一键还原（全部）」）。
   · 备份都在 backup\\ 目录，hosts.时间戳.bak，直接用记事本覆盖回 hosts 也行。

六、自定义命令
   工具栏第二行可以输入任意 PowerShell 命令，回车即执行，输出同样显示在「运行输出」里。
   例如：py -3 github520.py check --json

七、一键直连 GitHub（浏览器直连面板）
   为什么需要它：hosts 只改「系统解析结果」。下面三种情况会让 hosts 白改：
     · 浏览器开了「安全 DNS / DoH」——它自己查 DNS，根本不读 hosts；
     · DNS 客户端服务没重新读取 hosts（改完 hosts 立刻打开浏览器就可能这样）；
     · 上游 DNS 继续投毒（gist、raw 这类域名最常见）。
   所以「一键打开并直连 GitHub」会这样做（都只影响本次打开的浏览器实例，不动注册表）：
     · --host-resolver-rules：把 hosts 里的优选 IP 直接写进浏览器进程，彻底绕过 DNS/DoH；
     · --no-proxy-server：忽略系统代理/PAC，强制走直连线路；
     · 打开前可选刷新一次 DNS 缓存（ipconfig /flushdns）。
   用法：在「浏览器直连」面板里勾选要看的页面（首页 / 趋势 / 搜索 / Gist / raw 文件），
   需要的话改一下下面三个开关和浏览器，然后点「▶ 一键打开并直连 GitHub」。
   一个页面都没勾就打开 GitHub 首页；勾了「⓪ 直连自检」则只输出一份环境报告不打开浏览器。

   注意：
     · 想让参数生效，最好先完全退出目标浏览器（已运行的实例会接管新标签页，参数会被忽略，
       程序检测到这种情况会在输出里给出黄色警告）；
     · Firefox 不支持命令行锁定 IP，只能靠 hosts，需要自己去关 DoH；
     · 本机 DNS 若不按 hosts 返回，可运行「系统工具 → 重启 DNS 客户端服务（需管理员）」
       或重启系统后再试，让 hosts 全局生效。
"""


def main() -> int:
    enable_dpi_awareness()
    try:
        app = App()
    except tk.TclError as exc:
        print(f"无法创建窗口：{exc}")
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
