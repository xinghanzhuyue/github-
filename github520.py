#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub520 for Windows —— GitHub 访问优化脚本（纯标准库，不需要 pip 安装任何东西）

功能：
  1. check    自动检测「当前浏览器到 GitHub 的连接状态」：
              浏览器清单与默认浏览器、系统/浏览器代理、浏览器 DoH（安全 DNS）
              是否会绕过 hosts、hosts 加速条目状态、DNS 污染对比、
              直连与走代理两种链路的真实 HTTP 连通性与耗时。
  2. update   从 GitHub520 多个数据源拉取最新 hosts，用 DoH 补充候选 IP，
              对候选 IP 做 TCP + TLS(SNI 证书校验) 探测并选最快的可用 IP，
              然后原子化更新系统 hosts（带备份、标记块、自动刷新 DNS、写后校验）。
  3. watch    常驻进程，按固定间隔重复「检测 -> 拉取 -> 优选 -> 更新」。
  4. task     注册 / 卸载 Windows 计划任务，实现开机后定期自动更新。
  5. status   查看当前 hosts 加速状态、备份、计划任务。
  6. restore  从备份一键还原 hosts。

设计要点：
  * 只写入被 "# >>> github-accel >>>" / "# <<< github-accel <<<" 包裹的独立区块，
    你原有的 hosts 内容不会被触碰（与同目录的 github-accel.ps1 使用同一组标记，可互操作）。
  * 优选 IP 时用「SNI = 目标域名」的 TLS 握手并校验证书，确保该 IP 真的能服务这个域名，
    避免只测 TCP 端口连通就把 IP 写进去。
  * 任何一步失败都会退回 GitHub520 数据源的原始 IP，绝不会把 hosts 写得比原方案更差。

常用命令：
    python github520.py check                 # 先体检，看清是 DNS、TLS 还是代理问题
    python github520.py update --dry-run      # 预览将写入的 IP 与耗时，不改系统
    python github520.py update                # 需要管理员权限
    python github520.py update --elevate      # 自动弹 UAC 提权后执行
    python github520.py watch --interval 3600 # 常驻，每小时自检并更新
    python github520.py task install          # 注册计划任务（需要管理员）
    python github520.py restore               # 从最近备份还原 hosts
"""

from __future__ import annotations

import argparse
import codecs
import concurrent.futures as cf
import ctypes
import datetime as dt
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

VERSION = "1.0.0"
IS_WINDOWS = os.name == "nt"
SCRIPT_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_HOSTS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
MARKER_START = "# >>> github-accel >>>"
MARKER_END = "# <<< github-accel <<<"
BLOCK_RE = re.compile(re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END), re.S)

TASK_NAME = "GitHub520-HostsUpdate"

# GitHub520 数据源（按顺序回退，第一个成功即用）
SOURCES: Tuple[str, ...] = (
    "https://raw.hellogithub.com/hosts",
    "https://cdn.jsdelivr.net/gh/521xueweihan/GitHub520@main/hosts",
    "https://ghproxy.net/https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts",
    "https://ghfast.top/https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts",
    "https://hub.gitmirror.com/https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts",
)

# DoH（DNS over HTTPS）JSON 接口：用来拿「没被污染」的候选 IP
DOH_ENDPOINTS: Tuple[str, ...] = (
    "https://223.5.5.5/resolve?name={domain}&type=A",
    "https://doh.pub/dns-query?name={domain}&type=A",
    "https://1.1.1.1/dns-query?name={domain}&type=A",
)

# 兜底候选池：DoH 不可用时使用（GitHub 官方公布的常用地址段）
FALLBACK_POOLS: Dict[str, Tuple[str, ...]] = {
    # raw / pages / 静态资源（Fastly 承载）
    "cdn": (
        "185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133",
    ),
    # github.com 主站与 API（Azure / GitHub 自有网段）
    "main": (
        "20.205.243.166", "20.205.243.165", "20.205.243.168",
        "140.82.112.3", "140.82.112.4", "140.82.113.3", "140.82.113.4",
        "140.82.114.3", "140.82.114.4", "140.82.116.3", "140.82.116.4",
        "140.82.121.3", "192.30.255.112", "20.27.177.113",
    ),
}

# 参与「完整测速优选」的关键域名（其余域名复用同族最优结果，避免无谓探测）
KEY_DOMAINS: Tuple[str, ...] = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "gist.githubusercontent.com",
    "avatars.githubusercontent.com",
    "github.githubassets.com",
    "github.io",
)

# 允许写入 hosts 的域名规则（只放 GitHub 相关域名，防止数据源被篡改后污染其它域名）
EXTRA_ALLOWED = {"vscode.dev", "githubstatus.com", "github.io", "github.blog", "github.dev"}
IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
HOSTS_LINE_RE = re.compile(r"^\s*([\d.]+)\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:#.*)?$")
# 兼容「域名 IP」这种写反了的历史区块：Windows 只认「IP 域名」，写反等于整段失效
HOSTS_LINE_SWAPPED_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s+([\d.]+)\s*(?:#.*)?$")

CHROME_LIKE = (
    ("Google Chrome", Path("Google") / "Chrome" / "User Data"),
    ("Microsoft Edge", Path("Microsoft") / "Edge" / "User Data"),
    ("Brave", Path("BraveSoftware") / "Brave-Browser" / "User Data"),
)


# --------------------------------------------------------------------------
# 输出与日志
# --------------------------------------------------------------------------

class Log:
    def __init__(self, log_dir: Path, quiet: bool = False, verbose: bool = False) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.log_file: Optional[Path] = None
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_file = log_dir / f"github520-{dt.date.today():%Y%m%d}.log"
        except OSError:
            self.log_file = None

    def _c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _write(self, text: str, level: str, to_console: bool = True) -> None:
        if to_console and not (self.quiet and level in ("INFO", "STEP")):
            print(text, flush=True)
        if self.log_file:
            plain = re.sub(r"\033\[[0-9;]*m", "", text)
            try:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {plain}\n")
            except OSError:
                pass

    def title(self, text: str) -> None:
        self._write("", "RAW")
        self._write(self._c(f"===== {text} =====", "36"), "TITLE")

    def info(self, text: str) -> None:
        self._write(f"  {text}", "INFO")

    def step(self, text: str) -> None:
        self._write(self._c(f"  -> {text}", "90"), "STEP")

    def ok(self, text: str) -> None:
        self._write(self._c(f"  [OK] {text}", "32"), "OK")

    def warn(self, text: str) -> None:
        self._write(self._c(f"  [警告] {text}", "33"), "WARN")

    def err(self, text: str) -> None:
        self._write(self._c(f"  [错误] {text}", "31"), "ERROR")

    def debug(self, text: str) -> None:
        if self.verbose:
            self._write(self._c(f"  · {text}", "90"), "DEBUG")

    def plain(self, text: str = "") -> None:
        self._write(text, "RAW")


# --------------------------------------------------------------------------
# Windows 系统辅助
# --------------------------------------------------------------------------

def setup_console() -> None:
    """让中文在 cmd/PowerShell 里正常显示。"""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def is_admin() -> bool:
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_and_exit(argv: Sequence[str], log: Log) -> None:
    """用 UAC 重新以管理员身份启动自己。"""
    if not IS_WINDOWS:
        log.err("当前系统不支持自动提权。")
        return
    script = str(Path(__file__).resolve())
    params = " ".join(f'"{a}"' if " " in a else a for a in [script, *argv])
    log.info("正在请求管理员权限（会弹出 UAC 窗口）...")
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if rc <= 32:
            log.err(f"提权失败（ShellExecute 返回 {rc}），可能被取消了。")
    except Exception as exc:  # pragma: no cover
        log.err(f"提权失败：{exc}")


def run_cmd(cmd: Sequence[str], timeout: float = 30.0) -> Tuple[int, str]:
    """执行外部命令并取回输出；沙箱/受限环境下自动降级为丢弃输出。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, creationflags=flags)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=timeout, creationflags=flags)
            return proc.returncode, f"(输出不可捕获: {exc})"
        except Exception as exc2:
            return -1, f"{exc2}"


