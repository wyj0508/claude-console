#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双 AI 协作：Claude 服务商模型 + Codex 登录模型。

本模块只依赖标准库和 PyQt5，不导入 main.py，便于单独测试。

包含：
- Codex 登录模型发现（~/.codex/models_cache.json）
- 余额 / 额度查询（DeepSeek 余额、ChatGPT/Codex 限额）
- 两个命令行后端的流式调用（claude -p / codex exec）
- 主 AI（M）+ 执行 AI（W）的分工调度线程：
  上下文压缩 → 按档次和工作部件分配 → 串行执行 / 二次指导 → 按档次验收
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, pyqtSignal

import win_compat

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
CODEX_MODEL_CACHE = CODEX_HOME / "models_cache.json"
CODEX_AUTH = CODEX_HOME / "auth.json"


def _state_home() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg)
    if win_compat.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    if win_compat.IS_MACOS:
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".local" / "state"


RUN_STATE_ROOT = _state_home() / "claude-console/dual-runs"

CODEX_BIN_CANDIDATES = (
    "/usr/lib/chatgpt/resources/codex",
    str(Path.home() / ".codex/plugins/.plugin-appserver/codex"),
    "/usr/local/bin/codex",
    str(Path.home() / ".local/bin/codex"),
    str(Path.home() / ".npm-global/bin/codex"),
)
if win_compat.IS_WINDOWS:
    CODEX_BIN_CANDIDATES += tuple(
        str(p) for p in win_compat.npm_candidate_paths(("codex.exe", "codex.cmd")))

# 缓存里没有时的兜底；实际优先使用 ~/.codex/models_cache.json
FALLBACK_CODEX_MODELS = (
    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol",
     "supported_reasoning_levels": [{"effort": x} for x in
                                    ("low", "medium", "high", "xhigh", "max")]},
    {"slug": "gpt-6.1-terra", "display_name": "GPT-6.1 Terra",
     "supported_reasoning_levels": [{"effort": x} for x in
                                    ("low", "medium", "high", "xhigh", "max")]},
    {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra",
     "supported_reasoning_levels": [{"effort": x} for x in
                                    ("low", "medium", "high", "xhigh", "max")]},
)

MASTER = "master"
WORKER = "worker"


# --------------------------------------------------------------------------- #
# 工作部件与协作档次
# --------------------------------------------------------------------------- #
# 一次任务由这些工作部件组成；难度不同，按档次决定由主 AI 还是执行 AI 承担。
WORK_OPS = {
    "read": "读取",
    "locate": "定位",
    "search": "检索",
    "normalize": "上下文压缩",
    "think": "思考决策",
    "design": "方案设计",
    "execute": "执行修改",
    "monitor": "运行监控",
    "report": "输出汇报",
}
OP_ALIASES = {
    "read": "read", "读取": "read", "inspect": "read",
    "locate": "locate", "定位": "locate", "find": "locate",
    "search": "search", "检索": "search", "grep": "search",
    "normalize": "normalize", "compress": "normalize", "context": "normalize",
    "规范化": "normalize", "压缩": "normalize",
    "think": "think", "思考": "think", "reason": "think", "decide": "think",
    "design": "design", "设计": "design", "architect": "design",
    "diagnose": "think", "诊断": "think", "debug": "think",
    "execute": "execute", "执行": "execute", "edit": "execute", "write": "execute",
    "monitor": "monitor", "监控": "monitor", "verify": "monitor", "test": "monitor",
    "report": "report", "输出": "report", "summary": "report",
}
MEDIUM_OPS = ("normalize", "execute")
HARD_OPS = ("think", "design")


def normalize_ops(value, default=("execute",)):
    """把模型给出的工作部件归一为已知集合，未知项丢弃。"""
    if isinstance(value, str):
        value = [x for x in re.split(r"[,/+\s]+", value) if x]
    if not isinstance(value, (list, tuple)):
        return list(default)
    result = []
    for item in value:
        key = OP_ALIASES.get(str(item).strip().lower(), "")
        if key and key not in result:
            result.append(key)
    return result or list(default)


L0, L1, L2, L3, L4 = "L0", "L1", "L2", "L3", "L4"


@dataclass(frozen=True)
class TierPolicy:
    """一个协作档次：决定谁规划、谁执行、是否验收、能否中途询问。"""

    id: str
    label: str
    short: str
    summary: str
    solo_role: str = ""
    plan: bool = True
    plan_style: str = "standard"   # standard | contract | deep
    review: str = "full"           # full | on_failure | none
    precompress: bool = True
    max_asks: int = 1
    verbosity: str = "compact"     # minimal | compact | full

    def role_for(self, ops):
        """按工作部件难度分配角色；主 AI 只做高档工作，额度花在刀刃上。"""
        if self.solo_role:
            return self.solo_role
        ops = set(ops or ())
        if ops & set(HARD_OPS):
            return MASTER
        if self.id == L1 and ops & set(MEDIUM_OPS):
            return MASTER
        return WORKER


TIER_POLICIES = {
    L0: TierPolicy(
        L0, "全主档 · 最高", "全主",
        "所有读取、思考、执行、监控、输出都由主 AI 完成；只在最高难度或"
        "高风险、无法拆分的任务使用，最费主 AI 额度。",
        solo_role=MASTER, plan=False, review="none", precompress=False,
        max_asks=0, verbosity="full"),
    L1: TierPolicy(
        L1, "主控重档", "主控",
        "主 AI 负责规划、思考、设计、主要执行与验收；执行 AI 只承担读取、"
        "定位、检索、运行监控等简单工作，为主 AI 省去检索和跑命令的消耗。",
        plan_style="deep", review="full", precompress=True, max_asks=1,
        verbosity="minimal"),
    L2: TierPolicy(
        L2, "中高档 · 主从配合", "主从",
        "执行 AI 先读取并压缩有效上下文，主 AI 依据压缩结果给简洁指令；"
        "执行 AI 负责执行与监控，过程中可向主 AI 询问一次；主 AI 最终验收。",
        plan_style="standard", review="full", precompress=True, max_asks=1,
        verbosity="compact"),
    L3: TierPolicy(
        L3, "低档 · 从主导", "从主",
        "主 AI 只下发契约级指令；执行 AI 自主读取、定位、执行、监控并汇报，"
        "仅在失败或主动询问时由主 AI 二次指导；全部通过则不再调用主 AI 验收。",
        plan_style="contract", review="on_failure", precompress=True, max_asks=2,
        verbosity="full"),
    L4: TierPolicy(
        L4, "全从档 · 最低", "全从",
        "所有工作由执行 AI 独立完成，主 AI 不参与；用于简单、局部、"
        "容易验证的任务，最省主 AI 额度。",
        solo_role=WORKER, plan=False, review="none", precompress=False,
        max_asks=0, verbosity="full"),
}

TIER_ALIASES = {
    "l0": L0, "solo_master": L0, "master": L0, "all_master": L0,
    "l1": L1, "master_led": L1, "guided": L1, "high": L1,
    "l2": L2, "balanced": L2, "standard": L2, "middle": L2, "default": L2,
    "l3": L3, "worker_led": L3, "lean": L3, "low": L3,
    "l4": L4, "solo_worker": L4, "worker": L4, "all_worker": L4,
}


