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
    双击 github_gui.pyw           （推荐：pythonw 打开，全程不出现终端窗口）
    python github_gui.py          带终端窗口（报错看得见）
    python github_gui.py --flat   强制平面模式（不开任何玻璃效果）
    python github_gui.py --glass  整面亚克力玻璃（注意：留白处点击会穿透，见下）
"""

from __future__ import annotations

import codecs
import ctypes
import http.client
import json
import math
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont, messagebox, ttk
from types import SimpleNamespace
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

# --------------------------------------------------------------------------
# 设计系统（颜色 / 字体 / 间距）—— 界面里只允许出现这一份参数
# --------------------------------------------------------------------------
# · 颜色：主色只有靛蓝一个，其余靠灰度分层；绿/黄/红只用于「成功/警告/错误」语义。
# · 字体：正文微软雅黑 UI，等宽内容（终端/日志/hosts 表格）用 Consolas，全界面两种。
# · 玻璃质感：默认是「磨砂实底 + Win11 亚克力标题栏（Mica）+ 系统级圆角窗口」，
#   白色卡片浮在磨砂底上。加 --glass 可把亚克力铺满整个窗口（留白透出模糊桌面），
#   但 Windows 分层窗口的色键区域点击会穿透到下层窗口——这是系统固有行为，
#   无法规避，所以默认不开启整面玻璃。

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"

F_SECTION = (UI_FONT, 11, "bold")      # 卡片小节标题
F_BODY = (UI_FONT, 10)                 # 正文
F_CAPTION = (UI_FONT, 9)               # 辅助说明
F_MINI = (UI_FONT, 8, "bold")          # 行内小标签（需管理员）
F_CHIP = (UI_FONT, 9, "bold")          # 徽标
F_MONO = (MONO_FONT, 10)               # 等宽正文

UI = SimpleNamespace(
    chroma="#030509",        # 玻璃模式的色键：涂成此色的区域透出系统亚克力模糊
    bg="#e8ecf3",            # 平面模式背景（卡片间隙）
    surface="#ffffff",       # 卡片 / 面板
    surface_alt="#f3f5f9",   # 内嵌面：表头、输入条、未选中标签页
    hover="#eef2f8",         # 悬停
    border="#e3e7ee",        # 细边框
    text="#1e2637",
    text2="#5d6b83",
    text3="#9aa5b8",
    accent="#3e63dd",
    accent_hover="#3157cb",
    accent_down="#2848b0",
    accent_soft="#e9edfc",
    ok="#25875a",
    ok_soft="#e2f2e9",
    warn="#b3761a",
    warn_soft="#f9efdb",
    err="#c84e4e",
    err_soft="#f9e8e8",
    term_bg="#12161f",       # 运行输出保持深色终端：等宽日志在深底上更易读
    term_fg="#c8d3e2",
)

# 运行输出（深色终端）的行着色：tag -> (颜色, 是否加粗)
TERM_TAGS = {
    "info": (UI.term_fg, False),
    "ok": ("#6ecb88", False),
    "warn": ("#e3b25e", False),
    "err": ("#f29090", True),
    "title": ("#7ab3f0", True),
    "step": ("#8a95a8", False),
    "cmd": ("#c79bd0", False),
    "head": ("#f5f8ff", True),
}

FORCE_FLAT: Optional[bool] = None       # 截图脚本可置 True 强制平面模式（玻璃会透出桌面，公开截图禁用）


def _win11() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return int(sys.getwindowsversion().build) >= 22000
    except Exception:
        return False


def _transparency_on() -> bool:
    """系统「透明效果」开关——关着的话亚克力只会渲染成一坨纯色，不如不用。"""
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "EnableTransparency")
            return bool(value)
    except OSError:
        return True


def _flat_requested() -> bool:
    return FORCE_FLAT is True or bool(os.environ.get("GITHUB_GUI_FLAT"))


def _glass_requested() -> bool:
    """整面玻璃是可选项：色键区域的点击会穿透到下层窗口，所以默认不开。"""
    return "--glass" in sys.argv or bool(os.environ.get("GITHUB_GUI_GLASS"))


def _apply_window_effects(root, glass: bool) -> bool:
    """顶层窗口圆角 + Win11 标题栏玻璃（Mica）；glass=True 再把玻璃铺满客户区。

    整面玻璃需要色键透明才能透出来，而色键区域的鼠标点击会穿透到下层窗口
    （Windows 分层窗口的固有行为，无法规避），所以整面玻璃只在 --glass 时开启；
    默认模式客户区完全不透明，随便点都不会穿，玻璃只出现在标题栏。
    返回是否处于整面玻璃模式。--flat 时什么都不做，保持系统默认外观。
    """
    if not IS_WINDOWS:
        return False
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if _flat_requested() or not _win11():
            return False
        pref = ctypes.c_int(2)                    # DWMWCP_ROUND：Win11 圆角窗口
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        if not _transparency_on():
            return False
        backdrop = ctypes.c_int(3 if glass else 2)   # 亚克力整面 / Mica 只影响标题栏
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), 4) != 0:
            return False

        class _Margins(ctypes.Structure):
            _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                        ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

        if glass:
            margins = _Margins(-1, -1, -1, -1)    # 经典技巧：玻璃铺满整个客户区
            if ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) != 0:
                return False
            root.attributes("-transparentcolor", UI.chroma)
            return True
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            hwnd, ctypes.byref(_Margins(0, 0, 0, 0)))   # 玻璃只出现在标题栏
        return False
    except Exception:
        return False


def _probe_glass() -> bool:
    """整面玻璃需要先探明能用，才让主窗口进入玻璃模式（涂色键底色）。

    探测窗口必须真正映射过（alpha=0 也算映射）：withdraw 状态下 DWM 包装窗口
    还不存在，GetParent 拿不到正确句柄，所有 DWM 调用都会失败。
    """
    if not _glass_requested() or _flat_requested() or not _win11():
        return False
    try:
        probe = tk.Toplevel()
        probe.attributes("-alpha", 0)
        probe.geometry("1x1+0+0")
        probe.update_idletasks()
        probe.update()
        ok = _apply_window_effects(probe, glass=True)
        probe.destroy()
        return ok
    except Exception:
        return False


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


# 分组名（_build_task_list / GROUP_HINTS 共用，改名时两处一起动）
GROUP_PY = "Python 版 · github520.py"
GROUP_PS = "PowerShell 版 · github-accel.ps1"
GROUP_SYS = "系统工具"


def build_tasks() -> List[Task]:
    py = python_exe()
    pyw = [py, "-u", str(PY_SCRIPT)]
    g_py = GROUP_PY
    g_ps = GROUP_PS
    g_sys = GROUP_SYS

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

        # 持续监控：打开 GitHub 后不结束，保持探测关键域名，异常即报警（点「停止」结束）
        Task("web_monitor", WEB_GROUP, "⑥ 持续监控连接（每 30 秒）",
             "保持监控直到点「停止」：异常报警，持续爆红自动修复（弹 UAC 更新 hosts）",
             [], kind="monitor", opts={"interval": 30}),

        Task("py_check", g_py, "① 体检：浏览器到 GitHub 连接状态",
             "浏览器/代理/DoH/hosts/DNS/连通性（不需要管理员）", [*pyw, "check"],
             available=PY_SCRIPT.is_file()),
        Task("py_check_json", g_py, "① 体检（JSON 结果）",
             "同上，附机器可读 JSON", [*pyw, "check", "--json"], available=PY_SCRIPT.is_file()),
        Task("py_update_dry", g_py, "② 预览更新（DryRun）",
             "只拉取+测速并显示将写入的 IP，不改系统", [*pyw, "update", "--dry-run"],
             available=PY_SCRIPT.is_file()),
        Task("py_update", g_py, "③ 更新 hosts（推荐，自动测速）",
             "拉取 + DoH + TLS/HTTP 三层优选后写入 hosts", [*pyw, "update"],
             needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_update_fast", g_py, "③ 更新 hosts（快速，不测速）",
             "直接用 GitHub520 源里的 IP", [*pyw, "update", "--no-optimize"],
             needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_watch_once", g_py, "③ 检查并按需更新（等同计划任务）",
             "先体检，只在链路有问题时才更新", [*pyw, "watch", "--once"],
             needs_admin=True, available=PY_SCRIPT.is_file()),
        Task("py_watch", g_py, "③ 常驻监控（每 30 分钟）",
             "持续运行，直到点「停止」（需管理员）", [*pyw, "watch", "--interval", "1800"],
             needs_admin=True, long_running=True, available=PY_SCRIPT.is_file()),
        Task("py_status", g_py, "④ 查看状态",
             "加速条目 / 备份 / 计划任务 / 数据源可达性", [*pyw, "status"],
             available=PY_SCRIPT.is_file()),
        Task("py_verify", g_py, "④ 快速验证 hosts 生效",
             "几秒完成：解析对比 + github.com 探测（CLI 里是 verify 命令）", [*pyw, "verify"],
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

# 各分组卡片标题下的灰色说明：向新手解释「Python / PowerShell 两组功能等价、二选一」，
# 免得 34 个任务平铺开让人不知道从哪组下手。
GROUP_HINTS = {
    GROUP_PY: "推荐先用这组：自动测速挑最快 IP，失败自动回退到数据源 IP",
    GROUP_PS: "与 Python 组功能等价，二选一即可；没装 Python、或要用 git 代理 / 镜像下载时用这组",
    GROUP_SYS: "零碎的系统小工具：看 hosts 原文、刷 DNS 缓存、测延迟",
    WEB_GROUP: "对付 DNS 污染 / 浏览器 DoH 的终极手段：把优选 IP 直接锁进浏览器进程",
}


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


def probe_tls(domain: str, ip: Optional[str], timeout: float = 4.0) -> Tuple[bool, float, str]:
    """TLS + HTTP 双层探测某个域名是否真的可用（监控用）。

    只做 TLS 握手会被"API 前端"骗过——证书同属 *.github.com，握手能过，
    但所有网页请求一律 400 Bad Request。所以这里握手之后再发一个真实 GET：
    github.com（浏览器域名）要求状态码 < 400，其余域名 < 500 即可。
    ip 给定就直连该 IP（等价浏览器锁定优选 IP 后的真实链路）；ip 为空走系统解析。
    返回 (是否成功, 耗时 ms, 成功时="IP HTTP 状态码" / 失败时=错误摘要)。
    """
    t0 = time.perf_counter()
    try:
        target = ip or socket.gethostbyname(domain)
        path, strict = MONITOR_PROBE_PATHS.get(domain, ("/", False))
        conn = _SniHTTPConnection(target, domain, timeout=timeout,
                                  context=ssl.create_default_context())
        try:
            conn.request("GET", path, headers={"Host": domain,
                                               "User-Agent": f"github_gui/{APP_VERSION}"})
            resp = conn.getresponse()
            resp.read(256)
            status = resp.status
        finally:
            conn.close()
        ms = (time.perf_counter() - t0) * 1000
        if status < (400 if strict else 500):
            return True, ms, f"{target} HTTP {status}"
        return False, ms, f"HTTP {status}（该前端不路由网页流量）"
    except Exception as exc:
        return False, (time.perf_counter() - t0) * 1000, f"{type(exc).__name__}: {exc}"[:90]


# 监控探测的请求路径与判定标准：(路径, 是否要求 <400)
MONITOR_PROBE_PATHS = {
    "github.com": ("/robots.txt", True),
    "api.github.com": ("/", False),
    "raw.githubusercontent.com": ("/521xueweihan/GitHub520/main/hosts", False),
}


class _SniHTTPConnection(http.client.HTTPConnection):
    """按指定 IP 连接、SNI 与 Host 都用域名（监控探测用，同 github520.py 的验证方式）。"""

    def __init__(self, ip: str, domain: str, timeout: float,
                 context: ssl.SSLContext) -> None:
        super().__init__(ip, timeout=timeout)
        self._context = context
        self._domain = domain

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._domain)


# 监控自动修复：连续多少轮失败后触发 / 两次尝试之间的最小间隔（秒）。
# 3 轮 × 30 秒 ≈ 1.5 分钟的持续异常才动手，避免对瞬时抖动小题大做。
REPAIR_AFTER_ROUNDS = 3
REPAIR_COOLDOWN = 900


def self_check_report() -> List[str]:
    """浏览器直连自检：不开浏览器，只报告现状与可行性。"""
    lines: List[str] = []
    dname, dexe = default_browser()
    browsers = installed_browsers()
    lines.append(f"[OK] 默认浏览器：{dname or '未识别'}")
    if dexe:
        lines.append(f"-> 路径：{dexe}")
    # 只说数量，不打印清单：输出/截图不会泄露你装了哪些软件（清单在左侧下拉框里）
    if browsers:
        lines.append(f"-> 已检测到 {len(browsers)} 个可用浏览器（清单见左侧「浏览器」下拉框）")
    else:
        lines.append("-> 未检测到可用浏览器，请手动指定浏览器路径")
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
# 自绘组件：胶囊按钮 / 徽标 / 圆角卡片（tkinter 原生控件没有圆角，用 Canvas 画）
# --------------------------------------------------------------------------

def _bg_of(widget) -> str:
    try:
        return str(widget.cget("bg"))
    except Exception:
        return UI.surface


def _rounded_points(x0: float, y0: float, x1: float, y1: float, r: float,
                    steps: int = 7) -> List[float]:
    """圆角矩形的平滑多边形顶点（顺时针，供 create_polygon(smooth=True) 用）。"""
    pts: List[float] = []

    def arc(cx: float, cy: float, a0: float, a1: float) -> None:
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))

    arc(x1 - r, y0 + r, -90, 0)      # 右上
    arc(x1 - r, y1 - r, 0, 90)       # 右下
    arc(x0 + r, y1 - r, 90, 180)     # 左下
    arc(x0 + r, y0 + r, 180, 270)    # 左上
    return pts


def _mix(c1: str, c2: str, t: float) -> str:
    """两个 #rrggbb 颜色线性插值（动效用）。"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