def flush_dns(log: Log) -> None:
    """刷新 DNS 解析缓存：优先用系统 API，失败再退回 ipconfig。"""
    done = False
    if IS_WINDOWS:
        try:
            ctypes.windll.dnsapi.DnsFlushResolverCache()
            done = True
        except Exception:
            done = False
    if not done:
        rc, _ = run_cmd(["ipconfig", "/flushdns"], timeout=20)
        done = rc == 0
    if done:
        log.info("DNS 解析缓存已刷新。")
    else:
        log.warn("DNS 缓存刷新失败，可手动执行 ipconfig /flushdns。")


def resolve_ipv4(domain: str) -> List[str]:
    """走系统解析器（会读 hosts 文件）拿 IPv4。"""
    try:
        infos = socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
        return sorted({info[4][0] for info in infos})
    except socket.gaierror:
        return []


def valid_ipv4(text: str) -> bool:
    m = IPV4_RE.match(text)
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())


# --------------------------------------------------------------------------
# hosts 文件读写
# --------------------------------------------------------------------------

def read_hosts(path: Path) -> Tuple[str, str]:
    """返回 (文本, 编码)。hosts 通常是 ASCII/ANSI，这里保持原编码与 BOM 状态回写。"""
    raw = path.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def write_hosts(path: Path, text: str, encoding: str) -> None:
    path.write_bytes(text.encode(encoding, "replace"))


@dataclass
class BlockInfo:
    present: bool = False
    entries: List[Tuple[str, str]] = field(default_factory=list)  # (ip, domain)
    generated_at: Optional[str] = None
    swapped: int = 0          # 「域名 IP」写反了的行数（Windows 不认，等于没生效）

    @property
    def domains(self) -> List[str]:
        return [d for _, d in self.entries]


def parse_block(text: str) -> BlockInfo:
    m = BLOCK_RE.search(text)
    if not m:
        return BlockInfo()
    body = m.group(0)
    info = BlockInfo(present=True)
    ts = re.search(r"Generated by (?:github520\.py|github-accel\.ps1) at ([\d\-: ]+)", body)
    if ts:
        info.generated_at = ts.group(1).strip()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        mm = HOSTS_LINE_RE.match(line)
        if mm and valid_ipv4(mm.group(1)):
            info.entries.append((mm.group(1), mm.group(2).lower()))
            continue
        mm = HOSTS_LINE_SWAPPED_RE.match(line)
        if mm and valid_ipv4(mm.group(2)):
            info.swapped += 1
            info.entries.append((mm.group(2), mm.group(1).lower()))
    return info


def render_block(entries: Sequence[Tuple[str, str]], note: str = "") -> str:
    lines = [MARKER_START,
             f"# Generated by github520.py at {dt.datetime.now():%Y-%m-%d %H:%M:%S} - do not edit this block"]
    if note:
        lines.append(f"# {note}")
    lines += [f"{ip}\t{domain}" for ip, domain in entries]
    lines.append(MARKER_END)
    return "\n".join(lines)


def replace_block(text: str, block: Optional[str]) -> str:
    """把标记块替换成 block；block 为 None 表示删除该块。"""
    if BLOCK_RE.search(text):
        new = BLOCK_RE.sub(block or "", text, count=1)
    elif block is None:
        return text
    else:
        new = text.rstrip("\r\n") + "\n\n" + block + "\n"

    newline = "\r\n" if "\r\n" in text else "\n"
    new = new.replace("\r\n", "\n")
    if newline == "\r\n":
        new = new.replace("\n", "\r\n")
    # 收敛多余空行
    new = re.sub(r"(\r?\n){3,}", newline * 2, new)
    return new.rstrip("\r\n") + newline


def backup_hosts(path: Path, backup_dir: Path, keep: int, log: Log) -> Optional[Path]:
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / f"hosts.{dt.datetime.now():%Y%m%d-%H%M%S}.bak"
        shutil.copy2(path, dest)
        log.info(f"已备份 hosts：{dest}")
        old = sorted(backup_dir.glob("hosts.*.bak"))
        for stale in old[:-keep] if keep > 0 else []:
            try:
                stale.unlink()
            except OSError:
                pass
        return dest
    except OSError as exc:
        log.warn(f"备份 hosts 失败：{exc}")
        return None


# --------------------------------------------------------------------------
# 抓取数据源 / 解析 / DoH
# --------------------------------------------------------------------------

def http_get(url: str, timeout: float = 20.0, proxy: Optional[str] = None) -> bytes:
    """proxy=None 表示强制直连（忽略系统代理），否则走指定代理。"""
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": f"github520.py/{VERSION}"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def system_proxy_url() -> Optional[str]:
    """读取 Windows 系统代理（Chrome/Edge 默认跟随它）。"""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enable:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    if not server:
        return None
    # 形如 "1.2.3.4:8080" 或 "http=1.2.3.4:8080;https=1.2.3.4:8443"
    for part in str(server).split(";"):
        if "=" not in part:
            return f"http://{part}"
        scheme, _, host = part.partition("=")
        if scheme.lower() in ("http", "https"):
            return f"http://{host}"
    return None


def fetch_sources(sources: Sequence[str], timeout: float, prefer_proxy: Optional[str], log: Log) -> Tuple[str, str]:
    """按顺序尝试数据源，返回 (文本, 来源)。"""
    last_error = ""
    for url in sources:
        for attempt, proxy in enumerate((None, prefer_proxy)):
            if attempt == 1 and not prefer_proxy:
                break
            label = "直连" if proxy is None else f"代理 {proxy}"
            log.step(f"拉取 {url}（{label}）")
            try:
                data = http_get(url, timeout=timeout, proxy=proxy)
                text = data.decode("utf-8", "replace")
                if len(text) < 200:
                    last_error = "内容过短"
                    log.warn(f"内容异常（{len(text)} 字节），换下一个源")
                    break
                log.ok(f"数据源可用：{url}（{len(data)} 字节）")
                return text, url
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warn(f"{label}失败：{last_error}")
    log.err(f"所有数据源都拉取失败（最后错误：{last_error}）")
    return "", ""