def normalize_tier(value, default=L2):
    """接受 L0–L4、旧名称或 0–100 的旧占比配置。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        share = max(0, min(100, int(value)))
        if share <= 0:
            return L4
        if share >= 100:
            return L0
        if share < 25:
            return L3
        if share < 75:
            return L2
        return L1
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in TIER_ALIASES:
        return TIER_ALIASES[text]
    if text.upper() in TIER_POLICIES:
        return text.upper()
    return default


def tier_policy(tier):
    return TIER_POLICIES[normalize_tier(tier)]


def tier_choices():
    """界面用：按从难到易返回档次。"""
    return [TIER_POLICIES[key] for key in (L0, L1, L2, L3, L4)]


def tier_target_share(tier):
    """仅用于界面上的粗略工作量提示，不参与调度。"""
    return {L0: 100, L1: 75, L2: 50, L3: 25, L4: 0}[normalize_tier(tier)]


# --------------------------------------------------------------------------- #
# 角色与模型
# --------------------------------------------------------------------------- #
@dataclass
class RoleSpec:
    source: str = "claude"       # claude | codex
    model: str = ""
    name: str = ""
    efforts: tuple = ()
    # None 表示旧配置未设置；空字符串表示使用模型默认值。
    effort: str | None = None

    @property
    def label(self):
        if self.name:
            return self.name
        return self.model

    def to_dict(self):
        return {"source": self.source, "model": self.model, "name": self.name,
                "effort": self.effort}

    @classmethod
    def from_dict(cls, data, default_source="claude"):
        if not isinstance(data, dict):
            return cls(source=default_source)
        source = str(data.get("source") or default_source)
        model = str(data.get("model") or "")
        effort = data.get("effort")
        return cls(source=source, model=model, name=str(data.get("name") or ""),
                   effort=str(effort).strip() if effort is not None else None)


def supported_efforts(spec):
    """模型报告的档位与对应 CLI 能力取交集，未知能力不猜测。"""
    levels = tuple(dict.fromkeys(str(x) for x in spec.efforts if x))
    if spec.source == "claude":
        levels = tuple(x for x in levels
                       if x in ("low", "medium", "high", "xhigh", "max"))
    return levels


def find_codex_binary():
    env_bin = os.environ.get("CODEX_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    found = shutil.which("codex")
    if found:
        return found
    if win_compat.IS_WINDOWS:
        exe = shutil.which("codex.exe")
        if exe:
            return exe
    for candidate in CODEX_BIN_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    # ChatGPT 桌面版的运行时目录也带 codex
    for pattern in ("/usr/lib/chatgpt/resources/codex",
                    str(Path.home() / ".cache/codex-runtimes/*/**/codex")):
        import glob
        for match in glob.glob(pattern, recursive=True):
            path = Path(match)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return ""


def load_codex_models():
    """从 Codex 的模型缓存读取 ChatGPT 登录可用的模型。"""
    payload = None
    try:
        payload = json.loads(CODEX_MODEL_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    items = []
    if isinstance(payload, dict):
        items = payload.get("models") or []
    if not isinstance(items, list) or not items:
        items = list(FALLBACK_CODEX_MODELS)
    models = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or item.get("id") or "").strip()
        if not slug:
            continue
        # 只显示列表可见、可通过 API 调用的模型
        if item.get("visibility") not in (None, "list"):
            continue
        if item.get("supported_in_api") is False:
            continue
        levels = []
        for level in item.get("supported_reasoning_levels") or []:
            if isinstance(level, dict):
                value = str(level.get("effort") or "").strip()
            else:
                value = str(level or "").strip()
            if value:
                levels.append(value)
        models.append({
            "source": "codex",
            "id": slug,
            "name": str(item.get("display_name") or slug),
            "efforts": levels,
            "default_effort": item.get("default_reasoning_level") or "",
        })
    if not models:
        models = [{
            "source": "codex",
            "id": str(item.get("slug") or item.get("id")),
            "name": str(item.get("display_name") or item.get("slug")),
            "efforts": ["low", "medium", "high", "xhigh", "max"],
        } for item in FALLBACK_CODEX_MODELS]
    return models


def default_codex_model():
    models = load_codex_models()
    for item in models:
        if item["id"] == "gpt-6.1-sol":
            return item
    return models[0] if models else None


# --------------------------------------------------------------------------- #
# 余额 / 额度
# --------------------------------------------------------------------------- #
def _http_json(url, headers, timeout=12, proxy=""):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"https": proxy, "http": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _fmt_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分"


def fetch_claude_balance(env):
    """查询 Claude 服务商余额。目前 DeepSeek 有稳定公开接口。"""
    base = str(env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
             or env.get("OPENAI_API_KEY") or "")
    proxy = str(env.get("HTTPS_PROXY") or env.get("https_proxy") or "")
    if not token:
        return {"ok": False, "text": "未配置服务商 Key", "detail": ""}
    if "deepseek.com" in base:
        url = "https://api.deepseek.com/user/balance"
        proxy = ""  # NO_PROXY 通常包含 deepseek.com，直连更稳
    else:
        root = base
        for suffix in ("/anthropic", "/v1"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]
        if not root:
            return {"ok": False, "text": "未配置服务商地址", "detail": ""}
        url = root + "/user/balance"
    try:
        data = _http_json(url, {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }, proxy=proxy)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "text": "余额不可用",
                "detail": f"HTTP {exc.code}：{url}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "余额不可用",
                "detail": f"{type(exc).__name__}: {exc}"}
    infos = data.get("balance_infos") if isinstance(data, dict) else None
    if isinstance(infos, list) and infos:
        info = infos[0] if isinstance(infos[0], dict) else {}
        currency = str(info.get("currency") or "")
        total = info.get("total_balance")
        granted = info.get("granted_balance")
        symbol = "¥" if currency.upper() == "CNY" else (currency + " ")
        text = f"余额 {symbol}{total}"
        detail = [f"币种：{currency}", f"总余额：{total}",
                  f"赠送：{granted}",
                  f"充值：{info.get('topped_up_balance')}",
                  f"可用：{data.get('is_available')}"]
        return {"ok": True, "text": text,
                "detail": "\n".join(str(x) for x in detail)}
    return {"ok": False, "text": "余额接口无数据", "detail": json.dumps(
        data, ensure_ascii=False)[:400]}


def fetch_codex_balance(env=None):
    """读取 ChatGPT 登录额度（5 小时窗口 / 每周窗口）。"""
    try:
        auth = json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "text": "未登录 Codex", "detail": ""}
    tokens = auth.get("tokens") if isinstance(auth, dict) else {}
    token = str((tokens or {}).get("access_token") or "")
    account = str((tokens or {}).get("account_id") or "")
    if not token:
        return {"ok": False, "text": "未登录 Codex", "detail": ""}
    env = env or {}
    proxy = (env.get("HTTPS_PROXY") or env.get("https_proxy")
             or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or "")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "codex-cli/console",
    }
    if account:
        headers["ChatGPT-Account-Id"] = account
    data = None
    last_error = ""
    for url in ("https://chatgpt.com/backend-api/wham/usage",
                "https://chatgpt.com/backend-api/codex/usage"):
        try:
            data = _http_json(url, headers, proxy=proxy)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return {"ok": False, "text": "额度不可用", "detail": last_error}
    plan = str(data.get("plan_type") or "").strip()
    plan_label = {"plus": "Plus", "pro": "Pro", "team": "Team",
                  "free": "Free"}.get(plan.lower(), plan.title() or "ChatGPT")
    rl = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    primary = rl.get("primary_window") if isinstance(
        rl.get("primary_window"), dict) else {}
    secondary = rl.get("secondary_window") if isinstance(
        rl.get("secondary_window"), dict) else {}
    parts = []
    detail = [f"套餐：{plan_label}"]
    if primary:
        used = primary.get("used_percent")
        parts.append(f"5h {used}%")
        detail.append(
            f"5 小时窗口：已用 {used}%，"
            f"{_fmt_duration(primary.get('reset_after_seconds'))}后重置")
    if secondary:
        used = secondary.get("used_percent")
        parts.append(f"周 {used}%")
        detail.append(
            f"每周窗口：已用 {used}%，"
            f"{_fmt_duration(secondary.get('reset_after_seconds'))}后重置")
    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    if credits:
        detail.append(
            f"额度：{credits.get('balance')}（无限：{credits.get('unlimited')}）")
    if not parts:
        parts.append("额度正常")
    if rl.get("limit_reached"):
        text = f"{plan_label} · 已达上限"
    else:
        text = f"{plan_label} · " + " · ".join(parts)
    return {"ok": True, "text": text, "detail": "\n".join(str(x) for x in detail)}


def fetch_balance(spec, claude_env=None):
    env = claude_env or {}
    if spec.source == "codex":
        return fetch_codex_balance(env)
    return fetch_claude_balance(env)


class BalanceWorker(QObject):
    """后台查询余额。用守护线程而不是 QThread，退出程序时不会因线程未结束而崩溃。"""

    ready = pyqtSignal(str, dict)
    done = pyqtSignal(object)

    def __init__(self, role, spec, claude_env=None, parent=None):
        super().__init__(parent)
        self.role = role
        self.spec = spec
        self.claude_env = dict(claude_env or {})

    def start(self):
        threading.Thread(
            target=self._run, name="dual-balance", daemon=True).start()

    def _run(self):
        try:
            result = fetch_balance(self.spec, self.claude_env)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "text": "查询失败",
                      "detail": f"{type(exc).__name__}: {exc}"}
        try:
            self.ready.emit(self.role, result)
            self.done.emit(self)
        except RuntimeError:
            # 窗口已经销毁
            pass


# --------------------------------------------------------------------------- #
# 命令行后端
# --------------------------------------------------------------------------- #
@dataclass
class AgentResult:
    ok: bool = False
    text: str = ""
    error: str = ""
    cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    session_id: str = ""
    returncode: int = 0
    elapsed: float = 0.0
    cancelled: bool = False


class CliAgent:
    """一次非交互式 agent 调用，支持流式回调与取消。"""

    def __init__(self, cwd, env=None, claude_path="", codex_path="",
                 cancel_event=None, timeout=900):
        self.cwd = cwd
        self.env = dict(env or os.environ.copy())
        self.claude_path = claude_path
        self.codex_path = codex_path
        self.cancel_event = cancel_event or threading.Event()
        self.timeout = timeout
        self._proc = None
        self._proc_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle --
    def cancel(self):
        self.cancel_event.set()
        self._kill()

    def _kill(self):
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return
        try:
            win_compat.kill_process_tree(proc, timeout=4)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _popen(self, cmd, stdin_text):
        env = self.env.copy()
        path = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        for binary in (self.claude_path, self.codex_path):
            if binary:
                bin_dir = str(Path(binary).parent)
                if bin_dir and bin_dir not in path.split(os.pathsep):
                    path = bin_dir + os.pathsep + path
        env["PATH"] = path
        proc = win_compat.popen(
            cmd, cwd=self.cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, start_new_session=True)
        with self._proc_lock:
            self._proc = proc
        def write_input():
            try:
                if stdin_text:
                    proc.stdin.write(stdin_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=write_input, daemon=True).start()
        return proc

    def _stream(self, proc, on_line):
        """逐行读取 stdout；stderr 另起线程收集，支持超时和取消。"""
        lines = queue.Queue()
        stderr_lines = []

        def pump_stdout():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        def pump_stderr():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip())
                    if len(stderr_lines) > 200:
                        del stderr_lines[:100]
            except Exception:  # noqa: BLE001
                pass

        stdout_thread = threading.Thread(target=pump_stdout, daemon=True)
        stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        started = time.monotonic()
        timed_out = False
        stdout_done = False
        while True:
            if self.cancel_event.is_set():
                self._kill()
                break
            if time.monotonic() - started > self.timeout:
                timed_out = True
                self._kill()
                break
            try:
                line = lines.get(timeout=0.4)
            except queue.Empty:
                if stdout_done and proc.poll() is not None:
                    break
                continue
            if line is None:
                stdout_done = True
                if proc.poll() is not None:
                    break
                continue
            try:
                on_line(line.rstrip())
            except Exception:
                self._kill()
                with self._proc_lock:
                    self._proc = None
                raise
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        stdout_thread.join(timeout=0.5)
        stderr_thread.join(timeout=0.5)
        for pipe, thread in ((proc.stdout, stdout_thread), (proc.stderr, stderr_thread)):
            if not thread.is_alive():
                pipe.close()
        with self._proc_lock:
            self._proc = None
        return proc.returncode, stderr_lines, timed_out

    # --------------------------------------------------------------- claude --
    def run_claude(self, prompt, spec, permission_mode="acceptEdits",
                   read_only=False, on_delta=None, on_message=None,
                   on_tool=None, session_id=""):
        if not self.claude_path:
            return AgentResult(error="找不到 claude 命令")
        mode = "plan" if read_only else (
            "bypassPermissions" if permission_mode == "bypassPermissions"
            else "acceptEdits")
        cmd = [self.claude_path, "-p", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages",
               "--permission-mode", mode]
        if spec.model:
            cmd += ["--model", spec.model]
        if session_id:
            cmd += ["--resume", session_id]
        effort = str(getattr(spec, "effort", "") or "").strip()
        if effort in ("low", "medium", "high", "xhigh", "max"):
            cmd += ["--effort", effort]

        result = AgentResult()
        last_text = ""
        started = time.monotonic()

        def handle(raw):
            nonlocal last_text
            line = raw.strip()
            if not line.startswith("{"):
                return
            try:
                event = json.loads(line)
            except ValueError:
                return
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                result.session_id = str(event.get("session_id") or "")
            if kind == "stream_event":
                inner = event.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        chunk = delta.get("text") or ""
                        if chunk and on_delta:
                            on_delta(chunk)
                return
            if kind == "assistant":
                message = event.get("message") or {}
                texts, tools = [], []
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        texts.append(block.get("text") or "")
                    elif block.get("type") == "tool_use":
                        tool = {"name": block.get("name") or "工具",
                                "input": block.get("input") or {}}
                        tools.append(tool)
                        result.tools.append(tool)
                        if on_tool:
                            on_tool(tool["name"], tool["input"])
                text = "\n".join(x for x in texts if x).strip()
                if text:
                    last_text = text
                    if on_message:
                        on_message(text, tools)
                return
            if kind == "result":
                result.session_id = str(event.get("session_id") or result.session_id)
                result.cost_usd += float(event.get("total_cost_usd") or 0)
                usage = event.get("usage") or {}
                if isinstance(usage, dict):
                    for key, value in usage.items():
                        if isinstance(value, (int, float)):
                            result.usage[key] = result.usage.get(key, 0) + value
                if event.get("is_error"):
                    result.error = str(event.get("result") or "Claude 返回错误")
                elif event.get("result"):
                    last_text = str(event.get("result"))

        try:
            proc = self._popen(cmd, prompt)
        except OSError as exc:
            result.error = f"启动 claude 失败：{exc}"
            result.elapsed = time.monotonic() - started
            return result
        rc, stderr_lines, timed_out = self._stream(proc, handle)
        result.returncode = rc or 0
        result.elapsed = time.monotonic() - started
        result.text = last_text.strip()
        result.cancelled = self.cancel_event.is_set()
        if timed_out:
            result.error = f"Claude 调用超时（{self.timeout} 秒）"
        elif result.cancelled:
            result.error = "已取消"
        elif rc != 0:
            result.error = "Claude 退出码 " + str(rc) + (
                "：" + "\n".join(stderr_lines[-8:]) if stderr_lines else "")
        result.ok = bool(result.text) and not result.error
        if result.text and not result.error:
            result.ok = True
        return result

    # ---------------------------------------------------------------- codex --
    def run_codex(self, prompt, spec, permission_mode="acceptEdits",
                  read_only=False, on_delta=None, on_message=None,
                  on_tool=None, session_id=""):
        if not self.codex_path:
            return AgentResult(error="找不到 codex 命令")
        # exec 的权限和 cwd 参数置于 resume 子命令之前；resume 不接受 -s/-C。
        cmd = [self.codex_path, "exec", "--ignore-user-config", "-C", self.cwd]
        if read_only:
            cmd += ["--sandbox", "read-only"]
        elif permission_mode == "bypassPermissions":
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            cmd += ["--sandbox", "workspace-write"]
        if session_id:
            cmd += ["resume"]
        cmd += ["--json", "--skip-git-repo-check"]
        if spec.model:
            cmd += ["-m", spec.model]
        effort = str(getattr(spec, "effort", "") or "").strip()
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        if session_id:
            cmd.append(session_id)
        cmd.append("-")

        result = AgentResult()
        last_text = ""
        emitted_items = set()
        started = time.monotonic()

        def handle(raw):
            nonlocal last_text
            line = raw.strip()
            if not line.startswith("{"):
                return
            try:
                event = json.loads(line)
            except ValueError:
                return
            kind = event.get("type")
            if kind == "thread.started":
                result.session_id = str(event.get("thread_id") or "")
                return
            if kind == "turn.failed":
                error = event.get("error") or {}
                result.error = str(error.get("message") or error) if isinstance(error, dict) else str(error)
                result.error = result.error or "Codex 执行失败"
                return
            if kind == "turn.completed":
                usage = event.get("usage") or {}
                if isinstance(usage, dict):
                    result.usage.update({
                        str(k): v for k, v in usage.items()
                        if isinstance(v, (int, float))})
                return
            if kind not in ("item.started", "item.updated", "item.completed"):
                return
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message" and kind == "item.completed":
                text = str(item.get("text") or "").strip()
                if text:
                    last_text = text
                    if on_message:
                        on_message(text, [])
                return
            if item_type in ("reasoning",):
                return
            if item_type in ("command_execution", "file_change",
                             "mcp_tool_call", "web_search"):
                name = {
                    "command_execution": "Bash",
                    "file_change": "Edit",
                    "mcp_tool_call": "MCP",
                    "web_search": "WebSearch",
                }.get(item_type, item_type)
                data = dict(item)
                item_id = str(item.get("id") or "")
                if kind == "item.started":
                    if item_id:
                        emitted_items.add(item_id)
                    result.tools.append({"name": name, "input": data})
                    if on_tool:
                        on_tool(name, data)
                elif kind == "item.completed" and item_id not in emitted_items:
                    # 没有 started 事件的工具调用，在完成时补显示一次
                    if item_id:
                        emitted_items.add(item_id)
                    result.tools.append({"name": name, "input": data})
                    if on_tool:
                        on_tool(name, data)
                return

        try:
            proc = self._popen(cmd, prompt)
        except OSError as exc:
            result.error = f"启动 codex 失败：{exc}"
            result.elapsed = time.monotonic() - started
            return result
        rc, stderr_lines, timed_out = self._stream(proc, handle)
        result.returncode = rc or 0
        result.elapsed = time.monotonic() - started
        result.text = last_text.strip()
        result.cancelled = self.cancel_event.is_set()
        if timed_out:
            result.error = f"Codex 调用超时（{self.timeout} 秒）"
        elif result.cancelled:
            result.error = "已取消"
        elif rc != 0:
            result.error = "Codex 退出码 " + str(rc) + (
                "：" + "\n".join(stderr_lines[-8:]) if stderr_lines else "")
        result.ok = bool(result.text) and not result.error
        if result.text and not result.error:
            result.ok = True
        return result

    def run(self, spec, prompt, permission_mode="acceptEdits", read_only=False,
            on_delta=None, on_message=None, on_tool=None, session_id=""):
        if spec.source == "codex":
            return self.run_codex(
                prompt, spec, permission_mode, read_only,
                on_delta, on_message, on_tool, session_id)
        return self.run_claude(
            prompt, spec, permission_mode, read_only,
            on_delta, on_message, on_tool, session_id)


RESULT_INSTRUCTION = """
交付协议：最后单独输出一行 DUAL_RESULT: 后接 JSON 对象：
DUAL_RESULT: {"status":"passed","summary":"结果概述","verification":["实际运行的验证及结果"]}
status 只能是 passed / blocked / failed。只有任务完成且必要验证通过才用 passed；
无法验证、权限不足、需要用户操作或有未解决问题时用 blocked 或 failed。
verification 必须是非空字符串列表，写真实执行证据；无需运行测试的任务说明实际检查方式。
"""


def delivery_report(result):
    """仅解析最后一行，不能用早先的 passed 覆盖后续失败。"""
    if not result.ok or result.cancelled or result.error:
        return None
    line = result.text.rstrip().splitlines()[-1] if result.text.strip() else ""
    if not line.startswith("DUAL_RESULT:"):
        return None
    try:
        report = json.loads(line[len("DUAL_RESULT:"):].strip())
    except ValueError:
        return None
    return report if valid_report(report) else None


def valid_report(report):
    if not isinstance(report, dict):
        return False
    evidence = report.get("verification")
    return (report.get("status") in ("passed", "blocked", "failed")
            and isinstance(report.get("summary"), str) and bool(report["summary"].strip())
            and isinstance(evidence, list) and bool(evidence)
            and all(isinstance(x, str) and x.strip() for x in evidence)
            and all(isinstance(report.get(key, []), list)
                    and all(isinstance(x, str) and x.strip() for x in report.get(key, []))
                    for key in ("changed_files", "decisions", "interfaces", "open_issues")))


def accepted_result(result):
    """CLI 成功仅表示调用成功；缺失交付协议时不标记任务通过。"""
    report = delivery_report(result)
    if report is not None and report["status"] == "passed":
        # 包含逐项报告时不能只相信顶层 passed。
        tasks = report.get("tasks")
        if tasks is None or (isinstance(tasks, list) and tasks
                             and all(valid_report(t) and t["status"] == "passed"
                                     and not t.get("open_issues") for t in tasks)):
            if not report.get("open_issues"):
                return True
    result.error = result.error or (
        str(report.get("summary")) if report else
        "缺少有效的结构化交付与验证结果，未确认通过")
    return False


class RunJournal:
    """宿主维护的只读交接源；原始内容与精简状态分开，永不截断 JSON。"""

    def __init__(self, cwd, request, context, previous_state=""):
        RUN_STATE_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="run-", dir=RUN_STATE_ROOT))
        self.write("input.json", {"cwd": cwd, "request": request, "context": context})
        self.calls = 0
        self.previous = ""
        self.previous_error = ""
        if previous_state:
            try:
                previous_root = Path(previous_state)
                original = json.loads((previous_root / "input.json").read_text(encoding="utf-8"))
                previous = json.loads((previous_root / "state.json").read_text(encoding="utf-8"))
                if not isinstance(original, dict) or not isinstance(previous, dict):
                    raise ValueError("上轮交接记录格式损坏")
                if Path(original["cwd"]).resolve() != Path(cwd).resolve():
                    raise ValueError("上轮交接记录属于另一项目，已隔离")
                if previous.get("protocol_version") not in (2, 3):
                    raise ValueError("上轮交接记录版本不兼容")
                self.previous = self.write("previous.json", {
                    "source": str(previous_root), "plan": previous.get("plan", {}),
                    "records": previous.get("records", []),
                    "previous_checkpoint": previous.get("previous_checkpoint", ""),
                })
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.previous_error = f"上轮交接记录未载入：{exc}。请依据可见历史核实，不要假定已完成。"

    def write(self, name, value):
        path = self.root / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        return str(path)

    def call(self, role, label, result, resumed):
        self.calls += 1
        return self.write(f"call-{self.calls:03d}.json", {
            "role": role, "stage": label, "resumed": resumed,
            "session_id": result.session_id, "text": result.text,
            "error": result.error, "ok": result.ok, "usage": result.usage,
        })


# --------------------------------------------------------------------------- #
# 计划解析
# --------------------------------------------------------------------------- #
def extract_json_object(text):
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start:index + 1])
                    except ValueError:
                        return None
    return None


def _last_prefixed_json(text, prefix):
    """只解析最后一个非空行，防止旧询问覆盖后续的最终报告。"""
    if not text:
        return None
    line = text.rstrip().splitlines()[-1].strip()
    if not line.startswith(prefix):
        return None
    try:
        value = json.loads(line[len(prefix):].strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def extract_ask(result):
    """执行 AI 的中途询问：一条 DUAL_ASK 压缩行，随后停止等待指导。"""
    if not result or not result.ok or result.cancelled:
        return None
    value = _last_prefixed_json(result.text, "DUAL_ASK:")
    if value is None:
        return None
    ask = {
        "task": str(value.get("task") or "").strip(),
        "problem": str(value.get("problem") or value.get("question") or "").strip(),
        "need": str(value.get("need") or value.get("decision") or "").strip(),
        "tried": [str(x) for x in value.get("tried") or []
                  if str(x).strip()][:4] if isinstance(value.get("tried"), list) else [],
    }
    if not ask["problem"] and not ask["need"]:
        return None
    return ask


def extract_guide(result):
    """保留完整的结构化指导，不能把错误文本或截断内容当作指令。"""
    if not result or not result.ok or result.cancelled:
        return ""
    value = _last_prefixed_json(result.text, "DUAL_GUIDE:")
    if value is not None:
        parts = []
        for key in ("decision", "next", "avoid", "done_when"):
            item = value.get(key)
            if isinstance(item, list):
                item = "; ".join(str(x) for x in item if str(x).strip())
            if str(item or "").strip():
                parts.append(f"{key}: {str(item).strip()}")
        if parts:
            return "\n".join(parts)
    return ""


POINT_STEPS = (1, 2, 3, 5, 8)


def _as_int(value, default=1):
    """把模型给出的工作点归一到 1/2/3/5/8 这几档。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return min(POINT_STEPS, key=lambda step: abs(step - number))


