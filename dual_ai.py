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
import hashlib
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, pyqtSignal

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
CODEX_MODEL_CACHE = CODEX_HOME / "models_cache.json"
CODEX_AUTH = CODEX_HOME / "auth.json"
RUN_STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME") or
                      (Path.home() / ".local/state")) / "claude-console/dual-runs"

# Gemini CLI（@google/gemini-cli）：登录状态与模型目录都在 ~/.gemini。
GEMINI_HOME = Path(os.environ.get("GEMINI_CLI_HOME") or
                   (Path.home() / ".gemini")).expanduser()
GEMINI_OAUTH = GEMINI_HOME / "oauth_creds.json"
GEMINI_SETTINGS = GEMINI_HOME / "settings.json"
GEMINI_MODEL_CACHE_TTL = 600
_GEMINI_MODEL_CACHE = {"at": 0.0, "models": [], "error": ""}

# 无法运行 gemini --list-models 时的兜底（只用于展示，仍以 CLI 实际为准）。
FALLBACK_GEMINI_MODELS = (
    {"slug": "auto", "display_name": "Auto"},
    {"slug": "gemini-2.5-pro", "display_name": "Gemini 2.5 Pro"},
    {"slug": "gemini-3.5-flash", "display_name": "Gemini 3.5 Flash"},
    {"slug": "gemini-3.1-flash-lite", "display_name": "Gemini 3.1 Flash Lite"},
)

GEMINI_BIN_CANDIDATES = (
    "/usr/local/bin/gemini",
    "/usr/bin/gemini",
    "/snap/bin/gemini",
    str(Path.home() / ".local/bin/gemini"),
    str(Path.home() / ".npm-global/bin/gemini"),
    str(Path.home() / ".volta/bin/gemini"),
)
GEMINI_PATH_PATTERNS = (
    ".nvm/versions/node/*/bin/gemini",
    ".local/share/fnm/node-versions/*/installation/bin/gemini",
)

CODEX_BIN_CANDIDATES = (
    "/usr/lib/chatgpt/resources/codex",
    str(Path.home() / ".codex/plugins/.plugin-appserver/codex"),
    "/usr/local/bin/codex",
    str(Path.home() / ".local/bin/codex"),
    str(Path.home() / ".npm-global/bin/codex"),
)

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
    source: str = "claude"       # claude | codex | gemini
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
# Gemini CLI 登录模型
# --------------------------------------------------------------------------- #
def find_gemini_binary():
    """定位 gemini 可执行文件；桌面启动时 PATH 可能不含 nvm 目录。"""
    found = shutil.which("gemini")
    if found:
        return found
    import glob
    matches = [Path(p) for p in GEMINI_BIN_CANDIDATES]
    for pattern in GEMINI_PATH_PATTERNS:
        matches.extend(Path(match) for match in glob.glob(
            str(Path.home() / pattern)))
    matches = [p for p in matches if p.is_file() and os.access(p, os.X_OK)]
    if not matches:
        return ""
    return str(max(matches, key=lambda p: p.stat().st_mtime))


def gemini_auth_state():
    """返回 Gemini CLI 的登录方式；空字符串表示尚未登录。"""
    if os.environ.get("GEMINI_API_KEY"):
        return "api_key"
    if GEMINI_OAUTH.is_file():
        return "oauth"
    try:
        dotenv = (GEMINI_HOME / ".env").read_text(encoding="utf-8")
        if re.search(r"^\s*(?:export\s+)?GEMINI_API_KEY\s*=\s*\S", dotenv, re.MULTILINE):
            return "api_key"
    except OSError:
        pass
    try:
        settings = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    auth = ((settings.get("security") or {}).get("auth") or {}
            if isinstance(settings, dict) else {})
    return str(auth.get("selectedType") or "") if isinstance(auth, dict) else ""


def gemini_ready():
    return bool(find_gemini_binary()) and bool(gemini_auth_state())


def gemini_settings_mtime():
    """登录/设置变化时用于触发界面重新加载模型。"""
    mtimes = []
    for path in (GEMINI_OAUTH, GEMINI_SETTINGS, GEMINI_HOME / ".env"):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(mtimes) if mtimes else 0.0


def _parse_gemini_models(text):
    models = []
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        match = re.match(r"^([A-Za-z0-9._-]+)\s+\(([^)]*)\)\s*$", line)
        if match:
            model_id = match.group(1)
            label = match.group(2).strip() or model_id
            name = model_id if label == model_id else label
            current = {"source": "gemini", "id": model_id, "name": name,
                       "efforts": [], "default_effort": "", "context": None}
            models.append(current)
            continue
        if current is not None:
            ctx = re.match(r"^Context limit:\s*([\d,]+)\s*input", line)
            if ctx:
                try:
                    current["context"] = int(ctx.group(1).replace(",", ""))
                except ValueError:
                    pass
    return models


def load_gemini_models(refresh=False):
    """通过 gemini --list-models 读取登录后可用模型；结果短暂缓存。"""
    now = time.monotonic()
    if (not refresh and _GEMINI_MODEL_CACHE["models"]
            and now - _GEMINI_MODEL_CACHE["at"] < GEMINI_MODEL_CACHE_TTL):
        return [dict(item) for item in _GEMINI_MODEL_CACHE["models"]]
    binary = find_gemini_binary()
    if not binary:
        _GEMINI_MODEL_CACHE.update({"at": now, "models": [], "error": "未安装 gemini"})
        return []
    env = os.environ.copy()
    bin_dir = str(Path(binary).parent)
    if bin_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            [binary, "--list-models"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, env=env)
        models = _parse_gemini_models(proc.stdout)
        error = ""
        if not models:
            error = (proc.stderr or proc.stdout or "gemini --list-models 无输出").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        models, error = [], f"{type(exc).__name__}: {exc}"
    if not models:
        models = [{"source": "gemini", "id": item["slug"],
                   "name": item["display_name"], "efforts": [],
                   "default_effort": "", "context": None}
                  for item in FALLBACK_GEMINI_MODELS]
    _GEMINI_MODEL_CACHE.update({"at": now, "models": models, "error": error})
    return [dict(item) for item in models]


def peek_gemini_models():
    """不触发子进程，只返回已缓存的模型列表（供界面同步填充）。"""
    return [dict(item) for item in _GEMINI_MODEL_CACHE["models"]]


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
    if spec.source == "gemini":
        state = gemini_auth_state()
        if not state:
            return {"ok": False, "text": "未登录 Gemini", "detail": ""}
        return {"ok": True, "text": "Gemini 已登录",
                "detail": f"登录方式：{state}\n额度请在 Gemini CLI 中查看"}
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
    cost_usd: float | None = None
    usage: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    session_id: str = ""
    returncode: int = 0
    elapsed: float = 0.0
    cancelled: bool = False


# 交接预算默认值（估算 token，不是计费 token）。可通过 DualRunThread 参数覆盖。
HANDOFF_TOKEN_BUDGET = 1200
TOKEN_ESTIMATE_METHOD = "heuristic-ascii/4+cjk/1.5"
# 这些工具可能产生副作用；重复出现只记录，绝不自动跳过或去重执行。
SIDE_EFFECT_TOOLS = {
    "bash", "exec", "shell", "edit", "write", "notebookedit", "apply_patch",
    "multiedit", "delete", "move", "rename", "mcp", "file_change",
}


def estimate_tokens(text):
    """确定性估算，仅用于交接预算；不冒充服务商计费 token。

    英文/ASCII 约 4 字符一个 token，中文等非 ASCII 约 1.5 字符一个 token。
    结果向上取整，空文本为 0。
    """
    text = str(text or "")
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_chars = len(text) - ascii_chars
    return int(math.ceil(ascii_chars / 4.0 + other_chars / 1.5))


def estimate_tokens_label():
    return {"method": TOKEN_ESTIMATE_METHOD, "precision": "estimate", "billable": False}