def domain_allowed(domain: str) -> bool:
    d = domain.lower().rstrip(".")
    if d in EXTRA_ALLOWED:
        return True
    return "github" in d or "githubusercontent" in d


def parse_entries(text: str) -> List[Tuple[str, str]]:
    """解析 GitHub520 hosts 文本，返回去重后的 (domain, ip)。

    注意：返回值是 (域名, IP)！它的调用方 optimize() 按 (domain, ip) 解包。
    （历史上本函数的注释写反过，导致 --no-optimize 分支把区块写成了「域名 IP」。）
    """
    entries: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = HOSTS_LINE_RE.match(line)
        if not m:
            continue
        ip, domain = m.group(1), m.group(2).lower()
        if not valid_ipv4(ip) or ip in ("0.0.0.0", "127.0.0.1"):
            continue
        if not domain_allowed(domain):
            continue
        entries.setdefault(domain, ip)
    return sorted(entries.items())


def doh_lookup(domain: str, timeout: float, proxy: Optional[str], log: Log) -> List[str]:
    """用 DoH JSON 接口拿未被污染的 A 记录。"""
    ips: List[str] = []
    for tpl in DOH_ENDPOINTS:
        url = tpl.format(domain=domain)
        try:
            data = http_get(url, timeout=timeout, proxy=proxy)
            payload = json.loads(data.decode("utf-8", "replace"))
        except Exception as exc:
            log.debug(f"DoH {url} 失败：{type(exc).__name__}: {exc}")
            continue
        for ans in payload.get("Answer", []) or []:
            if ans.get("type") == 1 and valid_ipv4(str(ans.get("data", ""))):
                ips.append(str(ans["data"]))
        if ips:
            log.debug(f"DoH {domain} -> {', '.join(ips)}")
            break
    return list(dict.fromkeys(ips))


# --------------------------------------------------------------------------
# IP 优选（TCP + TLS/SNI 证书校验）
# --------------------------------------------------------------------------

@dataclass
class Probe:
    ip: str
    ok: bool
    ms: float
    error: str = ""


def family_of(domain: str) -> str:
    d = domain.lower()
    if ("githubusercontent" in d or "githubassets" in d or d.endswith("github.io")
            or "fastly" in d or "githubstatus" in d):
        return "cdn"
    return "main"


def probe_ip(ip: str, domain: str, timeout: float) -> Probe:
    """用 SNI=domain 建 TLS 连接并校验证书，确认该 IP 真的服务这个域名。"""
    start = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain):
                pass
        return Probe(ip, True, (time.perf_counter() - start) * 1000)
    except Exception as exc:
        return Probe(ip, False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"[:90])


# HTTP 层校验用的路径（越小越好，能真实反映该 IP 能否服务这个域名）
VERIFY_PATHS: Dict[str, str] = {
    "github.com": "/robots.txt",
    "api.github.com": "/",
    "raw.githubusercontent.com": "/521xueweihan/GitHub520/main/hosts",
    "codeload.github.com": "/",
    "objects.githubusercontent.com": "/",
    "gist.githubusercontent.com": "/",
    "avatars.githubusercontent.com": "/",
    "github.githubassets.com": "/",
    "github.io": "/",
}


class _SniConnection(http.client.HTTPSConnection):
    """按指定 IP 连接，但 SNI 与 Host 都用域名（用于验证某个 IP 是否真的服务该域名）。"""

    def __init__(self, ip: str, domain: str, **kwargs) -> None:
        super().__init__(ip, **kwargs)
        self._domain = domain

    def connect(self) -> None:  # type: ignore[override]
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._domain)


def http_verify(ip: str, domain: str, timeout: float) -> Tuple[bool, int, float, str]:
    """对选定 IP 做一次真实 HTTPS 请求，返回 (是否可用, 状态码, 毫秒, 错误)。"""
    path = VERIFY_PATHS.get(domain, "/")
    start = time.perf_counter()
    try:
        conn = _SniConnection(ip, domain, timeout=timeout, context=ssl.create_default_context())
        conn.request("GET", path, headers={"User-Agent": f"github520.py/{VERSION}", "Host": domain})
        resp = conn.getresponse()
        resp.read(256)
        conn.close()
        ms = (time.perf_counter() - start) * 1000
        return (resp.status < 500, resp.status, ms, "" if resp.status < 500 else f"HTTP {resp.status}")
    except Exception as exc:
        ms = (time.perf_counter() - start) * 1000
        return False, 0, ms, f"{type(exc).__name__}: {exc}"[:70]


def candidates_for(domain: str, source_ip: str, doh_ips: Sequence[str], full_pool: bool) -> List[str]:
    pool: List[str] = []
    for ip in (source_ip, *doh_ips):
        if valid_ipv4(ip) and ip not in pool:
            pool.append(ip)
    if full_pool:
        for ip in FALLBACK_POOLS[family_of(domain)]:
            if ip not in pool:
                pool.append(ip)
    return pool


@dataclass
class Choice:
    domain: str
    ip: str
    source_ip: str
    ms: Optional[float] = None
    verified: bool = False
    note: str = ""


