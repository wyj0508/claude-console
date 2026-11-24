#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台兼容层。

Windows 的 CreateProcess 不能直接执行 npm 安装的 ``claude.cmd`` /
``code.cmd`` 这类批处理 shim，也没有 ``xdg-open`` 和 bash 终端。这里把这些
平台差异收敛成几个小函数，供 main / dual_ai / agent_shared 共用。

其他平台（Linux / macOS）保持原有行为。
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

_BATCH_SUFFIXES = (".cmd", ".bat")
_DETACHED = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


# --------------------------------------------------------------------------- #
# 命令执行
# --------------------------------------------------------------------------- #
def is_batch(path) -> bool:
    """路径是否是 Windows 批处理脚本（需要 cmd.exe 才能执行）。"""
    return str(path or "").lower().endswith(_BATCH_SUFFIXES)


def comspec() -> str:
    return os.environ.get("COMSPEC") or "cmd.exe"


def wrap_argv(argv):
    """把 argv 变成当前平台能直接执行的命令。

    Windows 上 ``cmd`` 安装的 claude / code 都是 ``.cmd``，CreateProcess
    不会像 shell 那样解析 PATHEXT，必须显式交给 cmd.exe。
    """
    argv = [str(part) for part in argv]
    if IS_WINDOWS and argv and is_batch(argv[0]):
        return [comspec(), "/d", "/c", *argv]
    return argv


def popen(argv, **kwargs):
    """``subprocess.Popen`` 的跨平台版本（自动处理 .cmd）。"""
    return subprocess.Popen(wrap_argv(argv), **kwargs)


def _popen_detached(argv):
    """后台启动、不占用管道、Windows 上不弹控制台窗口。"""
    return subprocess.Popen(
        wrap_argv(argv), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=windowless_flags())


def run(argv, **kwargs):
    """``subprocess.run`` 的跨平台版本（自动处理 .cmd）。"""
    return subprocess.run(wrap_argv(argv), **kwargs)


def quote_arg(value) -> str:
    """按当前 shell 规则给单个参数加引号（用于拼终端命令行）。"""
    if IS_WINDOWS:
        return subprocess.list2cmdline([str(value)])
    return shlex.quote(str(value))


def kill_process_tree(proc, timeout: float = 4.0) -> None:
    """终止进程及其子进程；POSIX 下结束整个进程组。"""
    if proc is None:
        return
    if IS_WINDOWS:
        # terminate() 只结束 cmd.exe 包装层，必须用 taskkill 杀掉整棵树
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 文件管理器 / 编辑器 / 终端
# --------------------------------------------------------------------------- #
def open_path(path) -> bool:
    """用系统默认程序打开文件或目录。"""
    target = Path(path)
    try:
        if not target.exists():
            return False
    except OSError:
        return False
    try:
        if IS_WINDOWS:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        opener = ["open"] if IS_MACOS else ["xdg-open"]
        _popen_detached([*opener, str(target)])
        return True
    except OSError:
        return False


def find_editor() -> str | None:
    for name in ("code", "code.exe", "code.cmd"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_editor(path, line=None) -> bool:
    """用 VS Code 打开文件，可选跳转到指定行。"""
    editor = find_editor()
    if not editor:
        return False
    argv = ([editor, "-g", f"{path}:{line}"] if line
            else [editor, str(path)])
    try:
        _popen_detached(argv)
        return True
    except OSError:
        return False


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launch_terminal(cwd, command="", env=None) -> str:
    """在新终端窗口打开 cwd，可选执行 command，返回终端名称。"""
    cwd = str(cwd)
    env = env or os.environ.copy()
    if IS_WINDOWS:
        argv = [comspec(), "/d", "/k"] + ([command] if command else [])
        subprocess.Popen(
            argv, cwd=cwd, env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return "cmd"
    if IS_MACOS:
        script = f"cd {shlex.quote(cwd)}"
        if command:
            script += f" && {command}"
        subprocess.Popen([
            "/usr/bin/osascript",
            "-e", "tell application \"Terminal\" to do script " + _applescript_quote(script),
            "-e", "tell application \"Terminal\" to activate",
        ], **_DETACHED)
        return "Terminal"
    raise RuntimeError("未找到可用的终端模拟器")


# --------------------------------------------------------------------------- #
# 依赖发现
# --------------------------------------------------------------------------- #
def npm_candidate_paths(names) -> list[Path]:
    """Windows 上 node / npm 全局安装可执行文件的常见位置。"""
    roots = []
    appdata = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    if appdata:
        roots.append(Path(appdata) / "npm")
    if local:
        roots.append(Path(local) / "npm")
        roots.append(Path(local) / "Programs" / "nodejs")
    if program_files:
        roots.append(Path(program_files) / "nodejs")
    return [root / name for root in roots for name in names]


def windowless_flags() -> int:
    """创建子进程时不弹黑框（仅 Windows 有意义）。"""
    if IS_WINDOWS:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0