def fallback_plan(user_request, tier="balanced"):
    """规划失败时的最小计划；不再为「比例」拆出重复验收任务。"""
    policy = tier_policy(tier)
    if policy.solo_role == MASTER:
        return {"summary": "全主档：主 AI 独立完成。", "tasks": [{
            "id": "T1", "title": "完成用户请求", "goal": user_request,
            "role": MASTER, "points": 8, "files": [], "ops": ["think", "execute"],
            "acceptance": "按用户要求完成并验证",
            "verify": "运行相关检查或测试",
        }]}
    if policy.solo_role == WORKER:
        return {"summary": "全从档：执行 AI 独立完成。", "tasks": [{
            "id": "T1", "title": "完成用户请求", "goal": user_request,
            "role": WORKER, "points": 8, "files": [], "ops": ["execute", "monitor"],
            "acceptance": "按用户要求完成并验证",
            "verify": "运行相关检查或测试",
        }]}
    if policy.id == L1:
        return {"summary": "自动拆分：主 AI 实现疑难，执行 AI 核验。", "tasks": [
            {"id": "T1", "title": "实现需求", "goal": user_request,
             "role": MASTER, "points": 8, "files": [], "ops": ["think", "execute"],
             "acceptance": "按用户要求完成", "verify": "运行相关检查或测试"},
            {"id": "T2", "title": "定位并核验改动", "goal": "读取 T1 的改动并运行验证",
             "role": WORKER, "points": 3, "files": [],
             "ops": ["read", "locate", "monitor"],
             "acceptance": "改动位置和验证结果明确", "verify": "复核 T1 的验证结果"},
        ]}
    return {"summary": "自动拆分：执行 AI 实现并监控，主 AI 验收。", "tasks": [
        {"id": "T1", "title": "实现需求", "goal": user_request,
         "role": WORKER, "points": 8, "files": [], "ops": ["execute", "monitor"],
         "acceptance": "按用户要求完成", "verify": "运行相关检查或测试"},
    ]}