def optimize(entries: Sequence[Tuple[str, str]], args, log: Log,
             proxy: Optional[str]) -> List[Choice]:
    """为每个域名挑一个「能连、证书对、延迟低」的 IP。"""
    log.title("IP 优选（TCP + TLS/SNI 证书校验）")
    by_domain = dict(entries)
    key_set = set(KEY_DOMAINS)

    log.step(f"用 DoH 获取未污染的候选 IP（{len(KEY_DOMAINS)} 个关键域名）")
    doh_map: Dict[str, List[str]] = {}
    with cf.ThreadPoolExecutor(max_workers=min(8, len(KEY_DOMAINS))) as pool:
        futures = {pool.submit(doh_lookup, d, args.timeout, proxy, log): d for d in KEY_DOMAINS if d in by_domain}
        for fut in cf.as_completed(futures):
            doh_map[futures[fut]] = fut.result()

    # 1) 关键域名：源 IP + DoH IP + 兜底池，全部探测
    jobs: List[Tuple[str, str]] = []
    for domain in KEY_DOMAINS:
        if domain not in by_domain:
            continue
        for ip in candidates_for(domain, by_domain[domain], doh_map.get(domain, []), full_pool=True):
            jobs.append((domain, ip))
    log.step(f"并行探测 {len(jobs)} 个 (域名, IP) 组合，超时 {args.probe_timeout}s")
    results: Dict[Tuple[str, str], Probe] = {}
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(probe_ip, ip, domain, args.probe_timeout): (domain, ip) for domain, ip in jobs}
        for fut in cf.as_completed(futures):
            domain, ip = futures[fut]
            results[(domain, ip)] = fut.result()

    # 2) 汇总每个族的最优结果，作为非关键域名的首选 IP
    family_best: Dict[str, Probe] = {}
    for (domain, ip), probe in results.items():
        if not probe.ok:
            continue
        fam = family_of(domain)
        if fam not in family_best or probe.ms < family_best[fam].ms:
            family_best[fam] = probe

    # 3) HTTP 层校验：TLS 能通不代表真能取到内容，关键域名再做一次真实 HTTPS 请求
    http_results: Dict[Tuple[str, str], Tuple[bool, int, float, str]] = {}
    ranked: Dict[str, List[Probe]] = {}
    for domain in KEY_DOMAINS:
        if domain not in by_domain:
            continue
        cands = candidates_for(domain, by_domain[domain], doh_map.get(domain, []), True)
        good = sorted((results[(domain, ip)] for ip in cands
                       if (domain, ip) in results and results[(domain, ip)].ok), key=lambda p: p.ms)
        ranked[domain] = good[:3]
    if getattr(args, "http_verify", True):
        jobs2 = [(d, p.ip) for d, ps in ranked.items() for p in ps]
        if jobs2:
            log.step(f"HTTP 层校验 {len(jobs2)} 个候选（每个关键域名最多取前 3 个，超时 {args.http_timeout}s）")
            with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = {pool.submit(http_verify, ip, d, args.http_timeout): (d, ip) for d, ip in jobs2}
                for fut in cf.as_completed(futures):
                    http_results[futures[fut]] = fut.result()

    choices: List[Choice] = []
    for domain, source_ip in entries:
        if domain in key_set:
            cands = candidates_for(domain, source_ip, doh_map.get(domain, []), True)
            probes = [results[(domain, ip)] for ip in cands if (domain, ip) in results]
            good = ranked.get(domain, [])
            chosen: Optional[Tuple[Probe, str]] = None
            for probe in good:
                hres = http_results.get((domain, probe.ip))
                if hres and hres[0]:
                    chosen = (probe, f"HTTP {hres[1]} {hres[2]:.0f} ms")
                    break
            if chosen is None:
                source_ok = next((p for p in good if p.ip == source_ip), None)
                if source_ok is not None:
                    chosen = (source_ok, "仅 TLS 通过，保守保留源 IP")
                elif good:
                    chosen = (good[0], "仅 TLS 通过")
            if chosen is None:
                why = probes[0].error if probes else "无候选"
                choices.append(Choice(domain, source_ip, source_ip, None, False, "探测失败，保留源IP"))
                log.warn(f"{domain:<44} {source_ip:<16} 探测失败，保留源 IP（{why}）")
            else:
                probe, tag = chosen
                note = ("源IP" if probe.ip == source_ip
                        else ("DoH" if probe.ip in doh_map.get(domain, []) else "兜底池"))
                choices.append(Choice(domain, probe.ip, source_ip, probe.ms, True, f"{note}/{tag}"))
                log.ok(f"{domain:<44} {probe.ip:<16} {probe.ms:6.0f} ms  ({note}；{tag})")
            continue

        # 非关键域名：先试同族最优 IP，失败再试源 IP
        cand = family_best[family_of(domain)].ip if family_of(domain) in family_best else source_ip
        if cand != source_ip:
            probe = probe_ip(cand, domain, args.probe_timeout)
            results[(domain, cand)] = probe
            if probe.ok:
                choices.append(Choice(domain, cand, source_ip, probe.ms, True, "同族最优"))
                log.ok(f"{domain:<44} {cand:<16} {probe.ms:6.0f} ms  (同族最优)")
                continue
        probe = results.get((domain, source_ip)) or probe_ip(source_ip, domain, args.probe_timeout)
        results[(domain, source_ip)] = probe
        if probe.ok:
            choices.append(Choice(domain, source_ip, source_ip, probe.ms, True, "源IP"))
            log.ok(f"{domain:<44} {source_ip:<16} {probe.ms:6.0f} ms  (源IP)")
        else:
            choices.append(Choice(domain, source_ip, source_ip, None, False, "未验证"))
            log.warn(f"{domain:<44} {source_ip:<16} 未能验证，保留源 IP")

    verified = sum(1 for c in choices if c.verified)
    http_verified = sum(1 for d, p in http_results if http_results[(d, p)][0] and
                        any(c.domain == d and c.ip == p for c in choices))
    log.info(f"优选完成：{verified}/{len(choices)} 个域名拿到了已验证的可用 IP，"
             f"其中 {http_verified} 个通过了 HTTP 层校验。")
    return choices


# --------------------------------------------------------------------------
# 浏览器 / 代理 / DoH 检测
# --------------------------------------------------------------------------

def reg_query(root, path: str, name: Optional[str] = None):
    if not IS_WINDOWS:
        return None
    import winreg
    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name) if name else (winreg.QueryValue(key, None), None)
            return value
    except OSError:
        return None


def reg_subkeys(root, path: str) -> List[str]:
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


def extract_exe(cmd: Optional[str]) -> str:
    """从注册表的命令行里取出可执行文件路径（兼容带引号 / 不带引号 / 带参数三种写法）。"""
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
    """从注册表 + 常见安装路径列出浏览器 (名称, 路径)。"""
    import winreg
    found: Dict[str, str] = {}
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        base = r"SOFTWARE\Clients\StartMenuInternet"
        for sub in reg_subkeys(root, base):
            name = reg_query(root, rf"{base}\{sub}", "") or sub
            cmd = reg_query(root, rf"{base}\{sub}\shell\open\command", "")
            found.setdefault(str(name), extract_exe(cmd if isinstance(cmd, str) else ""))
    well_known = {
        "Google Chrome": Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        "Microsoft Edge": Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        "Mozilla Firefox": Path(os.environ.get("ProgramFiles", "")) / "Mozilla Firefox/firefox.exe",
    }
    for name, path in well_known.items():
        if path.is_file():
            found.setdefault(name, str(path))
    return sorted(found.items())


def default_browser() -> Optional[str]:
    import winreg
    prog = reg_query(winreg.HKEY_CURRENT_USER,
                     r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
                     "ProgId")
    if not prog:
        return None
    cmd = reg_query(winreg.HKEY_CLASSES_ROOT, rf"{prog}\shell\open\command", "")
    exe = extract_exe(cmd if isinstance(cmd, str) else "")
    return exe or str(prog)


def firefox_profiles() -> List[Path]:
    base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"
    if not base.is_dir():
        return []
    return [p for p in base.iterdir() if (p / "prefs.js").is_file()]


def firefox_proxy_settings() -> Optional[dict]:
    """Firefox 不跟随系统代理，需要单独读它的 prefs.js。"""
    for prof in firefox_profiles():
        prefs: Dict[str, str] = {}
        try:
            text = (prof / "prefs.js").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'user_pref\("([^"]+)",\s*(.+?)\);', text):
            prefs[m.group(1)] = m.group(2).strip().strip('"')
        ptype = prefs.get("network.proxy.type", "?")
        info = {"profile": prof.name, "type": ptype,
                "http": prefs.get("network.proxy.http", ""), "http_port": prefs.get("network.proxy.http_port", ""),
                "socks": prefs.get("network.proxy.socks", ""), "socks_port": prefs.get("network.proxy.socks_port", ""),
                "trr_mode": prefs.get("network.trr.mode", ""), "trr_uri": prefs.get("network.trr.uri", "")}
        return info
    return None


