#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台任务监控：让「进度查询」看到 Claude 启动的真实后台进程。

Claude 控制台本身通过 SDK 启动 ``claude`` CLI 子进程，所有工具命令都是它的
后代进程。本模块只依赖 Linux 的 ``/proc``，不引入额外依赖：

* 找到 ``claude`` 进程的直接子进程，按任务根进程聚合整棵子树；
* 读取进程已运行时长、CPU、读写字节数；
* 从进程打开的重定向日志里解析真实进度（ninja ``[N/M]``、wget/curl 百分比、
  git Receiving objects、CPack 阶段等），并据此计算百分比与预计剩余时间；
* 采样 ``/proc/net/dev`` 得到本机实时网速。

``snapshot()`` 是线程安全且带缓存的：1 秒内重复调用返回同一份结果。
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from collections import defaultdict

_PROC = "/proc"
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SPACES_RE = re.compile(r"\s+")

_LOCK = threading.Lock()
_CACHE = {"ts": 0.0, "snapshot": None}
_CACHE_INTERVAL = 0.6

# 进程状态采样：{root_pid: (ts, cpu_ticks, rchar, wchar, read_bytes, write_bytes)}
_PREV_GROUP: dict[int, tuple] = {}
# 日志进度采样：{log_path: (ts, current)}
_PREV_PROGRESS: dict[str, tuple] = {}
# 网络采样：(ts, rx_bytes, tx_bytes)
_PREV_NET: tuple | None = None

_KIND_RULES = (
    ("编译", {
        "ninja", "cmake", "make", "gmake", "cc1plus", "cc1", "gcc", "g++",
        "c++", "ld", "ld.bfd", "clang", "clang++", "rustc", "cargo", "meson",
        "moc", "uic", "rcc", "windres",
    }),
    ("打包", {
        "cpack", "dpkg-deb", "dpkg", "fakeroot", "makepkg", "rpmbuild",
        "checkinstall", "appimagetool", "linuxdeploy",
    }),
    ("下载/安装", {
        "wget", "curl", "aria2c", "pip", "pip3", "npm", "npx", "yarn",
        "pnpm", "apt", "apt-get", "aptitude", "git", "svn", "scp", "rsync",
        "snap", "flatpak", "conda", "mamba",
    }),
    ("解压/压缩", {
        "tar", "xz", "gzip", "zstd", "unzip", "zip", "7z", "7za", "7zr",
        "pigz", "bzip2",
    }),
)
_NAME_KIND: dict[str, str] = {}
for _kind, _names in _KIND_RULES:
    for _name in _names:
        _NAME_KIND[_name] = _kind

_DOWNLOAD_TOOLS = {
    "wget", "curl", "aria2c", "pip", "pip3", "npm", "npx", "yarn", "pnpm",
    "apt", "apt-get", "aptitude", "git", "svn", "scp", "rsync", "snap",
    "flatpak", "conda", "mamba",
}
_BUILD_TOOLS = {n for n in _NAME_KIND if _NAME_KIND[n] == "编译"}
_PACKAGE_TOOLS = {n for n in _NAME_KIND if _NAME_KIND[n] == "打包"}

# 这些根进程一般是常驻的 MCP/插件服务，不是用户关心的后台任务
_IDLE_SERVER_HINTS = ("mcp", "/server.js", "/server.py", "language-server",
                      "typescript-language-server", "pyright", "gopls")