def normalize_plan(raw, user_request, tier="balanced", review_enabled=True):
    tier = normalize_tier(tier)
    if not isinstance(raw, dict):
        raw = fallback_plan(user_request, tier)
    tasks = []
    raw_tasks = raw.get("tasks")
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in (MASTER, WORKER):
            role = WORKER
        title = str(item.get("title") or item.get("goal") or "").strip()
        goal = str(item.get("goal") or title).strip()
        if not title and not goal:
            continue
        files = item.get("files")
        if not isinstance(files, list):
            files = []
        tasks.append({
            "id": f"T{len(tasks) + 1}",
            "title": title or goal[:40],
            "goal": goal,
            "role": role,
            "points": _as_int(item.get("points"), 2),
            "files": [str(x) for x in files if str(x).strip()],
            "ops": normalize_ops(item.get("ops")),
            "acceptance": str(item.get("acceptance") or "按任务目标完成"),
            "verify": str(item.get("verify") or "运行相关检查或测试"),
        })
        if len(tasks) >= 8:
            break
    if not tasks:
        return normalize_plan(fallback_plan(user_request, tier), user_request, tier,
                              review_enabled=review_enabled)
    plan = {
        "summary": str(raw.get("summary") or "").strip(),
        "risks": str(raw.get("risks") or "").strip(),
        "tasks": tasks,
        "constraints": [x for x in raw.get("constraints", []) if isinstance(x, str)]
                       if isinstance(raw.get("constraints", []), list) else [],
    }
    apply_tier_policy(plan, tier, review_enabled=review_enabled)
    return plan


def apply_tier_policy(plan, tier, review_enabled=True):
    """按工作部件难度重新分配任务；不按比例凑工作量。"""
    tier = normalize_tier(tier)
    policy = tier_policy(tier)
    tasks = plan["tasks"]
    for task in tasks:
        task["ops"] = normalize_ops(task.get("ops"))
        task["role"] = policy.role_for(task["ops"])
    overhead = 0
    if policy.plan:
        overhead += 2
    if review_enabled and policy.review == "full":
        overhead += 2
    total = sum(task["points"] for task in tasks) + overhead or 1
    plan["tier"] = tier
    plan["tier_label"] = policy.label
    plan["tier_summary"] = policy.summary
    plan["overhead_points"] = overhead
    plan["master_points"] = overhead + sum(
        t["points"] for t in tasks if t["role"] == MASTER)
    plan["worker_points"] = total - plan["master_points"]
    plan["total_points"] = total
    # 占比只作为展示和粗略工作量提示，不参与分配。
    plan["target_share"] = tier_target_share(tier)
    plan["actual_share"] = round(plan["master_points"] / total * 100)


def rebalance_plan(plan, tier, review_enabled=True):
    """兼容旧调用名；现在只执行档次分配。"""
    apply_tier_policy(plan, tier, review_enabled=review_enabled)