def condense_output(text, max_chars=4000, max_lines=80, source_ref=""):
    """按规则筛选长日志/工具输出：保留错误、关键行和首尾，去重并标注原文位置。

    不返回原始全文；调用方应保留 source_ref 以便按需定位。
    """
    text = str(text or "")
    if not text:
        return ""
    error_pattern = re.compile(
        r"error|fail|traceback|exception|denied|not found|timeout|错误|失败|异常|超时|拒绝",
        re.IGNORECASE)
    kept, seen_repeats = [], []
    lines = text.splitlines()
    repeat = 0
    for line in lines:
        previous = kept[-1] if kept else None
        if line == previous:
            repeat += 1
            continue
        if repeat:
            seen_repeats.append(repeat)
            kept.append(f"… 重复 {repeat} 行已折叠")
            repeat = 0
        kept.append(line)
    if repeat:
        seen_repeats.append(repeat)
        kept.append(f"… 重复 {repeat} 行已折叠")
    total = len(kept)
    if total <= max_lines and len(text) <= max_chars:
        return text
    head = kept[:max(4, max_lines // 3)]
    tail = kept[-max(4, max_lines // 3):]
    middle = kept[len(head):len(kept) - len(tail)]
    errors = [line for line in middle if error_pattern.search(line)][:max_lines // 3]
    omitted = max(0, len(middle) - len(errors))
    marker = (f"… 省略 {omitted} 行（原文：{source_ref or '调用记录'}）" if omitted else
              f"（原文：{source_ref or '调用记录'}）")
    condensed = head + ([marker] if marker else []) + errors + tail
    result = "\n".join(condensed)
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n… 已截断（原文：{source_ref or '调用记录'}）"
    return result


def _stable_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def normalized_tool_call(tool):
    """规范化工具调用标识：名称 + 稳定序列化输入。用于重复事件识别，不落原文。"""
    tool = tool if isinstance(tool, dict) else {}
    name = str(tool.get("name") or tool.get("tool") or "工具").strip()
    signature = hashlib.sha256(
        (name + "\0" + _stable_json(tool.get("input") or {})).encode("utf-8")).hexdigest()
    resource = ""
    data = tool.get("input") if isinstance(tool.get("input"), dict) else {}
    # 只保留文件类定位；命令/请求正文可能包含凭据，不写入调用日志。
    for key in ("file_path", "path", "filename", "target"):
        value = data.get(key)
        if isinstance(value, str) and value:
            resource = value[:400]
            break
    kind = "side_effect" if name.lower() in SIDE_EFFECT_TOOLS else "read"
    return {"name": name, "signature": signature, "resource": resource, "kind": kind}


class DuplicateTracker:
    """记录重复读取/工具调用；有副作用的调用只计数，不自动跳过。"""

    def __init__(self):
        self.seen = {}
        self.duplicates = 0
        self.necessary_rechecks = 0

    def observe(self, tools, versions=None):
        versions = versions or {}
        events = []
        for tool in tools or []:
            event = normalized_tool_call(tool)
            prior = self.seen.get(event["signature"])
            resource = event["resource"]
            version = versions.get(resource)
            duplicate = prior is not None
            necessary = bool(duplicate and event["kind"] == "read"
                             and prior.get("version") != version)
            if not duplicate or necessary:
                self.seen[event["signature"]] = {
                    "version": version, "resource": resource,
                    "count": (prior or {}).get("count", 0) + 1}
            elif prior is not None:
                prior["count"] = prior.get("count", 1) + 1
            if duplicate:
                self.duplicates += 1
                if necessary:
                    self.necessary_rechecks += 1
            events.append(dict(event, duplicate=duplicate,
                               necessary_recheck=necessary,
                               auto_skipped=False))
        return events


def price_usage(metrics, model, pricing):
    """可选：只有提供带来源和版本的计费配置时才计算费用，否则返回未知。"""
    if not isinstance(pricing, dict) or not isinstance(model, str):
        return None
    price = (pricing.get("models") or {}).get(model)
    if not isinstance(price, dict) or not pricing.get("source") or not pricing.get("version"):
        return None
    total = 0.0
    for key, factor in (("uncached_input_tokens", price.get("input_per_million")),
                        ("cache_read_input_tokens", price.get("cache_read_per_million")),
                        ("cache_write_input_tokens", price.get("cache_write_per_million")),
                        ("output_tokens", price.get("output_per_million"))):
        value = metrics.get(key)
        if value is None or not isinstance(factor, (int, float)):
            return None
        total += float(value) * float(factor) / 1_000_000
    return total


def usage_metrics(source, usage):
    """Claude input excludes cache; Codex input includes cache. Unknown stays None."""
    usage = usage if isinstance(usage, dict) else {}
    def count(key):
        value = usage.get(key)
        return int(value) if (isinstance(value, (int, float)) and not isinstance(value, bool)
                              and math.isfinite(value) and value >= 0) else None
    raw = count("input_tokens")
    output = count("output_tokens")
    hosted = source in ("codex", "gemini")
    read = count("cached_input_tokens" if hosted else "cache_read_input_tokens")
    write = count("cache_write_input_tokens" if hosted else "cache_creation_input_tokens")
    reasoning = count("reasoning_output_tokens")
    if reasoning is None:
        reasoning = count("reasoning_tokens")
    if hosted:
        total = raw
        # Codex / Gemini's cached input is a subset of input_tokens, not extra input.
        uncached = max(0, raw - read - (write or 0)) if raw is not None and read is not None else None
    else:
        total = raw + read + write if None not in (raw, read, write) else None
        uncached = raw
    valid_cache = (total is not None and read is not None and read + (write or 0) <= total)
    return {"input_tokens": total, "uncached_input_tokens": uncached,
            "cache_read_input_tokens": read, "cache_write_input_tokens": write,
            "output_tokens": output, "reasoning_tokens": reasoning,
            "cache_hit_rate": read / total if valid_cache and total > 0 else None}


def usage_text(metrics):
    """Shared UI wording for SDK, CLI and the offline comparison tool."""
    parts = []
    for key, label in (("input_tokens", "输入"), ("output_tokens", "输出"),
                       ("uncached_input_tokens", "未缓存输入"),
                       ("cache_read_input_tokens", "缓存读取"),
                       ("cache_write_input_tokens", "缓存写入"),
                       ("reasoning_tokens", "推理")):
        value = metrics.get(key)
        if value is not None:
            parts.append(f"{label} {value:,}")
    rate = metrics.get("cache_hit_rate")
    parts.append(f"缓存命中 {rate:.1%}" if rate is not None else "缓存命中未知")
    return " · ".join(parts)


def record_single_usage(info):
    """Store metadata only; never log SDK prompt, credentials or tool arguments."""
    root = RUN_STATE_ROOT.parent / "single-calls"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = {k: info.get(k) for k in (
        "session_id", "model", "usage", "metrics", "cost", "duration", "elapsed",
        "is_error", "subtype", "resumed", "tool_count", "model_usage")}
    fd, name = tempfile.mkstemp(prefix="call-", suffix=".tmp", dir=root)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(dict(metadata, source="claude", mode="single"), stream, ensure_ascii=False, indent=2)
    path = Path(name).with_suffix(".json")
    Path(name).replace(path)
    return str(path)


class CliAgent:
    """一次非交互式 agent 调用，支持流式回调与取消。"""

    def __init__(self, cwd, env=None, claude_path="", codex_path="",
                 gemini_path="", cancel_event=None, timeout=900):
        self.cwd = cwd
        self.env = dict(env or os.environ.copy())
        self.claude_path = claude_path
        self.codex_path = codex_path
        self.gemini_path = gemini_path
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
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=4)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass

    def _popen(self, cmd, stdin_text):
        env = self.env.copy()
        path = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        for binary in (self.claude_path, self.codex_path, self.gemini_path):
            if binary:
                bin_dir = str(Path(binary).parent)
                if bin_dir and bin_dir not in path.split(os.pathsep):
                    path = bin_dir + os.pathsep + path
        env["PATH"] = path
        proc = subprocess.Popen(
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
        if getattr(self, "report_only", False):
            cmd += ["--tools", ""]
        if spec.model:
            cmd += ["--model", spec.model]
        if session_id:
            cmd += ["--resume", session_id]
        effort = str(getattr(spec, "effort", "") or "").strip()
        if effort in ("low", "medium", "high", "xhigh", "max"):
            cmd += ["--effort", effort]

        result = AgentResult()
        message_usage = {}
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
                if message.get("id") and isinstance(message.get("usage"), dict):
                    message_usage[message["id"]] = message["usage"]
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
                if isinstance(event.get("total_cost_usd"), (int, float)):
                    result.cost_usd = float(event["total_cost_usd"])
                if isinstance(event.get("modelUsage"), dict):
                    result.model_usage = event["modelUsage"]
                usage = event.get("usage") or {}
                if isinstance(usage, dict):
                    for key, value in usage.items():
                        if isinstance(value, (int, float)):
                            result.usage[key] = value
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
        if not result.usage:
            # Preserve observed partial usage on timeout/error; result usage is authoritative.
            for usage in message_usage.values():
                for key, value in usage.items():
                    if isinstance(value, (int, float)):
                        result.usage[key] = result.usage.get(key, 0) + value
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

    # --------------------------------------------------------------- gemini --
    def run_gemini(self, prompt, spec, permission_mode="acceptEdits",
                   read_only=False, on_delta=None, on_message=None,
                   on_tool=None, session_id=""):
        if not self.gemini_path:
            return AgentResult(error="找不到 gemini 命令")
        approval = "plan" if read_only else (
            "yolo" if permission_mode == "bypassPermissions" else "auto_edit")
        cmd = [self.gemini_path, "-p", prompt, "--skip-trust",
               "--output-format", "stream-json", "--approval-mode", approval]
        if spec.model:
            cmd += ["-m", spec.model]
        if session_id:
            cmd += ["--resume", session_id]

        result = AgentResult()
        parts = []
        started = time.monotonic()

        def handle(raw):
            line = raw.strip()
            if not line.startswith("{"):
                return
            try:
                event = json.loads(line)
            except ValueError:
                return
            kind = event.get("type")
            if kind == "init":
                result.session_id = str(event.get("session_id") or "")
                return
            if kind == "message":
                if str(event.get("role") or "") != "assistant":
                    return
                chunk = str(event.get("content") or "")
                if chunk:
                    parts.append(chunk)
                    if on_delta:
                        on_delta(chunk)
                return
            if kind == "tool_use":
                name = str(event.get("tool_name") or "工具")
                data = {"parameters": event.get("parameters"),
                        "tool_id": event.get("tool_id")}
                result.tools.append({"name": name, "input": data})
                if on_tool:
                    on_tool(name, data)
                return
            if kind == "error":
                message = str(event.get("message") or "")
                if message and str(event.get("severity") or "") == "error":
                    result.error = message
                return
            if kind == "result":
                if str(event.get("status") or "") == "error":
                    error = event.get("error") or {}
                    message = (error.get("message") if isinstance(error, dict)
                               else str(error)) or result.error
                    result.error = message or "Gemini 执行失败"
                stats = event.get("stats") or {}
                if isinstance(stats, dict):
                    usage = {}
                    for key, source_key in (
                            ("input_tokens", "input_tokens"),
                            ("output_tokens", "output_tokens"),
                            ("cached_input_tokens", "cached"),
                            ("total_tokens", "total_tokens")):
                        value = stats.get(source_key)
                        if isinstance(value, (int, float)):
                            usage[key] = value
                    result.usage.update(usage)
                return

        try:
            proc = self._popen(cmd, "")
        except OSError as exc:
            result.error = f"启动 gemini 失败：{exc}"
            result.elapsed = time.monotonic() - started
            return result
        rc, stderr_lines, timed_out = self._stream(proc, handle)
        result.returncode = rc or 0
        result.elapsed = time.monotonic() - started
        result.text = "".join(parts).strip()
        result.cancelled = self.cancel_event.is_set()
        if timed_out:
            result.error = f"Gemini 调用超时（{self.timeout} 秒）"
        elif result.cancelled:
            result.error = "已取消"
        elif rc != 0 and not result.error:
            result.error = "Gemini 退出码 " + str(rc) + (
                "：" + "\n".join(stderr_lines[-8:]) if stderr_lines else "")
        result.ok = bool(result.text) and not result.error
        if result.text and not result.error and on_message:
            on_message(result.text, [])
        return result

    def run(self, spec, prompt, permission_mode="acceptEdits", read_only=False,
            on_delta=None, on_message=None, on_tool=None, session_id=""):
        if spec.source == "codex":
            return self.run_codex(
                prompt, spec, permission_mode, read_only,
                on_delta, on_message, on_tool, session_id)
        if spec.source == "gemini":
            return self.run_gemini(
                prompt, spec, permission_mode, read_only,
                on_delta, on_message, on_tool, session_id)
        return self.run_claude(
            prompt, spec, permission_mode, read_only,
            on_delta, on_message, on_tool, session_id)


class SingleCliThread(QThread):
    """单模型模式使用 Codex / Gemini 登录模型时的一次命令行调用。

    Claude 服务商仍走 Agent SDK（保留权限弹窗、开发模式等能力）；
    其他来源用对应 CLI 非交互执行，信号与主界面的对话回调兼容。
    """

    delta = pyqtSignal(str)
    message = pyqtSignal(str, list)
    tool = pyqtSignal(str, dict)
    result = pyqtSignal(dict)
    error = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent=None, cwd="", request="", spec=None,
                 permission_mode="default", claude_path="", codex_path="",
                 gemini_path="", env=None, session_id=""):
        super().__init__(parent)
        self.cwd = cwd
        self.request = request
        self.spec = spec or RoleSpec()
        self.permission_mode = permission_mode or "default"
        self.claude_path = claude_path
        self.codex_path = codex_path
        self.gemini_path = gemini_path
        self.env = dict(env or os.environ.copy())
        self.session_id = session_id or ""
        self._cancel = threading.Event()
        self._agent = None
        self._lock = threading.Lock()

    def cancel(self):
        self._cancel.set()
        with self._lock:
            agent = self._agent
        if agent is not None:
            agent.cancel()

    def run(self):
        spec = self.spec
        self.busy_changed.emit(True)
        emitted = False

        def on_message(text, tools):
            nonlocal emitted
            if text:
                emitted = True
                self.message.emit(text, tools or [])

        agent = CliAgent(
            self.cwd, env=self.env, claude_path=self.claude_path,
            codex_path=self.codex_path, gemini_path=self.gemini_path,
            cancel_event=self._cancel, timeout=1800)
        with self._lock:
            self._agent = agent
        try:
            result = agent.run(
                spec, self.request, self.permission_mode,
                on_delta=self.delta.emit,
                on_message=on_message,
                on_tool=lambda name, data: self.tool.emit(name, data or {}),
                session_id=self.session_id)
        except Exception as exc:  # noqa: BLE001 - 错误交给界面提示
            self.error.emit(f"{type(exc).__name__}: {exc}")
            self.busy_changed.emit(False)
            return
        finally:
            with self._lock:
                self._agent = None
        if result.cancelled:
            self.error.emit("已取消")
            self.busy_changed.emit(False)
            return
        if result.error and not result.ok:
            self.error.emit(result.error)
            self.busy_changed.emit(False)
            return
        if not emitted and result.text:
            self.message.emit(result.text, [])
        metrics = usage_metrics(spec.source, result.usage)
        self.result.emit({
            "session_id": result.session_id or self.session_id,
            "is_error": False,
            "result": result.text,
            "cost": result.cost_usd,
            "duration": int((result.elapsed or 0) * 1000),
            "model": spec.model,
            "source": spec.source,
            "usage": result.usage,
            "metrics": metrics,
            "tool_count": len(result.tools or []),
            "elapsed": result.elapsed,
        })
        self.busy_changed.emit(False)


RESULT_INSTRUCTION = """
交付协议：最后单独输出一行 DUAL_RESULT: 后接 JSON 对象：
DUAL_RESULT: {"status":"passed","summary":"结果概述","verification":["实际运行的验证及结果"]}
status 只能是 passed / blocked / failed。只有任务完成且必要验证通过才用 passed；
无法验证、权限不足或本阶段必需工作仍未完成时用 blocked 或 failed。
用户明确留给自己执行的命令列在 pending_user_actions 字符串数组；若当前目标是准备材料，
材料完成并验证后可 passed，但必须明确尚未实际发布，不能把用户保留的操作冒称已执行。
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
    # A redundant wrapper field must not erase valid per-task verification (historical T1/T3).
    # Only absent evidence is derived; explicitly invalid evidence remains a protocol error.
    if isinstance(report, dict) and "verification" not in report:
        items = report.get("tasks")
        if isinstance(items, list) and items and all(valid_report(item) for item in items):
            report = dict(report, verification=[proof for item in items for proof in item["verification"]])
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
                    for key in ("changed_files", "decisions", "interfaces", "open_issues", "pending_user_actions")))


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
                if previous.get("protocol_version") not in (2, 3, 4):
                    raise ValueError("上轮交接记录版本不兼容")
                # 旧协议没有任务状态字段时保持未知，不补造完成结论。
                task_state = previous.get("task_state")
                if not isinstance(task_state, dict):
                    task_state = {
                        "status": "unknown",
                        "note": "上轮协议未记录版本化任务状态；请核实上轮记录后再复用。",
                        "previous_protocol_version": previous.get("protocol_version"),
                    }
                self.previous = self.write("previous.json", {
                    "source": str(previous_root), "plan": previous.get("plan", {}),
                    "records": previous.get("records", []),
                    "protocol_version": previous.get("protocol_version"),
                    "task_state": task_state,
                    "previous_checkpoint": previous.get("previous_checkpoint", ""),
                })
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.previous_error = f"上轮交接记录未载入：{exc}。请依据可见历史核实，不要假定已完成。"

    def write(self, name, value):
        path = self.root / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            # 序列化或替换失败时清理临时文件，原文件保持不变。
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
        return str(path)

    def call(self, role, label, result, resumed, source="", meta=None):
        self.calls += 1
        meta = meta if isinstance(meta, dict) else {}
        metrics = usage_metrics(source, result.usage)
        return self.write(f"call-{self.calls:03d}.json", {
            "role": role, "stage": label, "resumed": resumed,
            "session_id": result.session_id, "text": result.text,
            "error": result.error, "ok": result.ok, "usage": result.usage,
            "error_digest": condense_output(result.error, max_chars=1200, max_lines=40,
                                            source_ref=f"{label} 调用输出"),
            "cost_usd": result.cost_usd, "elapsed": result.elapsed,
            "returncode": result.returncode, "cancelled": result.cancelled,
            "tool_count": len(result.tools), "model_usage": result.model_usage,
            "source": source, "metrics": metrics,
            "model": meta.get("model", ""), "provider": meta.get("provider", source),
            "kind": meta.get("kind", ""), "handoff": meta.get("handoff"),
            "switch_reason": meta.get("switch_reason", ""),
            "duplicate_tool_calls": meta.get("duplicate_tool_calls", 0),
            "necessary_rechecks": meta.get("necessary_rechecks", 0),
            "summary_call": bool(meta.get("summary_call")),
            "tool_events": meta.get("tool_events", []),
            "token_method": meta.get("token_method", "provider-usage"),
            "cost_source": meta.get("cost_source"),
            "cost_estimated": meta.get("cost_estimated"),
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
        "completed": value.get("completed", []),
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
        return {"summary": "最小回退：主 AI 实现并自行验证。", "tasks": [
            {"id": "T1", "title": "实现需求", "goal": user_request,
             "role": MASTER, "points": 8, "files": [], "ops": ["think", "execute"],
             "acceptance": "按用户要求完成并实际验证", "verify": "运行相关检查或测试"},
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
                 claude_path="", codex_path="", gemini_path="", claude_env=None,
                 previous_state="",
                 review_enabled=True, session_state=None, context_delta=None,
                 handoff_token_budget=None, pricing=None):
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
        self.gemini_path = gemini_path
        self.claude_env = dict(claude_env or os.environ.copy())
        self.previous_state = previous_state
        # Only the owning UI conversation supplies live session handles. A disk checkpoint
        # alone is never sufficient authorization to resume another conversation.
        self._resume_candidates = session_state if isinstance(session_state, list) else []
        self.context_delta = context_delta
        self._started_keys = set()
        self._identity_cache = {}
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
        self._report_repairs = 0
        self._compressed_context = ""
        self._context_before = len(self.context)
        self._ask_count = 0
        self._prompt_chars = 0
        # 版本化任务状态与预算内增量交接。
        try:
            budget = int(handoff_token_budget)
        except (TypeError, ValueError):
            budget = HANDOFF_TOKEN_BUDGET
        self.handoff_token_budget = max(200, budget)
        self.pricing = pricing if isinstance(pricing, dict) else None
        self._revision = 0
        self._history = []
        self._ack = {MASTER: 0, WORKER: 0}
        self._pending_handoff = None
        self._last_handoff = None
        self._handoff_log = []
        self._artifacts = {}
        self._artifact_cache = {}
        self._switches = []
        self._unconfirmed = []
        self._resume_point = "尚未开始"
        self._summary_calls = 0
        self._duplicate_tracker = DuplicateTracker()
        self._duplicate_tool_calls = 0
        self._necessary_rechecks = 0
        self._tool_call_count = 0
        self._last_role = ""
        self._last_switch_reason = ""
        self._task_total = 0
        self._task_index = 0
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

    def _session_key(self, role):
        spec = self._effort_for(self._spec_for(role))
        if spec.source not in self._identity_cache:
            if spec.source == "claude":
                keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                        "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")
                identity = [self.claude_env.get(k, "") for k in keys] + [self.claude_path]
            elif spec.source == "gemini":
                keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")
                identity = [self.claude_env.get(k, "") for k in keys] + [self.gemini_path]
                try:
                    identity.append(hashlib.sha256(
                        GEMINI_OAUTH.read_bytes()).hexdigest())
                except OSError:
                    identity.append(gemini_auth_state())
            else:
                keys = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "CODEX_HOME")
                identity = [self.claude_env.get(k, "") for k in keys] + [self.codex_path]
                auth = Path(self.claude_env.get("CODEX_HOME") or CODEX_HOME) / "auth.json"
                try:
                    identity.append(hashlib.sha256(auth.read_bytes()).hexdigest())
                except OSError:
                    identity.append("")
            self._identity_cache[spec.source] = hashlib.sha256(
                json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        return (role, spec.source, spec.model, spec.effort,
                str(Path(self.cwd).resolve()), self.permission_mode, self._identity_cache[spec.source])

    def _export_sessions(self):
        return [{"key": list(key), "id": value} for key, value in self._sessions.items()]

    # ------------------------------------------------ 版本化任务状态与交接 --
    def _bump_revision(self, kind, summary="", task_id="", detail=None):
        self._revision += 1
        entry = {"revision": self._revision, "kind": kind, "task_id": str(task_id or ""),
                 "summary": str(summary or "")[:500]}
        if isinstance(detail, dict):
            entry["detail"] = {k: v for k, v in detail.items() if v not in (None, "", [], {})}
        self._history.append(entry)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        return self._revision

    def _artifact_info(self, path):
        """为产物计算可核对的版本/摘要；引用只表示可访问位置，不代表已被读取。"""
        raw = str(path or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path(self.cwd) / candidate
        try:
            resolved = candidate.resolve()
            stat = resolved.stat()
        except OSError:
            return {"path": raw, "version": None, "digest": None, "locator": f"{raw}:1",
                    "accessible": False, "note": "文件当前不可访问，需按路径重新核实"}
        key = (str(resolved), stat.st_mtime_ns, stat.st_size)
        cached = self._artifact_cache.get(key)
        if cached is None:
            digest = None
            method = ""
            try:
                with resolved.open("rb") as stream:
                    if stat.st_size <= 2 * 1024 * 1024:
                        digest = hashlib.sha256(stream.read()).hexdigest()
                        method = "sha256-full"
                    else:
                        digest = hashlib.sha256(stream.read(256 * 1024)).hexdigest()
                        method = "sha256-prefix+size"
            except OSError:
                pass
            cached = {"digest": digest, "method": method, "size": stat.st_size}
            self._artifact_cache[key] = cached
        digest = cached["digest"]
        try:
            display = str(resolved.relative_to(Path(self.cwd).resolve()))
        except (ValueError, OSError):
            display = str(resolved)
        return {"path": display or raw, "version": (digest or "")[:12] or None,
                "digest": digest, "digest_method": cached["method"],
                "size": cached["size"], "locator": f"{display or raw}:1",
                "accessible": True, "note": "引用不代表模型已读取文件内容"}

    def _refresh_artifacts(self):
        for record in self._records:
            report = record.get("report") or {}
            for path in report.get("changed_files") or []:
                info = self._artifact_info(path)
                if not info:
                    continue
                info["task_id"] = record.get("id", "")
                info["revision"] = record.get("revision", 0)
                self._artifacts[info["path"]] = info
        return self._artifacts

    def _next_action(self):
        if self._unconfirmed:
            first = self._unconfirmed[-1]
            return (f"先核对未确认操作（{first.get('label') or first.get('task_id')}，"
                    f"记录：{first.get('call_ref') or '见共享状态卡'}）：确认是否已提交、"
                    "文件是否已写入，再决定继续或重做；不要直接重放。")
        for task in self._plan.get("tasks", []) if isinstance(self._plan, dict) else []:
            if not any(r.get("id") == task.get("id") and r.get("ok") for r in self._records):
                return f"执行任务 {task.get('id')}：{task.get('goal') or task.get('title')}"
        if not self._records:
            return "读取任务状态，确认 revision 后开始执行用户请求"
        if self.review_mode != "none" and self._review_ok is None:
            return "执行主 AI 验收，核对未完成项和整体验证"
        return "汇总结果并交付用户；同时核对 pending 中的用户待执行项"

    def _task_state(self):
        """规范化的共享任务状态；事实、暂定判断和未完成项分别标注。"""
        plan = self._plan if isinstance(self._plan, dict) else {}
        self._refresh_artifacts()
        completed, decisions, verification, pending = [], [], [], []
        seen_decisions = set()
        for record in self._records:
            report = record.get("report") or {}
            task_id = str(record.get("id") or "")
            passed = bool(record.get("ok")) and report.get("status") == "passed"
            evidence = [str(x) for x in report.get("verification") or [] if str(x).strip()]
            fact_status = "verified" if (passed and evidence) else (
                "provisional" if passed else "incomplete")
            completed.append({
                "id": task_id, "title": record.get("title", ""),
                "status": fact_status,
                "summary": str(report.get("summary") or record.get("text") or "")[:1200],
                "evidence_ref": record.get("report_path", ""),
                "revision": record.get("revision", 0),
            })
            items = []
            reports = report.get("decisions") or []
            if isinstance(reports, list):
                items.extend(str(x) for x in reports if str(x).strip())
            interfaces = report.get("interfaces") or []
            if isinstance(interfaces, list):
                items.extend("接口：" + str(x) for x in interfaces if str(x).strip())
            for item in items[:20]:
                key = (task_id, item[:300])
                if key in seen_decisions:
                    continue
                seen_decisions.add(key)
                decisions.append({
                    "task_id": task_id, "decision": item[:600],
                    "basis": record.get("report_path", ""),
                    "conditions": "", "status": "confirmed" if passed else "provisional",
                    "revision": record.get("revision", 0),
                })
            for index, item in enumerate(evidence[:20]):
                verification.append({
                    "task_id": task_id, "check": item[:500],
                    "status": "verified" if passed else "provisional",
                    "evidence_ref": f"{record.get('report_path', '')}#verification[{index}]",
                    "revision": record.get("revision", 0),
                })
            for issue in report.get("open_issues") or []:
                pending.append({"id": task_id, "status": "blocked",
                                "reason": str(issue)[:500], "needs_check": False})
            for action in report.get("pending_user_actions") or []:
                pending.append({"id": task_id, "status": "pending_user_action",
                                "reason": str(action)[:500], "needs_check": False})
        for task in plan.get("tasks", []) if isinstance(plan.get("tasks"), list) else []:
            if not any(r.get("id") == task.get("id") and r.get("ok") for r in self._records):
                pending.append({"id": task.get("id"), "status": "incomplete",
                                "reason": "尚未记录通过的逐项结果", "needs_check": False})
        for operation in self._unconfirmed:
            pending.append({
                "id": operation.get("task_id", ""), "status": "unconfirmed",
                "reason": f"{operation.get('label')}: {operation.get('reason')}"[:500],
                "needs_check": True,
                "call_ref": operation.get("call_ref", ""),
                "tool_count": operation.get("tool_count", 0),
            })
        # 去重但保留未确认/阻塞状态优先。
        unique, seen = [], set()
        for item in pending:
            key = (item.get("id"), item.get("status"), item.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        artifacts = [{k: v for k, v in item.items() if k != "digest"}
                     for item in self._artifacts.values()]
        return {
            "protocol_version": 4,
            "task_id": (plan.get("tasks") or [{}])[0].get("id", "run") if plan.get("tasks") else "run",
            "revision": self._revision,
            "goal": self.request,
            "goal_source": "current_request",
            "supersedes_previous": bool(self._journal and self._journal.previous),
            "constraints": list(plan.get("constraints") or []),
            "completed": completed,
            "decisions": decisions,
            "artifacts": artifacts,
            "verification": verification,
            "pending": unique,
            "unconfirmed_operations": list(self._unconfirmed),
            "next_action": self._next_action(),
            "resume_point": self._resume_point,
            "tasks": [{"id": t.get("id"), "title": t.get("title"), "role": t.get("role")}
                      for t in (plan.get("tasks") or []) if isinstance(t, dict)],
            "switches": list(self._switches),
            "history": list(self._history[-50:]),
            "review_mode": self.review_mode,
        }

    def _state_brief(self):
        state = self._task_state()
        done = sum(1 for item in state["completed"]
                   if item.get("status") == "verified")
        total = len((self._plan or {}).get("tasks") or [])
        return (f"任务状态 revision {state['revision']}：{total} 项任务，"
                f"已验证 {done} 项；位置：{state['resume_point']}；"
                f"下一步：{state['next_action']}")

    @staticmethod
    def _compact_state(state, level):
        """按级别生成可序列化的交接卡；每级都保持完整 JSON。

        切换明细和修订历史留在 state.json / call-*.json，不重复注入提示；
        交接只需要当前结论、证据位置和下一步。
        """
        state = dict(state)
        state.pop("history", None)
        state.pop("switches", None)
        if level == 0:
            card = dict(state)
        elif level == 1:
            card = dict(state)
            card["completed"] = [{
                "id": x.get("id"), "status": x.get("status"),
                "summary": str(x.get("summary") or "")[:200],
                "evidence_ref": x.get("evidence_ref", ""),
                "revision": x.get("revision", 0)} for x in state.get("completed", [])]
            card["decisions"] = [{
                "task_id": x.get("task_id"), "decision": str(x.get("decision") or "")[:200],
                "status": x.get("status"), "basis": x.get("basis", ""),
                "revision": x.get("revision", 0)} for x in state.get("decisions", [])]
            card["verification"] = [{
                "task_id": x.get("task_id"), "check": str(x.get("check") or "")[:160],
                "status": x.get("status"), "evidence_ref": x.get("evidence_ref", ""),
                "revision": x.get("revision", 0)} for x in state.get("verification", [])]
            card["artifacts"] = [{
                "path": x.get("path"), "version": x.get("version"),
                "locator": x.get("locator"), "accessible": x.get("accessible"),
                "revision": x.get("revision", 0)} for x in state.get("artifacts", [])]
            card.pop("history", None)
        else:
            card = {
                "protocol_version": state.get("protocol_version", 4),
                "task_id": state.get("task_id", "run"),
                "revision": state.get("revision", 0),
                "goal": state.get("goal", ""),
                "goal_source": state.get("goal_source", "current_request"),
                "constraints": state.get("constraints", []),
                "completed": [{"id": x.get("id"), "status": x.get("status")}
                              for x in state.get("completed", [])],
                "decisions": [{"task_id": x.get("task_id"), "status": x.get("status")}
                              for x in state.get("decisions", [])],
                "verification": [{"task_id": x.get("task_id"), "status": x.get("status")}
                                 for x in state.get("verification", [])],
                "artifacts": [{"path": x.get("path"), "version": x.get("version"),
                               "accessible": x.get("accessible")}
                              for x in state.get("artifacts", [])],
                "pending": state.get("pending", []),
                "unconfirmed_operations": state.get("unconfirmed_operations", []),
                "next_action": state.get("next_action", ""),
                "resume_point": state.get("resume_point", ""),
                "overflow": True,
                "omitted_sections": ["completed_detail", "decisions_detail",
                                     "verification_detail", "artifact_digest", "history"],
                "segment_refs": state.get("segment_refs", []),
            }
        # 兼容既有状态索引字段，便于旧读取方和测试。
        card["tasks"] = state.get("tasks", card.get("tasks", []))
        card["handoffs"] = state.get("handoffs", [])
        return card

    @staticmethod
    def _with_legacy_index(state, records):
        tasks = [{"id": t.get("id"), "title": t.get("title"), "role": t.get("role")}
                 for t in (state.get("tasks") or [])]
        handoffs = []
        for record in records:
            report = record.get("report") or {}
            handoffs.append({
                "id": record.get("id"), "ok": record.get("ok"),
                "report": {"summary": report.get("summary", ""),
                           "interfaces": report.get("interfaces", []),
                           "verification_ref": record.get("report_path", "")},
                "revision": record.get("revision", 0),
            })
        merged = dict(state)
        merged["tasks"] = tasks
        merged["handoffs"] = handoffs
        return merged

    def _handoff_card(self, role):
        """首次发送必要状态；之后只发送该角色尚未确认的增量。"""
        state = self._task_state()
        state = self._with_legacy_index(state, self._records)
        valid_revisions = [item.get("revision", 0) for item in self._history]
        ack = self._ack.get(role, 0) if role in (MASTER, WORKER) else 0
        gap = bool(ack and valid_revisions and ack < min(valid_revisions))
        full = (not ack) or ack > self._revision or gap
        if full:
            card = dict(state)
            card["handoff_mode"] = "full"
        else:
            changes = [item for item in self._history if item.get("revision", 0) > ack]
            card = {
                "protocol_version": 4,
                "task_id": state.get("task_id", "run"),
                "revision": state.get("revision", 0),
                # 该角色已确认过目标且会话仍有效；增量只传变化，避免重复注入原文。
                "goal_ref": "state.json#task_state.goal",
                "goal_source": state.get("goal_source", "current_request"),
                "supersedes_previous": state.get("supersedes_previous", False),
                "constraints": state.get("constraints", []),
                "completed": [x for x in state.get("completed", [])
                              if x.get("revision", 0) > ack],
                "decisions": [x for x in state.get("decisions", [])
                              if x.get("revision", 0) > ack],
                "artifacts": [x for x in state.get("artifacts", [])
                              if x.get("revision", 0) > ack],
                "verification": [x for x in state.get("verification", [])
                                 if x.get("revision", 0) > ack],
                "pending": state.get("pending", []),
                "unconfirmed_operations": state.get("unconfirmed_operations", []),
                "next_action": state.get("next_action", ""),
                "resume_point": state.get("resume_point", ""),
                "delta": {"from_revision": ack, "to_revision": state.get("revision", 0),
                          "changes": changes},
                "switches": state.get("switches", [])[-3:],
                "handoff_mode": "delta",
                "tasks": state.get("tasks", []),
                "handoffs": [x for x in state.get("handoffs", [])
                             if x.get("revision", 0) > ack],
            }
            if gap:
                card["handoff_mode"] = "full_gap_fallback"
                card = dict(state, handoff_mode="full_gap_fallback")
        for level in (0, 1, 2):
            shaped = self._compact_state(card, level)
            text = json.dumps(shaped, ensure_ascii=False)
            tokens = estimate_tokens(text)
            if tokens <= self.handoff_token_budget or level == 2:
                overflow = tokens > self.handoff_token_budget
                if overflow:
                    shaped["budget_exceeded"] = True
                    shaped["budget_tokens"] = tokens
                    shaped["segment_refs"] = [
                        f"{self._journal.root / 'state.json'}#{key}" for key in
                        ("completed", "decisions", "verification", "artifacts")]
                    text = json.dumps(shaped, ensure_ascii=False)
                    tokens = estimate_tokens(text)
                meta = {
                    "role": role, "mode": shaped.get("handoff_mode", "full"),
                    "from_revision": ack, "revision": self._revision,
                    "sent_revision": self._revision,
                    "tokens_estimate": tokens, "method": TOKEN_ESTIMATE_METHOD,
                    "budget": self.handoff_token_budget, "level": level,
                    "trimmed": level > 0, "overflow": overflow,
                    "billable": False,
                }
                self._pending_handoff = dict(meta)
                self._last_handoff = meta
                self._handoff_log.append(meta)
                return text, meta
        return "{}", {"error": "handoff"}

    def _state_block(self, role=MASTER):
        self._ensure_journal()
        self._save_state()
        text, meta = self._handoff_card(role)
        return (f"共享状态卡（索引不足或与磁盘冲突时读取完整约束和验证证据）："
                f"{self._journal.root / 'state.json'}\n"
                f"完整计划：{self._journal.root / 'plan.json'}\n"
                f"状态索引：\n{text}\n"
                "按任务 ID 对接；已通过任务不重复实施。发现磁盘状态与记录不符时先核实。"
                "引用仅表示可按需读取的位置，不代表内容已进入本次上下文；"
                "索引超预算时按 segment_refs 分段读取。\n")

    def _stable_prefix(self, role):
        """稳定的角色指令前缀：不含时间、路径、任务 ID 或另一模型输出。"""
        label = self._role_label(role)
        return (
            f"你是双 AI 协作系统中的「{label}」。\n"
            f"协作档次：{self.tier} · {self.policy.label}（{self.policy.short}）。\n"
            "通用规则：只做本轮任务范围，保持已确定接口；已完成且验证有效的成果直接复用；"
            "不重放已提交的操作；不传递或推测另一个模型的内部思维链。\n"
            "交接规则：只依据共享任务状态中的结论、决策依据、证据位置和未完成项；"
            "引用只是可读取位置，不代表内容已加载；缺失、过期或冲突时先核实磁盘。\n"
            "报告规则：最后一行 DUAL_RESULT 使用结构化 JSON；未执行、未验证或有未解决项的"
            "任务必须标记 blocked/failed，不得把暂定判断写成已验证事实。\n")

    def _mark_unconfirmed(self, role, label, call_ref, result, reason):
        entry = {
            "role": role, "label": label, "task_id": "",
            "call_ref": call_ref, "reason": str(reason or "调用未确认成功")[:400],
            "tool_count": len(result.tools or []),
            "has_text": bool(result.text), "cancelled": bool(result.cancelled),
            "requires_check": True,
        }
        self._unconfirmed.append(entry)
        self._bump_revision("unconfirmed", entry["reason"], detail=entry)

    def _can_switch(self):
        with self._current_lock:
            busy = self._current is not None
        return not busy and not self._cancel.is_set()

    def _record_switch(self, role, boundary, reason=""):
        if not role or role == self._last_role:
            self._last_role = role or self._last_role
            return None
        from_role = self._last_role
        if not from_role:
            # 首次角色分配不是“切换”，不制造切换成本。
            self._last_role = role
            return None
        known_cache = {}
        for item_role in (MASTER, WORKER):
            stats = self._stats.get(item_role) or {}
            known_cache[item_role] = {
                "cache_hit_rate": stats.get("cache_hit_rate"),
                "cache_measured_calls": stats.get("cache_measured_calls", 0),
                "calls": stats.get("calls", 0),
            }
        requires_check = bool(self._unconfirmed)
        allowed = self._can_switch()
        entry = {
            "from": from_role, "to": role, "boundary": boundary,
            "allowed": allowed,
            "deferred": not allowed,
            "reason": reason or f"{from_role or '开始'} → {role}：{boundary}",
            "known_cache_state": known_cache,
            "handoff": self._last_handoff,
            "handoff_revision": self._revision,
            "reread_cost": "unknown",
            "cost": None,
            "requires_check": requires_check,
            "quality_basis": "任务边界、产物落盘/结果返回；不因单轮简单而切换",
        }
        entry["revision"] = self._bump_revision(
            "switch", entry["reason"], detail={"from": from_role, "to": role,
                                               "boundary": boundary})
        self._switches.append(entry)
        self._last_role = role
        self._last_switch_reason = entry["reason"]
        self._save_state()
        return entry

    def _run_agent(self, role, prompt, label, display=True, read_only=False,
                   timeout=1200, report_only=False):
        if self._cancel.is_set():
            return AgentResult(error="已取消", cancelled=True)
        spec = self._effort_for(self._spec_for(role))
        self._ensure_journal()
        # 角色隔离，即使同源同模型也不能互相恢复会话。
        session_key = self._session_key(role)
        session_id = self._sessions.get(session_key, "")
        self._status_touch(label, role=role, task=label)
        if display:
            self.role_started.emit(role, label)
        agent = CliAgent(
            self.cwd, env=self.claude_env, claude_path=self.claude_path,
            codex_path=self.codex_path, gemini_path=self.gemini_path,
            cancel_event=self._cancel, timeout=timeout)
        agent.report_only = report_only
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
        stable_prefix = self._stable_prefix(role)

        def payload(resume):
            if resume and session_key in self._started_keys:
                dynamic = ("同一角色会话续接。共享状态卡是跨角色交接依据；请核实磁盘现状。\n")
                self._pending_handoff = {
                    "role": role, "mode": "role_session_resume", "revision": self._revision,
                    "sent_revision": self._revision, "tokens_estimate": 0,
                    "method": TOKEN_ESTIMATE_METHOD, "budget": self.handoff_token_budget,
                    "trimmed": False, "overflow": False, "billable": False}
            else:
                dynamic = self._bootstrap(role, resumed=bool(resume))
            return stable_prefix + dynamic + stage_prompt

        initial_payload = payload(bool(session_id))
        handoff_meta = self._pending_handoff
        self._pending_handoff = None
        self._prompt_chars += len(initial_payload)
        tool_versions = {a.get("path"): a.get("version")
                         for a in self._refresh_artifacts().values()}

        def call_meta(handoff, result):
            metrics = usage_metrics(spec.source, result.usage)
            cost_source = "provider" if result.cost_usd is not None else None
            if result.cost_usd is None and self.pricing is not None:
                priced = price_usage(metrics, spec.model, self.pricing)
                if priced is not None:
                    result.cost_usd = priced
                    cost_source = (f"pricing:{self.pricing.get('source')}@"
                                   f"{self.pricing.get('version')}")
                    self.note.emit("费用依据已提供且带版本的计费配置估算；非接口返回值。")
            events = self._duplicate_tracker.observe(result.tools, tool_versions)
            self._tool_call_count += len(result.tools or [])
            duplicates = sum(1 for e in events if e.get("duplicate"))
            self._duplicate_tool_calls += duplicates
            self._necessary_rechecks += sum(1 for e in events if e.get("necessary_recheck"))
            return {
                "model": spec.model, "provider": spec.source, "kind": label,
                "handoff": handoff, "switch_reason": self._last_switch_reason,
                "token_method": "provider-usage" if result.usage else "unknown",
                "cost_source": cost_source,
                "cost_estimated": bool(cost_source and cost_source.startswith("pricing:")),
                "duplicate_tool_calls": duplicates,
                "necessary_rechecks": sum(1 for e in events if e.get("necessary_recheck")),
                "summary_call": ("压缩" in label or "摘要" in label),
                "tool_events": events[:40],
            }

        try:
            result = agent.run(
                spec, initial_payload, self.permission_mode, read_only,
                on_delta, on_message, on_tool, session_id=session_id)
            meta = call_meta(handoff_meta, result)
            self._account(role, result)
            if not result.ok and result.error:
                self._status_fail(result.error)
            self._last_call_path = self._journal.call(
                role, label, result, bool(session_id), spec.source, meta=meta)
            if not result.ok or result.cancelled:
                if result.tools or result.text:
                    self._mark_unconfirmed(role, label, self._last_call_path, result,
                                           result.error or "调用未确认成功")
            elif handoff_meta:
                self._ack[role] = max(self._ack.get(role, 0),
                                      int(handoff_meta.get("sent_revision") or 0))
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
                retry_handoff = self._pending_handoff
                self._pending_handoff = None
                self._prompt_chars += len(retry_payload)
                result = agent.run(
                    spec, retry_payload, self.permission_mode, read_only,
                    on_delta, on_message, on_tool, session_id="")
                retry_meta = call_meta(retry_handoff, result)
                self._account(role, result)
                self._last_call_path = self._journal.call(
                    role, label, result, False, spec.source, meta=retry_meta)
                if result.ok and not result.cancelled and retry_handoff:
                    self._ack[role] = max(self._ack.get(role, 0),
                                          int(retry_handoff.get("sent_revision") or 0))
            if result.session_id and result.ok and not result.cancelled:
                self._sessions[session_key] = result.session_id
                self._started_keys.add(session_key)
            elif not result.ok or result.cancelled:
                self._sessions.pop(session_key, None)
            if "压缩" in label or "摘要" in label:
                self._summary_calls += 1
            self._save_state()
        finally:
            with self._current_lock:
                self._current = None
        return result

    def _account(self, role, result):
        stats = self._stats[role]
        stats["calls"] += 1
        stats["cost"] += float(result.cost_usd or 0)
        stats["cost_known_calls"] = stats.get("cost_known_calls", 0) + (result.cost_usd is not None)
        stats["seconds"] += float(result.elapsed or 0)
        stats["tool_count"] = stats.get("tool_count", 0) + len(result.tools)
        metrics = usage_metrics(self._spec_for(role).source, result.usage)
        for key, value in metrics.items():
            if key == "cache_hit_rate":
                continue
            if value is not None:
                stats[key] = stats.get(key, 0) + value
                known = key + "_known_calls"
                stats[known] = stats.get(known, 0) + 1
        if metrics["cache_hit_rate"] is not None:
            stats["cache_measured_input"] = stats.get("cache_measured_input", 0) + metrics["input_tokens"]
            stats["cache_measured_read"] = stats.get("cache_measured_read", 0) + metrics["cache_read_input_tokens"]
            stats["cache_measured_calls"] = stats.get("cache_measured_calls", 0) + 1
            stats["cache_hit_rate"] = stats["cache_measured_read"] / stats["cache_measured_input"]
        stats["tokens"] = stats.get("input_tokens", 0) + stats.get("output_tokens", 0)
        calls = max(1, stats.get("calls", 0))
        stats["usage_coverage"] = round(stats.get("input_tokens_known_calls", 0) / calls, 4)
        stats["cost_coverage"] = round(stats.get("cost_known_calls", 0) / calls, 4)
        stats["cache_coverage"] = round(stats.get("cache_measured_calls", 0) / calls, 4)

    def _measurement(self):
        coverage = {}
        for role in (MASTER, WORKER):
            stats = self._stats.get(role) or {}
            coverage[role] = {
                "calls": stats.get("calls", 0),
                "usage_coverage": stats.get("usage_coverage", 0.0),
                "cost_coverage": stats.get("cost_coverage", 0.0),
                "cache_coverage": stats.get("cache_coverage", 0.0),
            }
        return {
            "token_counting": {
                "usage": "provider-returned token fields only; missing fields stay unknown",
                "handoff": estimate_tokens_label(),
            },
            "cost": {
                "source": ("provider total_cost_usd when returned; otherwise verified "
                           "pricing config with source/version; else unknown"),
                "pricing_source": (self.pricing.get("source") if self.pricing else None),
                "pricing_version": (self.pricing.get("version") if self.pricing else None),
            },
            "cache": ("cache_read_input_tokens / total input per role, model and provider; "
                      "unknown stays null; no cross-model or cross-provider sharing assumed"),
            "coverage": coverage,
            "summary_calls": self._summary_calls,
            "duplicate_tool_calls": self._duplicate_tool_calls,
            "necessary_rechecks": self._necessary_rechecks,
            "tool_calls_observed": self._tool_call_count,
        }

    def _ensure_journal(self):
        if self._journal is None:
            self._journal = RunJournal(self.cwd, self.request, self.context, self.previous_state)
            if self.context_delta is not None:
                self._journal.write("context-delta.json", {"context": self.context_delta})
            for role in (MASTER, WORKER):
                key = self._session_key(role)
                for row in self._resume_candidates:
                    if (isinstance(row, dict) and row.get("key") == list(key)
                            and isinstance(row.get("id"), str) and row["id"].strip()):
                        self._sessions[key] = row["id"]
            if self._journal.previous_error:
                self.note.emit(self._journal.previous_error)
            self._bump_revision("run_started", "共享任务状态初始化")
            self._save_state()

    def _save_state(self):
        if self._journal is None:
            return
        state = self._task_state()
        self._journal.write("state.json", {
            "protocol_version": 4,
            "revision": state["revision"],
            "task_state": state,
            "plan": self._plan,
            "review_enabled": self.review_enabled,
            "review_mode": self.review_mode,
            "previous_checkpoint": self._journal.previous,
            "context_brief": (str(self._journal.root / "context-brief.json")
                              if self._compressed_context else ""),
            "records": [{k: v for k, v in r.items() if k != "text"} for r in self._records],
            "sessions": [{"role": k[0], "source": k[1], "model": k[2], "id": v}
                         for k, v in self._sessions.items()],
            "stats": self._stats,
            "handoffs": self._handoff_log[-30:],
            "acknowledged_revisions": dict(self._ack),
            "switches": self._switches,
            "unconfirmed_operations": self._unconfirmed,
            "measurement": self._measurement(),
        })

    def _bootstrap(self, role="", resumed=False):
        self._ensure_journal()
        brief = self._state_brief()
        self._pending_handoff = {
            "role": role, "mode": "resumed_bootstrap" if resumed else "bootstrap",
            "revision": self._revision, "sent_revision": self._revision,
            "tokens_estimate": estimate_tokens(brief), "method": TOKEN_ESTIMATE_METHOD,
            "budget": self.handoff_token_budget, "trimmed": False,
            "overflow": False, "billable": False,
        }
        if resumed:
            context = self.context if self.context_delta is None else self.context_delta
            if len(context) > self.CONTEXT_COMPRESS_THRESHOLD:
                context = (self._compressed_context if role == MASTER and self._compressed_context else
                           f"必须读取新增上下文：{self._journal.root / ('input.json' if self.context_delta is None else 'context-delta.json')}")
            return (f"同一对话的新一轮请求（优先于旧任务）：\n{self.request}\n"
                    f"项目目录：{self.cwd}\n共享状态卡：{self._journal.root / 'state.json'}\n"
                    f"完整原始输入（缺项或冲突时核实）：{self._journal.root / 'input.json'}\n"
                    f"任务状态：{brief}\n"
                    f"新增上下文：\n{context or '（无，历史已在当前角色会话中）'}\n"
                    "旧阶段指令已结束；按本轮权限、任务和约束继续，已完成工作不重做。\n")
        prior = (f"上轮交接快照：{self._journal.previous}，首次接手按需核实。\n"
                 "沿用仍适用的接口、约束及未解决项；最新用户请求优先，已通过工作先核实再复用；"
                 "旧协议缺失字段保持未知。\n"
                 if self._journal.previous else self._journal.previous_error + "\n")
        long_uncompressed = (len(self.context) > self.CONTEXT_COMPRESS_THRESHOLD
                             and not self._compressed_context)
        if role == MASTER and self._compressed_context:
            input_rule = (
                f"上下文摘要：{self._journal.root / 'context-brief.json'}\n"
                f"完整原始输入（按需定位核实）：{self._journal.root / 'input.json'}\n"
                "读取上下文摘要和共享状态卡，保留全部用户约束；"
                "历史原文仅在摘要缺项、冲突或需要核实具体证据时按需定位读取。\n")
        elif long_uncompressed:
            # 压缩失败或未压缩的长上下文必须核实原文，避免丢失关键约束。
            input_rule = (
                f"完整原始输入：{self._journal.root / 'input.json'}\n"
                "首次接手必须读取完整原始输入和共享状态卡，保留全部用户约束。\n")
        else:
            input_rule = (
                f"完整原始输入（缺项或冲突时按需定位核实，不默认全量传递）："
                f"{self._journal.root / 'input.json'}\n"
                "共享状态卡是本轮交接依据；短上下文已附在下方，保留全部用户约束。\n")
        return (f"用户原始请求：\n{self.request}\n项目目录：{self.cwd}\n"
                f"协作档次：{self.tier} · {self.policy.label}（{self.policy.short}）\n" + prior +
                f"共享状态卡：{self._journal.root / 'state.json'}\n"
                f"任务状态：{brief}\n"
                + input_rule +
                "最近对话上下文：\n" + self._context_block(role) + "\n")

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
- 已定位且足够的证据直接复用；不要把规划已完成的调查再拆给另一个模型。
- 简单任务可只含一个执行任务；不要为调用第二个模型制造工作。
- constraints 只保留跨阶段用户约束；本次规划的只读限制不要误写成后续执行禁令。
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
                        '  DUAL_ASK: {"task":"当前任务 ID","problem":"一句话","need":"需要的决定","tried":["已试"],"completed":[]}\n'
                        "- completed 可携带已完成的连续前序任务的完整逐项报告和验证证据，避免询问后重做。\n")
        return f"""你是双 AI 协作系统中的「{label}」。档次 {self.tier} · {self.policy.short}。
{self._state_block(role)}
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
        source = (self._journal.root / 'context-delta.json'
                  if self._session_key(MASTER) in self._sessions and self.context_delta is not None
                  else self._journal.root / 'input.json')
        return f"""你是执行 AI。先为下一个角色压缩有效上下文，节省主 AI 输入额度。

项目目录：{self.cwd}
压缩源：读取 {source} 的 context 字段。已有主 AI 会话时仅压缩新增内容，历史约束仍保留在原会话。
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
当前任务包：
{json.dumps(tasks, ensure_ascii=False)}

已确认的成功前缀已从任务包移除；只续做上述剩余 ID，不重做或重复报告已通过项。
按指导继续，不要扩大范围。完成后按交付协议输出 DUAL_RESULT。
本轮剩余询问次数：{max(0, self.policy.max_asks - self._ask_count)}；用尽后仍有阻塞则报告 blocked。
"""

    def _ask_prefix(self, tasks, ask):
        """Accept only a contiguous, verified prefix before the task asking for help."""
        items = ask.get("completed")
        if not isinstance(items, list) or not items or len(items) >= len(tasks):
            return []
        if ([item.get("id") if isinstance(item, dict) else None for item in items]
                != [task["id"] for task in tasks[:len(items)]]):
            return []
        if ask["task"] not in {t["id"] for t in tasks[len(items):]}:
            return []
        if not all(valid_report(item) and item["status"] == "passed" and not item.get("open_issues")
                   for item in items):
            return []
        return items

    def _repair_result(self, role, tasks, result):
        """One read-only report repair; never retry a failed transport or replay implementation."""
        if not result.ok or result.cancelled or result.error or self._cancel.is_set():
            return result
        raw = _last_prefixed_json(result.text, "DUAL_RESULT:")
        if not raw or raw.get("status") != "passed" or raw.get("open_issues"):
            return result
        items = raw.get("tasks")
        expected = [t["id"] for t in tasks]
        if items is not None:
            if not isinstance(items, list):
                return result
            ids = [item.get("id") if isinstance(item, dict) else None for item in items]
            if ids != expected[:len(items)] or any(
                    item.get("status") != "passed" or item.get("open_issues") for item in items):
                return result
        reports = self._task_reports(tasks, result)
        if all(item["status"] == "passed" for item in reports):
            return result
        original_path = self._last_call_path
        self._report_repairs += 1
        self.note.emit("交付报告缺项：先只读补报一次，不重做实施或验证命令。")
        repaired = self._run_agent(role,
            "仅补齐已执行工作的交付报告，禁止执行工具、修改文件、重跑测试或重新实施。\n"
            "只能引用当前会话和原报告已有的实际验证证据；没有证据的项必须 blocked。\n"
            "不得把明确失败、未执行或留待用户执行的操作改称已完成。\n"
            f"按顺序报告全部 ID：{json.dumps(expected)}。保留已有效的成功前缀内容。\n"
            f"原始调用记录：{original_path or '见下方原报告'}\n原报告：\n{result.text}\n"
            "最后一行 DUAL_RESULT 必须有 status、summary、非空 verification 和逐项 tasks。",
            f"{self._role_label(role)} · 补报 {','.join(expected)}",
            display=False, read_only=True, timeout=self.GUIDE_TIMEOUT, report_only=True)
        fixed = self._task_reports(tasks, repaired)
        if all(item["status"] == "passed" for item in fixed) and all(
                old["status"] != "passed" or old == new for old, new in zip(reports, fixed)):
            return repaired
        # Failed repair cannot erase the original verified prefix.
        self._last_call_path = original_path
        return result

    def _takeover_prompt(self, plan, task, failed_result):
        reason = failed_result.error or self._records[-1].get("report", {}).get("summary") or "执行 AI 未能给出可用结果"
        return f"""你是主 AI（M）。执行 AI 未能完成任务 {task['id']}，现在由你接管。
{self._state_block(MASTER)}
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

{self._state_block(MASTER)}

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
        record["revision"] = self._bump_revision(
            "record", f"{task['id']} {report['status']}: {report.get('summary', '')}"[:300],
            task_id=task["id"])
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
            compress_context = (self.context_delta if self._session_key(MASTER) in self._sessions
                                and self.context_delta is not None else self.context)
            if policy.precompress and len(compress_context) > self.CONTEXT_COMPRESS_THRESHOLD:
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
                    if len(serialized) < len(compress_context):
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
            self._resume_point = "规划阶段"
            self._record_switch(MASTER, "规划/契约开始（上下文压缩结果已落盘）" if
                                self._compressed_context else "规划/契约开始")
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
            self._task_total = len(plan.get("tasks") or [])
            self._resume_point = (f"已规划 {self._task_total} 项任务，等待执行"
                                  if self._task_total else "已规划，无独立任务")
            self._bump_revision("plan", plan.get("summary", "计划已落盘"),
                                detail={"tasks": [t.get("id") for t in plan.get("tasks", [])]})
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
                self._task_index = index
                self._resume_point = (f"任务 {index + 1}/{len(tasks)}"
                                      f"（{','.join(t['id'] for t in batch)}）")
                self._record_switch(role, "任务包开始（前序结果已返回并落盘）")
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
                    prefix = self._ask_prefix(batch, ask)
                    for finished_task, finished_report in zip(batch, prefix):
                        self._work_points[role] += finished_task["points"]
                        self._record(finished_task, finished_report, result)
                        index += 1
                    batch = batch[len(prefix):]
                    batch_ids = {t["id"] for t in batch}
                    ask = dict(ask, completed=[])
                    self._ask_count += 1
                    ids = ",".join(t["id"] for t in batch)
                    self.note.emit(
                        f"执行 AI 询问 {ask['task'] or ids}：{ask['problem'][:120]}"
                        " → 主 AI 二次指导")
                    self._record_switch(MASTER, "执行 AI 已返回询问，工具结果已返回")
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
                    self._record_switch(WORKER, "主 AI 指导已返回，继续未完成任务")
                    result = self._run_agent(
                        role, self._followup_prompt(batch, ask, guide),
                        f"{self._role_label(role)} · {ids}（续）",
                        display=True, timeout=self.TASK_TIMEOUT)
                    if self._cancel.is_set() or result.cancelled:
                        self._finish_cancelled(plan)
                        return
                    ask = extract_ask(result)
                result = self._repair_result(role, batch, result)
                if self._cancel.is_set() or result.cancelled:
                    self._finish_cancelled(plan)
                    return
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
                    self._record_switch(
                        MASTER, "执行失败已停止；先核对未确认操作再接管",
                        reason=(f"执行 AI 未完成 {task['id']}，结果已返回；"
                                "恢复前必须核对未确认操作"))
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
                self._resume_point = "验收阶段"
                self._record_switch(MASTER, "全部任务结果已落盘，进入验收")
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
            "ok": (not cancelled and not self._unconfirmed and bool(self._records)
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
            "protocol_version": 4,
            "revision": self._revision,
            "state_dir": str(self._journal.root) if self._journal else "",
            "session_resets": self._session_resets,
            "session_state": self._export_sessions(),
            "report_repairs": self._report_repairs,
            "handoffs": self._handoff_log[-30:],
            "acknowledged_revisions": dict(self._ack),
            "handoff_tokens": sum(int(item.get("tokens_estimate") or 0)
                                  for item in self._handoff_log),
            "handoff_method": TOKEN_ESTIMATE_METHOD,
            "handoff_budget": self.handoff_token_budget,
            "switches": self._switches,
            "switch_count": len(self._switches),
            "summary_calls": self._summary_calls,
            "duplicate_tool_calls": self._duplicate_tool_calls,
            "necessary_rechecks": self._necessary_rechecks,
            "tool_calls_observed": self._tool_call_count,
            "unconfirmed_operations": self._unconfirmed,
            "measurement": self._measurement(),
            "task_state": self._task_state(),
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