def chromium_doh_state() -> List[dict]:
    """检测 Chrome/Edge 的「安全 DNS(DoH)」——开启后浏览器会绕过 hosts 文件！"""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    out: List[dict] = []
    for name, rel in CHROME_LIKE:
        state_file = local / rel / "Local State"
        if not state_file.is_file():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        doh = data.get("dns_over_https") or {}
        if not isinstance(doh, dict):
            doh = {"mode": data.get("dns_over_https.mode", ""), "templates": data.get("dns_over_https.templates", "")}
        mode = str(doh.get("mode", "") or "").lower()
        out.append({"browser": name, "mode": mode or "off", "templates": doh.get("templates", ""),
                    "file": str(state_file)})
    return out


def chrome_running() -> List[str]:
    names = ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"]
    running: List[str] = []
    rc, output = run_cmd(["tasklist", "/FO", "CSV", "/NH"], timeout=15)
    if rc != 0:
        return []
    low = output.lower()
    for n in names:
        if f'"{n}"' in low:
            running.append(n)
    return running


# --------------------------------------------------------------------------
# 连通性检测
# --------------------------------------------------------------------------

@dataclass
class HttpResult:
    url: str
    ok: bool
    status: int = 0
    ms: float = 0.0
    error: str = ""


CHECK_URLS: Tuple[Tuple[str, str], ...] = (
    ("github.com", "https://github.com/"),
    ("api.github.com", "https://api.github.com/"),
    ("raw.githubusercontent.com", "https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts"),
)


def http_probe(url: str, proxy: Optional[str], timeout: float) -> HttpResult:
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": f"github520.py/{VERSION}"})
    start = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(2048)
            return HttpResult(url, True, resp.status, (time.perf_counter() - start) * 1000)
    except urllib.error.HTTPError as exc:
        return HttpResult(url, exc.code < 400, exc.code, (time.perf_counter() - start) * 1000,
                          f"HTTP {exc.code}")
    except Exception as exc:
        return HttpResult(url, False, 0, (time.perf_counter() - start) * 1000,
                          f"{type(exc).__name__}: {exc}"[:120])


def connectivity_report(proxy: Optional[str], timeout: float, log: Log) -> Tuple[bool, str, List[str]]:
    """返回 (是否全部可达, 判定文本, 不可达目标列表)。"""
    log.title("GitHub 连通性（等价于浏览器链路）")
    label = f"代理 {proxy}" if proxy else "直连"
    log.info(f"测试链路：{label}")
    results: List[HttpResult] = []
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(http_probe, url, proxy, timeout) for _, url in CHECK_URLS]
        for fut in futures:
            results.append(fut.result())
    healthy = 0
    failures: List[str] = []
    for (name, _), res in zip(CHECK_URLS, results):
        if res.ok:
            log.ok(f"{name:<30} HTTP {res.status}  {res.ms:6.0f} ms")
            healthy += 1
        else:
            log.err(f"{name:<30} 失败：{res.error}  ({res.ms:.0f} ms)")
            failures.append(name)
    verdict = f"{label}：{healthy}/{len(CHECK_URLS)} 个目标可达"
    return healthy == len(CHECK_URLS), verdict, failures


def dns_report(log: Log, proxy: Optional[str], timeout: float) -> List[str]:
    """对比系统解析与 DoH 结果，找出被污染的域名。"""
    log.title("DNS 解析对比（系统 hosts/缓存  vs  DoH）")
    polluted: List[str] = []
    for domain, _ in CHECK_URLS:
        local = resolve_ipv4(domain)
        doh = doh_lookup(domain, timeout, proxy, log)
        local_txt = ", ".join(local) or "无结果"
        if not doh:
            log.warn(f"{domain:<30} 系统: {local_txt}   DoH: 查询失败，无法对比")
            continue
        bad = (not local) or all(ip in ("0.0.0.0", "127.0.0.1") for ip in local)
        overlap = bool(set(local) & set(doh))
        if bad:
            log.err(f"{domain:<30} 系统: {local_txt}   DoH: {', '.join(doh)}   -> 被污染/黑洞")
            polluted.append(domain)
        elif not overlap:
            log.warn(f"{domain:<30} 系统: {local_txt}   DoH: {', '.join(doh)}   -> 与公共解析不一致")
            polluted.append(domain)
        else:
            log.ok(f"{domain:<30} 系统: {local_txt}   DoH: {', '.join(doh)}   -> 一致")
    return polluted


# --------------------------------------------------------------------------
# 子命令：check
# --------------------------------------------------------------------------