class Pill(tk.Canvas):
    """胶囊按钮。

    kind：accent（实心主按钮）/ soft（浅色底强调）/ danger（浅红，用于「停止」）/
          ghost（描边幽灵）/ quiet（无边框灰字）。
    悬停变色、按下文字下沉 1px，反馈克制；禁用整体降灰。
    configure(state=… / text=…) 与旧代码的 ttk.Button 调用兼容。
    """

    _KINDS = {
        #           normal            hover        down         文字色      边框
        "accent": (UI.accent,       UI.accent_hover, UI.accent_down, "#ffffff", None),
        "soft":   (UI.accent_soft,  "#dbe3fa",       "#cdd9f8",      UI.accent, None),
        "danger": (UI.err_soft,     "#f3d9d9",       "#eecaca",      UI.err,    None),
        "ghost":  (UI.surface,      UI.hover,        "#e3e8f1",      UI.text2,  UI.border),
        "quiet":  (UI.surface,      UI.hover,        "#e3e8f1",      UI.text2,  None),
    }

    def __init__(self, master, text: str, command=None, kind: str = "quiet",
                 size: str = "md", outer: Optional[str] = None, **kw):
        self._kind = kind if kind in self._KINDS else "quiet"
        self._command = command
        self._text = text
        self._state = "normal"
        self._font = F_BODY if size == "md" else F_CAPTION
        self._h = 32 if size == "md" else 26
        self._padx = 16 if size == "md" else 11
        self._outer = outer or _bg_of(master)
        self._hover = False
        self._pressed = False
        tk.Canvas.__init__(self, master, width=self._width(), height=self._h,
                           bg=self._outer, highlightthickness=0, bd=0, **kw)
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _width(self) -> int:
        return tkfont.Font(font=self._font).measure(self._text) + self._padx * 2

    def _draw(self) -> None:
        base, hover, down, fg, border = self._KINDS[self._kind]
        if self._state == "disabled":
            base = _mix(base, self._outer, 0.72)
            fg = _mix(fg, UI.text3, 0.75)
            if border:
                border = _mix(border, self._outer, 0.6)
        fill = base
        if self._state == "normal":
            fill = down if self._pressed else (hover if self._hover else base)
        w, h = self._width(), self._h
        tk.Canvas.configure(self, width=w, height=h)
        self.delete("all")
        pts = _rounded_points(1, 1, w - 1, h - 1, h // 2)
        self.create_polygon(pts, fill=fill, outline=border or fill, smooth=True, width=1)
        ty = h // 2 + (1 if self._pressed and self._state == "normal" else 0)
        self.create_text(w // 2, ty, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _e=None) -> None:
        self._hover = True
        if self._state == "normal":
            self._draw()

    def _on_leave(self, _e=None) -> None:
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _e=None) -> None:
        if self._state != "normal":
            return
        self._pressed = True
        self._draw()

    def _on_release(self, e) -> None:
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        was_pressed = self._pressed
        self._pressed = False
        self._draw()
        if inside and was_pressed and self._state == "normal" and self._command:
            self._command()

    def configure(self, **kw):  # type: ignore[override]
        if "state" in kw:
            self._state = kw.pop("state")
        if "text" in kw:
            self._text = str(kw.pop("text"))
        if kw:
            tk.Canvas.configure(self, **kw)
        self._draw()


class Chip(tk.Canvas):
    """只读小徽标（权限 / 计数 / 运行状态）。tone: neutral | accent | ok | warn | err。"""

    _TONES = {
        "neutral": (UI.surface_alt, UI.text2),
        "accent":  (UI.accent_soft, UI.accent),
        "ok":      (UI.ok_soft, UI.ok),
        "warn":    (UI.warn_soft, UI.warn),
        "err":     (UI.err_soft, UI.err),
    }

    def __init__(self, master, text: str = "", tone: str = "neutral",
                 outer: Optional[str] = None, **kw):
        self._outer = outer or _bg_of(master)
        self._tone = tone
        self._text = text
        tk.Canvas.__init__(self, master, height=22, bg=self._outer,
                           highlightthickness=0, bd=0, **kw)
        self._draw()

    def _draw(self) -> None:
        bg, fg = self._TONES.get(self._tone, self._TONES["neutral"])
        w = tkfont.Font(font=F_CHIP).measure(self._text) + 20 if self._text else 2
        tk.Canvas.configure(self, width=w)
        self.delete("all")
        if not self._text:
            return
        pts = _rounded_points(1, 1, w - 1, 21, 10)
        self.create_polygon(pts, fill=bg, outline=bg, smooth=True, width=1)
        self.create_text(w // 2, 11, text=self._text, fill=fg, font=F_CHIP)

    def set(self, text: Optional[str] = None, tone: Optional[str] = None) -> None:
        if text is not None:
            self._text = text
        if tone is not None:
            self._tone = tone
        self._draw()

    def configure(self, **kw):  # type: ignore[override]
        if "text" in kw:
            self._text = str(kw.pop("text"))
        if kw:
            tk.Canvas.configure(self, **kw)
        self._draw()


class PulseDot(tk.Canvas):
    """状态圆点：空闲是灰点；运行中沿「浅↔深」缓慢呼吸（主色），不抢注意力。"""

    _STEPS = 12
    _MS = 120

    def __init__(self, master, size: int = 10, outer: Optional[str] = None, **kw):
        self._size = size
        self._outer = outer or _bg_of(master)
        self._running = False
        self._phase = 0
        self._job: Optional[str] = None
        tk.Canvas.__init__(self, master, width=size, height=size, bg=self._outer,
                           highlightthickness=0, bd=0, **kw)
        self._paint(UI.text3)

    def _paint(self, color: str) -> None:
        self.delete("all")
        self.create_oval(2, 2, self._size - 2, self._size - 2, fill=color, outline="")

    def set_running(self, running: bool) -> None:
        self._running = running
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if not running:
            self._paint(UI.text3)
            return
        self._phase = 0
        self._tick()

    def _tick(self) -> None:
        if not self._running:
            return
        t = (1 - math.cos(2 * math.pi * self._phase / self._STEPS)) / 2
        self._paint(_mix(UI.accent_soft, UI.accent, t))
        self._phase = (self._phase + 1) % self._STEPS
        self._job = self.after(self._MS, self._tick)


class ToolTip:
    """轻量悬停提示：指针停留 ~600ms 后，在控件下方弹出说明小框（不用第三方库）。"""

    def __init__(self, widget, text: str, delay: int = 600) -> None:
        self._widget = widget
        self._text = text
        self._delay = delay
        self._job: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._job = self._widget.after(self._delay, self._show)

    def _cancel(self) -> None:
        if self._job:
            try:
                self._widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        tip = tk.Toplevel(self._widget)
        tip.wm_overrideredirect(True)
        tk.Label(tip, text=self._text, justify="left", font=F_CAPTION,
                 bg=UI.surface, fg=UI.text, padx=9, pady=5,
                 highlightthickness=1, highlightbackground=UI.border).pack()
        tip.update_idletasks()
        x = (self._widget.winfo_rootx() + self._widget.winfo_width() // 2
             - tip.winfo_reqwidth() // 2)
        x = max(4, min(x, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 4))
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip


class RoundedCard(tk.Canvas):
    """圆角卡片：内容放进 .body（与卡片同底色）。

    四角留出 radius 空隙——玻璃模式下正好透出亚克力背景，卡片像浮在玻璃上；
    body 距卡片边缘至少 pad 像素，保证内容永远不会压到圆角上。
    """

    def __init__(self, master, radius: int = 12, pad: int = 14, bg: Optional[str] = None,
                 border: str = UI.border, outer: Optional[str] = None, **kw):
        self._radius = radius
        self._pad = pad
        self._bg = bg or UI.surface
        self._border = border
        self._outer = outer or _bg_of(master)
        tk.Canvas.__init__(self, master, bg=self._outer, highlightthickness=0, bd=0, **kw)
        self.body = tk.Frame(self, bg=self._bg)
        self._win = self.create_window((pad, pad), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._size_to_body)
        self.bind("<Configure>", lambda e: self._paint())
        self._paint()

    def _size_to_body(self, e) -> None:
        self.configure(height=e.height + self._pad * 2)
        self.coords(self._win, self._pad, self._pad)

    def _paint(self) -> None:
        w = max(int(self.winfo_width()), 2)
        h = max(int(self.winfo_height()), 2)
        r = min(self._radius, w // 2, h // 2)
        self.delete("card")
        pts = _rounded_points(1, 1, w - 1, h - 1, r)
        self.create_polygon(pts, fill=self._bg, outline=self._border, smooth=True,
                            width=1, tags="card")
        self.tag_lower("card")
        self.itemconfigure(self._win, width=max(w - self._pad * 2, 1))


# --------------------------------------------------------------------------
# 可滚动区域
# --------------------------------------------------------------------------

class ScrollFrame(tk.Frame):
    """纵向滚动容器：玻璃/背景色画布 + 细滚动条。用法不变（.inner 里放内容）。"""

    def __init__(self, master, width: int = 330, bg: Optional[str] = None, **kw):
        self._bg = bg or UI.bg
        super().__init__(master, bg=self._bg, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                width=width, bg=self._bg, yscrollincrement=1)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style="Glass.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=self._bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))


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
        self.vars["web_monitor"].set(True)      # 一键直连打开 GitHub 后默认持续监控
        self.queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.proc: Optional[subprocess.Popen] = None
        self.running = False
        self.log_follow = tk.BooleanVar(value=True)
        self._last_log_sig: Tuple[str, int] = ("", -1)
        # 浏览器直连选项
        self.opt_pin = tk.BooleanVar(value=True)        # 锁定 hosts 优选 IP
        self.opt_noproxy = tk.BooleanVar(value=True)    # 禁用浏览器代理
        self.opt_flush = tk.BooleanVar(value=True)      # 打开前刷新 DNS 缓存
        self.opt_autorepair = tk.BooleanVar(value=True)  # 监控爆红时自动修复
        self._monitor_stop: Optional[threading.Event] = None
        self.browsers: List[Tuple[str, str]] = installed_browsers()
        self._default_browser: Tuple[str, str] = default_browser()
        self.cmb_browser: Optional[ttk.Combobox] = None
        self._row_seq = 0

        # 在搭界面之前先探明玻璃模式能不能用（决定底色用色键还是浅灰）
        self._glass = _probe_glass()
        self._root_bg = UI.chroma if self._glass else UI.bg
        self._build_style()
        self._build_toolbar()
        # 顺序很重要：先占好底部状态栏，再让主体填充剩余空间，否则状态栏会被挤没
        self._build_statusbar()
        self._build_body()
        self.update_idletasks()
        _apply_window_effects(self, glass=self._glass)   # 圆角窗口 + 标题栏玻璃 / 整面玻璃
        self._welcome()

    def _welcome(self) -> None:
        """首屏引导：让第一次打开的人不用翻帮助页也知道先点哪里。"""
        self._append("───  第一次用？按这个顺序三步走（详细说明在「帮助」标签页）  ───", "title")
        self._append("① 点工具栏「常用组合」，再点「▶ 运行选中」：体检 + 更新 hosts 一次完成"
                     "（需要管理员时会弹窗询问）", "info")
        self._append("② 完成后点「▶ 一键打开并直连 GitHub」实际打开一次，验证真的能访问了"
                     "（打开后默认持续监控，爆红自动修复）", "info")
        self._append("③ 想确认 hosts 是否生效：运行「④ 快速验证 hosts 生效」；"
                     "出问题先看「帮助」标签页的排错指引", "info")
        self._append("", "info")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<MouseWheel>", self._on_wheel_all)
        self.after(80, self._pump)
        self.after(200, self._refresh_all)

    # ---------------------------------------------------------------- 样式
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")      # clam 才能完全自定义配色
        except tk.TclError:
            pass
        root_bg = self._root_bg
        self.configure(bg=root_bg)

        style.configure(".", background=root_bg, foreground=UI.text, font=F_BODY,
                        borderwidth=0)

        # 输入框：白底细边，聚焦时边框变主色（唯一的聚焦反馈，不画光圈）
        style.configure("TEntry", fieldbackground="#ffffff", foreground=UI.text,
                        bordercolor=UI.border, lightcolor=UI.border, darkcolor=UI.border,
                        insertcolor=UI.text, padding=(10, 7))
        style.map("TEntry", bordercolor=[("focus", UI.accent)],
                  lightcolor=[("focus", UI.accent)], darkcolor=[("focus", UI.accent)])

        # 下拉框
        style.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff",
                        foreground=UI.text, arrowcolor=UI.text2, bordercolor=UI.border,
                        lightcolor=UI.border, darkcolor=UI.border, padding=(10, 6))
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")],
                  background=[("readonly", "#ffffff")],
                  bordercolor=[("focus", UI.accent)])
        self.option_add("*TCombobox*Listbox.background", "#ffffff")
        self.option_add("*TCombobox*Listbox.foreground", UI.text)
        self.option_add("*TCombobox*Listbox.selectBackground", UI.accent_soft)
        self.option_add("*TCombobox*Listbox.selectForeground", UI.accent)
        self.option_add("*TCombobox*Listbox.font", F_BODY)
        self.option_add("*TCombobox*Listbox.relief", "flat")
        self.option_add("*TCombobox*Listbox.borderwidth", 1)

        # 复选框（勾选后指示块变主色，其余状态保持安静）
        style.configure("Opt.TCheckbutton", background=UI.surface, foreground=UI.text2,
                        focuscolor=UI.surface, padding=(0, 2), indicatormargin=(0, 0, 6, 0),
                        indicatorbackground="#ffffff", indicatorforeground="#ffffff")
        style.map("Opt.TCheckbutton", background=[("active", UI.surface)],
                  indicatorbackground=[("selected", UI.accent), ("active", "#f0f3f9")])

        # 滚动条：细、低对比，尽量不被人注意到
        for name, trough in (("Glass.Vertical.TScrollbar", root_bg),
                             ("Card.Vertical.TScrollbar", UI.surface),
                             ("Term.Vertical.TScrollbar", UI.term_bg),
                             ("Term.Horizontal.TScrollbar", UI.term_bg)):
            style.configure(name, background="#c3cdda", troughcolor=trough,
                            bordercolor=trough, arrowcolor=UI.text3,
                            relief="flat", gripcount=0, arrowsize=12)
            style.map(name, background=[("active", "#aeb9c9")])

        # 标签页：未选中是浅灰小片，选中变白并与页面连成一体
        style.configure("TNotebook", background=root_bg, borderwidth=0, tabmargins=(2, 6, 2, 0))
        style.configure("TNotebook.Tab", padding=(18, 8), borderwidth=0, font=F_BODY,
                        background=UI.surface_alt, foreground=UI.text2,
                        lightcolor=UI.surface_alt, darkcolor=UI.surface_alt,
                        bordercolor=UI.surface_alt)
        style.map("TNotebook.Tab",
                  background=[("selected", UI.surface)],
                  foreground=[("selected", UI.accent)],
                  lightcolor=[("selected", UI.surface)],
                  darkcolor=[("selected", UI.surface)])

        # hosts 表格
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground=UI.text, rowheight=30, borderwidth=0, font=F_MONO)
        style.configure("Treeview.Heading", background=UI.surface_alt, foreground=UI.text2,
                        relief="flat", font=(UI_FONT, 9, "bold"), padding=(10, 7),
                        borderwidth=0)
        style.map("Treeview.Heading", background=[("active", UI.surface_alt)])
        style.map("Treeview", background=[("selected", UI.accent_soft)],
                  foreground=[("selected", UI.text)])

    # -------------------------------------------------------------- 工具栏
    def _build_toolbar(self) -> None:
        card = tk.Frame(self, bg=UI.surface, highlightthickness=1,
                        highlightbackground=UI.border, highlightcolor=UI.border)
        card.pack(fill="x", padx=16, pady=(14, 8))

        row1 = tk.Frame(card, bg=UI.surface)
        row1.pack(fill="x", padx=14, pady=(12, 10))
        self.btn_run = Pill(row1, "▶  运行选中", command=self._run_selected,
                            kind="accent", outer=UI.surface)
        self.btn_run.pack(side="left")
        self.btn_stop = Pill(row1, "■  停止", command=self._stop, kind="danger",
                             outer=UI.surface)
        self.btn_stop.configure(state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self._vline(row1).pack(side="left", fill="y", padx=12, pady=4)
        for text, cmd in (("全选", lambda: self._set_all(True)),
                          ("全不选", lambda: self._set_all(False)),
                          ("常用组合", self._preset_common),
                          ("清空输出", self._clear_output)):
            pill = Pill(row1, text, command=cmd, kind="quiet", size="sm",
                        outer=UI.surface)
            pill.pack(side="left", padx=(0, 6))
            if text == "常用组合":
                ToolTip(pill, "自动勾选：体检 → 预览更新 → 更新 hosts → 查看状态"
                              "（更新需要管理员，会弹窗询问）")

        ToolTip(self.btn_run, "按勾选顺序执行左侧选中的任务")
        ToolTip(self.btn_stop, "终止正在运行的命令及其子进程")

        self.chip_admin = Chip(row1, outer=UI.surface)
        self.chip_admin.pack(side="right")
        self.btn_elevate = Pill(row1, "提权重启", command=self._restart_as_admin,
                                kind="quiet", size="sm", outer=UI.surface)
        self.btn_elevate.pack(side="right", padx=(0, 8))
        ToolTip(self.btn_elevate, "写系统 hosts / 注册计划任务需要管理员权限；"
                                  "点击会弹 UAC，确认后以管理员身份重启本程序")

        self._hline(card, pady=0)
        row2 = tk.Frame(card, bg=UI.surface)
        row2.pack(fill="x", padx=14, pady=(10, 12))
        tk.Label(row2, text="自定义命令", font=F_CAPTION, fg=UI.text3,
                 bg=UI.surface).pack(side="left")
        self.ent_cmd = ttk.Entry(row2, font=F_MONO)
        self.ent_cmd.pack(side="left", fill="x", expand=True, padx=10)
        self.ent_cmd.insert(0, "py -3 github520.py status")
        self.ent_cmd.bind("<Return>", lambda e: self._run_custom())
        Pill(row2, "运行", command=self._run_custom, kind="soft", size="sm",
             outer=UI.surface).pack(side="left")
        tk.Label(row2, text="在 PowerShell 中执行，输出显示在「运行输出」",
                 font=F_CAPTION, fg=UI.text3, bg=UI.surface).pack(side="left", padx=(10, 0))

    # ---------------------------------------------------------------- 主体
    def _build_body(self) -> None:
        paned = tk.PanedWindow(self, orient="horizontal", bg=self._root_bg, bd=0,
                               sashwidth=12, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=16, pady=(0, 2))

        left = tk.Frame(paned, bg=self._root_bg)
        paned.add(left, width=400, minsize=360, stretch="never")
        self.scroll = ScrollFrame(left, width=396, bg=self._root_bg)
        self.scroll.pack(fill="both", expand=True)
        self._build_web_panel(self.scroll.inner)
        self._build_task_list(self.scroll.inner)

        right = tk.Frame(paned, bg=self._root_bg)
        paned.add(right, minsize=480, stretch="always")
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)

        self._build_output_tab()
        self._build_log_tab()
        self._build_hosts_tab()
        self._build_help_tab()

    # ---------------------------------------------------------- 左栏公共块
    def _card(self, parent, title: str, count=None, hint: str = "") -> tk.Frame:
        """一张圆角卡片：小节标题 + 数量徽标 +（可选）一行灰色说明，返回内容容器。"""
        card = RoundedCard(parent, outer=self._root_bg)
        card.pack(fill="x", padx=8, pady=(0, 10))
        head = tk.Frame(card.body, bg=UI.surface)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text=title, font=F_SECTION, fg=UI.text,
                 bg=UI.surface).pack(side="left")
        if count is not None:
            Chip(head, f"{count} 项", tone="neutral", outer=UI.surface).pack(
                side="left", padx=(8, 0))
        if hint:
            tk.Label(card.body, text=hint, font=F_CAPTION, fg=UI.text3, bg=UI.surface,
                     wraplength=340, justify="left", anchor="w").pack(fill="x", pady=(0, 8))
        return card.body

    def _hline(self, parent, pady: int = 6) -> None:
        tk.Frame(parent, height=1, bg=UI.border).pack(fill="x", pady=pady)

    def _vline(self, parent) -> tk.Frame:
        return tk.Frame(parent, width=1, bg=UI.border)

    def _task_row(self, parent, task: Task) -> None:
        """一条任务：勾选框 + 标题（需管理员的在行尾挂小标签）+ 灰色说明 + 悬停高亮。"""
        self._row_seq += 1
        row_style = f"Row{self._row_seq}.TCheckbutton"
        style = ttk.Style(self)
        style.configure(row_style, background=UI.surface, foreground=UI.text,
                        focuscolor=UI.surface, indicatorbackground="#ffffff",
                        indicatorforeground="#ffffff", indicatormargin=(0, 0, 8, 0),
                        padding=(0, 2))
        style.map(row_style, background=[("disabled", UI.surface), ("active", UI.hover)],
                  indicatorbackground=[("selected", UI.accent), ("active", "#f0f3f9")],
                  foreground=[("disabled", UI.text3)])

        row = tk.Frame(parent, bg=UI.surface)
        row.pack(fill="x")
        top = tk.Frame(row, bg=UI.surface)
        top.pack(fill="x")
        cb = ttk.Checkbutton(top, text=task.title, variable=self.vars[task.key],
                             style=row_style)
        cb.pack(side="left")
        if not task.available:
            cb.state(["disabled"])
            self.vars[task.key].set(False)
        desc: Optional[tk.Label] = None
        if task.desc:
            desc = tk.Label(row, text=task.desc, font=F_CAPTION, fg=UI.text3,
                            bg=UI.surface, wraplength=330, justify="left")
            desc.pack(anchor="w", padx=(24, 0), pady=(0, 5))
        if not task.available:
            tk.Label(top, text=" 脚本缺失 ", font=F_MINI, fg=UI.err, bg=UI.err_soft,
                     padx=4, pady=1).pack(side="right", pady=(2, 0))
        if task.needs_admin:
            tk.Label(top, text=" 管理员 ", font=F_MINI, fg=UI.warn, bg=UI.warn_soft,
                     padx=4, pady=1).pack(side="right", pady=(2, 0))

        hoverable = (row, top, cb) + ((desc,) if desc else ())

        def _hover(_e=None) -> None:
            if not task.available:
                return
            for w in (row, top):
                w.configure(bg=UI.hover)
            if desc is not None:
                desc.configure(bg=UI.hover)
            style.configure(row_style, background=UI.hover)

        def _unhover(_e=None) -> None:
            for w in (row, top):
                w.configure(bg=UI.surface)
            if desc is not None:
                desc.configure(bg=UI.surface)
            style.configure(row_style, background=UI.surface)

        for w in hoverable:
            w.bind("<Enter>", _hover)
            w.bind("<Leave>", _unhover)

    def _build_task_list(self, parent) -> None:
        groups: Dict[str, List[Task]] = {}
        for task in self.tasks:
            if task.group == WEB_GROUP:      # 由 _build_web_panel 单独渲染
                continue
            groups.setdefault(task.group, []).append(task)

        for group, items in groups.items():
            body = self._card(parent, group, count=len(items),
                              hint=GROUP_HINTS.get(group, ""))
            for task in items:
                self._task_row(body, task)

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
        web_tasks = [t for t in self.tasks if t.group == WEB_GROUP]
        body = self._card(parent, "浏览器直连", count=len(web_tasks),
                          hint=GROUP_HINTS[WEB_GROUP])
        for task in web_tasks:
            self._task_row(body, task)
        self._hline(body)

        brow = tk.Frame(body, bg=UI.surface)
        brow.pack(fill="x", pady=(2, 8))
        tk.Label(brow, text="浏览器", font=F_CAPTION, fg=UI.text3,
                 bg=UI.surface).pack(side="left", padx=(0, 10))
        self.cmb_browser = ttk.Combobox(brow, state="readonly", font=F_BODY,
                                        values=self._browser_labels())
        self.cmb_browser.pack(side="left", fill="x", expand=True)
        self.cmb_browser.set(BROWSER_AUTO)

        for text, var in (("锁定优选 IP（绕过 DNS/DoH）", self.opt_pin),
                          ("禁用浏览器代理（强制直连）", self.opt_noproxy),
                          ("打开前刷新 DNS 缓存", self.opt_flush)):
            ttk.Checkbutton(body, text=text, variable=var,
                            style="Opt.TCheckbutton").pack(anchor="w", pady=1)
        ttk.Checkbutton(body, text="监控爆红时自动修复（弹 UAC 更新 hosts）",
                        variable=self.opt_autorepair,
                        style="Opt.TCheckbutton").pack(anchor="w", pady=(5, 1))

        self.btn_web = Pill(body, "▶  一键打开并直连 GitHub", command=self._one_click_direct,
                            kind="accent", outer=UI.surface)
        self.btn_web.pack(fill="x", pady=(10, 0))
        ToolTip(self.btn_web, "用 hosts 里的优选 IP 直接锁进浏览器进程打开勾选的页面，"
                              "绕过 DNS 污染与浏览器 DoH（只对本次启动的浏览器生效）")
        tk.Label(body, text="勾选上面的页面后点按钮；一个都没勾就打开首页。",
                 font=F_CAPTION, fg=UI.text3, bg=UI.surface).pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------- 标签页 / 滚动区公共块
    def _tab(self, title: str) -> tk.Frame:
        """新建一个 Notebook 页（白色面板 + 统一内边距），返回内容容器。"""
        frame = tk.Frame(self.nb, bg=UI.surface)
        self.nb.add(frame, text=title)
        inner = tk.Frame(frame, bg=UI.surface)
        inner.pack(fill="both", expand=True, padx=14, pady=12)
        return inner

    def _section_head(self, parent, title: str, subtitle: str = "") -> tk.Frame:
        """小节标题行：左侧标题（可带灰字副标题），返回行容器供右侧挂控件。"""
        bar = tk.Frame(parent, bg=UI.surface)
        bar.pack(fill="x", pady=(0, 8))
        tk.Label(bar, text=title, font=F_SECTION, fg=UI.text,
                 bg=UI.surface).pack(side="left")
        if subtitle:
            tk.Label(bar, text=subtitle, font=F_CAPTION, fg=UI.text3,
                     bg=UI.surface).pack(side="left", padx=(8, 0))
        return bar

    def _grid_scroll(self, parent, widget, vsb_style: str) -> None:
        """「内容 + 细滚动条」的标准网格布局（自适应伸缩）。"""
        vsb = ttk.Scrollbar(parent, orient="vertical", command=widget.yview,
                            style=vsb_style)
        widget.configure(yscrollcommand=vsb.set)
        widget.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

    # ------------------------------------------------------------ 输出视图
    def _build_output_tab(self) -> None:
        inner = self._tab("运行输出")
        bar = self._section_head(inner, "实时输出", "stdout + stderr")
        self.lbl_run_state = Chip(bar, text="空闲", tone="neutral", outer=UI.surface)
        self.lbl_run_state.pack(side="right")

        wrap = tk.Frame(inner, bg=UI.surface)
        wrap.pack(fill="both", expand=True)
        self.txt_out = tk.Text(wrap, wrap="none", bg=UI.term_bg, fg=UI.term_fg,
                               insertbackground=UI.term_fg, font=F_MONO,
                               relief="flat", padx=14, pady=10, state="disabled", bd=0,
                               highlightthickness=1, highlightbackground=UI.border,
                               highlightcolor=UI.border)
        hsb = ttk.Scrollbar(inner, orient="horizontal", command=self.txt_out.xview,
                            style="Term.Horizontal.TScrollbar")
        self.txt_out.configure(xscrollcommand=hsb.set)
        self._grid_scroll(wrap, self.txt_out, "Term.Vertical.TScrollbar")
        hsb.pack(fill="x")
        for tag, (color, bold) in TERM_TAGS.items():
            self.txt_out.tag_configure(tag, foreground=color,
                                       font=(MONO_FONT, 10, "bold") if bold else F_MONO)

    # ------------------------------------------------------------ 日志视图
    def _build_log_tab(self) -> None:
        inner = self._tab("日志文件")
        bar = self._section_head(inner, "日志文件")
        self.cmb_log = ttk.Combobox(bar, state="readonly", width=34, font=F_MONO)
        self.cmb_log.pack(side="left", padx=(12, 8))
        self.cmb_log.bind("<<ComboboxSelected>>", lambda e: self._load_log(force=True))
        Pill(bar, "刷新列表", command=self._refresh_logs, kind="quiet", size="sm",
             outer=UI.surface).pack(side="left")
        ttk.Checkbutton(bar, text="自动刷新（2 秒）", variable=self.log_follow,
                        style="Opt.TCheckbutton").pack(side="left", padx=(12, 0))

        bar2 = tk.Frame(inner, bg=UI.surface)
        bar2.pack(fill="x", pady=(0, 8))
        Pill(bar2, "打开日志目录", command=lambda: self._open_path(LOG_DIR),
             kind="quiet", size="sm", outer=UI.surface).pack(side="left")
        Pill(bar2, "打开工作目录", command=lambda: self._open_path(HERE),
             kind="quiet", size="sm", outer=UI.surface).pack(side="left", padx=(6, 0))
        tk.Label(bar2, text="日志和「运行输出」内容一致，便于事后排查。",
                 font=F_CAPTION, fg=UI.text3, bg=UI.surface).pack(side="left", padx=(10, 0))

        wrap = tk.Frame(inner, bg=UI.surface)
        wrap.pack(fill="both", expand=True)
        self.txt_log = tk.Text(wrap, wrap="word", bg=UI.surface_alt, fg=UI.text,
                               font=F_MONO, relief="flat", padx=14, pady=10,
                               state="disabled", bd=0, highlightthickness=1,
                               highlightbackground=UI.border, highlightcolor=UI.border)
        self._grid_scroll(wrap, self.txt_log, "Card.Vertical.TScrollbar")
        self.txt_log.tag_configure("errline", foreground=UI.err)
        self.txt_log.tag_configure("warnline", foreground=UI.warn)
        self.txt_log.tag_configure("okline", foreground=UI.ok)

    # ----------------------------------------------------------- hosts 视图
    def _build_hosts_tab(self) -> None:
        inner = self._tab("当前 hosts")
        bar = self._section_head(inner, "当前 hosts")
        actions = tk.Frame(bar, bg=UI.surface)
        actions.pack(side="right")
        for text, cmd, gap in (("刷新", self._refresh_hosts, 0),
                               ("验证解析结果", self._verify_resolution, 6),
                               ("用记事本打开", self._open_hosts_notepad, 6),
                               ("复制全部条目", self._copy_hosts, 6)):
            Pill(actions, text, command=cmd, kind="quiet", size="sm",
                 outer=UI.surface).pack(side="left", padx=(gap, 0))

        self.lbl_hosts_info = tk.Label(inner, text="", font=F_CAPTION, fg=UI.text3,
                                       bg=UI.surface, justify="left", anchor="w")
        self.lbl_hosts_info.pack(fill="x", pady=(0, 8))

        wrap = tk.Frame(inner, bg=UI.surface)
        wrap.pack(fill="both", expand=True)
        cols = ("ip", "domain", "resolved")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=16)
        self.tree.heading("ip", text="IP")
        self.tree.heading("domain", text="域名")
        self.tree.heading("resolved", text="实际解析（点「验证解析结果」后显示）")
        self.tree.column("ip", width=175, anchor="w", stretch=False)
        self.tree.column("domain", width=360, anchor="w")
        self.tree.column("resolved", width=250, anchor="w")
        self._grid_scroll(wrap, self.tree, "Card.Vertical.TScrollbar")
        self.tree.tag_configure("bad", foreground=UI.err)
        self.tree.tag_configure("good", foreground=UI.ok)

    # ------------------------------------------------------------- 帮助视图
    def _build_help_tab(self) -> None:
        frame = tk.Frame(self.nb, bg=UI.surface)
        self.nb.add(frame, text="帮助")
        txt = tk.Text(frame, wrap="word", font=F_BODY, relief="flat",
                      padx=20, pady=16, bg=UI.surface, fg=UI.text2,
                      bd=0, highlightthickness=0)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")

    # ---------------------------------------------------------------- 状态栏
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=UI.surface, highlightthickness=1,
                       highlightbackground=UI.border, highlightcolor=UI.border)
        bar.pack(fill="x", side="bottom", padx=16, pady=(0, 12))
        inner = tk.Frame(bar, bg=UI.surface)
        inner.pack(fill="x", padx=14, pady=7)
        self.dot = PulseDot(inner, outer=UI.surface)
        self.dot.pack(side="left")
        self.lbl_status = tk.Label(inner, text="就绪", font=F_BODY, fg=UI.text2,
                                   bg=UI.surface)
        self.lbl_status.pack(side="left", padx=(8, 0))
        self.lbl_state = tk.Label(inner, text="", font=F_CAPTION, fg=UI.text3,
                                  bg=UI.surface)
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

        # 监控任务勾着（默认勾选）就追加到计划末尾：打开页面后不结束，持续监控连接
        monitor = next((t for t in self.tasks
                        if t.key == "web_monitor" and t.available and self.vars[t.key].get()),
                       None)

        if pin and not entries:
            answer = messagebox.askyesno(
                APP_TITLE,
                "hosts 里还没有加速条目，没有 IP 可以锁定。\n\n"
                "要现在先跑一次「③ 更新 hosts（推荐，自动测速）」吗？\n"
                "（需要管理员权限，会弹 UAC；更新完自动继续打开浏览器）")
            if not answer:
                return
            update = next((t for t in self.tasks if t.key == "py_update"), None)
            if update is None:
                messagebox.showerror(APP_TITLE, "找不到 github520.py 的更新任务。")
                return
            plan = [update] + (sites or [Task("web_open", WEB_GROUP, "打开 GitHub 首页", "",
                                              [], kind="web", urls=urls)])
            if monitor is not None:
                plan.append(monitor)
            self._launch_tasks(plan)
            return

        if entries:
            self._append(f"-> 将使用 hosts 里的 {len(entries)} 条优选 IP"
                         f"（生成于 {gen}）打开 {len(urls)} 个页面", "step")
        plan = sites or [Task("web_open", WEB_GROUP, "打开 GitHub 首页", "",
                              [], kind="web", urls=urls)]
        if monitor is not None:
            plan.append(monitor)
        self._launch_tasks(plan)

    def _monitor_opts(self, task: Task) -> Dict[str, object]:
        """把监控相关选项打成快照交给后台线程（避免跨线程读 tk 变量）。"""
        return {"interval": int(task.opts.get("interval", 30)),
                "auto_repair": bool(self.opt_autorepair.get())}

    def _launch_tasks(self, chosen: Sequence[Task]) -> None:
        chosen = [
            replace(t, opts=dict(t.opts) or self._web_opts()) if t.kind == "web" else
            replace(t, opts=self._monitor_opts(t)) if t.kind == "monitor" else t
            for t in chosen
        ]

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
        self.lbl_run_state.set(text="运行中…", tone="accent")
        self.dot.set_running(True)
        self.nb.select(0)
        self._append("", "info")
        self._append(f"════════ 开始执行 {len(tasks)} 个任务  {datetime.now():%Y-%m-%d %H:%M:%S} ════════", "head")
        threading.Thread(target=self._worker, args=(list(tasks),), daemon=True).start()

    def _worker(self, tasks: List[Task]) -> None:
        summary: List[Tuple[str, int, float]] = []
        for task in tasks:
            self.queue.put(("task_start", task))
            start = time.time()
            if task.kind == "web":
                try:
                    lines, code = self._run_web(task)
                except Exception as exc:      # pragma: no cover
                    lines, code = [f"[错误] 打开浏览器失败：{type(exc).__name__}: {exc}"], 1
                for line in lines:
                    self.queue.put(("line", line))
            elif task.kind == "monitor":
                code = self._run_monitor_task(task)
            else:
                code = self._run_cmd_task(task)
            elapsed = time.time() - start
            summary.append((task.title, code, elapsed))
            self.queue.put(("task_end", (task, code, elapsed)))
        self.queue.put(("all_done", summary))

    def _run_cmd_task(self, task: Task) -> int:
        """命令行任务：起子进程并逐行转发输出，返回退出码。"""
        code = -1
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
        return code

    def _run_monitor_task(self, task: Task) -> int:
        """监控任务：包装停止事件的生命周期，返回退出码（被停止视为 0）。"""
        stop_event = threading.Event()
        self._monitor_stop = stop_event
        code = 0
        try:
            self._run_monitor(task, stop_event)
        except Exception as exc:      # pragma: no cover
            self.queue.put(("line", f"[错误] 监控线程异常：{type(exc).__name__}: {exc}"))
            code = 1
        finally:
            self._monitor_stop = None
        return code

    def _run_web(self, task: Task) -> Tuple[List[str], int]:
        """浏览器直连任务：在后台线程里执行，只返回文本，不碰任何 tk 控件。"""
        if task.key == "web_doctor":
            return self_check_report(), 0
        exe = str(task.opts.get("browser") or "")
        lines = open_websites(task.urls, exe, bool(task.opts.get("pin")),
                              bool(task.opts.get("noproxy")), bool(task.opts.get("flush")))
        code = 1 if any(ln.startswith("[错误]") for ln in lines) else 0
        return lines, code

    def _run_monitor(self, task: Task, stop_event: threading.Event) -> None:
        """持续监控：每 interval 秒对关键域名做 TLS 探测（在后台线程里跑）。

        探测路径与浏览器直连一致——hosts 里有锁定 IP 就直连该 IP，等价于浏览器
        打开页面后真正走的链路；没有条目则走系统解析。正常报延迟，异常报警；
        持续爆红（REPAIR_AFTER_ROUNDS 轮）且开关打开时调用 _auto_repair 修复，
        修复后继续探测验证效果，恢复时报告中断时长。
        """
        interval = max(int(task.opts.get("interval", 30)), 5)
        auto_repair = bool(task.opts.get("auto_repair", True))
        domains = ("github.com", "api.github.com", "raw.githubusercontent.com")
        self.queue.put(("line", f"-> 监控已开始：每 {interval} 秒对 {'、'.join(domains)} "
                                f"做 TLS 探测；持续爆红自动尝试修复={('开' if auto_repair else '关')}"
                                "（点「停止」结束）"))
        fail_streak = 0
        last_repair = 0.0
        incident_start: Optional[float] = None
        incident_notified = False
        round_no = 0
        while not stop_event.is_set():
            round_no += 1
            entries, _gen, _swapped = hosts_entries()
            table = {d: ip for ip, d in entries}
            results: Dict[str, Tuple[bool, float, str]] = {}
            with ThreadPoolExecutor(max_workers=len(domains)) as pool:
                futs = {d: pool.submit(probe_tls, d, table.get(d)) for d in domains}
                for d, fut in futs.items():
                    results[d] = fut.result()
            stamp = f"{datetime.now():%H:%M:%S}"
            if all(r[0] for r in results.values()):
                fail_streak = 0
                if incident_start is not None:
                    self.queue.put(("line", f"[OK] {stamp} 连接已恢复"
                                            f"（异常持续约 {time.time() - incident_start:.0f} 秒）"))
                    incident_start = None
                incident_notified = False
                parts = " · ".join(f"{d.split('.')[0]} {results[d][1]:.0f}ms" for d in domains)
                self.queue.put(("line", f"[OK] 第 {round_no} 轮 {stamp}  {parts} —— 连接正常"))
                self.queue.put(("monitor_status", ("监控中：连接正常", "ok")))
            else:
                fail_streak += 1
                if incident_start is None:
                    incident_start = time.time()
                detail = "、".join(
                    f"{d}（{table.get(d) or '系统解析'}，{results[d][2]}）"
                    for d in domains if not results[d][0])
                self.queue.put(("line", f"[错误] 第 {round_no} 轮 {stamp}  探测失败：{detail}"
                                        f" —— 连接异常（连续 {fail_streak} 次）"))
                short = "、".join(d.split(".")[0] for d in domains if not results[d][0])
                self.queue.put(("monitor_status", (f"监控中：异常（{short}）", "err")))
                if fail_streak >= REPAIR_AFTER_ROUNDS and not incident_notified:
                    incident_notified = True
                    if auto_repair and PY_SCRIPT.is_file():
                        if time.time() - last_repair >= REPAIR_COOLDOWN:
                            last_repair = time.time()
                            self._auto_repair(stop_event)
                            incident_notified = False   # 允许冷却期后再次尝试
                        else:
                            self.queue.put(("line", "[警告] 连接持续异常：距上次自动修复"
                                                    f"不足 {REPAIR_COOLDOWN // 60} 分钟，稍后再试；"
                                                    "也可手动运行「③ 更新 hosts」"))
                    else:
                        self.queue.put(("line", "[警告] 连接持续异常：建议点「停止」后运行"
                                                "「③ 更新 hosts（推荐，自动测速）」换一批 IP，"
                                                "或改用 PowerShell 版的代理方案"))
            # 等下一轮：0.5 秒步进，随时响应「停止」
            for _ in range(interval * 2):
                if stop_event.is_set():
                    break
                time.sleep(0.5)
        self.queue.put(("line", "-> 监控已停止。"))

    def _auto_repair(self, stop_event: threading.Event) -> None:
        """监控爆红后的自动修复：更新 hosts 换一批可用 IP（结果由监控继续探测验证）。"""
        self.queue.put(("monitor_status", ("自动修复中…", "accent")))
        self.queue.put(("line", "-> [自动修复] 尝试更新 hosts 换一批可用 IP…"))
        if not PY_SCRIPT.is_file():
            self.queue.put(("line", "[错误] [自动修复] 找不到 github520.py，无法自动修复。"))
            return
        if is_admin():
            # 已有管理员权限：直接跑 update，完整输出实时进「运行输出」
            self.queue.put(("line", "-> [自动修复] 当前已是管理员权限，直接执行更新："))
            try:
                proc = subprocess.Popen(
                    [python_exe(), "-u", str(PY_SCRIPT), "update", "--quiet"],
                    cwd=str(HERE), env=child_env(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=CREATE_NO_WINDOW)
            except Exception as exc:
                self.queue.put(("line", f"[错误] [自动修复] 启动更新失败：{exc}"))
                return
            self.proc = proc
            rc = -1
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if stop_event.is_set():
                        proc.kill()
                        self.queue.put(("line", "[警告] [自动修复] 已被「停止」终止"))
                        return
                    self.queue.put(("line", "      " + line.rstrip()))
                proc.wait()
                rc = proc.returncode
            finally:
                self.proc = None
            if rc == 0:
                self.queue.put(("line", "[OK] [自动修复] hosts 已更新，继续监控验证效果…"))
            else:
                self.queue.put(("line", f"[错误] [自动修复] 更新退出码 {rc}，继续监控观察；"
                                        "可手动运行「③ 更新 hosts」查看详细原因"))
            return

        # 非管理员：提权（弹 UAC）在新窗口里跑更新，完成后写标记文件，这里等它
        marker = LOG_DIR / f"repair-{int(time.time())}.done"
        cmd = (f'"{python_exe()}" -u "{PY_SCRIPT}" update & echo done > "{marker}"')
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe",
                                                     f"/c {cmd}", str(HERE), 1)
        except Exception as exc:
            self.queue.put(("line", f"[错误] [自动修复] 提权失败：{exc}"))
            return
        if rc <= 32:
            self.queue.put(("line", "[警告] [自动修复] UAC 被取消，放弃本次修复（继续监控报警）"))
            return
        self.queue.put(("line", "-> [自动修复] 已弹出 UAC：请点「是」，更新会在新的管理员窗口进行"
                                "（进度在那个窗口看），这里最长等 10 分钟"))
        deadline = time.time() + 600
        waited = 0
        while time.time() < deadline:
            if stop_event.is_set():
                self.queue.put(("line", "[警告] [自动修复] 已被「停止」终止"
                                        "（若提权窗口还在跑，可手动关掉它）"))
                return
            if marker.is_file():
                break
            time.sleep(1)
            waited += 1
            if waited % 60 == 0:
                self.queue.put(("line", f"-> [自动修复] 提权更新仍在进行…（已等待 {waited} 秒）"))
        else:
            self.queue.put(("line", "[警告] [自动修复] 等待超时（10 分钟）：更新可能还在进行，"
                                    "继续监控观察即可"))
            return
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        self.queue.put(("line", "[OK] [自动修复] 提权更新已结束，继续监控验证效果…"))

    def _stop(self) -> None:
        if self._monitor_stop is not None and not self._monitor_stop.is_set():
            self._monitor_stop.set()
            self._append("正在停止监控…（当前这轮探测结束后立即退出）", "warn")
            return
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
    def _on_wheel_all(self, event) -> None:
        """全局滚轮路由：指针落在左侧任务面板上就滚动面板。

        面板里的内容是卡片、按钮、勾选框等一层层子控件，老写法（进入面板才绑定、
        离开就解绑）在指针悬停到子控件上时 Enter/Leave 链会断，滚轮就失灵了。
        这里改成按指针的屏幕坐标是否落在面板矩形内来判定，跟悬停在哪个控件上无关。
        右侧终端 / 日志 / 表格不经过这里，滚轮走控件自带的原生滚动。
        滚动量：画布的 yscrollincrement 被设成 1（一格 = 一像素），一个滚轮刻度
        （±120）≈ 两行任务，手感正常；tk Canvas 不支持按像素滚的 "pixels" 写法。
        """
        canvas = self.scroll.canvas
        if not canvas.winfo_ismapped():
            return
        x0, y0 = canvas.winfo_rootx(), canvas.winfo_rooty()
        if not (x0 <= event.x_root < x0 + canvas.winfo_width()
                and y0 <= event.y_root < y0 + canvas.winfo_height()):
            return
        canvas.yview_scroll(-event.delta, "units")

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
                    self.lbl_run_state.set(text="空闲", tone="neutral")
                    self.dot.set_running(False)
                    self._append("════════ 全部完成：%d 个任务，失败 %d 个 ════════"
                                 % (len(summary), len(failed)), "head" if not failed else "err")
                    self._refresh_all()
                    self.lbl_state.configure(
                        text=f"上次运行：{datetime.now():%H:%M:%S}  失败 {len(failed)} 个")
                elif kind == "monitor_status":
                    text, tone = payload  # type: ignore[misc]
                    self.lbl_run_state.set(text=str(text), tone=str(tone))
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
            self.chip_admin.set(text="管理员 ✓", tone="ok")
            self.btn_elevate.configure(state="disabled")
        else:
            self.chip_admin.set(text="普通权限", tone="warn")
            self.btn_elevate.configure(state="normal")
            self.lbl_state.configure(text="写 hosts / 注册计划任务需要管理员权限")

    def _refresh_hosts(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            raw = HOSTS_PATH.read_bytes()
            text, enc = read_hosts_text()
        except OSError as exc:
            self.lbl_hosts_info.configure(text=f"无法读取 hosts：{exc}",
                                          foreground=UI.err)
            return
        bom = "带 BOM" if raw.startswith(b"\xef\xbb\xbf") else "无 BOM"
        rows, ts, swapped = block_rows(text)
        if not rows and swapped == 0 and MARKER_START not in text:
            self.lbl_hosts_info.configure(
                text=f"{HOSTS_PATH}\n状态：未启用加速条目（没有 {MARKER_START} 标记块）",
                foreground=UI.text3)
            return
        for ip, domain in rows:
            self.tree.insert("", "end", values=(ip, domain, "—"))
        info = (f"{HOSTS_PATH}\n状态：已启用　条数：{len(rows)}　生成：{ts}　"
                f"编码：{enc}　{bom}　大小：{fmt_size(len(raw))}")
        if swapped:
            info += (f"\n[！] {swapped} 行写成了「域名 IP」顺序（Windows 只认「IP 域名」），"
                     "当前完全没生效 → 请跑一次「③ 更新 hosts」覆写")
            self.lbl_hosts_info.configure(text=info, foreground=UI.err)
        else:
            self.lbl_hosts_info.configure(text=info, foreground=UI.text3)

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
   · 输出里出现 Traceback 或「[错误]」：把那一屏内容 + 「日志文件」页里最新一个日志，
     发到项目仓库的 Issues（github.com/xinghanzhuyue/github-/issues）。
   · 想确认 hosts 是否生效：运行「④ 快速验证 hosts 生效」，几秒出结论，不用等完整体检。
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

   打开后默认进入「⑥ 持续监控连接」：每 30 秒对 github.com / api.github.com /
   raw.githubusercontent.com 做 TLS 探测——hosts 里有锁定 IP 就直接探测该 IP，
   和浏览器打开页面走的是同一条链路。正常时输出各域名延迟；异常立即报警，持续爆红
   （约 1.5 分钟）自动尝试修复：已是管理员就直接在输出区跑更新，否则弹 UAC 在新窗口
   更新（15 分钟内不重复弹），修复结果由监控继续探测验证，恢复时报告中断时长。
   不需要时取消勾选「⑥」，或随时点「停止」结束监控；自动修复可在面板里关掉。

   注意：
     · 想让参数生效，最好先完全退出目标浏览器（已运行的实例会接管新标签页，参数会被忽略，
       程序检测到这种情况会在输出里给出黄色警告）；
     · Firefox 不支持命令行锁定 IP，只能靠 hosts，需要自己去关 DoH；
     · 本机 DNS 若不按 hosts 返回，可运行「系统工具 → 重启 DNS 客户端服务（需管理员）」
       或重启系统后再试，让 hosts 全局生效。
"""


def _relaunch_windowless_if_needed() -> None:
    """双击 .py 文件时，系统会专门为 python.exe 配一个终端窗口。

    检测到「这个终端是专为本次启动创建的」（控制台上只有本进程）就自动改用
    pythonw.exe 重启自己，终端随本进程退出而消失。从 cmd / PowerShell / VSCode
    终端里手动运行时不触发（控制台是共用的，报错看得见）；pythonw 下本来就
    没有终端，也不会触发。设环境变量 GITHUB_GUI_NO_RELAUNCH=1 可禁用。
    """
    if not IS_WINDOWS or os.environ.get("GITHUB_GUI_NO_RELAUNCH"):
        return
    exe = Path(sys.executable)
    if exe.name.lower() != "python.exe":
        return                                   # 已经是 pythonw 等无终端宿主
    try:
        if not (sys.stdout and sys.stdout.isatty()):
            return                               # 输出被重定向（脚本/CI），保持原样
        buf = (ctypes.c_uint * 8)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        if n > 1:
            return                               # 控制台是共用的（cmd/终端），别动
        pythonw = exe.with_name("pythonw.exe")
        if not pythonw.is_file():
            return
        argv = [str(pythonw), str(Path(__file__).resolve()), *sys.argv[1:]]
        subprocess.Popen(argv, cwd=str(HERE), close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        sys.exit(0)                              # 终端随本进程退出而关闭
    except Exception:
        return                                   # 任何意外都按老样子继续跑


def main() -> int:
    _relaunch_windowless_if_needed()
    enable_dpi_awareness()
    if "--flat" in sys.argv or os.environ.get("GITHUB_GUI_FLAT"):
        globals()["FORCE_FLAT"] = True       # 命令行/环境变量强制平面模式
    try:
        app = App()
        app.mainloop()
    except Exception as exc:
        traceback.print_exc()
        try:                                  # pythonw 下没有终端，弹窗把错误说清楚
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, f"启动失败：{type(exc).__name__}: {exc}\n\n"
                                 "详情见终端输出；用 python github_gui.py 启动可以看到完整报错。")
            root.destroy()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