# --------------------------------------------------------------------------- #
# 协作调度线程
# --------------------------------------------------------------------------- #
class DualRunThread(QThread):
    """主 AI + 执行 AI 的串行协作调度。"""

    status = pyqtSignal(str)
    note = pyqtSignal(str)
    plan_ready = pyqtSignal(dict)
    role_started = pyqtSignal(str, str)          # role, label
    delta = pyqtSignal(str, str)                 # role, chunk
    message = pyqtSignal(str, str, list)         # role, text, tools
    tool = pyqtSignal(str, str, dict)            # role, name, input
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    # 单次调用的硬上限；到点会终止子进程并在界面提示。
    PLAN_TIMEOUT = 300
    TASK_TIMEOUT = 900
    REVIEW_TIMEOUT = 900
    GUIDE_TIMEOUT = 180
    # 超过该长度才先让执行 AI 压缩上下文，避免为短上下文白付一次调用。
    CONTEXT_COMPRESS_THRESHOLD = 7000

    def __init__(self, parent=None, cwd="", request="", context="",
                 master=None, worker=None, tier="balanced", share=None,
                 strategy="complete", permission_mode="default", effort="high",
                 claude_path="", codex_path="", claude_env=None, previous_state="",
                 review_enabled=True):
        super().__init__(parent)
        self.cwd = cwd
        self.request = request
        self.context = context or ""
        self.master = master or RoleSpec()
        self.worker = worker or RoleSpec(source="codex")
        # share 仅用于读取旧配置；新调度只看档次。
        self.tier = normalize_tier(share if share is not None else tier)
        self.policy = tier_policy(self.tier)
        # 复查开关：关闭时跳过调度器的独立主 AI 验收；执行者自检、
        # 失败接管与任务结果判定不受影响。
        self.review_enabled = bool(review_enabled)
        self.share = tier_target_share(self.tier)
        # strategy 仅为兼容旧调用方保留，不再参与调度。
        self.permission_mode = permission_mode
        self.effort = effort or ""
        self.claude_path = claude_path
        self.codex_path = codex_path
        self.claude_env = dict(claude_env or os.environ.copy())
        self.previous_state = previous_state
        self._cancel = threading.Event()
        self._current = None
        self._current_lock = threading.Lock()
        self._stats = {
            MASTER: {"calls": 0, "cost": 0.0, "tokens": 0, "seconds": 0.0},
            WORKER: {"calls": 0, "cost": 0.0, "tokens": 0, "seconds": 0.0},
        }
        self._records = []
        self._review_ok = None
        self._work_points = {MASTER: 0, WORKER: 0}
        self._journal = None
        self._plan = {}
        self._sessions = {}
        self._last_call_path = ""
        self._session_resets = 0
        self._compressed_context = ""
        self._context_before = len(self.context)
        self._ask_count = 0
        self._prompt_chars = 0
        # 进度快照：工作线程写入，界面线程只读，供「进度查询」实时显示。
        self._status_lock = threading.Lock()
        self._status_started = time.monotonic()
        self._status_activity = self._status_started
        self._status_label = "准备协作…"
        self._status_role = ""
        self._status_task = ""
        self._status_last_tool = ""
        self._status_tool_count = 0
        self._status_permission = ""
        self._status_error = ""
        self._status_done = False

    @property
    def review_mode(self):
        """本轮到实际生效的复查策略；关闭开关后一律不再独立复查。"""
        return self.policy.review if self.review_enabled else "none"

    # --------------------------------------------------------------- cancel --
    def cancel(self):
        self._cancel.set()
        with self._current_lock:
            current = self._current
        if current is not None:
            current.cancel()

    # --------------------------------------------------------------- progress --
    def _status_touch(self, label="", role=None, task=None):
        """记录最近活动；界面「进度查询」据此判断是否真的在推进。"""
        now = time.monotonic()
        with self._status_lock:
            self._status_activity = now
            if label:
                self._status_label = label
            if role is not None:
                self._status_role = role
            if task is not None:
                self._status_task = task

    def _status_tool(self, name):
        now = time.monotonic()
        with self._status_lock:
            self._status_activity = now
            self._status_last_tool = str(name or "")
            self._status_tool_count += 1
            if self._status_last_tool:
                self._status_label = f"执行工具 {self._status_last_tool}"

    def _status_fail(self, message):
        with self._status_lock:
            self._status_error = str(message or "")[:300]
            self._status_activity = time.monotonic()

    def _status_finish(self):
        with self._status_lock:
            self._status_done = True
            self._status_activity = time.monotonic()

    def _emit_status(self, text, role=None, task=None):
        self._status_touch(text, role=role, task=task)
        self.status.emit(text)

    def status_snapshot(self):
        """线程安全的只读状态，字段与单模型 worker 的快照保持兼容。"""
        now = time.monotonic()
        with self._status_lock:
            started = self._status_started
            last = self._status_activity
            label = self._status_label
            role = self._status_role
            task = self._status_task
            tool = self._status_last_tool
            count = self._status_tool_count
            permission = self._status_permission
            error = self._status_error
            done = self._status_done
        return {
            "busy": bool(self.isRunning() and not done),
            "dual": True,
            "permission_mode": self.permission_mode or "default",
            "elapsed": (now - started) if started else 0.0,
            "since_activity": (now - last) if last else None,
            "activity_label": label,
            "tool_count": count,
            "last_tool": tool,
            "last_tool_age": None,
            "pending_permissions": 0,
            "pending_tools": [],
            "permission_tool": permission,
            "permission_age": None,
            "dev_mode": "",
            "dev_roots": 0,
            "last_error": error,
            "tier": self.tier,
            "review_enabled": self.review_enabled,
            "role_label": self._role_label(role) if role else "",
            "task_label": task or "",
        }

    # ---------------------------------------------------------------- helpers --
    def _spec_for(self, role):
        return self.master if role == MASTER else self.worker

    def _role_label(self, role):
        return "主 AI" if role == MASTER else "执行 AI"

    def _effort_for(self, spec):
        # 仅旧调用方未提供角色档位时兼容全局参数，不改写角色配置。
        effort = str(self.effort if spec.effort is None else spec.effort).strip()
        levels = supported_efforts(spec)
        return replace(spec, effort=effort if effort in levels else "")

    def _run_agent(self, role, prompt, label, display=True, read_only=False,
                   timeout=1200):
        if self._cancel.is_set():
            return AgentResult(error="已取消", cancelled=True)
        spec = self._effort_for(self._spec_for(role))
        self._ensure_journal()
        # 角色隔离，即使同源同模型也不能互相恢复会话。
        session_key = (role, spec.source, spec.model, spec.effort,
                       str(Path(self.cwd).resolve()), self.permission_mode)
        session_id = self._sessions.get(session_key, "")
        self._status_touch(label, role=role, task=label)
        if display:
            self.role_started.emit(role, label)
        agent = CliAgent(
            self.cwd, env=self.claude_env, claude_path=self.claude_path,
            codex_path=self.codex_path, cancel_event=self._cancel,
            timeout=timeout)
        with self._current_lock:
            self._current = agent

        def on_delta(chunk):
            self._status_touch()
            if display:
                self.delta.emit(role, chunk)

        def on_message(text, tools):
            self._status_touch()
            if display:
                self.message.emit(role, text, tools)

        def on_tool(name, data):
            self._status_tool(name)
            if display:
                self.tool.emit(role, name, data)

        stage_prompt = (f"当前阶段：{label}。以本轮任务为准；此前规划只读指令仅适用于规划阶段。\n"
                        f"本轮权限：{'只读，禁止修改' if read_only else '按已配置权限执行任务'}。\n" + prompt)
        if display:
            stage_prompt += "\n" + RESULT_INSTRUCTION
            if role == WORKER and self._ask_count < self.policy.max_asks:
                stage_prompt += "\n中途需要指导时以 DUAL_ASK 行代替最终交付；完成后才输出 DUAL_RESULT。\n"
        def payload(resume):
            return ("同一角色会话续接。共享状态卡是跨角色交接依据；请核实磁盘现状。\n"
                    if resume else self._bootstrap(role)) + stage_prompt
        initial_payload = payload(bool(session_id))
        self._prompt_chars += len(initial_payload)
        try:
            result = agent.run(
                spec, initial_payload, self.permission_mode, read_only,
                on_delta, on_message, on_tool, session_id=session_id)
            self._account(role, result)
            if not result.ok and result.error:
                self._status_fail(result.error)
            self._last_call_path = self._journal.call(role, label, result, bool(session_id))
            # 只在明确的会话失效、没有执行输出时重建一次；普通错误不能重放写操作。
            stale = re.search(r"(?:no (?:conversation|session) found|"
                              r"(?:session|conversation|thread)[^\n]*(?:not found|does not exist))",
                              result.error, re.IGNORECASE)
            if (session_id and stale and not result.text and not result.tools
                    and not result.cancelled and not self._cancel.is_set()):
                self._sessions.pop(session_key, None)
                self._session_resets += 1
                self.note.emit("原角色会话已失效，使用完整状态卡重建会话（仅重试一次）。")
                retry_payload = payload("")
                self._prompt_chars += len(retry_payload)
                result = agent.run(
                    spec, retry_payload, self.permission_mode, read_only,
                    on_delta, on_message, on_tool, session_id="")
                self._account(role, result)
                self._last_call_path = self._journal.call(role, label, result, False)
            if result.session_id and result.ok and not result.cancelled:
                self._sessions[session_key] = result.session_id
            self._save_state()
        finally:
            with self._current_lock:
                self._current = None
        return result

    def _account(self, role, result):
        stats = self._stats[role]
        stats["calls"] += 1
        stats["cost"] += float(result.cost_usd or 0)
        stats["seconds"] += float(result.elapsed or 0)
        for key, value in (result.usage or {}).items():
            if key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens") and isinstance(value, (int, float)):
                stats["tokens"] += int(value)

    def _ensure_journal(self):
        if self._journal is None:
            self._journal = RunJournal(self.cwd, self.request, self.context, self.previous_state)
            if self._journal.previous_error:
                self.note.emit(self._journal.previous_error)
            self._save_state()

    def _save_state(self):
        if self._journal is None:
            return
        self._journal.write("state.json", {
            "protocol_version": 3, "plan": self._plan,
            "review_enabled": self.review_enabled,
            "review_mode": self.review_mode,
            "previous_checkpoint": self._journal.previous,
            "context_brief": (str(self._journal.root / "context-brief.json")
                              if self._compressed_context else ""),
            "records": [{k: v for k, v in r.items() if k != "text"} for r in self._records],
            "sessions": [{"role": k[0], "source": k[1], "model": k[2], "id": v}
                         for k, v in self._sessions.items()],
            "stats": self._stats,
        })

    def _bootstrap(self, role=""):
        prior = (f"上轮交接快照：{self._journal.previous}，首次接手必须读取。\n"
                 "沿用仍适用的接口、约束及未解决项；最新用户请求优先，已通过工作先核实再复用。\n"
                 if self._journal.previous else self._journal.previous_error + "\n")
        if role == MASTER and self._compressed_context:
            input_rule = (
                f"上下文摘要：{self._journal.root / 'context-brief.json'}\n"
                f"完整原始输入（按需定位核实）：{self._journal.root / 'input.json'}\n"
                "首次接手读取上下文摘要和共享状态卡，保留全部用户约束；"
                "历史原文仅在摘要缺项、冲突或需要核实具体证据时按需定位读取。\n")
        else:
            input_rule = (
                f"完整原始输入：{self._journal.root / 'input.json'}\n"
                "首次接手必须读取完整原始输入和共享状态卡，保留全部用户约束。\n")
        return (f"用户原始请求：\n{self.request}\n项目目录：{self.cwd}\n"
                f"协作档次：{self.tier} · {self.policy.label}（{self.policy.short}）\n" + prior +
                f"共享状态卡：{self._journal.root / 'state.json'}\n"
                + input_rule +
                "最近对话上下文：\n" + self._context_block(role) + "\n")

    def _state_block(self):
        self._ensure_journal()
        self._save_state()
        card = {"summary": self._plan.get("summary", ""),
                "constraints": self._plan.get("constraints", []),
                "tasks": [{"id": t["id"], "title": t["title"], "role": t["role"]}
                          for t in self._plan.get("tasks", [])],
                "handoffs": [{k: v for k, v in r.items() if k != "text"}
                             for r in self._records]}
        serialized = json.dumps(card, ensure_ascii=False)
        if len(serialized) > 12000:
            # 大报告留在文件；发送完整有效的索引，不把 JSON 切成不完整片段。
            serialized = json.dumps({"summary": self._plan.get("summary", ""),
                "tasks": card["tasks"], "handoffs": [
                    {k: r[k] for k in ("id", "ok", "error", "report_path") if k in r}
                    for r in self._records]}, ensure_ascii=False)
        return (f"共享状态卡（必须读取，含完整约束、接口决定和验证证据）：{self._journal.root / 'state.json'}\n"
                f"完整计划：{self._journal.root / 'plan.json'}\n"
                f"状态索引：\n{serialized}\n"
                "按任务 ID 对接；已通过任务不重复实施。发现磁盘状态与记录不符时先核实。\n")

    # --------------------------------------------------------------- prompts --
    def _context_block(self, role=""):
        if not self.context.strip():
            return "（无）"
        if role == MASTER and self._compressed_context:
            return ("执行 AI 已压缩的有效上下文（原文见 input.json；未截断）：\n"
                    + self._compressed_context)
        if len(self.context) <= self.CONTEXT_COMPRESS_THRESHOLD:
            return self.context.strip()
        return "上下文较长，完整保存在 input.json 的 context 字段，必须读取；未进行截断。"

    def _ops_text(self, ops):
        return "/".join(WORK_OPS.get(x, x) for x in ops or [])

    def _plan_prompt(self):
        master_name = self.master.label or self.master.model
        worker_name = self.worker.label or self.worker.model
        style = self.policy.plan_style
        if style == "contract":
            role_rule = (
                "本档只需下发契约：1–4 个任务，把可执行动作、边界和完成条件写清楚，"
                "不要长篇方案。执行 AI 自主完成读取、定位、执行和监控。")
        elif style == "deep":
            role_rule = (
                "本档主 AI 负责主要实现：把思考和执行类任务留给自己，"
                "只把读取、定位、检索、运行监控等简单工作交给 worker。")
        else:
            role_rule = (
                "本档主 AI 只接思考、设计类工作；读取、上下文压缩、执行和监控交给 worker。")
        if self.review_enabled:
            review_rule = (
                "- 调度器会在任务结束后自行决定是否做最终验收；不要把单独的主 AI"
                "验收 / 复查排成任务，也不要安排重复汇报或交接任务。\n")
        else:
            review_rule = (
                "- 本轮已关闭主 AI 复查：不要安排独立验收 / 复查任务；任务结果由"
                "调度器按执行者的逐项报告与验证证据判定，执行者仍须自行验证。\n")
        return f"""你是双 AI 协作系统中的「主 AI（M）」，负责规划；分工按工作部件难度和档次，不按比例凑数。

项目目录：{self.cwd}
主 AI 模型：{master_name}（{self.master.source}）
执行 AI 模型：{worker_name}（{self.worker.source}）
协作档次：{self.tier} · {self.policy.label}。{self.policy.summary}
分工原则：{role_rule}
工作部件 ops（可多选）：read 读取 / locate 定位 / search 检索 / normalize 上下文压缩 /
think 思考决策 / design 方案设计 / execute 执行修改 / monitor 运行监控 / report 输出汇报。

请先用只读方式了解必要代码，然后输出实施计划。只输出一个 JSON 对象，不要输出 JSON 之外的文字：
{{
  "summary": "一句话说明方案",
  "constraints": ["必须保留的用户约束、已确定接口、关键决定；不要省略早期约定"],
  "tasks": [
    {{
      "id": "T1",
      "title": "简短标题",
      "goal": "具体要做什么",
      "role": "worker 或 master（只作参考，宿主会按 ops 难度重新分配）",
      "ops": ["read", "execute"],
      "points": 3,
      "files": ["预计涉及的文件或目录"],
      "acceptance": "可检查的验收标准",
      "verify": "建议的验证命令或检查方式"
    }}
  ],
  "risks": "主要风险（可留空）"
}}

规则：
- 任务按执行顺序排列；相邻同角色任务会合并调用。
- 只按“谁做更省且更稳”分配：高档工作（思考/设计/疑难）给 master，
  低档工作（读取/检索/执行/监控）给 worker；不要再安排重复汇报或独立交接任务。
{review_rule}- 不要为了贴占比而增减任务；任务总数不超过 8 个。
- 这一步只规划，不要修改任何文件。
"""

    def _solo_prompt(self, role):
        label = self._role_label(role)
        if role == MASTER:
            extra = ("当前为全主档（最高档）：所有读取、思考、执行、监控和输出都由你完成，"
                     "不调用另一个模型。请自行控制读取范围，避免重复大范围检索。")
        else:
            extra = ("当前为全从档（最低档）：所有工作由你独立完成，主 AI 不参与。"
                     "按用户要求直接落地并验证。")
        return f"""你是双 AI 协作系统中的「{label}」。{extra}

项目目录：{self.cwd}

完成后用简短列表说明修改文件、实际验证结果和未解决问题。不要只报告“应该可以”。
"""

    def _task_prompt(self, plan, task):
        return self._batch_prompt(plan, [task])

    def _batch_prompt(self, plan, tasks):
        self._ensure_journal()
        if self._plan != plan:
            self._plan = plan
            self._journal.write("plan.json", plan)
        label = self._role_label(tasks[0]["role"])
        role = tasks[0]["role"]
        compact = [{key: t[key] for key in
                    ("id", "title", "goal", "role", "ops", "points", "files",
                     "acceptance", "verify") if key in t} for t in tasks]
        if self.policy.verbosity == "minimal":
            rules = ("- 只做任务包内工作，保持接口；逐项给出验证证据。\n"
                     "- 失败即停并在 DUAL_RESULT 标记 blocked。\n")
        elif self.policy.verbosity == "full":
            rules = ("- 先读取/定位再改；只处理任务范围，保持已确定接口。\n"
                     "- 相邻任务连续完成并复用理解；每项都要实际验证。\n"
                     "- 第一项失败立即停止，后续项 blocked；禁止跳过失败项。\n"
                     "- 有疑问先尝试一次；仍不确定则按剩余 ASK 次数询问或报告 blocked。\n")
        else:
            rules = ("- 只处理任务包范围，保持接口；相邻任务连续完成、复用理解。\n"
                     "- 每项实际验证；第一项失败即停，后续项 blocked。\n")
        ask_rule = ""
        if role == WORKER and self._ask_count < self.policy.max_asks:
            ask_rule = ("- 需要主 AI 决策时，只输出一行压缩询问后停止：\n"
                        '  DUAL_ASK: {"task":"当前任务 ID","problem":"一句话","need":"需要的决定","tried":["已试"]}\n')
        return f"""你是双 AI 协作系统中的「{label}」。档次 {self.tier} · {self.policy.short}。
{self._state_block()}
当前任务包：
{json.dumps(compact, ensure_ascii=False)}

DIRECTIVE v3（内部链路可压缩，不需要用户可读）：
tier={self.tier}; role={role}; ops={self._ops_text(tasks[0].get("ops"))}
{rules}{ask_rule}- 最后一行 DUAL_RESULT 增加 tasks 数组，按顺序覆盖本包全部 ID：
  {{"id":"T1","status":"passed/blocked/failed","summary":"具体结果",
  "changed_files":[],"decisions":[],"interfaces":[],"verification":["实际命令与结果"],
  "open_issues":[]}}。
- 有未解决项必须 blocked/failed。顶层 status 仅在所有任务通过时为 passed。
"""

    def _compress_prompt(self):
        return f"""你是执行 AI。先为下一个角色压缩有效上下文，节省主 AI 输入额度。

项目目录：{self.cwd}
只读操作；不要修改任何文件。只输出一个 JSON 对象：
{{"summary":"目标与现状（≤400字）","constraints":["必须保留的约束/接口"],
 "refs":["关键文件:行号或路径"],"risks":["风险或未知项"]}}
不要复述大段代码或历史；只保留主 AI 做决策必需的信息。
约束、接口、未解决项必须完整保留，不得为压缩而省略；不确定处标明原文引用。
"""

    def _guide_prompt(self, plan, tasks, ask):
        ids = ",".join(t["id"] for t in tasks)
        return f"""你是主 AI（M）。执行 AI 在任务 {ids} 中途询问，请给一次压缩二次指导。
档次：{self.tier}；用户目标：{self.request}
任务：{json.dumps([{k: t[k] for k in ("id", "goal", "ops") if k in t} for t in tasks], ensure_ascii=False)}
询问：{json.dumps(ask, ensure_ascii=False)}
约束：{json.dumps(plan.get("constraints", []), ensure_ascii=False)}

只读，不修改文件。只输出一行：
DUAL_GUIDE: {{"decision":"结论","next":["下一步动作"],"avoid":["禁忌"],"done_when":"完成条件"}}
指导必须短、可执行；不要让执行 AI 重做已完成工作。
"""

    def _followup_prompt(self, tasks, ask, guide):
        ids = ",".join(t["id"] for t in tasks)
        return f"""主 AI 的二次指导（任务 {ids}）：
{guide}

原询问：{json.dumps(ask, ensure_ascii=False)}
按指导继续原任务包，不要扩大范围。完成后按交付协议输出 DUAL_RESULT。
本轮剩余询问次数：{max(0, self.policy.max_asks - self._ask_count)}；用尽后仍有阻塞则报告 blocked。
"""

    def _takeover_prompt(self, plan, task, failed_result):
        reason = failed_result.error or self._records[-1].get("report", {}).get("summary") or "执行 AI 未能给出可用结果"
        return f"""你是主 AI（M）。执行 AI 未能完成任务 {task['id']}，现在由你接管。
{self._state_block()}
当前任务：{json.dumps(task, ensure_ascii=False)}
完整失败记录：{self._last_call_path or '见共享状态卡'}
失败原因：{reason}

先读取失败记录、核实当前代码及已完成的修改，从现场继续；禁止重复成功的前序任务。
完成修复并实际验证。DUAL_RESULT 中补充 changed_files、decisions、interfaces、open_issues 字符串数组。
"""

    def _review_prompt(self, plan, records):
        unresolved_ids = [t["id"] for t in plan["tasks"] if not any(
            r["id"] == t["id"] and r.get("ok") for r in records)]
        action = ("只读检查，禁止修改文件。发现未达标项时返回 blocked。"
                  if self.policy.review == "on_failure" else
                  "检查实际代码和验证结果，修复未达标之处。只改必要内容。")
        return f"""你是主 AI（M），负责最终验收。

{self._state_block()}

{action}
逐项检查完整计划和原始约束，包括未执行任务及未解决项，不能仅相信先前摘要。
若修复未通过或未执行任务，DUAL_RESULT 必须提供 tasks 数组，使用相同 ID、逐项状态和验证证据。
待修复任务 ID：{json.dumps(unresolved_ids)}。tasks 按计划顺序列出，可同时包含已经通过的任务。
已通过任务不必重复运行相同的局部检查；必须检查跨任务集成。
必须实际运行适合本需求的整体验证，并在交付协议中说明证据。
最后输出：
验收结论：通过 / 未通过
修复内容：...
遗留问题：...
"""

    def _task_reports(self, tasks, result):
        """保留有效的成功前缀，缺项/乱序/失败后的结果不得静默通过。"""
        report = delivery_report(result)
        items = report.get("tasks") if report else None
        if report and items is None and len(tasks) == 1:
            items = [dict(report, id=tasks[0]["id"])]  # 兼容单任务旧协议
        if not isinstance(items, list):
            items = []
        ids = [x.get("id") for x in items if isinstance(x, dict)]
        expected = [t["id"] for t in tasks]
        if (len(ids) != len(items) or len(set(str(x) for x in ids)) != len(ids)
                or ids != expected[:len(ids)]):
            items = []
        reports, stopped = [], False
        for i, task in enumerate(tasks):
            item = items[i] if i < len(items) else None
            if stopped or not valid_report(item):
                item = {"id": task["id"], "status": "blocked",
                        "summary": result.error or "缺少有效逐项报告或前序任务未通过",
                        "verification": ["交付协议检查未通过，未确认任务完成"],
                        "open_issues": ["需要核实代码及验证证据"]}
            else:
                item = dict(item)
                if item.get("open_issues"):
                    item["status"] = "blocked"
            if item["status"] != "passed":
                stopped = True
            reports.append(item)
        # 顶层明确失败却所有子项成功时，也必须留下阻塞，避免自相矛盾的交付。
        if (report and (report["status"] != "passed" or report.get("open_issues"))
                and all(r["status"] == "passed" for r in reports)):
            reports[-1] = dict(reports[-1], status="blocked", summary=report["summary"])
        return reports

    def _record(self, task, report, result):
        record = {"id": task["id"], "title": task["title"], "role": task["role"],
                  "ok": report["status"] == "passed", "report": report,
                  "text": report["summary"], "error": result.error if not result.ok else "",
                  "report_path": self._last_call_path}
        self._records.append(record)
        self._save_state()
        return record

    # ----------------------------------------------------------------- run --
    def run(self):
        try:
            self._emit_status("准备协作…")
            if not self.cwd or not Path(self.cwd).is_dir():
                self.failed.emit("请先选择一个项目目录")
                return
            self._ensure_journal()
            policy = self.policy
            if policy.solo_role:
                role = policy.solo_role
                self.note.emit(
                    f"档次 {self.tier}（{policy.label}）：只调用{self._role_label(role)}，"
                    "另一个模型不参与，以节省额度。")
                result = self._run_agent(
                    role, self._solo_prompt(role),
                    f"{self._role_label(role)} · 独立执行",
                    display=True, timeout=self.TASK_TIMEOUT)
                self._finish_solo(result, role)
                return

            # 1) 长上下文先由执行 AI 压缩，主 AI 只读压缩结果。
            if policy.precompress and len(self.context) > self.CONTEXT_COMPRESS_THRESHOLD:
                self._emit_status("执行 AI 正在压缩上下文…", role=WORKER)
                comp = self._run_agent(
                    WORKER, self._compress_prompt(), "执行 AI · 上下文压缩",
                    display=False, read_only=True, timeout=self.PLAN_TIMEOUT)
                if self._cancel.is_set() or comp.cancelled:
                    self._finish_cancelled({})
                    return
                data = extract_json_object(comp.text) if comp.ok else None
                if (isinstance(data, dict) and isinstance(data.get("summary"), str)
                        and data["summary"].strip()
                        and all(isinstance(data.get(key), list)
                                and all(isinstance(x, str) and x.strip() for x in data[key])
                                for key in ("constraints", "refs", "risks"))):
                    compact = {
                        key: data[key] for key in ("summary", "constraints", "refs", "risks")
                    }
                    serialized = json.dumps(compact, ensure_ascii=False)
                    if len(serialized) < len(self.context):
                        self._journal.write("context-brief.json", compact)
                        self._compressed_context = serialized
                if self._compressed_context:
                    self.note.emit(
                        f"上下文已压缩：{self._context_before} → "
                        f"{len(self._compressed_context)} 字符，主 AI 只读压缩结果。")
                else:
                    self.note.emit("上下文压缩未取得有效的较短摘要，主 AI 改用原文文件引用读取。")

            # 2) 主 AI 规划/契约
            contract = self.policy.plan_style == "contract"
            plan_label = "主 AI · 契约" if contract else "主 AI · 规划"
            self._emit_status("主 AI 正在下发契约…" if contract else "主 AI 正在规划…", role=MASTER)
            plan_result = self._run_agent(
                MASTER, self._plan_prompt(), plan_label,
                display=False, read_only=True, timeout=self.PLAN_TIMEOUT)
            self._work_points[MASTER] += 2
            plan = None
            if plan_result.ok:
                plan = extract_json_object(plan_result.text)
            if self._cancel.is_set() or plan_result.cancelled:
                self._finish_cancelled({})
                return
            if plan is None:
                plan = fallback_plan(self.request, self.tier)
                self.note.emit(
                    "规划/契约结果无法解析，已使用按档次的最小拆分。")
            plan = normalize_plan(plan, self.request, self.tier,
                                  review_enabled=self.review_enabled)
            self._plan = plan
            self._journal.write("plan.json", plan)
            self._save_state()
            self.plan_ready.emit(plan)
            if self._cancel.is_set():
                self._finish_cancelled(plan)
                return

            # 2) 连续同角色任务合并，保持原顺序及逐项验收，不跨角色重排。
            tasks = plan["tasks"]
            index = 0
            while index < len(tasks):
                if self._cancel.is_set():
                    self._finish_cancelled(plan)
                    return
                task = tasks[index]
                role = task["role"]
                end = index + 1
                while end < len(tasks) and tasks[end]["role"] == role:
                    end += 1
                batch = tasks[index:end]
                label = f"{self._role_label(role)} · {','.join(t['id'] for t in batch)}"
                self._emit_status(
                    f"正在执行 {index + 1}—{end}/{len(tasks)}（{self._role_label(role)}）",
                    role=role, task=",".join(t["id"] for t in batch))
                result = self._run_agent(
                    role, self._batch_prompt(plan, batch), label,
                    display=True, timeout=self.TASK_TIMEOUT)
                if self._cancel.is_set() or result.cancelled:
                    self._finish_cancelled(plan)
                    return
                # 执行 AI 中途询问：只把压缩询问转给主 AI，取回短指导后原地续做。
                ask = extract_ask(result) if role == WORKER else None
                batch_ids = {t["id"] for t in batch}
                while (ask and ask["task"] in batch_ids
                       and self._ask_count < self.policy.max_asks):
                    self._ask_count += 1
                    ids = ",".join(t["id"] for t in batch)
                    self.note.emit(
                        f"执行 AI 询问 {ask['task'] or ids}：{ask['problem'][:120]}"
                        " → 主 AI 二次指导")
                    guide_result = self._run_agent(
                        MASTER, self._guide_prompt(plan, batch, ask),
                        f"主 AI · 指导 {ask['task'] or ids}", display=False,
                        read_only=True, timeout=self.GUIDE_TIMEOUT)
                    self._work_points[MASTER] += 1
                    if self._cancel.is_set() or guide_result.cancelled:
                        self._finish_cancelled(plan)
                        return
                    guide = extract_guide(guide_result)
                    if not guide:
                        self.note.emit("主 AI 未返回可用指导，按原结果继续。")
                        break
                    self._emit_status(f"执行 AI 依据指导继续 {ids}…",
                                     role=WORKER, task=ids)
                    result = self._run_agent(
                        role, self._followup_prompt(batch, ask, guide),
                        f"{self._role_label(role)} · {ids}（续）",
                        display=True, timeout=self.TASK_TIMEOUT)
                    if self._cancel.is_set() or result.cancelled:
                        self._finish_cancelled(plan)
                        return
                    ask = extract_ask(result)
                reports = self._task_reports(batch, result)
                for task, report in zip(batch, reports):
                    self._work_points[role] += task["points"]
                    record = self._record(task, report, result)
                    if not record["ok"]:
                        break
                    index += 1
                if not record["ok"] and role == WORKER:
                    self.note.emit(
                        f"执行 AI 未完成 {task['id']}，按完成优先规则由主 AI 接管。")
                    takeover = self._run_agent(
                        MASTER, self._takeover_prompt(plan, task, result),
                        f"主 AI · 接管 {task['id']}", display=True,
                        timeout=self.TASK_TIMEOUT)
                    self._work_points[MASTER] += task["points"]
                    if self._cancel.is_set() or takeover.cancelled:
                        self._finish_cancelled(plan)
                        return
                    takeover_report = self._task_reports([task], takeover)[0]
                    record["takeover"] = takeover_report["status"] == "passed"
                    record["ok"] = record["takeover"]
                    record["error"] = "" if record["ok"] else (takeover.error or takeover_report["summary"])
                    record["text"] = (record.get("text") or "") + (
                        "\n\n【主 AI 接管】\n" + (takeover.text or takeover.error))
                    record["attempts"] = [record["report_path"], self._last_call_path]
                    record["previous_report"] = record["report"]
                    record["report_path"] = self._last_call_path
                    record["report"] = takeover_report
                    if record["ok"]:
                        index += 1  # 后续未完成项重新打包，成功前缀不重做。
                elif not record["ok"] and role == MASTER:
                    record["blocked"] = True
                self._save_state()
                if not record["ok"]:
                    break  # 未完成的前序任务不可静默跳过

            if self._cancel.is_set():
                self._finish_cancelled(plan)
                return

            # 3) 是否验收由档次和复查开关共同决定：关闭复查或低档全部通过时跳过。
            review_text = ""
            review_mode = self.review_mode
            needs_review = (review_mode == "full" or (
                review_mode == "on_failure"
                and not all(r["ok"] for r in self._records)))
            if not self.review_enabled and not self._cancel.is_set():
                self.note.emit(
                    f"档次 {self.tier}：已关闭主 AI 复查，跳过独立验收调用；"
                    "任务结果仍按逐项验证证据判定。")
            elif not needs_review and not self._cancel.is_set():
                self.note.emit(
                    f"档次 {self.tier}：全部任务通过，跳过主 AI 验收调用以节省额度。")
            if not self._cancel.is_set() and needs_review:
                self._emit_status("主 AI 正在验收…", role=MASTER)
                review = self._run_agent(
                    MASTER, self._review_prompt(plan, self._records),
                    "主 AI · 验收", display=True,
                    read_only=self.policy.review == "on_failure",
                    timeout=self.REVIEW_TIMEOUT)
                self._work_points[MASTER] += 2
                if self._cancel.is_set() or review.cancelled:
                    self._finish_cancelled(plan)
                    return
                self._review_ok = accepted_result(review)
                # 审查不能只用顶层 passed 掩盖未执行/未通过任务。
                unresolved = [t for t in tasks if not any(
                    r["id"] == t["id"] and r["ok"] for r in self._records)]
                if self._review_ok and unresolved:
                    # 修复验收必须提供显式 ID，不能把通用结论套到缺失任务。
                    review_report = delivery_report(review)
                    items = review_report.get("tasks") or []
                    ids = [r.get("id") for r in items if isinstance(r, dict)]
                    known = {t["id"] for t in tasks}
                    valid_ids = (len(ids) == len(items) and all(isinstance(x, str) and x in known for x in ids)
                                 and len(set(str(x) for x in ids)) == len(ids))
                    needed = {t["id"] for t in unresolved}
                    repair_report = dict(review_report, tasks=[r for r in items if r.get("id") in needed])
                    fixed = self._task_reports(unresolved, replace(
                        review, text="DUAL_RESULT: " + json.dumps(repair_report, ensure_ascii=False)))
                    self._review_ok = bool(items) and valid_ids and all(
                        r["status"] == "passed" for r in fixed)
                    if self._review_ok:
                        for task, report in zip(unresolved, fixed):
                            previous = next((r for r in self._records if r["id"] == task["id"]), None)
                            if previous:
                                previous["previous_report"] = previous["report"]
                                previous.update(ok=True, resolved_by_review=True, report=report,
                                                report_path=self._last_call_path, error="")
                            else:
                                self._record(task, report, review)["resolved_by_review"] = True
                review_text = review.text or review.error
                if not self._review_ok:
                    self.note.emit(f"验收未通过：{review.error or '缺少未完成任务的逐项修复证据'}")

            # 4) 汇总
            final_text = review_text.strip()
            if not final_text:
                for record in reversed(self._records):
                    if record.get("text"):
                        final_text = record["text"]
                        break
            self._finish(plan, final_text, cancelled=False)
        except Exception as exc:  # noqa: BLE001
            message = f"协作调度出错：{type(exc).__name__}: {exc}"
            self._status_fail(message)
            self.failed.emit(message)
        finally:
            # 兜底：确保进度快照不会一直停在忙碌状态
            self._status_finish()

    # -------------------------------------------------------------- finish --
    def _finish_solo(self, result, role):
        if self._cancel.is_set() or result.cancelled:
            self._finish_cancelled({"endpoint": True})
            return
        self._work_points[role] += 1
        ok = accepted_result(result)
        if not ok:
            self.failed.emit(result.error or "模型没有返回结果")
            return
        self._records.append({
            "id": "T1", "title": "独立完成", "role": role,
            "ok": True, "text": result.text, "error": "",
            "report": delivery_report(result), "report_path": self._last_call_path,
        })
        self._finish({
            "summary": f"{self.policy.label}：由单一模型独立完成。",
            "tier": self.tier, "tier_label": self.policy.label,
            "tasks": [{"id": "T1", "title": "独立完成", "role": role,
                       "points": 1, "ops": ["think", "execute", "monitor"],
                       "ok": True}],
            "master_points": 1 if role == MASTER else 0,
            "worker_points": 1 if role == WORKER else 0,
            "total_points": 1,
            "target_share": self.share,
            "actual_share": 100 if role == MASTER else 0,
            "endpoint": True,
            "solo": True,
        }, result.text, cancelled=False)

    def _finish_cancelled(self, plan):
        self._finish(plan, "", cancelled=True)

    def _finish(self, plan, final_text, cancelled=False):
        self._plan = plan
        if self._journal:
            self._journal.write("plan.json", plan)
        stats = {}
        for role in (MASTER, WORKER):
            item = dict(self._stats[role])
            item["model"] = self._spec_for(role).label or self._spec_for(role).model
            item["source"] = self._spec_for(role).source
            stats[role] = item
        info = {
            "ok": (not cancelled and bool(self._records)
                   and all(any(r["id"] == t["id"] and r.get("ok") for r in self._records)
                           for t in plan.get("tasks", []))
                   and (self._review_ok is True or (self._review_ok is None
                        and all(r.get("ok") for r in self._records)))),
            "review_ok": self._review_ok,
            "review_enabled": self.review_enabled,
            "review_mode": self.review_mode,
            "performed_points": dict(self._work_points),
            "performed_share": round(self._work_points[MASTER] / max(1, sum(self._work_points.values())) * 100),
            "cancelled": cancelled,
            "final_text": final_text or "",
            "summary": plan.get("summary") or "",
            "tasks": plan.get("tasks") or [],
            "records": self._records,
            "master_points": plan.get("master_points", 0),
            "worker_points": plan.get("worker_points", 0),
            "total_points": plan.get("total_points", 0),
            "target_share": plan.get("target_share", self.share),
            "actual_share": plan.get("actual_share", 0),
            "tier": self.tier,
            "tier_label": self.policy.label,
            "tier_summary": self.policy.summary,
            "asks": self._ask_count,
            "prompt_chars": self._prompt_chars,
            "context_before": self._context_before,
            "context_after": (len(self._compressed_context)
                              if self._compressed_context else self._context_before),
            "stats": stats,
            "endpoint": plan.get("endpoint", False),
            "solo": bool(self.policy.solo_role),
            "protocol_version": 3,
            "state_dir": str(self._journal.root) if self._journal else "",
            "session_resets": self._session_resets,
        }
        self._save_state()
        if self._journal:
            self._journal.write("result.json", info)
        self.completed.emit(info)


# --------------------------------------------------------------------------- #
# 兼容：把聊天历史压成计划提示可用的上下文
# --------------------------------------------------------------------------- #
def history_context(blocks, limit=7000):
    parts = []
    for block in blocks if limit is None else blocks[-20:]:
        kind = block.get("kind")
        if kind == "user":
            parts.append("用户：" + str(block.get("text") or ""))
        elif kind in ("assistant", "master", "worker"):
            name = {"assistant": "助手", "master": "主 AI",
                    "worker": "执行 AI"}.get(kind, "助手")
            parts.append(name + "：" + str(block.get("text") or ""))
    text = "\n\n".join(parts)
    return text if limit is None else text[-limit:]