def cmd_check(args, log: Log) -> int:
    hosts_path = Path(args.hosts_file)
    proxy_arg = args.proxy
    sys_proxy = system_proxy_url()
    if proxy_arg == "auto":
        proxy = sys_proxy
    elif proxy_arg in ("", "none", "off"):
        proxy = None
    else:
        proxy = proxy_arg

    report: dict = {"hosts_file": str(hosts_path), "admin": is_admin()}

    log.title("环境")
    log.info(f"Python {sys.version.split()[0]}（{sys.executable}）")
    log.info(f"hosts 文件：{hosts_path}")
    log.info("管理员权限：" + ("是" if is_admin() else "否（更新 hosts 需要）"))
    browsers = installed_browsers()
    if browsers:
        log.info("检测到浏览器：")
        for name, exe in browsers:
            log.plain(f"      {name}  {exe}")
    else:
        log.warn("未从注册表检测到浏览器。")
    dflt = default_browser()
    if dflt:
        log.ok(f"默认浏览器：{dflt}")
    running = chrome_running()
    if running:
        log.info("正在运行的浏览器进程：" + ", ".join(running))
    report["browsers"] = [{"name": n, "exe": e} for n, e in browsers]
    report["default_browser"] = dflt

    log.title("代理设置")
    if sys_proxy:
        log.ok(f"系统代理（Chrome/Edge 跟随）：{sys_proxy}")
    else:
        log.info("系统代理：未开启")
    import winreg
    pac = reg_query(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings", "AutoConfigURL")
    if pac:
        log.warn(f"系统 PAC 脚本：{pac}（本脚本解析不了 PAC，连通性测试会按{'系统代理' if sys_proxy else '直连'}进行）")
    report["system_proxy"] = sys_proxy
    report["pac"] = pac
    ff = firefox_proxy_settings()
    if ff:
        mode_txt = {"0": "不使用代理（直连）", "1": "手动配置", "2": "自动代理配置(PAC)",
                    "4": "自动检测", "5": "使用系统代理"}.get(str(ff["type"]), f"未知({ff['type']})")
        log.info(f"Firefox 代理（独立于系统）：{mode_txt}  profile={ff['profile']}")
        if ff["http"]:
            log.plain(f"      HTTP  {ff['http']}:{ff['http_port']}   SOCKS  {ff['socks']}:{ff['socks_port']}")
        report["firefox"] = ff

    log.title("浏览器 DNS over HTTPS（关键：DoH 会绕过 hosts 文件）")
    doh_states = chromium_doh_state()
    doh_bypass = False
    if doh_states:
        for st in doh_states:
            mode = st["mode"]
            if mode == "off" or mode == "":
                log.ok(f"{st['browser']:<16} 安全 DNS：关闭（hosts 生效）")
            else:
                doh_bypass = True
                log.warn(f"{st['browser']:<16} 安全 DNS：{mode}"
                         + (f"  模板 {st['templates']}" if st["templates"] else "")
                         + "  -> 会绕过 hosts！请到浏览器设置里关闭「使用安全 DNS」")
    else:
        log.info("未检测到 Chrome/Edge/Brave 的用户数据（或读不到 Local State）。")
    if ff and str(ff.get("trr_mode", "")) not in ("", "0", "5"):
        doh_bypass = True
        log.warn(f"Firefox 的 TRR(DoH) 已启用：network.trr.mode={ff['trr_mode']} -> 会绕过 hosts！")
    report["chromium_doh"] = doh_states
    report["doh_bypass_hosts"] = doh_bypass

    log.title("hosts 加速条目")
    try:
        text, enc = read_hosts(hosts_path)
    except OSError as exc:
        text, enc = "", "?"
        log.err(f"读取 hosts 失败：{exc}")
    block = parse_block(text)
    if block.present:
        log.ok(f"已启用，共 {len(block.entries)} 条，生成时间 {block.generated_at or '未知'}（编码 {enc}）")
        if block.swapped:
            log.err(f"但其中 {block.swapped} 行写成了「域名 IP」顺序：Windows 只认「IP 域名」，"
                    "这些行当前完全没生效（重新运行 update 即可覆写修正）。")
    else:
        log.info("未启用（没有本脚本生成的标记块）")
    report["hosts_block"] = {"present": block.present, "count": len(block.entries),
                             "generated_at": block.generated_at, "swapped": block.swapped}

    polluted = dns_report(log, proxy, args.timeout)
    direct_ok, direct_txt, direct_failed = connectivity_report(None, args.timeout, log)
    if proxy:
        proxy_ok, proxy_txt, proxy_failed = connectivity_report(proxy, args.timeout, log)
    else:
        proxy_ok, proxy_txt, proxy_failed = False, "", []
    report["direct_ok"] = direct_ok
    report["proxy_ok"] = proxy_ok
    report["direct_failed"] = direct_failed
    report["proxy_failed"] = proxy_failed

    log.title("结论与建议")
    advice: List[str] = []
    if polluted:
        advice.append(f"DNS 异常域名 {len(polluted)} 个（{', '.join(polluted)}）-> 执行 update 写入 hosts 即可绕过")
    if doh_bypass:
        advice.append("浏览器开了「安全 DNS(DoH)」，hosts 会被绕过 -> 先在浏览器设置里关掉，再执行 update")
    if not direct_ok and proxy and proxy_ok:
        advice.append("直连不通但代理可用 -> 浏览器里开启/保持代理，git 也建议走代理")
    elif not direct_ok and proxy and len(proxy_failed) < len(CHECK_URLS):
        advice.append(f"走代理仍有目标不可达（{', '.join(proxy_failed)}）-> 换节点，或 update --force 换 IP 再试")
    elif not direct_ok and not proxy and 0 < len(direct_failed) < len(CHECK_URLS):
        advice.append(f"直连时 {', '.join(direct_failed)} 不可达，其余正常 -> 该域名被针对性干扰，"
                      "可 update --force 换 IP；若仍不行就需要代理")
    elif not direct_ok and not proxy_ok:
        advice.append("直连与代理都不通 -> 先确认代理软件是否在运行，或换一个可用节点")
    if not is_admin():
        advice.append("当前不是管理员 -> 执行 update 时加 --elevate 自动提权")
    if not advice:
        advice.append("未发现明显问题。")
    for i, tip in enumerate(advice, 1):
        log.warn(f"{i}) {tip}")
    report["advice"] = advice

    if args.json:
        log.plain(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (direct_ok or proxy_ok) and not doh_bypass else 1


# --------------------------------------------------------------------------
# 子命令：update / watch
# --------------------------------------------------------------------------

def do_update(args, log: Log, reason: str = "") -> bool:
    hosts_path = Path(args.hosts_file)
    if not hosts_path.is_file():
        log.err(f"hosts 文件不存在：{hosts_path}")
        return False

    needs_admin = os.path.normcase(str(hosts_path)) == os.path.normcase(str(DEFAULT_HOSTS))
    if not args.dry_run and needs_admin and not is_admin():
        log.err("更新系统 hosts 需要管理员权限。")
        log.info("两种办法：① 用管理员身份打开终端再跑；② 加 --elevate 自动弹 UAC。")
        log.info("（若指定了 --hosts-file 指向别的文件，则不需要管理员。）")
        return False

    log.title("拉取 GitHub520 数据源" + (f"（{reason}）" if reason else ""))
    proxy = system_proxy_url()
    text, source = fetch_sources(SOURCES, args.timeout, proxy, log)
    if not text:
        return False
    entries = parse_entries(text)
    if len(entries) < 5:
        log.err(f"解析出的条目太少（{len(entries)} 条），放弃本次更新。")
        return False
    log.ok(f"解析出 {len(entries)} 个 GitHub 域名。")

    choices: List[Choice]
    if args.no_optimize:
        log.step("已指定 --no-optimize，直接使用数据源 IP（不做测速）")
        # parse_entries 返回的是 (domain, ip)，这里必须按同样顺序解包，
        # 否则 render_block 会写成「域名 IP」，Windows 会整段忽略。
        choices = [Choice(d, ip, ip, None, False, "源IP") for d, ip in entries]
    else:
        choices = optimize(entries, args, log, proxy)

    entries_out = sorted(((c.ip, c.domain) for c in choices), key=lambda item: item[1])
    block = render_block(entries_out, note=f"source: {source} | entries: {len(entries_out)}")

    # 写盘前自检：区块必须能原样解析回来，否则绝不落盘（防止再写出失效的 hosts）
    probe = parse_block(block)
    if probe.swapped or len(probe.entries) != len(entries_out):
        log.err(f"生成的区块自检失败（可解析 {len(probe.entries)}/{len(entries_out)} 条，"
                f"顺序错误的行 {probe.swapped} 行），已放弃写入以避免破坏 hosts。")
        log.info("这属于程序 bug，请把这段输出反馈给脚本作者。")
        return False

    log.title("更新 hosts")
    if args.dry_run:
        log.info("[DryRun] 不会修改任何文件。将写入的区块：")
        for ip, domain in entries_out:
            log.plain(f"      {ip:<16} {domain}")
        return True

    try:
        backup_hosts(hosts_path, Path(args.backup_dir), args.keep_backups, log)
        current, enc = read_hosts(hosts_path)
        write_hosts(hosts_path, replace_block(current, block), enc)
    except PermissionError as exc:
        log.err(f"写入 hosts 被拒绝（{exc}）。请以管理员身份运行。")
        return False
    except OSError as exc:
        log.err(f"写入 hosts 失败：{exc}")
        return False
    log.ok(f"hosts 已更新：{len(entries_out)} 条加速条目。")

    flush_dns(log)

    log.title("写入后校验")
    if not needs_admin:
        text2, enc2 = read_hosts(hosts_path)
        block2 = parse_block(text2)
        bom_txt = "带 BOM" if text2.startswith("\ufeff") else "无 BOM"
        if block2.present and not block2.swapped and len(block2.entries) == len(entries_out):
            log.ok(f"文件内容正确：标记块 {len(block2.entries)} 条，编码 {enc2}（{bom_txt}）")
        elif block2.swapped:
            log.err(f"文件校验失败：标记块里有 {block2.swapped} 行是「域名 IP」顺序，"
                    "Windows 不认，等于没生效。")
        else:
            log.err(f"文件校验失败：块存在={block2.present}，条目数={len(block2.entries)}，期望 {len(entries_out)}")
        return True

    log.info("走系统解析器（会读 hosts 文件）核对：")
    bad = 0
    for domain in KEY_DOMAINS:
        want = next((ip for ip, d in entries_out if d == domain), None)
        if not want:
            continue
        got = resolve_ipv4(domain)
        if want in got:
            log.ok(f"{domain:<38} -> {', '.join(got)}")
        elif got:
            log.warn(f"{domain:<38} -> {', '.join(got)}（期望含 {want}）")
            bad += 1
        else:
            log.err(f"{domain:<38} -> 解析失败")
            bad += 1
    if bad:
        log.warn("部分域名解析与写入值不一致：可能被上游 DNS 覆盖，或浏览器 DoH 绕过了 hosts。")
    else:
        log.ok("全部关键域名解析正常。")
    return True


def cmd_update(args, log: Log) -> int:
    if args.elevate and not is_admin() and not args.dry_run:
        elevate_and_exit(sys.argv[1:], log)
        return 0
    return 0 if do_update(args, log) else 1


def cmd_watch(args, log: Log) -> int:
    if args.elevate and not is_admin():
        elevate_and_exit(sys.argv[1:], log)
        return 0
    log.title("常驻监控")
    log.info(f"间隔：{args.interval} 秒；Ctrl+C 退出。")
    round_no = 0
    while True:
        round_no += 1
        log.title(f"第 {round_no} 轮：{dt.datetime.now():%Y-%m-%d %H:%M:%S}")
        proxy = system_proxy_url()
        healthy, verdict, _failed = connectivity_report(proxy, args.timeout, log)
        block = parse_block(read_hosts(Path(args.hosts_file))[0]) if Path(args.hosts_file).is_file() else BlockInfo()
        need = (not healthy) or (not block.present) or args.force
        if need:
            why = "连通性不佳" if not healthy else ("hosts 未启用" if not block.present else "强制刷新")
            log.info(f"需要更新（{why}）：{verdict}")
            do_update(args, log, reason=why)
        else:
            log.ok(f"链路正常（{verdict}），本轮跳过更新。")
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("已停止。")
            return 0


# --------------------------------------------------------------------------
# 子命令：status / restore / task
# --------------------------------------------------------------------------

def cmd_status(args, log: Log) -> int:
    hosts_path = Path(args.hosts_file)
    log.title("当前状态")
    log.info(f"hosts 文件：{hosts_path}")
    log.info("管理员权限：" + ("是" if is_admin() else "否"))
    if hosts_path.is_file():
        text, enc = read_hosts(hosts_path)
        block = parse_block(text)
        if block.present:
            log.ok(f"加速条目：{len(block.entries)} 条（生成于 {block.generated_at or '未知'}，编码 {enc}）")
            if block.swapped:
                log.err(f"其中 {block.swapped} 行写成了「域名 IP」顺序——Windows 要求「IP 域名」，"
                        "这些行等于没生效！请重新运行一次 update 覆写该区块。")
            for ip, domain in block.entries[:5]:
                log.plain(f"      {ip:<16} {domain}")
            if len(block.entries) > 5:
                log.plain(f"      ... 其余 {len(block.entries) - 5} 条")
        else:
            log.info("加速条目：未启用")
    else:
        log.err("hosts 文件不存在。")

    log.title("备份")
    backups = sorted(Path(args.backup_dir).glob("hosts.*.bak")) if Path(args.backup_dir).is_dir() else []
    if backups:
        for b in backups[-5:]:
            log.plain(f"      {b.name}  ({b.stat().st_size} 字节)")
        log.info(f"共 {len(backups)} 个备份，目录 {args.backup_dir}")
    else:
        log.info(f"暂无备份（目录 {args.backup_dir}）")

    log.title("计划任务")
    rc, out = run_cmd(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], timeout=20)
    if rc == 0:
        log.ok(f"{TASK_NAME} 已注册")
        for line in out.splitlines():
            if re.match(r"\s*(任务名|TaskName|状态|Status|下次运行时间|Next Run Time|要运行的任务|Task To Run)", line):
                log.plain("      " + line.strip())
    else:
        log.info(f"{TASK_NAME} 未注册（可用 task install 注册）")

    log.title("数据源可达性")
    proxy = system_proxy_url()
    for url in SOURCES:
        try:
            t0 = time.perf_counter()
            http_get(url, timeout=args.timeout, proxy=proxy)
            log.ok(f"{(time.perf_counter() - t0) * 1000:6.0f} ms  {url}")
        except Exception as exc:
            log.err(f"  失败  {url}  ({type(exc).__name__})")
    if log.log_file:
        log.info(f"日志：{log.log_file}")
    return 0


def cmd_restore(args, log: Log) -> int:
    hosts_path = Path(args.hosts_file)
    backup_dir = Path(args.backup_dir)
    backups = sorted(backup_dir.glob("hosts.*.bak")) if backup_dir.is_dir() else []
    if args.list:
        if not backups:
            log.info("没有备份。")
            return 0
        for b in backups:
            log.plain(f"  {b.name}  {b.stat().st_size} 字节")
        return 0
    if not is_admin():
        log.err("还原 hosts 需要管理员权限（可加 --elevate）。")
        return 1
    if args.backup:
        target = Path(args.backup)
        if not target.is_file():
            log.err(f"备份不存在：{target}")
            return 1
    elif backups:
        target = backups[-1]
    else:
        log.warn("没有备份，改为仅移除本脚本生成的区块。")
        text, enc = read_hosts(hosts_path)
        backup_hosts(hosts_path, backup_dir, args.keep_backups, log)
        write_hosts(hosts_path, replace_block(text, None), enc)
        flush_dns(log)
        log.ok("已移除加速区块。")
        return 0
    shutil.copy2(target, hosts_path)
    log.ok(f"已从 {target.name} 还原 hosts。")
    flush_dns(log)
    return 0


def cmd_task(args, log: Log) -> int:
    if args.action == "status":
        rc, out = run_cmd(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"], timeout=20)
        log.plain(out if rc == 0 else f"{TASK_NAME} 未注册。")
        return 0 if rc == 0 else 1
    if args.action == "install" and args.dry_run:
        script = str(Path(__file__).resolve())
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw) if pythonw.is_file() else sys.executable
        hours = max(1, int(round(args.interval / 3600)))
        tr = f'"{exe}" "{script}" update --quiet'
        log.info("[DryRun] 将执行：schtasks /Create /TN " + TASK_NAME + " /SC HOURLY /MO "
                 + str(hours) + " /RL HIGHEST /F /TR " + tr)
        return 0
    if not is_admin():
        log.err("注册 / 删除计划任务需要管理员权限（可加 --elevate）。")
        return 1
    if args.action == "uninstall":
        rc, out = run_cmd(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=20)
        if rc == 0:
            log.ok(f"已删除计划任务 {TASK_NAME}。")
            return 0
        log.err(f"删除失败：{out.strip()}")
        return 1

    script = str(Path(__file__).resolve())
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw) if pythonw.is_file() else sys.executable
    hours = max(1, int(round(args.interval / 3600)))
    tr = f'"{exe}" "{script}" update --quiet'
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", str(hours),
           "/RL", "HIGHEST", "/F", "/TR", tr]
    if args.dry_run:
        log.info("[DryRun] 将执行：" + " ".join(cmd))
        return 0
    rc, out = run_cmd(cmd, timeout=30)
    if rc == 0:
        log.ok(f"已注册计划任务 {TASK_NAME}：每 {hours} 小时自动更新一次 hosts。")
        log.info(f"执行命令：{tr}")
        log.info("删除：python github520.py task uninstall")
        return 0
    log.err(f"注册失败：{out.strip()}")
    log.info("也可以手动创建计划任务，程序填：" + tr)
    return 1


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