_MAX_LOG_BYTES = 128 * 1024
_MAX_DETAIL = 140


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
def format_duration(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def format_clock(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_bytes(value) -> str:
    value = float(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return f"{value:.0f} {units[index]}"
    return f"{value:.1f} {units[index]}"


def format_speed(bps) -> str:
    if bps is None:
        return "—"
    return f"{format_bytes(bps)}/s"


def _split_size(number: str, unit: str) -> int:
    try:
        value = float(str(number).replace(",", ""))
    except ValueError:
        return 0
    factor = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
              "T": 1024 ** 4}.get((unit or "")[:1].upper(), 1)
    return int(value * factor)


def _parse_eta(text) -> float | None:
    """解析 wget/curl 的 eta（如 5s、1m23s、--:--）为秒。"""
    text = str(text or "").strip()
    if not text or set(text) <= {"-", ":", " ", "?"}:
        return None
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if not match:
        return None
    hours, minutes, secs = match.groups()
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(secs or 0)
    return total or None


# --------------------------------------------------------------------------- #
# /proc 读取
# --------------------------------------------------------------------------- #
def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8", "replace")
    except (OSError, PermissionError):
        return ""


def _read_stat(pid: int) -> dict | None:
    raw = _read_text(f"{_PROC}/{pid}/stat")
    if not raw:
        return None
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 0 or right < 0:
        return None
    comm = raw[left + 1:right]
    fields = raw[right + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return {
            "pid": pid,
            "comm": comm,
            "state": fields[0],
            "ppid": int(fields[1]),
            "utime": int(fields[11]),
            "stime": int(fields[12]),
            "starttime": int(fields[19]),
        }
    except (ValueError, IndexError):
        return None


def _read_cmdline(pid: int) -> str:
    raw = _read_text(f"{_PROC}/{pid}/cmdline")
    if not raw:
        return ""
    return " ".join(part for part in raw.split("\0") if part).strip()


def _read_io(pid: int) -> tuple[int, int, int, int]:
    """返回 (rchar, wchar, read_bytes, write_bytes)。"""
    values = {}
    for line in _read_text(f"{_PROC}/{pid}/io").splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            try:
                values[key.strip()] = int(value.strip())
            except ValueError:
                pass
    return (values.get("rchar", 0), values.get("wchar", 0),
            values.get("read_bytes", 0), values.get("write_bytes", 0))


def _proc_uptime() -> float:
    raw = _read_text(f"{_PROC}/uptime")
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return time.monotonic()


def _collect_processes() -> dict[int, dict]:
    procs: dict[int, dict] = {}
    try:
        names = os.listdir(_PROC)
    except OSError:
        return procs
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        stat = _read_stat(pid)
        if stat is None:
            continue
        stat["cmdline"] = _read_cmdline(pid)
        procs[pid] = stat
    return procs


def _descendants(procs: dict[int, dict], root_pid: int) -> list[int]:
    children: dict[int, list[int]] = defaultdict(list)
    for pid, info in procs.items():
        children[info["ppid"]].append(pid)
    result: list[int] = []
    stack = list(children.get(root_pid, ()))
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        stack.extend(children.get(pid, ()))
    return result


def _is_claude_process(info: dict) -> bool:
    if info.get("comm") == "claude":
        return True
    argv0 = (info.get("cmdline") or "").split(" ", 1)[0]
    return os.path.basename(argv0) == "claude"


def _looks_like_idle_server(info: dict) -> bool:
    cmdline = (info.get("cmdline") or "").lower()
    if info.get("comm") not in ("node", "python", "python3"):
        return False
    return any(hint in cmdline for hint in _IDLE_SERVER_HINTS)


def _process_name(info: dict) -> str:
    argv0 = (info.get("cmdline") or "").split(" ", 1)[0]
    return os.path.basename(argv0) or info.get("comm") or "?"


def _kind_for(*infos: dict) -> str | None:
    """只按可执行文件名判断，避免把 shell 脚本正文里的单词误当成工具名。"""
    for info in infos:
        name = _process_name(info)
        if name in _NAME_KIND:
            return _NAME_KIND[name]
        cmdline = (info.get("cmdline") or "").lower()
        # python3 -m pip / node .../npm-cli.js 这类间接调用
        if re.search(r"(?:^|\s)-m\s+(pip\d?)\b", cmdline):
            return "下载/安装"
        if re.search(r"(?:^|[\s/])(npm-cli\.js|yarn\.js|pnpm\.cjs)\b", cmdline):
            return "下载/安装"
    return None


# --------------------------------------------------------------------------- #
# 日志进度解析
# --------------------------------------------------------------------------- #
def _tail_log(path: str) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > _MAX_LOG_BYTES:
                handle.seek(size - _MAX_LOG_BYTES)
            data = handle.read()
        return data.decode("utf-8", "replace")
    except (OSError, PermissionError):
        return ""


def _open_logs(pid: int, now: float) -> list[tuple[str, float, int]]:
    """返回进程打开的、最近仍在写入的普通文件：(路径, mtime, 大小)。"""
    found = []
    fd_dir = f"{_PROC}/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except (OSError, PermissionError):
        return found
    for fd in fds:
        try:
            target = os.readlink(f"{fd_dir}/{fd}")
        except OSError:
            continue
        if not target.startswith("/") or target.startswith("/dev/"):
            continue
        try:
            stat = os.stat(target)
        except OSError:
            continue
        if not os.path.isfile(target) or stat.st_size <= 0:
            continue
        if now - stat.st_mtime > 180:
            continue
        found.append((target, stat.st_mtime, stat.st_size))
    return found


def _parse_progress(text: str, elapsed: float, now: float,
                    log_path: str) -> dict:
    clean = _ANSI_RE.sub("", text)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    tail = "\n".join(lines[-160:])
    result = {
        "percent": None,
        "current": None,
        "total": None,
        "stage": "",
        "detail": _SPACES_RE.sub(" ", lines[-1])[:_MAX_DETAIL] if lines else "",
        "eta": None,
        "speed_bps": None,
        "bytes_done": None,
        "bytes_total": None,
        "log": log_path,
    }
    if not tail:
        return result

    # CPack：阶段比 [N/M] 更能反映打包的真实进度，优先处理。
    if "CPack" in tail or "CPACK" in tail:
        lower = tail.lower()
        if re.search(r"package:.*generated", lower):
            result.update(percent=100, stage="安装包已生成", eta=0)
        elif "create package" in lower:
            result.update(percent=92, stage="生成安装包")
        elif "dependenc" in lower:
            result.update(percent=86, stage="分析运行库依赖")
        elif re.search(r"\binstall", lower):
            result.update(percent=72, stage="部署安装树")
        else:
            result.update(percent=60, stage="CPack 打包")
        return result

    # wget 进度条（日志里会刷新很多行，取最后一条，不能只取第一处）
    matches = re.findall(
        r"(\d{1,3})%\s*\[[^\]]*\]\s*([\d.,]+)\s*([KMG]?)\s*"
        r"([\d.,]+)\s*([KMG]?B/s)\s*(?:eta\s+(\S+))?", tail)
    if matches:
        percent, done, unit, speed, speed_unit, eta_text = matches[-1]
        result.update(
            percent=int(percent),
            bytes_done=_split_size(done, unit),
            speed_bps=_split_size(speed, speed_unit),
            eta=_parse_eta(eta_text),
            stage="下载",
            detail=_SPACES_RE.sub(" ", lines[-1])[:_MAX_DETAIL],
        )
        return result

    # git 克隆/拉取
    match = re.search(
        r"(Receiving objects|Resolving deltas|Counting objects|"
        r"Compressing objects|Writing objects)\s*:?\s*(\d+)%\s*"
        r"\((\d+)/(\d+)\)(?:,\s*([\d.]+)\s*([KMGT]?iB)\s*"
        r"\|\s*([\d.]+)\s*([KMGT]?iB/s))?", tail)
    if match:
        (stage, percent, done, total, done_size, done_unit,
         speed, speed_unit) = match.groups()
        result.update(
            percent=int(percent), current=int(done), total=int(total),
            bytes_done=_split_size(done_size, done_unit) if done_size else None,
            speed_bps=_split_size(speed, speed_unit) if speed else None,
            stage=stage.replace(" objects", "").replace(" deltas", ""),
        )
        return result

    # curl / 通用下载百分比
    match = re.findall(r"(?<![\w.])(\d{1,3})%", tail)
    if match and re.search(r"(B/s|eta|Time:|Download|Upload|Total)", tail):
        percent = int(match[-1])
        result.update(percent=percent, stage="下载",
                      detail=_SPACES_RE.sub(" ", lines[-1])[:_MAX_DETAIL])
        return result

    # apt / dpkg
    match = re.search(r"Progress:\s*\[\s*(\d+)%\]", tail)
    if match:
        result.update(percent=int(match.group(1)), stage="安装软件包")
        return result
    match = re.search(r"\((\d+)%\)", tail)
    if match:
        result.update(percent=int(match.group(1)), stage="解包/安装")
        return result

    # ninja / make 的 [当前/总数]
    matches = list(re.finditer(r"\[(\d+)/(\d+)\]", clean))
    if matches:
        current, total = (int(part) for part in matches[-1].groups())
        if total > 0:
            percent = min(100, int(current * 100 / total))
            eta = None
            previous = _PREV_PROGRESS.get(log_path)
            rate = None
            if previous and now > previous[0] + 0.2 and current >= previous[1]:
                rate = (current - previous[1]) / (now - previous[0])
            elif elapsed > 0 and current > 0:
                rate = current / elapsed
            if rate and rate > 0:
                eta = max(0.0, (total - current) / rate)
            _PREV_PROGRESS[log_path] = (now, current)
            result.update(
                percent=percent, current=current, total=total, eta=eta,
                stage="构建",
                detail=_SPACES_RE.sub(" ", lines[-1])[:_MAX_DETAIL],
            )
            return result

    # pip 下载：只有已下载体积，没有总大小
    match = re.search(r"Downloading\s+\S+.*?\(([\d.]+)\s*([KMGT]?B)\)", tail)
    if match:
        result.update(bytes_done=_split_size(match.group(1), match.group(2)),
                      stage="下载")
        return result

    return result


def _pick_log(group: list[dict], now: float) -> str | None:
    """从任务组里挑一个最近写入、体积最大的重定向日志。"""
    candidates: list[tuple[float, int, str]] = []
    for info in group:
        for path, mtime, size in _open_logs(info["pid"], now):
            if path.endswith((".log", ".txt", ".out", ".err")):
                mtime += 1.0  # 轻微优先
            candidates.append((mtime, size, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
def _read_net():
    if not os.path.isdir(_PROC):
        return None
    rx_total = tx_total = 0
    for line in _read_text(f"{_PROC}/net/dev").splitlines():
        if ":" not in line:
            continue
        iface, _, payload = line.partition(":")
        if iface.strip() == "lo":
            continue
        fields = payload.split()
        if len(fields) < 16:
            continue
        try:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
        except ValueError:
            pass
    return rx_total, tx_total


def _network_snapshot(now: float) -> dict:
    global _PREV_NET
    raw = _read_net()
    if raw is None:
        # 非 Linux 平台没有 /proc：界面上显示「—」而不是 0 B/s
        _PREV_NET = None
        return {"rx_bps": None, "tx_bps": None}
    rx, tx = raw
    rx_bps = tx_bps = None
    if _PREV_NET is not None:
        prev_ts, prev_rx, prev_tx = _PREV_NET
        delta_t = now - prev_ts
        if delta_t > 0.05:
            rx_bps = max(0.0, (rx - prev_rx) / delta_t)
            tx_bps = max(0.0, (tx - prev_tx) / delta_t)
    _PREV_NET = (now, rx, tx)
    return {"rx_bps": rx_bps, "tx_bps": tx_bps}


# --------------------------------------------------------------------------- #
# 主快照
# --------------------------------------------------------------------------- #
def _build_tasks(procs: dict[int, dict], claude_pids: set[int],
                 now: float, uptime: float) -> list[dict]:
    children: dict[int, list[int]] = defaultdict(list)
    for pid, info in procs.items():
        children[info["ppid"]].append(pid)

    roots: list[int] = []
    for claude_pid in claude_pids:
        for pid in children.get(claude_pid, ()):
            info = procs.get(pid)
            if info is None or pid in claude_pids:
                continue
            if _looks_like_idle_server(info):
                continue
            roots.append(pid)

    tasks: list[dict] = []
    for root in roots:
        group_pids = [root] + [
            pid for pid in _descendants(procs, root) if pid not in claude_pids
        ]
        group = [procs[pid] for pid in group_pids if pid in procs]
        if not group:
            continue
        root_info = procs.get(root) or group[0]
        elapsed = max(0.0, uptime - root_info["starttime"] / _CLK_TCK)

        ticks = 0
        rchar = wchar = read_bytes = write_bytes = 0
        for info in group:
            ticks += info["utime"] + info["stime"]
            io_values = _read_io(info["pid"])
            rchar += io_values[0]
            wchar += io_values[1]
            read_bytes += io_values[2]
            write_bytes += io_values[3]

        previous = _PREV_GROUP.get(root)
        cpu_percent = None
        if previous and now > previous[0] + 0.2:
            delta_t = now - previous[0]
            cpu_percent = max(0.0, (ticks - previous[1]) / _CLK_TCK / delta_t * 100)
            read_rate = max(0.0, (rchar - previous[2]) / delta_t)
            write_rate = max(0.0, (wchar - previous[3]) / delta_t)
        else:
            read_rate = write_rate = None
            if elapsed > 0.5:
                cpu_percent = ticks / _CLK_TCK / elapsed * 100
        _PREV_GROUP[root] = (now, ticks, rchar, wchar, read_bytes, write_bytes)

        primary = max(group, key=lambda item: item["utime"] + item["stime"])
        kind = _kind_for(primary, root_info, *group)
        name = _process_name(primary)
        cmdline = _SPACES_RE.sub(" ", primary.get("cmdline") or "")[:160]

        log_path = _pick_log(group, now)
        progress = _parse_progress(_tail_log(log_path), elapsed, now,
                                   log_path) if log_path else {
            "percent": None, "current": None, "total": None, "stage": "",
            "detail": "", "eta": None, "speed_bps": None,
            "bytes_done": None, "bytes_total": None, "log": None}

        # shell 包裹的任务（如 bash -c 'ninja ...'）没有工具名可判，
        # 用日志解析出的阶段兜底，让标签仍是"编译/打包/下载"。
        if kind is None:
            stage = str(progress.get("stage") or "")
            if stage == "构建":
                kind = "编译"
            elif stage == "下载":
                kind = "下载/安装"
            elif "安装包" in stage or "打包" in stage:
                kind = "打包"
            elif "安装" in stage or "解包" in stage:
                kind = "下载/安装"

        interesting = bool(
            kind or progress.get("percent") is not None
            or (log_path and progress.get("detail"))
            or (cpu_percent is not None and cpu_percent >= 1.0)
        )
        if not interesting:
            continue

        if progress.get("speed_bps") is None and read_rate:
            # 下载类工具用读字节速度兜底（wget/curl 的进度常写入管道）
            if kind == "下载/安装" and read_rate > 1024:
                progress["speed_bps"] = read_rate
        if kind == "下载/安装" and progress.get("bytes_done") is None \
                and rchar > 0 and elapsed > 1:
            progress["bytes_done"] = rchar
        if progress.get("detail") and log_path:
            pass

        label = f"{kind} · {name}" if kind else name
        tasks.append({
            "root_pid": root,
            "pid": primary["pid"],
            "name": name,
            "cmdline": cmdline,
            "kind": kind or "执行",
            "label": label,
            "elapsed": elapsed,
            "cpu_percent": cpu_percent,
            "read_rate": read_rate,
            "write_rate": write_rate,
            "progress": progress,
        })

    # 有真实百分比的排前面，其次按 CPU
    tasks.sort(key=lambda item: (
        item["progress"].get("percent") is None,
        -(item.get("cpu_percent") or 0.0),
    ))
    return tasks


def _prune_state(now: float) -> None:
    for key in list(_PREV_GROUP):
        if now - _PREV_GROUP[key][0] > 120:
            _PREV_GROUP.pop(key, None)
    for key in list(_PREV_PROGRESS):
        if now - _PREV_PROGRESS[key][0] > 600:
            _PREV_PROGRESS.pop(key, None)


def snapshot(root_pid: int | None = None, force: bool = False,
             session_pids: set[int] | None = None) -> dict:
    """返回后台任务与网速快照（1 秒内复用缓存，避免重复采样）。

    ``session_pids`` 仅用于测试/嵌入：直接指定哪些进程算作“会话进程”
    （默认自动识别 claude CLI 进程）；指定时不做缓存。
    """
    global _CACHE
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get("snapshot")
        if (session_pids is None and not force and cached is not None
                and now - _CACHE.get("ts", 0.0) < _CACHE_INTERVAL):
            return cached

        root_pid = root_pid or os.getpid()
        procs = _collect_processes()
        uptime = _proc_uptime()
        descendants = _descendants(procs, root_pid)
        if session_pids is None:
            claude_pids = {
                pid for pid in descendants
                if pid in procs and _is_claude_process(procs[pid])
            }
        else:
            claude_pids = {pid for pid in session_pids if pid in procs}
        tasks = _build_tasks(procs, claude_pids, now, uptime)
        network = _network_snapshot(now)
        _prune_state(now)

        result = {
            "tasks": tasks,
            "network": network,
            "claude_count": len(claude_pids),
            "updated": time.time(),
        }
        _CACHE = {"ts": now, "snapshot": result}
        return result


# --------------------------------------------------------------------------- #
# HTML 渲染（供进度浮窗直接使用）
# --------------------------------------------------------------------------- #
def render_task_html(task: dict) -> str:
    progress = task.get("progress") or {}
    parts = [
        f"<b>{html.escape(str(task.get('label') or '后台任务'))}</b>"
        f" · 已运行 {format_clock(task.get('elapsed'))}"
    ]
    cpu = task.get("cpu_percent")
    if cpu is not None and cpu >= 0.5:
        parts[-1] += f" · CPU {cpu:.0f}%"

    percent = progress.get("percent")
    if percent is not None:
        line = f"进度：{percent}%"
        if progress.get("current") is not None and progress.get("total"):
            line += f"（{progress['current']}/{progress['total']}）"
        elif progress.get("bytes_done") is not None:
            line += f" · {format_bytes(progress['bytes_done'])}"
        if progress.get("speed_bps"):
            line += f" · {format_speed(progress['speed_bps'])}"
        eta = progress.get("eta")
        if eta is not None:
            line += f" · 预计剩余 {format_duration(eta)}"
        parts.append(line)
    elif progress.get("bytes_done"):
        line = f"已传输：{format_bytes(progress['bytes_done'])}"
        if progress.get("speed_bps"):
            line += f" · {format_speed(progress['speed_bps'])}"
        parts.append(line)
    elif task.get("read_rate"):
        parts.append(f"读取速率：{format_bytes(task['read_rate'])}/s")

    if progress.get("stage") and progress.get("percent") is None:
        parts.append(f"阶段：{html.escape(str(progress['stage']))}")
    detail = str(progress.get("detail") or "").strip()
    if detail:
        parts.append(f"当前：{html.escape(detail[:120])}")
    if progress.get("log"):
        parts.append(f"日志：{html.escape(str(progress['log']))}")
    return "<br>".join(parts)


def render_network_html(network: dict | None) -> str:
    network = network or {}
    return (f"网速（本机合计）：↓ {format_speed(network.get('rx_bps'))}"
            f"　↑ {format_speed(network.get('tx_bps'))}")


def render_markup(snapshot_data: dict, limit: int = 4) -> list[str]:
    """把快照渲染成若干行 HTML，供调用方与原有进度信息拼接。"""
    tasks = (snapshot_data or {}).get("tasks") or []
    network = (snapshot_data or {}).get("network") or {}
    lines: list[str] = []
    if tasks:
        lines.append("")
        lines.append(f"<b>后台任务</b>（{len(tasks)} 个）")
        for task in tasks[:limit]:
            lines.append(render_task_html(task))
        if len(tasks) > limit:
            lines.append(f"…另有 {len(tasks) - limit} 个后台任务")
    else:
        lines.append("")
        lines.append("<b>后台任务</b>：无")
    lines.append(render_network_html(network))
    return lines