EXAMPLES = """\
常用示例：
  python github520.py check                     # 体检：浏览器/代理/DoH/hosts/DNS/连通性
  python github520.py check --json              # 同上，输出 JSON 便于脚本处理
  python github520.py update --dry-run          # 预览将要写入的 IP 与延迟
  python github520.py update --elevate          # 提权并真正写入 hosts
  python github520.py update --no-optimize      # 不测速，直接用 GitHub520 源里的 IP
  python github520.py watch --interval 1800     # 常驻，每 30 分钟自检并按需更新
  python github520.py task install --interval 3600   # 注册计划任务（每小时）
  python github520.py status                    # 查看状态 / 备份 / 计划任务
  python github520.py restore --list            # 列出备份
  python github520.py restore                   # 从最近备份还原
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hosts-file", default=argparse.SUPPRESS, help="hosts 文件路径（默认系统 hosts）")
    common.add_argument("--backup-dir", default=argparse.SUPPRESS, help="备份目录")
    common.add_argument("--log-dir", default=argparse.SUPPRESS, help="日志目录")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="网络超时秒数（默认 15）")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="安静模式")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出调试信息")

    p = argparse.ArgumentParser(
        prog="github520.py", parents=[common],
        description="GitHub520 for Windows：检测浏览器到 GitHub 的连接状态，定期拉取最新可用 IP，自动更新 hosts。",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"github520.py {VERSION}")
    sub = p.add_subparsers(dest="command")

    up = sub.add_parser("update", parents=[common], help="拉取最新 IP 并更新 hosts")
    up.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    up.add_argument("--force", action="store_true", help="跳过连通性预检，强制更新")
    up.add_argument("--elevate", action="store_true", help="非管理员时自动请求 UAC 提权")
    up.add_argument("--no-optimize", action="store_true", help="不测速，直接使用数据源 IP")
    up.add_argument("--http-verify", dest="http_verify", action="store_true", default=True,
                    help="关键域名额外做 HTTP 层校验（默认开启）")
    up.add_argument("--no-http-verify", dest="http_verify", action="store_false",
                    help="关闭 HTTP 层校验，只做 TCP+TLS 校验")
    up.add_argument("--http-timeout", type=float, default=8.0, help="单次 HTTP 校验超时秒数（默认 8）")
    up.add_argument("--jobs", type=int, default=24, help="并发探测线程数（默认 24）")
    up.add_argument("--probe-timeout", type=float, default=3.0, help="单个 IP 探测超时秒数（默认 3）")
    up.add_argument("--keep-backups", type=int, default=10, help="保留最近 N 个备份（默认 10）")

    ck = sub.add_parser("check", parents=[common], help="检测浏览器到 GitHub 的连接状态")
    ck.add_argument("--proxy", default="auto", help="auto(默认,跟随系统代理) / none(强制直连) / http://ip:port")
    ck.add_argument("--json", action="store_true", help="额外输出 JSON 结果")

    w = sub.add_parser("watch", parents=[common], help="常驻监控，定期自动更新")
    w.add_argument("--interval", type=int, default=3600, help="检查间隔秒数（默认 3600）")
    w.add_argument("--once", action="store_true", help="只跑一轮（供计划任务调用）")
    w.add_argument("--force", action="store_true", help="每轮都强制更新")
    w.add_argument("--elevate", action="store_true", help="非管理员时自动请求 UAC 提权")
    w.add_argument("--no-optimize", action="store_true", help="不测速")
    w.add_argument("--http-verify", dest="http_verify", action="store_true", default=True)
    w.add_argument("--no-http-verify", dest="http_verify", action="store_false")
    w.add_argument("--http-timeout", type=float, default=8.0)
    w.add_argument("--jobs", type=int, default=24)
    w.add_argument("--probe-timeout", type=float, default=3.0)
    w.add_argument("--keep-backups", type=int, default=10)

    sub.add_parser("status", parents=[common], help="查看当前状态")

    rs = sub.add_parser("restore", parents=[common], help="从备份还原 hosts")
    rs.add_argument("--list", action="store_true", help="只列出备份")
    rs.add_argument("--backup", help="指定备份文件")
    rs.add_argument("--elevate", action="store_true", help="非管理员时自动请求 UAC 提权")
    rs.add_argument("--keep-backups", type=int, default=10)

    tk = sub.add_parser("task", parents=[common], help="注册 / 删除 / 查看计划任务")
    tk.add_argument("action", nargs="?", default="status", choices=["install", "uninstall", "status"])
    tk.add_argument("--interval", type=int, default=3600, help="自动更新间隔秒数（默认 3600）")
    tk.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    tk.add_argument("--elevate", action="store_true", help="非管理员时自动请求 UAC 提权")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    args.hosts_file = getattr(args, "hosts_file", None) or str(DEFAULT_HOSTS)
    args.backup_dir = getattr(args, "backup_dir", None) or str(SCRIPT_DIR / "backup")
    args.log_dir = getattr(args, "log_dir", None) or str(SCRIPT_DIR / "logs")
    args.timeout = float(getattr(args, "timeout", None) or 15.0)
    args.keep_backups = getattr(args, "keep_backups", 10)
    args.dry_run = getattr(args, "dry_run", False)
    args.http_verify = getattr(args, "http_verify", True)
    args.http_timeout = float(getattr(args, "http_timeout", None) or 8.0)
    log = Log(Path(args.log_dir), quiet=getattr(args, "quiet", False),
              verbose=getattr(args, "verbose", False))

    command = args.command or "check"
    log.step(f"github520.py {VERSION} | {command} | hosts={args.hosts_file}")

    if command == "check":
        return cmd_check(args, log)
    if command == "update":
        return cmd_update(args, log)
    if command == "watch":
        return cmd_watch(args, log)
    if command == "status":
        return cmd_status(args, log)
    if command == "restore":
        if getattr(args, "elevate", False) and not is_admin():
            elevate_and_exit(sys.argv[1:], log)
            return 0
        return cmd_restore(args, log)
    if command == "task":
        if getattr(args, "elevate", False) and not is_admin():
            elevate_and_exit(sys.argv[1:], log)
            return 0
        return cmd_task(args, log)
    parser.print_help()
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
