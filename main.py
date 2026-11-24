#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude 控制台 —— 在桌面窗口中与 Claude Code 对话。"""

import asyncio
import difflib
import html
import json
import os
import queue
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from PyQt5.QtCore import (QBuffer, QByteArray, QDir, QEvent, QIODevice, QObject,
                          QPoint, QProcess, QRect, QSize, QSortFilterProxyModel,
                          QStringListModel, Qt, QThread, QTimer, QUrl,
                          pyqtSignal)
from PyQt5.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics,
                         QGuiApplication, QIcon, QImage, QKeySequence, QPainter,
                         QPixmap, QSyntaxHighlighter, QTextCharFormat,
                         QTextCursor, QTextFormat)
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                             QComboBox, QCompleter, QDialog, QFileDialog,
                             QFormLayout, QFrame, QFileSystemModel,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QListView,
                             QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QScrollArea,
                             QShortcut, QSlider, QSpinBox,
                             QSplitter, QStackedWidget, QTabWidget,
                             QTextBrowser, QTextEdit, QToolButton, QTreeView,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

import agent_shared as shared_agents
import background_monitor
import dual_ai
import image_ocr
import win_compat

# --------------------------------------------------------------------------- #
# 常量 & 依赖
# --------------------------------------------------------------------------- #
APP_NAME = "Claude 控制台"
APP_VERSION = "2.0"
ACCENT = "#E4638C"

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

try:
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ClaudeSDKClient, PermissionResultAllow,
                                  PermissionResultDeny, ResultMessage,
                                  StreamEvent, TextBlock, ToolUseBlock)
    SDK_AVAILABLE = True
    SDK_IMPORT_ERROR = ""
except Exception as _sdk_exc:  # noqa: BLE001 - 缺少依赖时界面给出提示
    SDK_AVAILABLE = False
    SDK_IMPORT_ERROR = str(_sdk_exc)

warnings.filterwarnings("ignore", message="can_use_tool will not be invoked.*")


def default_config_home() -> Path:
    """配置根目录：有 XDG_CONFIG_HOME 时沿用，否则按平台惯例。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    if win_compat.IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    if win_compat.IS_MACOS:
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".config"


CONFIG_DIR = default_config_home() / "claude-console"
CONFIG_PATH = CONFIG_DIR / "config.json"
STUDY_DIR = CONFIG_DIR / "studies"
DEV_RECORDS_DIR = CONFIG_DIR / "dev_records"
ICON_PATH = BASE_DIR / "icon.png"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Claude CLI 以 NDJSON 往 stdout 写消息，SDK 默认只给单条消息 1 MiB 缓冲。
# 粘贴截图（base64）、读取大文件或大段工具输出时，单条消息会超过 1 MiB，
# SDK 就会抛 CLIJSONDecodeError 并把整轮对话判为连接失败。这里放宽到 64 MiB，
# 仍然有上限，避免异常输出把内存吃光。
def _cli_max_buffer_bytes():
    raw = os.environ.get("CLAUDE_CONSOLE_MAX_BUFFER_MB", "")
    try:
        mb = int(raw)
    except ValueError:
        mb = 0
    if mb <= 0:
        mb = 64
    return mb * 1024 * 1024


MAX_CLI_MESSAGE_BYTES = _cli_max_buffer_bytes()

DEFAULT_CONFIG = {
    "projects": [],
    "history": [],
    "favorites": [],
    "geometry": "",
    "permission_mode": "default",
    "model": "",
    "effort": "high",
    # 双 AI 协作：主 AI / 执行 AI 的模型与协作档次
    "dual_mode": False,
    "dual_master": {},
    "dual_worker": {},
    "dual_tier": "balanced",
    # 完成后是否让主 AI 独立复查；默认开启，兼容旧配置。
    "dual_review_enabled": True,
    # 旧配置仅用于一次性迁移：dual_share / dual_strategy 不再参与调度。
    "dual_share": 30,
    "dual_strategy": "complete",
    # 代码开发页：允许修改的文件夹与修改模式
    "dev_roots": [],
    "dev_mode": "function",
    # 技能分类：{技能目录名: 分类}，可在技能库右键修改
    "skill_categories": {},
}

# 技能库分类。顺序即界面显示顺序。
SKILL_CATEGORIES = (
    ("media", "🎨 生图 / 视频"),
    ("ppt", "📊 PPT"),
    ("code", "💻 开发 / 代码"),
    ("design", "🖌 设计 / 网页"),
    ("other", "📦 其他"),
)
SKILL_CATEGORY_OVERRIDES = {
    "media-tools": "media",
    "vision-review": "media",
    "handdrawn-tech-illustrations": "media",
    "atlas-cloud": "media",
    "seedance-2-5-skill": "media",
    "universal-video-prompt-skill": "media",
    "gpt-image-2-style-library": "media",
    "dsh-media-skills": "media",
    "ppt-master": "ppt",
    "web-artifact-designer": "media",
}


def infer_skill_category(skill):
    name = f"{skill.get('dir_name', '')} {skill.get('name', '')}".lower()
    desc = str(skill.get("description") or "").lower()
    haystack = name + " " + desc
    if any(key in haystack for key in (
            "ppt", "presentation", "slide", "deck", "幻灯片", "演示文稿")):
        return "ppt"
    if any(key in haystack for key in (
            "图片", "图像", "生图", "插画", "配图", "海报", "封面", "视频",
            "image", "illustration", "media", "video", "poster", "banner",
            "stable diffusion", "midjourney", "gpt-image", "seedance",
            "atlas cloud", "手绘", "视觉素材")):
        return "media"
    if any(key in haystack for key in (
            "网页", "设计", "html", "svg", "css", "artifact", "ui ",
            "前端", "landing page", "infographic")):
        return "design"
    if any(key in haystack for key in (
            "code", "代码", "review", "debug", "test", "security", "git",
            "refactor", "工程")):
        return "code"
    return "other"


def skill_category(skill, config=None):
    dir_name = skill.get("dir_name", "")
    saved = (config or {}).get("skill_categories", {}) or {}
    return saved.get(dir_name) or SKILL_CATEGORY_OVERRIDES.get(
        dir_name) or infer_skill_category(skill)


# 对话输入框里可以直接命令控制台生图 / 生视频的前缀
IMAGE_COMMAND_PREFIXES = (
    "/img ", "/image ", "/pic ", "/draw ", "/生图 ", "/画 ",
    "生成图片：", "生成图片:", "生成图片 ", "生成一张图片：",
    "生成一张图片:", "生成一张图片 ", "画一张", "画一幅", "画个图",
    "画图：", "画图:", "生成插图：", "生成插图:", "配图：", "配图:",
    "帮我生成图片：", "帮我生成图片:", "帮我画一张", "帮我画一幅",
    "请帮我生成图片：", "请帮我生成图片:", "给我生成图片：",
    "给我生成图片:", "生成一张图：", "生成一张图:",
)
VIDEO_COMMAND_PREFIXES = (
    "/video ", "/vid ", "/视频 ",
    "生成视频：", "生成视频:", "生成视频 ", "生成一段视频：",
    "生成一段视频:", "生成一段视频 ", "做一段视频：", "做一段视频:",
    "做个视频：", "做个视频:", "做个视频 ", "帮我生成视频：",
    "帮我生成视频:", "帮我做一段视频：", "帮我做一段视频:",
    "请帮我生成视频：", "请帮我生成视频:", "帮我做个视频：",
)

MEDIA_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
MEDIA_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".avi")

# 权限模式：(显示名称, Claude Code 模式, 说明)
PERMISSION_MODES = (
    ("请求批准", "default", "每次敏感操作都会弹窗询问是否允许"),
    ("帮我批准", "acceptEdits", "自动批准文件编辑，其他敏感操作仍会询问"),
    ("完全访问", "bypassPermissions", "跳过所有权限确认，Claude 可以直接执行任何操作"),
)

# 模型列表优先从当前服务商（cc-switch 写入的 settings.json）自动获取，
# 下面这份只是获取失败时的兜底。
FALLBACK_MODELS = (
    {"id": "deepseek-flash[1M]", "name": "Flash · 快速"},
    {"id": "deepseek-v4-pro[1M]", "name": "Pro · 深度"},
)

# 思考档位直接显示服务商返回的英文值（low / high / max 等），不做翻译。
FALLBACK_EFFORTS = ("low", "high", "max")
# Claude Code CLI 的 --effort 实际接受的取值；服务商档位需与之取交集
EFFORT_CLI_LEVELS = ("low", "medium", "high", "xhigh", "max")

TOOL_LABELS = {
    "Bash": "终端命令",
    "BashOutput": "终端输出",
    "Edit": "编辑文件",
    "Write": "写入文件",
    "Read": "读取文件",
    "Glob": "查找文件",
    "Grep": "搜索内容",
    "WebFetch": "访问网页",
    "WebSearch": "网络搜索",
    "TodoWrite": "任务清单",
    "Task": "子任务",
    "NotebookEdit": "编辑笔记本",
}

# 开发模式：会改动文件的工具，以及无需询问的只读工具
DEV_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
DEV_READ_ONLY_TOOLS = {
    "Read", "Grep", "Glob", "LS", "BashOutput", "WebFetch", "WebSearch",
    "TodoWrite", "NotebookRead", "ListMcpResourcesTool", "ReadMcpResourceTool",
    "ExitPlanMode",
}

_claude_cache = {"done": False, "path": None}

# 从桌面/应用菜单启动时 PATH 往往不含 nvm、npm-global 等目录，
# 因此不能只依赖 shutil.which，还要主动检查这些常见安装位置。
CLAUDE_PATH_PATTERNS = (
    ".nvm/versions/node/*/bin/claude",
    ".local/share/fnm/node-versions/*/installation/bin/claude",
    ".nodenv/versions/*/bin/claude",
    ".asdf/installs/nodejs/*/bin/claude",
    ".volta/bin/claude",
)
CLAUDE_FIXED_PATHS = (
    "~/.local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.bun/bin/claude",
    "/usr/local/bin/claude",
    "/usr/bin/claude",
)
if win_compat.IS_WINDOWS:
    # npm 全局安装：claude.cmd（shim）或原生安装器：claude.exe
    CLAUDE_FIXED_PATHS += tuple(
        str(p) for p in win_compat.npm_candidate_paths(("claude.exe", "claude.cmd")))
    CLAUDE_FIXED_PATHS += (
        str(Path.home() / ".local" / "bin" / "claude.exe"),
    )


def _version_key(path):
    match = re.search(r"/v?(\d+(?:\.\d+)*)/", str(path))
    return tuple(int(x) for x in match.group(1).split(".")) if match else ()


def find_claude():
    """定位 claude 可执行文件，兼容桌面启动时 PATH 不完整的情况。"""
    if _claude_cache["done"]:
        return _claude_cache["path"]

    found = shutil.which("claude")

    if not found:
        matches = [Path(p).expanduser() for p in CLAUDE_FIXED_PATHS]
        for pattern in CLAUDE_PATH_PATTERNS:
            matches.extend(Path.home().glob(pattern))
        matches = [p for p in matches if p.is_file() and os.access(p, os.X_OK)]
        if matches:
            # 优先选版本号最高、其次最新安装的那一个
            found = str(max(matches, key=lambda p: (_version_key(p), p.stat().st_mtime)))

    if not found and shutil.which("bash"):
        # 最后向登录 shell 询问一次（登录 shell 会加载 nvm 等配置）
        try:
            result = subprocess.run(
                ["bash", "-lc", "command -v claude"],
                capture_output=True, text=True, timeout=5,
                env=os.environ.copy())
            for line in reversed(result.stdout.splitlines()):
                candidate = line.strip()
                if candidate and Path(candidate).is_file():
                    found = candidate
                    break
        except (OSError, subprocess.SubprocessError):
            pass

    _claude_cache["done"] = True
    _claude_cache["path"] = found
    return found


def claude_path_env():
    """返回补上 claude 所在目录后的 PATH，供新终端/SDK 使用。"""
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    executable = find_claude()
    if executable:
        bin_dir = str(Path(executable).parent)
        if bin_dir not in path_env.split(os.pathsep):
            path_env = bin_dir + os.pathsep + path_env
    return path_env


def find_native_claude_exe():
    """返回可直接被 CreateProcess 执行的 claude.exe。

    Windows 上 npm 安装的是 ``claude.cmd`` shim，Claude Agent SDK 会出于
    命令注入风险拒绝执行它。优先找原生安装的 claude.exe；找不到时退回
    SDK 自带的 claude.exe（Windows wheel 会打包一个）。
    """
    if not win_compat.IS_WINDOWS:
        return find_claude()
    found = shutil.which("claude.exe")
    if found:
        return found
    candidates = [Path.home() / ".local" / "bin" / "claude.exe"]
    candidates.extend(win_compat.npm_candidate_paths(("claude.exe",)))
    try:
        import claude_agent_sdk
        candidates.append(
            Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude.exe")
    except Exception:  # noqa: BLE001 - SDK 缺失时继续尝试其他位置
        pass
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# 服务商模型目录（cc-switch 会把当前服务商写入 ~/.claude/settings.json）
# --------------------------------------------------------------------------- #
def provider_env():
    """合并进程环境与 ~/.claude/settings.json 里的 env，后者优先。"""
    env = {str(k): str(v) for k, v in os.environ.items()}
    try:
        data = json.loads(
            (Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
        settings_env = data.get("env") if isinstance(data, dict) else None
        if isinstance(settings_env, dict):
            env.update({str(k): str(v) for k, v in settings_env.items()})
    except (OSError, ValueError):
        pass
    return env


def provider_signature():
    """用于判断 cc-switch 是否切换了服务商。"""
    env = provider_env()
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
             or env.get("OPENAI_API_KEY") or "")
    return (env.get("ANTHROPIC_BASE_URL", ""), token[-8:],
            env.get("ANTHROPIC_MODEL", ""))


def provider_settings_mtime():
    try:
        return (Path.home() / ".claude" / "settings.json").stat().st_mtime
    except OSError:
        return 0.0


def _model_catalog_urls(base_url):
    base = (base_url or "").rstrip("/")
    if not base:
        return []
    urls = []
    if base.endswith("/anthropic"):
        root = base[: -len("/anthropic")]
        urls += [root + "/models", base + "/v1/models", root + "/v1/models"]
    elif base.endswith("/v1"):
        urls.append(base + "/models")
    else:
        urls += [base + "/models", base + "/v1/models"]
    return list(dict.fromkeys(urls))


def _parse_model_payload(payload):
    if isinstance(payload, dict):
        items = (payload.get("data") or payload.get("models")
                 or payload.get("result") or [])
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    models = []
    for item in items:
        if isinstance(item, str):
            model_id, name, extra = item, item, {}
        elif isinstance(item, dict):
            model_id = (item.get("id") or item.get("model")
                        or item.get("name") or "")
            name = (item.get("display_name") or item.get("displayName")
                    or item.get("name") or model_id)
            extra = item
        else:
            continue
        if not model_id:
            continue
        effort_meta = extra.get("effort") if isinstance(extra, dict) else None
        levels = []
        default_level = None
        if isinstance(effort_meta, dict):
            raw_levels = effort_meta.get("supported_levels")
            if isinstance(raw_levels, list):
                # 服务商报告的档位全部保留，界面只负责翻译显示
                levels = [str(x) for x in raw_levels if str(x).strip()]
            raw_default = effort_meta.get("default_level")
            if raw_default is not None and str(raw_default).strip():
                default_level = str(raw_default)
        context = extra.get("context_window") if isinstance(extra, dict) else None
        models.append({
            "id": str(model_id),
            "name": str(name),
            "efforts": levels,
            "default_effort": default_level,
            "context": context if isinstance(context, int) else None,
        })
    return models


def fetch_provider_models():
    """按当前服务商读取模型列表。失败时抛出异常，由界面回退到内置列表。"""
    env = provider_env()
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
             or env.get("OPENAI_API_KEY") or "")
    urls = _model_catalog_urls(base_url)
    if not urls:
        raise RuntimeError("未配置 ANTHROPIC_BASE_URL")

    headers = {
        "Accept": "application/json",
        "User-Agent": "claude-console",
        "anthropic-version": "2023-06-01",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token

    last_error = None
    for url in urls:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 换下一个候选地址继续尝试
            last_error = exc
            continue
        models = _parse_model_payload(payload)
        if models:
            models = _apply_context_suffix(models, env.get("ANTHROPIC_MODEL", ""))
            for model in models:
                model["source"] = url
            return models
    raise RuntimeError(f"无法获取模型列表：{last_error or '服务商返回了空列表'}")


def _apply_context_suffix(models, env_model):
    """沿用当前模型上的上下文后缀（如 [1M]），避免切换后丢掉 1M 上下文。"""
    match = re.search(r"(\[[^\]]+\])$", env_model or "")
    if not match:
        return models
    suffix = match.group(1)
    base_env_model = env_model[: -len(suffix)]
    for model in models:
        model_id = model["id"]
        if model_id == env_model or model_id.endswith(suffix):
            continue
        if model_id == base_env_model:
            model["id"] = env_model
        elif "[" not in model_id:
            model["id"] = model_id + suffix
    return models


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def default_projects():
    projects = []
    desktop = Path.home() / "桌面"
    if desktop.is_dir():
        projects.append({"name": "桌面", "path": str(desktop)})
    projects.append({"name": "主目录", "path": str(Path.home())})
    return projects


def legacy_config_path() -> Path | None:
    """Windows 上旧版本把配置写在 ~/.config 下，升级后沿用一次。"""
    if not win_compat.IS_WINDOWS:
        return None
    return Path.home() / ".config" / "claude-console" / "config.json"


def load_config():
    path = CONFIG_PATH
    if not path.exists():
        legacy = legacy_config_path()
        if legacy is not None and legacy.is_file():
            path = legacy
        else:
            cfg = dict(DEFAULT_CONFIG)
            cfg["projects"] = default_projects()
            return cfg
    cfg = dict(DEFAULT_CONFIG)
    migrate_tier = False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, default in DEFAULT_CONFIG.items():
            value = data.get(key)
            if isinstance(value, type(default)):
                cfg[key] = value
        # 合并默认值后就无法区分旧配置；在这里用原始键完成迁移。
        if not data.get("dual_tier"):
            cfg["dual_tier"] = dual_ai.normalize_tier(cfg["dual_share"])
            migrate_tier = True
    except (OSError, ValueError):
        pass
    cfg["projects"] = [
        p for p in cfg["projects"]
        if isinstance(p, dict) and p.get("path") and p.get("name")
    ]
    if migrate_tier:
        save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 会话历史
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    sid: str
    path: Path
    title: str
    rounds: int
    updated: datetime
    cwd: str = ""
    source: str = "claude"


_session_cache = {}


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return None


def parse_session(path):
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _session_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]

    sid = path.stem
    title = ""
    first_prompt = ""
    rounds = 0
    last_ts = None
    cwd = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                ts = parse_timestamp(entry.get("timestamp"))
                if ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
                if not cwd and entry.get("cwd"):
                    cwd = str(entry["cwd"])
                kind = entry.get("type")
                if kind == "ai-title":
                    candidate = (entry.get("aiTitle") or "").strip()
                    if candidate:
                        title = candidate
                elif kind == "user" and not entry.get("isMeta"):
                    message = entry.get("message") or {}
                    text = content_text(message.get("content")).strip()
                    if text and not text.startswith("<"):
                        rounds += 1
                        if not first_prompt:
                            first_prompt = " ".join(text.split())[:120]
    except OSError:
        return None

    updated = last_ts
    if updated is None:
        updated = datetime.fromtimestamp(mtime).astimezone()
    session = Session(
        sid=sid,
        path=path,
        title=title or first_prompt or "未命名会话",
        rounds=rounds,
        updated=updated,
        cwd=cwd,
    )
    _session_cache[str(path)] = (mtime, session)
    return session


def load_session_messages(path, limit=40):
    """读取会话文件中的纯文本消息，用于在界面里预览恢复的会话。"""
    messages = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("isMeta") or entry.get("isSidechain"):
                    continue
                kind = entry.get("type")
                if kind not in ("user", "assistant"):
                    continue
                message = entry.get("message") or {}
                text = content_text(message.get("content")).strip()
                if not text:
                    continue
                if kind == "user" and text.startswith("<"):
                    continue
                messages.append((kind, text))
    except OSError:
        return []
    return messages[-limit:]


def encoded_project_dir(project_path):
    return re.sub(r"[^A-Za-z0-9]", "-", project_path.rstrip("/") or "/")


def read_workdir(session_file, max_lines=60):
    try:
        with session_file.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("cwd"):
                    return entry["cwd"]
    except OSError:
        pass
    return None


def find_project_dir(project_path):
    direct = CLAUDE_PROJECTS / encoded_project_dir(project_path)
    if direct.is_dir():
        return direct
    target = Path(project_path).resolve()
    try:
        for candidate in CLAUDE_PROJECTS.iterdir():
            if not candidate.is_dir():
                continue
            files = sorted(candidate.glob("*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:1]
            for f in files:
                cwd = read_workdir(f)
                try:
                    if cwd and Path(cwd).resolve() == target:
                        return candidate
                except OSError:
                    continue
    except OSError:
        pass
    return None


def load_project_sessions(project_path, limit=30):
    directory = find_project_dir(project_path)
    if directory is None:
        return []
    try:
        files = sorted(directory.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return []
    sessions = [parse_session(f) for f in files]
    sessions = [s for s in sessions if s is not None]
    sessions.sort(key=lambda s: s.updated, reverse=True)
    return sessions


def load_all_claude_sessions(limit=200):
    """读取所有项目的 Claude 会话，供「全部项目」历史视图使用。"""
    if not CLAUDE_PROJECTS.is_dir():
        return []
    best = {}
    try:
        for directory in CLAUDE_PROJECTS.iterdir():
            if not directory.is_dir():
                continue
            for path in directory.glob("*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                current = best.get(str(path))
                if current is None or mtime > current[0]:
                    best[str(path)] = (mtime, path)
    except OSError:
        return []
    files = sorted((item[1] for item in best.values()),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    sessions = [parse_session(f) for f in files]
    sessions = [s for s in sessions if s is not None]
    sessions.sort(key=lambda s: s.updated, reverse=True)
    return sessions


def session_to_entry(session):
    return {
        "source": "claude",
        "sid": session.sid,
        "path": session.path,
        "title": session.title,
        "rounds": session.rounds,
        "updated": session.updated,
        "cwd": session.cwd,
    }


def codex_entry(info):
    return {
        "source": "codex",
        "sid": info["sid"],
        "path": info["path"],
        "title": info["title"],
        "rounds": info["rounds"],
        "updated": info["updated"],
        "cwd": info.get("cwd", ""),
    }


def humanize(dt):
    now = datetime.now().astimezone()
    try:
        delta = now - dt
    except TypeError:
        dt = dt.astimezone()
        delta = now - dt
    seconds = max(delta.total_seconds(), 0)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    days = int(seconds // 86400)
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days} 天前"
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 外部终端（保留给「运行命令」和「打开终端」）
# --------------------------------------------------------------------------- #
def terminal_candidates():
    return [
        ("gnome-terminal", lambda exe, cwd, inner: [exe, f"--working-directory={cwd}", "--", "bash", "-c", inner]),
        ("kgx", lambda exe, cwd, inner: [exe, f"--working-directory={cwd}", "--", "bash", "-c", inner]),
        ("konsole", lambda exe, cwd, inner: [exe, "--workdir", cwd, "-e", "bash", "-c", inner]),
        ("xfce4-terminal", lambda exe, cwd, inner: [exe, f"--working-directory={cwd}", "-x", "bash", "-c", inner]),
        ("kitty", lambda exe, cwd, inner: [exe, "--directory", cwd, "bash", "-c", inner]),
        ("alacritty", lambda exe, cwd, inner: [exe, "--working-directory", cwd, "-e", "bash", "-c", inner]),
        ("x-terminal-emulator", lambda exe, cwd, inner: [exe, "-e", "bash", "-c", inner]),
        ("xterm", lambda exe, cwd, inner: [exe, "-e", "bash", "-c", inner]),
    ]


def launch_terminal(cwd, command=""):
    if win_compat.IS_WINDOWS or win_compat.IS_MACOS:
        env = os.environ.copy()
        env["PATH"] = claude_path_env()
        return win_compat.launch_terminal(cwd, command, env=env)

    path_env = claude_path_env()
    parts = [f"cd {shlex.quote(cwd)}", f"export PATH={shlex.quote(path_env)}"]
    if command:
        parts.append(command)
    parts.append("exec bash")
    inner = " && ".join(parts[:2]) + "; " + "; ".join(parts[2:])

    for name, build in terminal_candidates():
        executable = shutil.which(name)
        if not executable:
            continue
        argv = build(executable, cwd, inner)
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True, env=os.environ.copy())
            return name
        except OSError:
            continue
    raise RuntimeError("未找到可用的终端模拟器")


def permission_display(mode):
    for label, value, _tip in PERMISSION_MODES:
        if value == mode:
            return label
    return PERMISSION_MODES[0][0]


def default_model_choice():
    """未保存过选择时，跟随服务商配置里的 ANTHROPIC_MODEL。"""
    model = provider_env().get("ANTHROPIC_MODEL", "").strip()
    return model or FALLBACK_MODELS[0]["id"]


def tool_summary(name, data):
    if not isinstance(data, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "url", "query",
                "description", "prompt"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text[:90] + ("…" if len(text) > 90 else "")
    return ""


# --------------------------------------------------------------------------- #
# 模型目录后台线程
# --------------------------------------------------------------------------- #
class ModelCatalogWorker(QThread):
    """在后台请求服务商的 /models，避免阻塞界面。"""

    loaded = pyqtSignal(object, str)

    def run(self):
        try:
            models = fetch_provider_models()
        except Exception as exc:  # noqa: BLE001 - 失败时界面回退到内置列表
            self.loaded.emit([], str(exc))
        else:
            self.loaded.emit(models, "")


# --------------------------------------------------------------------------- #
# Claude 后台工作线程
# --------------------------------------------------------------------------- #
class ClaudeWorker(QObject):
    """在独立线程中运行 Claude Agent SDK，通过信号与界面通信。"""

    text_delta = pyqtSignal(str)
    assistant = pyqtSignal(str, list)
    result = pyqtSignal(dict)
    error = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)
    permission_asked = pyqtSignal(int, str, str)
    # 开发模式附加信息：(请求号, 目标路径, 是否位于允许修改的文件夹之外)
    permission_detail = pyqtSignal(int, str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._loop = None
        self._ready = threading.Event()
        self._client = None
        self._cwd = None
        self._permission_mode = "default"
        self._model = None
        self._effort = None
        self._resume = None
        self._busy = False
        self._generation = 0
        self._perm_seq = 0
        self._pending = {}
        self._pending_lock = threading.Lock()
        # 开发模式：允许修改的文件夹、修改模式、每轮是否已授权、
        # 本轮写文件前的原始内容（用于改前/改后对比）
        self._dev_roots = []
        self._dev_mode = ""
        self._dev_turn_auto = False
        self._dev_approved_files = set()
        self._dev_deny_writes = False
        self._dev_pre_images = {}
        self._dev_lock = threading.Lock()
        self._dev_perm_context = {}
        # 进度查询：记录本轮开始/最近活动/工具/权限等待，供界面只读快照
        self._status_lock = threading.Lock()
        self._turn_started = None
        self._last_activity = None
        self._last_activity_label = ""
        self._tool_count = 0
        self._last_tool = ""
        self._last_tool_time = None
        self._last_permission_tool = ""
        self._last_permission_time = None
        self._last_error = ""

    # ------------------------------------------------------------- threading --
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="claude-sdk", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _submit(self, coro):
        loop = self._loop
        if loop is None or not loop.is_running():
            return None
        return asyncio.run_coroutine_threadsafe(coro, loop)

    # -------------------------------------------------------------- lifecycle --
    def configure(self, cwd, permission_mode, resume=None, model=None, effort=None):
        self._cwd = cwd
        self._permission_mode = permission_mode
        self._resume = resume
        self._model = model
        self._effort = effort
        self.reset()

    def reset(self):
        self._generation += 1
        self._deny_pending()
        client, self._client = self._client, None
        if client is not None:
            self._submit(self._disconnect(client))
        if self._busy:
            self._busy = False
            self.busy_changed.emit(False)

    def shutdown(self):
        self._generation += 1
        self._deny_pending()
        client, self._client = self._client, None
        loop = self._loop
        if loop is not None and loop.is_running():
            async def close():
                if client is not None:
                    await self._disconnect(client)
                loop.stop()

            self._submit(close())
            if self._thread is not None:
                self._thread.join(timeout=3)

    @staticmethod
    async def _disconnect(client):
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - 关闭阶段的异常无需打扰用户
            pass

    def _deny_pending(self):
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for holder in pending:
            holder["allowed"] = False
            holder["event"].set()

    # ------------------------------------------------------------------ send --
    @property
    def busy(self):
        return self._busy

    # --------------------------------------------------------------- progress --
    def _status_touch(self, label: str) -> None:
        now = time.monotonic()
        with self._status_lock:
            self._last_activity = now
            self._last_activity_label = label

    def _status_tool(self, name: str) -> None:
        now = time.monotonic()
        with self._status_lock:
            self._last_activity = now
            self._last_activity_label = f"执行工具 {name}"
            self._tool_count += 1
            self._last_tool = name
            self._last_tool_time = now

    def _status_permission(self, tool_name: str) -> None:
        now = time.monotonic()
        with self._status_lock:
            self._last_permission_tool = tool_name
            self._last_permission_time = now
            self._last_activity_label = f"等待权限确认：{tool_name}"

    def _status_permission_done(self, allowed: bool) -> None:
        with self._status_lock:
            self._last_permission_tool = ""
            self._last_activity = time.monotonic()
            self._last_activity_label = (
                "权限已允许，继续执行" if allowed else "权限被拒绝")

    def _status_reset_turn(self) -> None:
        now = time.monotonic()
        with self._status_lock:
            self._turn_started = now
            self._last_activity = now
            self._last_activity_label = "请求已发送"
            self._tool_count = 0
            self._last_tool = ""
            self._last_tool_time = None
            self._last_permission_tool = ""
            self._last_permission_time = None
            self._last_error = ""

    def _status_error(self, message: str) -> None:
        with self._status_lock:
            self._last_error = str(message or "")[:300]
            self._last_activity = time.monotonic()
            self._last_activity_label = "出错"

    def status_snapshot(self) -> dict:
        """线程安全的只读状态，供「进度查询」浮窗使用。"""
        now = time.monotonic()
        with self._status_lock:
            busy = bool(self._busy)
            mode = self._permission_mode or "default"
            started = self._turn_started
            last = self._last_activity
            label = self._last_activity_label
            tool_count = self._tool_count
            last_tool = self._last_tool
            last_tool_time = self._last_tool_time
            perm_tool = self._last_permission_tool
            perm_time = self._last_permission_time
            last_error = self._last_error
        with self._pending_lock:
            pending = len(self._pending)
            pending_tools = [
                str(holder.get("tool") or "") for holder in self._pending.values()
            ]
        with self._dev_lock:
            dev_mode = self._dev_mode or ""
            dev_roots = len(self._dev_roots)
        return {
            "busy": busy,
            "permission_mode": mode,
            "elapsed": (now - started) if started else 0.0,
            "since_activity": (now - last) if last else None,
            "activity_label": label,
            "tool_count": tool_count,
            "last_tool": last_tool,
            "last_tool_age": (now - last_tool_time) if last_tool_time else None,
            "pending_permissions": pending,
            "pending_tools": pending_tools,
            "permission_tool": perm_tool,
            "permission_age": (now - perm_time) if perm_time else None,
            "dev_mode": dev_mode,
            "dev_roots": dev_roots,
            "last_error": last_error,
        }

    def send(self, text):
        if self._busy:
            return False
        if not SDK_AVAILABLE:
            self.error.emit(f"未安装 Claude Agent SDK：{SDK_IMPORT_ERROR}")
            return False
        if not self._ready.is_set():
            self.start()
        if not self._ready.is_set():
            self.error.emit("后台线程尚未启动")
            return False
        self._busy = True
        self.busy_changed.emit(True)
        self._status_reset_turn()
        if self._dev_mode:
            # 新一轮请求开始：重新按“功能”授权，并清空本轮的改前快照
            self._dev_turn_auto = False
            self._dev_approved_files = set()
            with self._dev_lock:
                self._dev_pre_images = {}
        generation = self._generation
        future = self._submit(self._send_async(text, generation))
        if future is None:
            self._busy = False
            self.busy_changed.emit(False)
            self.error.emit("后台线程尚未启动")
            return False
        return True

    async def _send_async(self, text, generation):
        try:
            client = await self._ensure_client(generation)
            if client is None or generation != self._generation:
                return
            await client.query(text)
            async for message in client.receive_response():
                if generation != self._generation:
                    return
                self._handle(message)
        except Exception as exc:  # noqa: BLE001 - 错误显示在对话中
            if generation == self._generation:
                self._status_error(f"{type(exc).__name__}: {exc}")
                self.error.emit(f"{type(exc).__name__}: {exc}")
                # 解析/连接失败后客户端通常已经不可用，丢弃它，下一轮重新连接，
                # 否则用户修好问题再次提问还会命中同一个坏连接。
                broken, self._client = self._client, None
                if broken is not None:
                    await self._disconnect(broken)
        finally:
            if generation == self._generation:
                self._status_touch("本轮结束")
                self._busy = False
                self.busy_changed.emit(False)

    async def _ensure_client(self, generation):
        if self._client is not None:
            return self._client
        if not self._cwd:
            raise RuntimeError("请先选择一个项目目录")
        executable = find_claude()
        # Windows 的 npm 安装只有 claude.cmd，SDK 会拒绝执行批处理 shim；
        # 这里给它 claude.exe（原生安装或 SDK 自带），找不到就交给 SDK 自己找。
        cli_path = executable
        if win_compat.IS_WINDOWS:
            cli_path = find_native_claude_exe()
        if not executable and not cli_path:
            raise RuntimeError("环境变量 PATH 中找不到 claude 命令，请确认 Claude Code 已安装")
        # 每次都重新合并 settings.json，cc-switch 切换服务商后无需重启控制台
        env = provider_env()
        env["PATH"] = claude_path_env()
        options = ClaudeAgentOptions(
            cwd=self._cwd,
            permission_mode=self._permission_mode,
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
            setting_sources=["user", "project", "local"],
            resume=self._resume,
            model=self._model or None,
            effort=self._effort or None,
            cli_path=cli_path or None,
            env=env,
            # 截图/大文件工具结果会产生超过默认 1 MiB 的单条 JSON 消息
            max_buffer_size=MAX_CLI_MESSAGE_BYTES,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        if generation != self._generation:
            await self._disconnect(client)
            return None
        self._client = client
        return client

    def _handle(self, message):
        if isinstance(message, StreamEvent):
            if message.parent_tool_use_id:
                return
            event = message.event or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text") or ""
                    if chunk:
                        self._status_touch("正在输出回答")
                        self.text_delta.emit(chunk)
            return

        if isinstance(message, AssistantMessage):
            if message.parent_tool_use_id:
                return
            texts = []
            tools = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tools.append({"name": block.name, "input": block.input})
            text = "\n".join(part for part in texts if part).strip()
            if tools:
                for tool in tools:
                    self._status_tool(str(tool.get("name") or "工具"))
            elif text:
                self._status_touch("正在生成回答")
            self.assistant.emit(text, tools)
            return

        if isinstance(message, ResultMessage):
            self._status_touch("已返回结果")
            self.result.emit({
                "session_id": message.session_id,
                "is_error": bool(message.is_error),
                "result": message.result or "",
                "cost": message.total_cost_usd,
                "duration": message.duration_ms,
                "subtype": message.subtype,
            })

    # ------------------------------------------------------------- permission --
    def set_dev_policy(self, roots, mode):
        """开发页使用：设置允许修改的文件夹与修改模式。

        开发模式一律用 SDK 的 default 权限模式启动，再由这里按用户选择的
        三种模式自行放行/询问，这样才能在写入前抓到文件原始内容。
        """
        resolved = []
        for root in roots or []:
            try:
                resolved.append(str(Path(root).expanduser().resolve()))
            except OSError:
                continue
        with self._dev_lock:
            self._dev_roots = resolved
            self._dev_mode = mode or ""

    def begin_dev_turn(self, deny_writes=False):
        self._dev_turn_auto = False
        self._dev_approved_files = set()
        self._dev_deny_writes = bool(deny_writes)

    def take_dev_pre_images(self):
        """取走本轮写入前的文件内容快照。"""
        with self._dev_lock:
            data = dict(self._dev_pre_images)
            self._dev_pre_images.clear()
        return data

    def _dev_target_path(self, input_data):
        if not isinstance(input_data, dict):
            return ""
        raw = (input_data.get("file_path") or input_data.get("notebook_path")
               or input_data.get("path") or "")
        if not raw:
            return ""
        raw = str(raw)
        if not raw.strip():
            return ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            base = self._cwd or os.getcwd()
            path = Path(base) / path
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())

    def _dev_path_allowed(self, path):
        if not path:
            return False
        root_list = list(self._dev_roots)
        for root in root_list:
            try:
                if os.path.commonpath([path, root]) == root:
                    return True
            except ValueError:
                continue
        return False

    def _capture_dev_pre_image(self, tool_name, input_data):
        path = self._dev_target_path(input_data)
        if not path:
            return
        with self._dev_lock:
            if path in self._dev_pre_images:
                return
            try:
                text = Path(path).read_text(
                    encoding="utf-8", errors="ignore")
            except FileNotFoundError:
                text = None  # 新建文件：取消时应删除
            except OSError:
                return
            self._dev_pre_images[path] = text

    def _dev_decision(self, tool_name, input_data):
        """返回 (动作, 路径, 是否越界)。开发模式一律放行，不再弹权限窗。

        是否覆盖代码由开发页的「修改模式」控制：自动覆盖 / 每轮确认 /
        每次确认。只有代码检查请求会拒绝写入。
        """
        path = self._dev_target_path(input_data)
        is_write = tool_name in DEV_WRITE_TOOLS
        if self._dev_deny_writes and (is_write or tool_name == "Bash"):
            return "deny", path, False
        return "allow", path, False

    async def _can_use_tool(self, tool_name, input_data, _context):
        dev_active = bool(self._dev_roots) and self._dev_mode != ""
        if dev_active:
            action, path, outside = self._dev_decision(tool_name, input_data)
            if action == "allow":
                if tool_name in DEV_WRITE_TOOLS:
                    self._capture_dev_pre_image(tool_name, input_data)
                return PermissionResultAllow(updated_input=input_data)
            if action == "deny":
                return PermissionResultDeny(
                    message="当前是代码检查请求，用户要求不要修改文件")
        elif self._permission_mode == "bypassPermissions":
            return PermissionResultAllow(updated_input=input_data)

        self._perm_seq += 1
        request_id = self._perm_seq
        holder = {"event": threading.Event(), "allowed": None,
                  "tool": tool_name}
        with self._pending_lock:
            self._pending[request_id] = holder
        pretty = json.dumps(input_data, ensure_ascii=False, indent=2, default=str)
        self._status_permission(tool_name)
        if dev_active:
            self.permission_detail.emit(request_id, path, bool(outside))
        self.permission_asked.emit(request_id, tool_name, pretty)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, holder["event"].wait)
        with self._pending_lock:
            self._pending.pop(request_id, None)
        self._status_permission_done(bool(holder["allowed"]))
        if holder["allowed"]:
            if dev_active:
                if tool_name in DEV_WRITE_TOOLS:
                    self._capture_dev_pre_image(tool_name, input_data)
                if tool_name in DEV_WRITE_TOOLS and path:
                    self._dev_approved_files.add(path)
                if self._dev_mode == "function":
                    # 这一轮已经询问过一次，后续 Bash 等命令不再重复询问
                    self._dev_turn_auto = True
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="用户拒绝了这次操作")

    def respond_permission(self, request_id, allowed):
        with self._pending_lock:
            holder = self._pending.get(request_id)
            if holder is not None:
                holder["allowed"] = bool(allowed)
                holder["event"].set()

    def set_permission_mode(self, mode):
        self._permission_mode = mode
        client = self._client
        if client is None:
            return

        async def apply():
            try:
                await client.set_permission_mode(mode)
            except Exception as exc:  # noqa: BLE001
                self.error.emit(f"切换权限模式失败：{exc}")

        self._submit(apply())

    def set_model(self, model, resume=None):
        """切换模型。已连接时重建会话，并用 resume 保留上下文。"""
        changed = model != self._model
        self._model = model
        if not changed or self._client is None:
            return
        if resume:
            self._resume = resume
        self.reset()

    def set_effort(self, effort, resume=None):
        """切换思考强度。--effort 只在启动时生效，因此需要重建会话。"""
        changed = effort != self._effort
        self._effort = effort
        if not changed or self._client is None:
            return
        if resume:
            self._resume = resume
        self.reset()

    def reconnect(self, resume=None):
        """按最新服务商配置重建会话，同时保留对话上下文。"""
        if resume:
            self._resume = resume
        self.reset()

    def interrupt(self):
        client = self._client
        if client is None or not self._busy:
            return

        async def stop():
            try:
                await client.interrupt()
            except Exception as exc:  # noqa: BLE001
                self.error.emit(f"停止失败：{exc}")

        self._submit(stop())


# --------------------------------------------------------------------------- #
# 界面组件
# --------------------------------------------------------------------------- #
QSS = """
* { outline: none; }
QWidget { background-color: #fffafc; color: #4b3742; font-size: 13px; }
QMainWindow, QDialog { background-color: #fffafc; }
QFrame#Sidebar { background-color: #ffeef5; border: none; border-right: 1px solid #f7d3e2; }
QFrame#Card { background-color: #ffffff; border: 1px solid #f6d9e6; border-radius: 12px; }
QFrame#UserBubble { background-color: #ffdfeb; border: 1px solid #f7c3d8; border-radius: 13px; }
QFrame#AssistantBubble { background-color: #ffffff; border: 1px solid #f2d6e3; border-radius: 13px; }
QFrame#MasterBubble { background-color: #f4f8ff; border: 1px solid #c9ddf7; border-radius: 13px; }
QFrame#WorkerBubble { background-color: #fffdf4; border: 1px solid #f0e0b8; border-radius: 13px; }
QLabel#MasterRole { color: #3f6b9e; font-size: 11px; font-weight: 600; }
QLabel#WorkerRole { color: #9a7524; font-size: 11px; font-weight: 600; }
QFrame#DualPanel { background-color: #ffffff; border: 1px solid #f2d6e3; border-radius: 10px; }
QLabel#DualBalance { color: #8a6073; font-size: 11px; }
QLabel#DualShare { color: #c25d84; font-size: 11px; font-weight: 600; }
QFrame#ToolChip { background-color: #fff2f7; border: 1px solid #f5cfe0; border-radius: 9px; }
QLabel { background: transparent; }
QLabel#H1 { font-size: 21px; font-weight: 600; color: #3f2a34; }
QLabel#H2 { font-size: 13px; font-weight: 600; color: #6b4c5a; }
QLabel#Muted { color: #a58292; font-size: 12px; }
QLabel#Empty { color: #b294a2; font-size: 13px; }
QLabel#ItemTitle { font-size: 13px; font-weight: 600; color: #4b3742; }
QLabel#ItemSub { font-size: 11px; color: #a58292; }
QLabel#ChatRole { color: #c25d84; font-size: 11px; font-weight: 600; }
QLabel#ChatBody { color: #4b3742; font-size: 13px; }
QLabel#ChatNote { color: #a58292; font-size: 12px; }
QLabel#ChatEmpty { color: #b294a2; font-size: 14px; }
QLabel#ToolText { color: #9c5a76; font-size: 12px; }
QToolButton#QuoteButton { background: transparent; border: none; color: #b98aa0; font-size: 11px; padding: 1px 5px; border-radius: 5px; }
QToolButton#QuoteButton:hover { background: #ffeaf3; color: #d9457a; }
QFrame#QuoteBar { background-color: #eef5ff; border: 1px solid #c9ddf7; border-radius: 8px; }
QLabel#QuoteBarText { color: #4d6f9e; font-size: 12px; }
QLabel#SkillCategory { color: #8a6073; font-size: 11px; font-weight: 600; padding: 8px 8px 2px 8px; }
QFrame#MediaResult { background-color: #f7fbff; border: 1px solid #cfe2f8; border-radius: 10px; }
QLabel#MediaResultTitle { color: #3f6b9e; font-size: 12px; font-weight: 600; }
QWidget#ChatBody { background-color: #fffafc; }
QPushButton { background-color: #ffffff; border: 1px solid #f0c9da; border-radius: 8px; padding: 8px 14px; color: #5a3f4b; }
QPushButton:hover { background-color: #fff1f7; border-color: #eba9c4; }
QPushButton:pressed { background-color: #ffe6f0; }
QPushButton:disabled { color: #cfb3c0; background-color: #fff7fa; border-color: #f6e2ea; }
QPushButton#Primary { background-color: #e4638c; border-color: #e4638c; color: #ffffff; font-weight: 600; }
QPushButton#Primary:hover { background-color: #ef7aa0; border-color: #ef7aa0; }
QPushButton#Primary:disabled { background-color: #f6c3d6; border-color: #f6c3d6; color: #fff7fa; }
QPushButton#Danger { background-color: transparent; border: 1px solid #efb2c3; color: #d9536f; }
QPushButton#Danger:hover { background-color: #ffe9ef; }
QPushButton#Danger:disabled { color: #e3bcc8; border-color: #f3dde5; }
QPushButton#Flat { background: transparent; border: 1px solid transparent; color: #a26f84; padding: 6px 10px; }
QPushButton#Flat:hover { background-color: #ffeaf3; color: #7a4d61; }
QToolButton#Small { background: transparent; border: none; border-radius: 6px; color: #b0778e; font-size: 16px; padding: 2px 8px; }
QToolButton#Small:hover { background-color: #ffdfeb; color: #7a4d61; }
QLineEdit { background-color: #ffffff; border: 1px solid #f0c9da; border-radius: 8px; padding: 8px 10px; color: #4b3742; selection-background-color: #ffc4da; }
QLineEdit:focus { border-color: #e4638c; }
QPlainTextEdit#ChatInput { background-color: #ffffff; border: 1px solid #f0c9da; border-radius: 10px; padding: 8px 10px; color: #4b3742; selection-background-color: #ffc4da; }
QPlainTextEdit#ChatInput:focus { border-color: #e4638c; }
QPlainTextEdit#PermissionInput { background-color: #fffafc; border: 1px solid #f2d6e3; border-radius: 8px; padding: 6px; color: #5a3f4b; }
QProgressBar { background-color: #fff1f7; border: 1px solid #f0c9da; border-radius: 9px; color: #a25477; text-align: center; font-size: 11px; }
QProgressBar::chunk { background-color: #e4638c; border-radius: 8px; }
QLabel#AttachmentTitle { color: #a58292; font-size: 11px; }
QFrame#AttachmentChip { background-color: #ffffff; border: 1px solid #f0c9da; border-radius: 9px; }
QLabel#AttachmentStatus { color: #a58292; font-size: 11px; }
QComboBox { background-color: #ffffff; border: 1px solid #f0c9da; border-radius: 8px; padding: 6px 10px; min-width: 104px; color: #4b3742; }
QComboBox:hover { border-color: #eba9c4; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background-color: #ffffff; border: 1px solid #f0c9da; selection-background-color: #ffe1ed; selection-color: #3f2a34; outline: none; }
QListWidget { background: transparent; border: none; }
QListWidget::item { border-radius: 9px; margin: 2px 4px; }
QListWidget::item:hover { background-color: #ffeaf3; }
QListWidget::item:selected { background-color: #ffd9e8; border: 1px solid #f2a9c7; }
QListWidget#Favorites::item { padding: 3px 8px; color: #6b4c5a; }
QListWidget#Favorites::item:hover { background-color: #ffeaf3; }
QListWidget#Favorites::item:selected { background-color: #ffd9e8; }
QScrollArea#ChatScroll { border: none; background-color: #fffafc; }
QScrollArea#ChatScroll > QWidget > QWidget { background-color: #fffafc; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #f0c3d6; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #e7a8c2; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QSplitter::handle { background-color: #f7d3e2; }
QSplitter::handle:hover { background-color: #f4b4cc; }
QSplitter::handle:pressed { background-color: #ffb1b7; }
QStatusBar { background-color: #ffeef5; color: #a58292; border-top: 1px solid #f7d3e2; }
QStatusBar::item { border: none; }
QToolTip { background-color: #ffffff; color: #5a3f4b; border: 1px solid #f0c9da; padding: 4px 6px; }
QMenu { background-color: #ffffff; border: 1px solid #f0c9da; padding: 6px; }
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background-color: #ffe1ed; color: #3f2a34; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { background: transparent; color: #a58292; padding: 7px 16px; border-radius: 7px; margin-right: 4px; }
QTabBar::tab:hover { background: #ffeaf3; color: #7a4d61; }
QTabBar::tab:selected { background: #ffffff; color: #d9457a; border: 1px solid #f6d0e0; font-weight: 600; }
"""


class ListItemWidget(QWidget):
    """列表里的两行条目：标题 + 次要信息，自动省略过长文本。"""

    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        self._title = title
        self._subtitle = subtitle
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(1)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("ItemTitle")
        self.sub_label = QLabel(self)
        self.sub_label.setObjectName("ItemSub")
        layout.addWidget(self.title_label)
        layout.addWidget(self.sub_label)
        self.setStyleSheet("background: transparent;")
        self.setToolTip(f"{title}\n{subtitle}")
        self._apply_elide()

    def _apply_elide(self):
        width = max(self.width() - 26, 60)
        fm = QFontMetrics(self.title_label.font())
        self.title_label.setText(fm.elidedText(self._title, Qt.ElideRight, width))
        fm_sub = QFontMetrics(self.sub_label.font())
        self.sub_label.setText(fm_sub.elidedText(self._subtitle, Qt.ElideMiddle, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
                  ".tif", ".tiff")


def _is_image_path(path):
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


# Markdown 图片 `![alt](path)` 和消息里裸写的本地图片路径，用于在气泡内直接显示。
IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^)\n]+?))\s*\)")
IMAGE_BARE_RE = re.compile(
    r"(?:^|[\s\"'`（(：:,，])"
    r"((?:file:///|~|/)[^\s\"'`<>()\[\]，。；、]+?"
    r"\.(?:png|jpe?g|gif|webp|bmp|tif|tiff))",
    re.IGNORECASE | re.MULTILINE)


def _resolve_local_image(ref, base_dir=""):
    """把消息里的图片引用解析成本机存在且确实是图片的文件路径。"""
    ref = (ref or "").strip().strip("<>")
    if not ref:
        return None
    lowered = ref.lower()
    if lowered.startswith(("http://", "https://", "data:")):
        return None
    if lowered.startswith("file://"):
        ref = unquote(ref[7:])
    if ref.startswith("~"):
        path = Path(ref).expanduser()
    elif ref.startswith("/"):
        path = Path(ref)
    elif base_dir:
        path = Path(base_dir) / ref
    else:
        return None
    try:
        path = path.resolve()
    except OSError:
        return None
    if path.is_file() and _is_image_path(str(path)):
        return path
    return None


def extract_local_images(text, base_dir=""):
    """从消息文本中提取可在气泡里直接显示的本地图片（去重、保序）。"""
    if not text:
        return []
    found = []
    for match in IMAGE_MD_RE.finditer(text):
        path = _resolve_local_image(
            match.group(1) or match.group(2), base_dir)
        if path and path not in found:
            found.append(path)
    for match in IMAGE_BARE_RE.finditer(text):
        path = _resolve_local_image(match.group(1), base_dir)
        if path and path not in found:
            found.append(path)
    return found


def strip_local_image_markdown(text, base_dir=""):
    """本地图片会用控件直接显示，去掉 Markdown 图片标记避免显示成坏图标。"""
    def replace(match):
        ref = match.group(1) or match.group(2)
        return "" if _resolve_local_image(ref, base_dir) else match.group(0)
    return IMAGE_MD_RE.sub(replace, text)


class ChatInput(QPlainTextEdit):
    """Enter 发送，Shift+Enter 换行；支持粘贴/拖入图片。"""

    submitted = pyqtSignal()
    image_pasted = pyqtSignal(QImage)

    def canInsertFromMimeData(self, source):
        if source.hasImage():
            return True
        if source.hasUrls() and any(
                url.isLocalFile() and _is_image_path(url.toLocalFile())
                for url in source.urls()):
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasImage():
            image = QImage(source.imageData())
            if not image.isNull():
                self.image_pasted.emit(image)
                return
        if source.hasUrls():
            images = []
            for url in source.urls():
                if not url.isLocalFile():
                    continue
                path = url.toLocalFile()
                if not _is_image_path(path):
                    continue
                image = QImage(path)
                if not image.isNull():
                    images.append(image)
            if images:
                for image in images:
                    self.image_pasted.emit(image)
                return
        super().insertFromMimeData(source)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not (event.modifiers() & Qt.ShiftModifier)):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ChatBubble(QFrame):
    link_activated = pyqtSignal(str)
    quote_requested = pyqtSignal(str, str)  # (角色, 原文)
    selection_changed = pyqtSignal(str, str)  # (选中文字, 角色)
    selection_asked = pyqtSignal(str, str)  # (选中文字, 角色)

    def __init__(self, role, text="", plain=False, parent=None, base_dir=""):
        super().__init__(parent)
        self.role = role
        self.raw_text = text or ""
        self.base_dir = base_dir or ""
        self._inline_images = []
        object_name = {
            "user": "UserBubble",
            "master": "MasterBubble",
            "worker": "WorkerBubble",
        }.get(role, "AssistantBubble")
        self.setObjectName(object_name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 9, 13, 11)
        layout.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        role_text = {
            "user": "你",
            "master": "主 AI · M",
            "worker": "执行 AI · W",
        }.get(role, "Claude")
        role_label = QLabel(role_text)
        role_label.setObjectName(
            "MasterRole" if role == "master" else
            "WorkerRole" if role == "worker" else "ChatRole")
        head.addWidget(role_label)
        head.addStretch(1)
        self.quote_btn = QToolButton()
        self.quote_btn.setObjectName("QuoteButton")
        self.quote_btn.setText("↩ 引用")
        self.quote_btn.setToolTip("引用这条消息进行回复")
        self.quote_btn.setCursor(Qt.PointingHandCursor)
        self.quote_btn.clicked.connect(
            lambda: self.quote_requested.emit(
                self.role, self.raw_text.strip()))
        head.addWidget(self.quote_btn)
        self.body = QLabel()
        self.body.setObjectName("ChatBody")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.PlainText if plain else Qt.MarkdownText)
        self.body.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(False)
        self.body.linkActivated.connect(self._on_link_activated)
        self.body.setContextMenuPolicy(Qt.CustomContextMenu)
        self.body.customContextMenuRequested.connect(self._show_body_menu)
        self.body.installEventFilter(self)
        layout.addLayout(head)
        layout.addWidget(self.body)

        # 消息里引用的本地图片直接显示在气泡内，点击可打开原图
        self.image_container = QWidget(self)
        self.image_layout = QVBoxLayout(self.image_container)
        self.image_layout.setContentsMargins(0, 4, 0, 0)
        self.image_layout.setSpacing(6)
        self.image_container.setVisible(False)
        layout.addWidget(self.image_container)
        self._set_body_text(text or "", plain)
        self.setMaximumWidth(780)
        # 给气泡一个最小宽度，避免长句被挤成细长条
        self.setMinimumWidth(
            340 if role in ("assistant", "master", "worker") else 280)

    def _set_body_text(self, text, plain):
        self.raw_text = text or ""
        self.body.setTextFormat(Qt.PlainText if plain else Qt.MarkdownText)
        display = text or ""
        if not plain:
            display = strip_local_image_markdown(display, self.base_dir)
        self.body.setText(display)
        self._sync_inline_images(plain)

    def _sync_inline_images(self, plain=False):
        for widget in self._inline_images:
            widget.deleteLater()
        self._inline_images = []
        if plain:
            self.image_container.setVisible(False)
            return
        for path in extract_local_images(self.raw_text, self.base_dir)[:4]:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                continue
            label = QLabel(self.image_container)
            label.setPixmap(pixmap.scaled(
                520, 620, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            label.setToolTip(f"{path}\n点击打开原图")
            label.setCursor(Qt.PointingHandCursor)
            label.mousePressEvent = (
                lambda _event, target=path: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(target))))
            self.image_layout.addWidget(label)
            self._inline_images.append(label)
        self.image_container.setVisible(bool(self._inline_images))

    def _on_link_activated(self, href):
        if href.startswith(("px4code://", "code://")):
            self.link_activated.emit(href)
        else:
            QDesktopServices.openUrl(QUrl(href))

    # --------------------------------------------------- 选中内容问答 --
    def selected_text(self):
        text = self.body.selectedText().replace(" ", "\n").strip()
        return text

    def eventFilter(self, obj, event):
        # QLabel 没有 selectionChanged 信号，用鼠标/键盘事件同步选区，
        # 这样点击「针对选中内容提问」时即使焦点已移走也能拿到原文。
        if obj is self.body and event.type() in (
                QEvent.MouseButtonRelease, QEvent.KeyRelease):
            QTimer.singleShot(0, self._emit_selection)
        return super().eventFilter(obj, event)

    def _emit_selection(self):
        # 空选区也同步，避免主窗口继续使用上一次的旧选区
        self.selection_changed.emit(self.selected_text(), self.role)

    def _show_body_menu(self, pos):
        selected = self.selected_text()
        menu = QMenu(self)
        ask = menu.addAction("针对选中内容提问")
        ask.setEnabled(bool(selected))
        ask.triggered.connect(
            lambda: self.selection_asked.emit(selected, self.role))
        menu.addSeparator()
        quote = menu.addAction("引用这条消息")
        quote.triggered.connect(
            lambda: self.quote_requested.emit(
                self.role, self.raw_text.strip()))
        menu.exec_(self.body.mapToGlobal(pos))

    def set_plain(self, text):
        self._set_body_text(text, plain=True)

    def set_markdown(self, text):
        self._set_body_text(text, plain=False)


# --------------------------------------------------------------------------- #
# 代码研读页：左侧 AI 解释 + 局部问答，右侧源码查看器
# --------------------------------------------------------------------------- #
STUDY_REF_RE = re.compile(r"\[\[code:\s*([^\[\]|]+?)\s*(?:\|\s*([^\]]*))?\]\]")

# Qt 的 Markdown 解析器（md4c）会把正文里形如 <Vector3d> 的片段当成 HTML
# 标签；一旦遇到无法闭合的标签，整篇文档从那里开始直接被丢弃。C++ 模板
# 参数（vector<vector<Vector3d>>）和函数签名里非常常见，所以渲染前要把
# 代码区之外的 '<' 转义成 &lt;（代码块/行内代码里的保持原样）。
_MD_INLINE_CODE_RE = re.compile(r"(`+[^`\n]*?`+)")
_MD_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _escape_markdown_angles(text):
    """转义代码区之外的 '<'，避免 Qt Markdown 把模板参数当 HTML 标签。"""
    if "<" not in text:
        return text
    lines = []
    in_fence = False
    for line in text.split("\n"):
        if _MD_FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            continue
        parts = _MD_INLINE_CODE_RE.split(line)
        for index in range(0, len(parts), 2):  # 偶数下标是代码区之外
            parts[index] = parts[index].replace("<", "&lt;")
        lines.append("".join(parts))
    return "\n".join(lines)


def _escape_link_label(label):
    """链接文字里的方括号/反斜杠会把 Markdown 链接拆坏，需要转义。"""
    return (label.replace("\\", "\\\\")
            .replace("[", "\\[").replace("]", "\\]"))

# 问答窗口只渲染最近若干条消息。历史太长时 Qt 的 Markdown 富文本渲染会
# 明显变慢，内容过多还会错行/显示成方块，所以三个问答窗口都统一截断显示。
QA_WINDOW_HISTORY = 10


def visible_qa_messages(messages, limit=QA_WINDOW_HISTORY):
    """取最近 limit 条消息；必要时把最前面那条回答配对的用户问题一起带上。"""
    if len(messages) <= limit:
        return messages
    start = len(messages) - limit
    if messages[start].get("role") != "user" and start > 0:
        start -= 1
    return messages[start:]
# 行号分隔符可能被模型写成 ASCII 连字符、短横线、破折号或波浪号
_STUDY_RANGE_SEP = r"[-–—~]"
_STUDY_LINE_REF_RE = re.compile(
    r"^(?P<path>.*?)(?::\s*L?(?P<start>\d+)"
    r"(?:\s*" + _STUDY_RANGE_SEP + r"\s*L?(?P<end>\d+))?"
    r"|#L(?P<start2>\d+)(?:-L?(?P<end2>\d+))?)$")
_STUDY_SYMBOL_RE = re.compile(
    r"^(?P<path>.+?):(?P<symbol>[A-Za-z_$][\w$:.]*(?:\(\))?)$")
_IDENTIFIER_LABEL_RE = re.compile(r"^[A-Za-z_$][\w$:.]*(?:\(\))?$")


def parse_study_ref(target):
    """解析 `路径:起行-止行` / `路径#L起-L止` / `路径:符号名`。

    返回 {"path", "start", "end", "symbol"}，无法识别时返回 None。
    """
    target = target.strip().strip("`").replace("\\", "/")
    match = _STUDY_LINE_REF_RE.match(target)
    if match is not None:
        path = (match.group("path") or "").strip()
        if not path:
            return None
        start = int(match.group("start") or match.group("start2") or 1)
        end = int(match.group("end") or match.group("end2") or start)
        return {"path": path, "start": min(start, end),
                "end": max(start, end), "symbol": ""}
    match = _STUDY_SYMBOL_RE.match(target)
    if match is not None:
        return {"path": match.group("path").strip(), "start": 1, "end": 1,
                "symbol": match.group("symbol").strip()}
    return None


def study_refs_to_markdown(text):
    """把模型输出的 [[code:path:12-20]] 转成可点击的 code:// 链接。"""

    def replace(match):
        parsed = parse_study_ref(match.group(1))
        if parsed is None:
            return match.group(0)
        path, start, end = parsed["path"], parsed["start"], parsed["end"]
        symbol = parsed["symbol"]
        given_label = (match.group(2) or "").strip()
        label = given_label
        if not label:
            label = Path(path).name + f":{start}"
            if end != start:
                label += f"–{end}"
        elif not symbol and _IDENTIFIER_LABEL_RE.match(label):
            # 模型把函数/类名写在显示文字里时，用它校验并纠正行号
            symbol = label
        fragment = f"#L{start}" + (f"-L{end}" if end != start else "")
        query = f"?sym={quote(symbol, safe='')}" if symbol else ""
        label = _escape_link_label(label)
        return (f"[📄 {label}](code://{quote(path, safe='/._-')}"
                f"{query}{fragment})")

    # 先替换引用再统一转义 '<'：链接文字里的模板参数（vector<...>）同样要转义。
    return _escape_markdown_angles(STUDY_REF_RE.sub(replace, text))


# --------------------------------------------------------------------------- #
# 项目符号索引：让研读回答引用真实文件与真实行号
# --------------------------------------------------------------------------- #
SOURCE_SUFFIXES = {
    ".py", ".pyi", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts",
    ".rs", ".go", ".rb", ".php", ".cs", ".swift", ".lua", ".sh", ".bash",
    ".zsh", ".m", ".mm", ".scala", ".dart", ".vue", ".svelte", ".sql", ".jl",
}

_DEF_KEYWORDS = {
    "if", "for", "while", "switch", "return", "else", "do", "try", "catch",
    "finally", "case", "new", "delete", "sizeof", "typedef", "using",
    "namespace", "template", "operator", "include", "define", "import",
    "from", "elif", "except", "with", "lambda", "assert", "raise", "yield",
    "await", "async", "public", "private", "protected", "static", "const",
    "void", "int", "char", "float", "double", "bool", "unsigned", "signed",
    "long", "short", "auto", "std", "this", "elif", "and", "or", "not",
}

# (kind, regex)。顺序即匹配优先级。
_DEF_PATTERNS = (
    ("def", re.compile(
        r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\(")),
    ("class", re.compile(
        r"^(?P<indent>\s*)class\s+(?P<name>[A-Za-z_]\w*)")),
    ("fn", re.compile(
        r"^(?P<indent>\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        r"function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*\(")),
    ("class", re.compile(
        r"^(?P<indent>\s*)(?:export\s+)?(?:default\s+)?"
        r"(?:abstract\s+|final\s+|sealed\s+|data\s+)?"
        r"(?:class|interface|enum|record|struct|trait|protocol)\s+"
        r"(?P<name>[A-Za-z_$][\w$]*)")),
    ("fn", re.compile(
        r"^(?P<indent>\s*)(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?"
        r"(?:async\s+)?fn\s+(?P<name>[A-Za-z_]\w*)")),
    ("func", re.compile(
        r"^(?P<indent>\s*)func\s+(?:\([^)]*\)\s*)?"
        r"(?P<name>[A-Za-z_]\w*)\s*\(")),
    ("type", re.compile(
        r"^(?P<indent>\s*)(?:pub\s+)?(?:struct|enum|trait|type)\s+"
        r"(?P<name>[A-Za-z_]\w*)")),
    ("def", re.compile(
        r"^(?P<indent>\s*)def\s+(?:self\.)?"
        r"(?P<name>[A-Za-z_]\w*[?!]?)")),
    ("const", re.compile(
        r"^(?P<indent>\s*)(?:export\s+)?(?:const|let|var)\s+"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
        r"(?:function|\([^)]*\)\s*=>)")),
    # C / C++ 顶层函数定义：类型 + 名称 + 参数 + 行尾 {
    ("fn", re.compile(
        r"^(?P<indent>\s*)[A-Za-z_][\w:<>,*&\s]*?\b"
        r"(?P<name>[A-Za-z_]\w*(?:::\w+)*)\s*\([^;{}]*\)\s*"
        r"(?:const\s*)?(?:noexcept\s*)?\{")),
)


def _extract_symbols(text, max_lines=12000):
    """从源码里抽取主要定义，返回 [(行号, 种类, 名称)]。"""
    symbols = []
    seen = set()
    for number, line in enumerate(text.splitlines()[:max_lines], 1):
        if not line.strip() or line.lstrip().startswith(("#", "//", "*", "/*")):
            continue
        for kind, pattern in _DEF_PATTERNS:
            match = pattern.match(line)
            if match is None:
                continue
            indent = len(match.group("indent") or "")
            name = match.group("name").strip()
            if not name or name in _DEF_KEYWORDS:
                continue
            # C/C++ 正则容易误伤缩进很深的语句，只认顶层/类内浅缩进
            if pattern is _DEF_PATTERNS[-1] and indent > 8:
                continue
            key = (number, name)
            if key in seen:
                continue
            seen.add(key)
            symbols.append((number, kind, name))
            break
    return symbols


_CODE_MAP_CACHE = {}
_CODE_MAP_TTL = 120  # 秒；项目源码改动后最多两分钟自动刷新


def project_code_map(root, limit_chars=7000, max_files=600):
    """扫描项目，生成「文件 + 真实行号 + 主要符号」的紧凑索引。

    返回空字符串表示没有可用源码。索引只用于给模型定位，不包含源码内容。
    """
    root = str(root or "")
    if not root:
        return ""
    now = time.time()
    cached = _CODE_MAP_CACHE.get(root)
    if cached is not None and now - cached[0] < _CODE_MAP_TTL:
        return cached[1]
    root_path = Path(root)
    if not root_path.is_dir():
        return ""
    files = []
    scanned = 0
    ignored_dirs = SourceTreeProxy.IGNORED_DIRS | {
        "vendor", "third_party", "thirdparty", "external", "deps", "dist",
        "out", "target", "obj", "bin", "site-packages", "Pods",
        "bower_components", "packages", "coverage", "logs", "tmp",
    }
    try:
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in ignored_dirs and not d.startswith("."))
            for name in sorted(filenames):
                if Path(name).suffix.lower() not in SOURCE_SUFFIXES:
                    continue
                path = Path(dirpath) / name
                try:
                    if path.stat().st_size > 800_000:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                scanned += 1
                rel = path.relative_to(root_path).as_posix()
                files.append((rel, text.count("\n") + 1,
                              _extract_symbols(text)))
                if scanned >= max_files:
                    break
            if scanned >= max_files:
                break
    except OSError:
        return ""

    with_defs = [item for item in files if item[2]]
    without_defs = [item for item in files if not item[2]]
    with_defs.sort(key=lambda item: (item[0].count("/"), item[0]))
    without_defs.sort(key=lambda item: (item[0].count("/"), item[0]))

    lines = []
    used = 0
    header = ("（以下为项目符号索引：路径相对项目根目录，行号来自真实文件；"
              "索引没有覆盖的位置请先读取文件再引用。）")
    lines.append(header)
    used += len(header)
    truncated = False
    for rel, line_count, symbols in with_defs:
        items = [f"L{n} {kind} {name}" for n, kind, name in symbols[:24]]
        if len(symbols) > 24:
            items.append(f"…另有 {len(symbols) - 24} 个符号")
        entry = f"- {rel} ({line_count}行): " + "; ".join(items)
        if used + len(entry) + 1 > limit_chars:
            truncated = True
            break
        lines.append(entry)
        used += len(entry) + 1
    if not truncated:
        for rel, line_count, _symbols in without_defs[:120]:
            entry = f"- {rel} ({line_count}行)"
            if used + len(entry) + 1 > limit_chars:
                truncated = True
                break
            lines.append(entry)
            used += len(entry) + 1
    if truncated:
        lines.append("（索引已截断，问到的文件若未列出请先读取）")
    result = "\n".join(lines) if len(lines) > 1 else ""
    _CODE_MAP_CACHE[root] = (now, result)
    return result
# --------------------------------------------------------------------------- #
IMAGE_SIZE_CHOICES = (
    ("方形 1:1（1K）", "1:1"),
    ("横版 16:9（1K）", "16:9"),
    ("竖版 9:16（1K）", "9:16"),
    ("横版 4:3（1K）", "4:3"),
    ("竖版 3:4（1K）", "3:4"),
    ("方形 2048×2048（2K）", "2048x2048"),
    ("横版 2752×1536（2K）", "2752x1536"),
    ("竖版 1536×2752（2K）", "1536x2752"),
)
VIDEO_SIZE_CHOICES = ("720P", "1080P", "1K", "2K")
VIDEO_RATIO_CHOICES = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
SILICONFLOW_VIDEO_SIZES = ("480p", "768p")
SILICONFLOW_VIDEO_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
SILICONFLOW_VIDEO_MODEL = "MiniMaxAI/MiniMax-H3"
ATLAS_IMAGE_SIZES = (
    ("自动（模型默认）", ""),
    ("1024×1024", "1024x1024"),
    ("1536×1024", "1536x1024"),
    ("1024×1536", "1024x1536"),
    ("2048×2048", "2048x2048"),
)
ATLAS_IMAGE_MODELS = (
    ("GPT Image 2.6 Sunburst", "openai/gpt-image-2.6-sunburst/text-to-image"),
    ("Seedream v5.0 Pro", "bytedance/seedream-v5.0-pro/text-to-image"),
    ("Seedream v5.0 Flash", "bytedance/seedream-v5.0-flash/text-to-image"),
    ("Qwen Image 3.0", "qwen-image-3.0/text-to-image"),
    ("Nano Banana 2 Lite", "google/nano-banana-2-lite/text-to-image-developer"),
    ("Grok Imagine Image 2.0", "xai/grok-imagine-image-2.0-developer/text-to-image"),
)
ATLAS_VIDEO_MODELS = (
    ("Seedance 2.5", "bytedance/seedance-2.5/text-to-video"),
    ("Seedance 2.0 Mini", "bytedance/seedance-2.0-mini/text-to-video"),
    ("Kling v3.0 Std", "kwaivgi/kling-v3.0-std/text-to-video"),
    ("Wan 3.0", "alibaba/wan-3.0/text-to-video"),
    ("MiniMax H3 Fast", "minimax/h3-fast/text-to-video"),
)
ATLAS_VIDEO_SIZES = ("480p", "720p", "1080p")
ATLAS_VIDEO_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9")
IMAGE_STYLE_CHOICES = (
    ("默认（media-tools 直接生成）", ""),
    ("手绘技术插画 · handdrawn-tech-illustrations",
     "中文手绘技术插画风格：白色背景、黑色手绘线条、淡彩点缀、"
     "结构清晰、信息层级明确，适合技术文章配图。"),
    ("GPT-Image 2 风格库 · gpt-image-2-style-library",
     "按 GPT-Image 2 工业级提示词标准：主体明确、构图有层级、"
     "排版清晰、配色克制、细节丰富、避免廉价渐变和默认模板感。"),
)
VIDEO_STYLE_CHOICES = (
    ("默认（media-tools 直接生成）", ""),
    ("Seedance 分镜 · seedance-2-5-skill",
     "按 Seedance 分镜提示词格式描述：主体、动作、镜头运动、环境、"
     "光线、节奏、画面风格。"),
    ("通用视频提示词 · universal-video-prompt-skill",
     "按通用视频提示词规范描述：主体、动作、场景、镜头、光线、"
     "时长节奏、画面风格。"),
)


def find_media_script(filename):
    """在共享技能库里找到 media-tools 的生成脚本。"""
    candidates = [
        Path(shared_agents.SHARED_SKILLS) / "media-tools" / "scripts" / filename,
        Path.home() / ".claude" / "skills" / "media-tools" / "scripts" / filename,
        Path.home() / ".codex" / "skills" / "media-tools" / "scripts" / filename,
        Path.home() / ".dsh" / "skills" / "media-tools" / "scripts" / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def read_secret_key(name):
    """读取环境变量或 secrets 文件里的 key（不打印、不写回）。"""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    for env_file in (Path.home() / ".dsh" / "secrets" / "media-tools.env",
                     Path.home() / ".codex" / "secrets" / "media-tools.env"):
        try:
            if not env_file.is_file():
                continue
            for line in env_file.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def media_error_hint(message):
    text = str(message or "").strip()
    if "Key" in text or "key" in text or "密钥" in text:
        text += (
            "\n\n请先在 ~/.dsh/secrets/media-tools.env 里配置服务商密钥：\n"
            "SILICONFLOW_API_KEY=你的Key   （图片免费 Kolors；视频 MiniMax-H3）\n"
            "或 AGNES_API_KEY=你的Key      （图片 + 视频）\n"
            "也支持 SENSENOVA_API_KEY（仅图片）。")
    return text


class MediaWorker(QThread):
    """后台运行 media-tools 脚本；支持取消，并把日志回传界面。"""

    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, script, args, output_path, cwd=None, parent=None):
        super().__init__(parent)
        self.script = Path(script)
        self.args = [str(arg) for arg in args]
        self.output_path = Path(output_path)
        self.cwd = str(cwd) if cwd else None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if not self.script.is_file():
            self.failed.emit(
                f"找不到生成脚本：{self.script}\n"
                "请先在「技能与插件 → 技能库」安装 media-tools 技能。")
            return
        cmd = [sys.executable, "-I", str(self.script)] + self.args
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, start_new_session=True)
        except OSError as exc:
            self.failed.emit(f"无法启动生成脚本：{exc}")
            return
        lines = []
        line_queue = None
        if os.name == "nt":
            # Windows 的 select() 只支持套接字，读管道要另起线程
            line_queue = queue.Queue()

            def _pump():
                try:
                    for raw in proc.stdout:
                        line_queue.put(raw)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    line_queue.put(None)

            threading.Thread(target=_pump, daemon=True).start()

        while True:
            if self._cancelled:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
                self.failed.emit("已取消生成")
                return
            if line_queue is None:
                if proc.poll() is not None:
                    break
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], 0.4)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    continue
            else:
                try:
                    line = line_queue.get(timeout=0.4)
                except queue.Empty:
                    continue
                if line is None:
                    break
            line = line.rstrip()
            if line:
                lines.append(line)
                self.log.emit(line)
        try:
            rest = proc.stdout.read() if proc.stdout is not None else ""
        except Exception:  # noqa: BLE001
            rest = ""
        if rest:
            lines.extend(rest.splitlines())
        code = proc.returncode
        if code != 0:
            tail = "\n".join(lines[-12:]) or f"退出码 {code}"
            self.failed.emit(media_error_hint(tail))
            return
        if not self.output_path.is_file():
            tail = "\n".join(lines[-12:]) or "脚本没有生成目标文件"
            self.failed.emit(media_error_hint(tail))
            return
        self.succeeded.emit(str(self.output_path))


class MediaPromptDialog(QDialog):
    """输入提示词并选择尺寸/时长，然后生成图片或视频。"""

    def __init__(self, kind="image", prompt="", title=None, parent=None):
        super().__init__(parent)
        self.kind = "video" if kind == "video" else "image"
        self.setWindowTitle(
            title or ("生成图片" if self.kind == "image" else "生成视频"))
        self.setModal(True)
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        head = QLabel(
            "描述你想要的画面（图片用 media-tools / Agnes，视频用 Agnes）："
            if self.kind == "image" else
            "描述视频内容（生成时间较长，可随时取消）：")
        head.setObjectName("H2")
        layout.addWidget(head)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setObjectName("ChatInput")
        self.prompt_edit.setPlaceholderText("例如：赛博朋克风格的城市夜景，霓虹灯倒映在雨后的街道上，电影感")
        self.prompt_edit.setPlainText(prompt or "")
        self.prompt_edit.setMinimumHeight(110)
        layout.addWidget(self.prompt_edit)

        form = QFormLayout()
        form.setSpacing(8)
        if self.kind == "image":
            self.image_backend_combo = QComboBox()
            self.image_backend_combo.addItem(
                "Atlas Cloud · GPT Image / Seedream 等", "atlas")
            self.image_backend_combo.addItem(
                "media-tools · SiliconFlow / Agnes / SenseNova", "media")
            if not read_secret_key("ATLASCLOUD_API_KEY"):
                self.image_backend_combo.setCurrentIndex(1)
            form.addRow("图片服务商", self.image_backend_combo)
            self.atlas_model_combo = QComboBox()
            for label, model_id in ATLAS_IMAGE_MODELS:
                self.atlas_model_combo.addItem(label, model_id)
            form.addRow("Atlas 模型", self.atlas_model_combo)
            self.size_combo = QComboBox()
            form.addRow("尺寸", self.size_combo)
            self.style_combo = QComboBox()
            for label, prefix in IMAGE_STYLE_CHOICES:
                self.style_combo.addItem(label, prefix)
            form.addRow("提示词风格", self.style_combo)
            self.image_backend_combo.currentIndexChanged.connect(
                self._refresh_image_options)
            self._refresh_image_options()
        else:
            self.video_backend_combo = QComboBox()
            self.video_backend_combo.addItem(
                "Atlas Cloud · Seedance / Kling / Wan", "atlas")
            self.video_backend_combo.addItem(
                "SiliconFlow · 图片和视频同一把 Key", "siliconflow")
            self.video_backend_combo.addItem(
                "Agnes · 图片和视频", "agnes")
            if not read_secret_key("ATLASCLOUD_API_KEY"):
                if read_secret_key("SILICONFLOW_API_KEY"):
                    self.video_backend_combo.setCurrentIndex(1)
                else:
                    self.video_backend_combo.setCurrentIndex(2)
            form.addRow("视频服务商", self.video_backend_combo)
            self.atlas_model_combo = QComboBox()
            for label, model_id in ATLAS_VIDEO_MODELS:
                self.atlas_model_combo.addItem(label, model_id)
            form.addRow("Atlas 模型", self.atlas_model_combo)
            self.seconds_spin = QSpinBox()
            self.seconds_spin.setSuffix(" 秒")
            form.addRow("时长", self.seconds_spin)
            self.size_combo = QComboBox()
            form.addRow("分辨率", self.size_combo)
            self.ratio_combo = QComboBox()
            form.addRow("画幅", self.ratio_combo)
            self.style_combo = QComboBox()
            for label, prefix in VIDEO_STYLE_CHOICES:
                self.style_combo.addItem(label, prefix)
            form.addRow("提示词风格", self.style_combo)
            self.video_backend_combo.currentIndexChanged.connect(
                self._refresh_video_options)
        layout.addLayout(form)

        self.provider_hint = QLabel("")
        self.provider_hint.setObjectName("Muted")
        self.provider_hint.setWordWrap(True)
        layout.addWidget(self.provider_hint)

        if self.kind == "image":
            self._refresh_image_options()
        else:
            self._refresh_video_options()

        hint = QLabel(
            "图片：Atlas Cloud 或 media-tools；视频：Atlas Cloud / SiliconFlow / "
            "Agnes。\n一把 Atlas Cloud Key 同时覆盖图片和视频。"
            "文件会保存到当前项目的 .claude-console/generated/ 目录；"
            "Key 配置在 ~/.dsh/secrets/media-tools.env。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("开始生成")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._accept_if_ready)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
        QTimer.singleShot(0, self.prompt_edit.setFocus)

    def _refresh_image_options(self, *_args):
        backend = self.image_backend_combo.currentData()
        self.size_combo.blockSignals(True)
        self.size_combo.clear()
        hint = getattr(self, "provider_hint", None)
        if backend == "atlas":
            self.atlas_model_combo.setEnabled(True)
            for label, value in ATLAS_IMAGE_SIZES:
                self.size_combo.addItem(label, value)
            self.size_combo.setCurrentIndex(
                1 if self.size_combo.count() > 1 else 0)
            key_ok = bool(read_secret_key("ATLASCLOUD_API_KEY"))
            if hint is not None:
                hint.setText(
                    "Atlas Cloud：图片按张计费，模型可选 GPT Image / "
                    "Seedream / Qwen Image 等。"
                    + ("已检测到 ATLASCLOUD_API_KEY。" if key_ok
                       else "未检测到 ATLASCLOUD_API_KEY，请先配置。"))
        else:
            self.atlas_model_combo.setEnabled(False)
            for label, value in IMAGE_SIZE_CHOICES:
                self.size_combo.addItem(label, value)
            providers = []
            for name in ("SILICONFLOW_API_KEY", "AGNES_API_KEY",
                         "SENSENOVA_API_KEY"):
                if read_secret_key(name):
                    providers.append(name.split("_", 1)[0])
            if hint is not None:
                hint.setText(
                    "media-tools：图片走 SiliconFlow / Agnes / SenseNova。"
                    + ("已检测到 " + " / ".join(providers) + "。"
                       if providers else "未检测到图片 Key，请先配置。"))
        self.size_combo.blockSignals(False)

    def _refresh_video_options(self, *_args):
        backend = self.video_backend_combo.currentData()
        self.size_combo.blockSignals(True)
        self.ratio_combo.blockSignals(True)
        self.size_combo.clear()
        self.ratio_combo.clear()
        hint = getattr(self, "provider_hint", None)
        if backend == "atlas":
            self.atlas_model_combo.setEnabled(True)
            self.seconds_spin.setRange(4, 30)
            self.seconds_spin.setValue(5)
            self.size_combo.addItems(ATLAS_VIDEO_SIZES)
            self.size_combo.setCurrentIndex(
                1 if self.size_combo.count() > 1 else 0)
            self.ratio_combo.addItems(ATLAS_VIDEO_RATIOS)
            key_ok = bool(read_secret_key("ATLASCLOUD_API_KEY"))
            if hint is not None:
                hint.setText(
                    "Atlas Cloud：视频按模型/时长计费，可选 Seedance 2.5、"
                    "Kling、Wan 等。"
                    + ("已检测到 ATLASCLOUD_API_KEY。" if key_ok
                       else "未检测到 ATLASCLOUD_API_KEY，请先配置。"))
        elif backend == "siliconflow":
            self.atlas_model_combo.setEnabled(False)
            self.seconds_spin.setRange(5, 15)
            self.seconds_spin.setValue(5)
            self.size_combo.addItems(SILICONFLOW_VIDEO_SIZES)
            self.ratio_combo.addItems(SILICONFLOW_VIDEO_RATIOS)
            self.size_combo.setCurrentIndex(
                1 if self.size_combo.count() > 1 else 0)
            key_ok = bool(read_secret_key("SILICONFLOW_API_KEY"))
            if hint is not None:
                hint.setText(
                    f"SiliconFlow：图片用 Kolors（免费），视频用 "
                    f"{SILICONFLOW_VIDEO_MODEL}（按次计费）。"
                    + ("已检测到 SILICONFLOW_API_KEY。" if key_ok
                       else "未检测到 SILICONFLOW_API_KEY，请先配置。"))
        else:
            self.atlas_model_combo.setEnabled(False)
            self.seconds_spin.setRange(4, 12)
            self.seconds_spin.setValue(5)
            self.size_combo.addItems(VIDEO_SIZE_CHOICES)
            self.ratio_combo.addItems(VIDEO_RATIO_CHOICES)
            key_ok = bool(read_secret_key("AGNES_API_KEY"))
            if hint is not None:
                hint.setText(
                    "Agnes：图片和视频共用 AGNES_API_KEY。"
                    + ("已检测到 AGNES_API_KEY。" if key_ok
                       else "未检测到 AGNES_API_KEY，请先配置。"))
        self.size_combo.blockSignals(False)
        self.ratio_combo.blockSignals(False)

    def _accept_if_ready(self):
        if not self.prompt_edit.toPlainText().strip():
            QMessageBox.information(self, "还需要描述", "请先写下画面描述。")
            return
        self.accept()

    def values(self):
        raw = self.prompt_edit.toPlainText().strip()
        prefix = self.style_combo.currentData() or ""
        data = {"prompt": (prefix + "\n" + raw) if prefix else raw,
                "display_prompt": raw}
        if self.kind == "image":
            backend = self.image_backend_combo.currentData()
            data["image_backend"] = backend
            data["size"] = self.size_combo.currentData() or ""
            if backend == "atlas":
                data["model"] = self.atlas_model_combo.currentData()
        else:
            backend = self.video_backend_combo.currentData()
            data["video_backend"] = backend
            data["seconds"] = self.seconds_spin.value()
            data["video_size"] = self.size_combo.currentText()
            data["ratio"] = self.ratio_combo.currentText()
            if backend == "atlas":
                data["model"] = self.atlas_model_combo.currentData()
        return data


class MediaResultWidget(QFrame):
    """在聊天/研读页里展示生成结果：缩略图或视频入口。"""

    def __init__(self, path, kind="image", caption="", parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.kind = "video" if kind == "video" else "image"
        self.setObjectName("MediaResult")
        self.setMaximumWidth(580)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        preview = QLabel()
        preview.setFixedSize(150, 104)
        preview.setAlignment(Qt.AlignCenter)
        preview.setCursor(Qt.PointingHandCursor)
        if self.kind == "image":
            pixmap = QPixmap(str(self.path))
            if not pixmap.isNull():
                preview.setPixmap(pixmap.scaled(
                    150, 104, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            preview.setText("🎬")
            preview.setStyleSheet("font-size: 42px;")
        preview.setToolTip("点击打开")
        preview.mousePressEvent = lambda _event: self._open()
        layout.addWidget(preview)

        info = QVBoxLayout()
        info.setSpacing(3)
        title = QLabel(caption or (
            "已生成图片" if self.kind == "image" else "已生成视频"))
        title.setObjectName("MediaResultTitle")
        info.addWidget(title)
        path_label = QLabel(str(self.path))
        path_label.setObjectName("Muted")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(path_label)
        open_btn = QPushButton(
            "打开图片" if self.kind == "image" else "打开视频")
        open_btn.setObjectName("Flat")
        open_btn.clicked.connect(self._open)
        info.addWidget(open_btn, 0, Qt.AlignLeft)
        layout.addLayout(info, 1)
        self.setToolTip(str(self.path))

    def _open(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))


# Cute Pink Light Theme（webfreak.cute-theme）的编辑器配色
CUTE_THEME = {
    "bg": "#ffffff",
    "fg": "#333333",
    "comment": "#008000",
    "keyword": "#0000FF",
    "string": "#A31515",
    "number": "#098658",
    "function": "#795E26",
    "type": "#267F99",
    "constant": "#328267",
    "preprocessor": "#0000FF",
    "gutter_bg": "#fceaf1",
    "line_number": "#39007f",
    "line_number_active": "#1d5f9e",
    # 跳转目标行用淡蓝色 + 高透明度，避免绿色/粉色底色干扰阅读。
    # Qt 的 8 位颜色是 #AARRGGBB：0x26 透明度 + 淡蓝 4A9EFF。
    "highlight": "#264a9eff",
    "border": "#f4b4cc",
    "tab_bg": "#fceaf1",
    "tab_active": "#fcdee9",
}

CPP_KEYWORDS = {
    "alignas", "alignof", "and", "asm", "auto", "bool", "break", "case",
    "catch", "char", "class", "const", "constexpr", "const_cast", "continue",
    "decltype", "default", "delete", "do", "double", "dynamic_cast", "else",
    "enum", "explicit", "export", "extern", "false", "final", "float", "for",
    "friend", "goto", "if", "inline", "int", "long", "mutable", "namespace",
    "new", "noexcept", "not", "nullptr", "operator", "or", "override",
    "private", "protected", "public", "register", "reinterpret_cast",
    "return", "short", "signed", "sizeof", "static", "static_assert",
    "static_cast", "struct", "switch", "template", "this", "throw", "true",
    "try", "typedef", "typeid", "typename", "union", "unsigned", "using",
    "virtual", "void", "volatile", "wchar_t", "while", "xor",
    "and_eq", "bitand", "bitor", "compl", "not_eq", "or_eq", "xor_eq",
}

CPP_TYPES = {
    "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "size_t", "ssize_t", "param_t", "Vector2f", "Vector3f", "Vector4f",
    "Matrix3f", "Matrix4f", "Quatf", "Quaternion", "Dcmf", "Eulerf",
    "Vector", "Matrix",
}

CPP_CONSTANTS = {
    "true", "false", "nullptr", "NULL", "NAN", "INFINITY", "PI", "PI_F",
    "FLT_EPSILON", "FLT_MAX", "OK",
}

CPP_NUMBER_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)[uUlLfF]*")
CPP_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


class CppHighlighter(QSyntaxHighlighter):
    """够用的 C/C++ 高亮：注释、字符串、预处理、关键字、类型、函数、数字。"""

    def __init__(self, document):
        super().__init__(document)
        self._f = {}
        for key, color in CUTE_THEME.items():
            if key in ("bg", "gutter_bg", "border", "tab_bg", "tab_active"):
                continue
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self._f[key] = fmt
        # 常量/枚举值用略深一点的绿色，与 VS Code 的 Cute 主题一致
        self._f["constant"] = QTextCharFormat()
        self._f["constant"].setForeground(QColor(CUTE_THEME["constant"]))

    def highlightBlock(self, text):
        formats = self._f
        length = len(text)
        index = 0
        if self.previousBlockState() == 1:
            end = text.find("*/")
            if end < 0:
                self.setFormat(0, length, formats["comment"])
                self.setCurrentBlockState(1)
                return
            self.setFormat(0, end + 2, formats["comment"])
            index = end + 2
            self.setCurrentBlockState(0)

        is_preprocessor = text.lstrip().startswith("#")

        while index < length:
            if text.startswith("//", index):
                self.setFormat(index, length - index, formats["comment"])
                break
            if text.startswith("/*", index):
                end = text.find("*/", index + 2)
                if end < 0:
                    self.setFormat(index, length - index, formats["comment"])
                    self.setCurrentBlockState(1)
                    break
                self.setFormat(index, end + 2 - index, formats["comment"])
                index = end + 2
                continue
            char = text[index]
            if char == '"':
                end = index + 1
                while end < length:
                    if text[end] == "\\":
                        end += 2
                        continue
                    if text[end] == '"':
                        end += 1
                        break
                    end += 1
                self.setFormat(index, end - index, formats["string"])
                index = end
                continue
            if char == "'":
                end = index + 1
                while end < length:
                    if text[end] == "\\":
                        end += 2
                        continue
                    if text[end] == "'":
                        end += 1
                        break
                    end += 1
                self.setFormat(index, end - index, formats["string"])
                index = end
                continue
            if is_preprocessor and char == "<":
                end = text.find(">", index + 1)
                if end >= 0 and "#include" in text[:index]:
                    self.setFormat(index, end + 1 - index, formats["string"])
                    index = end + 1
                    continue
            if char == "#" and not text[:index].strip():
                end = index + 1
                while end < length and text[end] in " \t":
                    end += 1
                word_end = end
                while word_end < length and (text[word_end].isalnum()
                                             or text[word_end] == "_"):
                    word_end += 1
                if word_end > end:
                    self.setFormat(end, word_end - end,
                                   formats["preprocessor"])
                index = word_end
                continue
            if char.isdigit() or (char == "." and index + 1 < length
                                  and text[index + 1].isdigit()):
                match = CPP_NUMBER_RE.match(text, index)
                if match:
                    self.setFormat(index, match.end() - index,
                                   formats["number"])
                    index = match.end()
                    continue
            if char.isalpha() or char == "_":
                match = CPP_IDENT_RE.match(text, index)
                word = match.group(0)
                end = match.end()
                look = end
                while look < length and text[look] in " \t":
                    look += 1
                is_call = look < length and text[look] == "("
                if word in CPP_KEYWORDS:
                    self.setFormat(index, end - index, formats["keyword"])
                elif word in CPP_TYPES or word.endswith("_t"):
                    self.setFormat(index, end - index, formats["type"])
                elif word in CPP_CONSTANTS:
                    self.setFormat(index, end - index, formats["constant"])
                elif is_call:
                    self.setFormat(index, end - index, formats["function"])
                elif word[:1].isupper() and not word.isupper():
                    self.setFormat(index, end - index, formats["type"])
                index = end
                continue
            index += 1


class StudyLineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


class StudyCodeView(QPlainTextEdit):
    """带行号的只读代码视图，支持跳转到指定行并高亮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.target_range = (0, 0)
        font = QFont("DejaVu Sans Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPixelSize(13)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet(
            "QPlainTextEdit { background: #ffffff; border: 1px solid #f4b4cc;"
            " border-radius: 6px; padding: 6px; color: #333333;"
            " selection-background-color: #304a9eff;"
            " selection-color: #333333; }")
        self.highlighter = CppHighlighter(self.document())
        self.line_number_area = StudyLineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width(0)

    def line_number_area_width(self):
        digits = 1
        count = max(1, self.blockCount())
        while count >= 10:
            count //= 10
            digits += 1
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _count):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(rect.left(), rect.top(),
                  self.line_number_area_width(), rect.height()))

    def paint_line_numbers(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(CUTE_THEME["gutter_bg"]))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        target_start, target_end = self.target_range
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                line = number + 1
                active = target_start <= line <= target_end
                font = QFont(self.font())
                font.setBold(active)
                painter.setFont(font)
                painter.setPen(QColor(
                    CUTE_THEME["line_number_active"] if active
                    else CUTE_THEME["line_number"]))
                painter.drawText(
                    0, int(top), self.line_number_area.width() - 6,
                    self.fontMetrics().height(), Qt.AlignRight, str(line))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    def goto_line(self, start, end=None):
        start = max(1, int(start or 1))
        end = max(start, int(end or start))
        count = self.document().blockCount()
        if count > 0:
            start = min(start, count)
            end = min(max(end, start), count)
        self.target_range = (start, end)
        self.line_number_area.update()
        document = self.document()
        first = document.findBlockByNumber(start - 1)
        if not first.isValid():
            return
        last = document.findBlockByNumber(end - 1) or first
        cursor = QTextCursor(first)
        cursor.setPosition(
            last.position() + max(0, last.length() - 1), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        # 让选区“起始行”位于视口中部。
        # 注意：QPlainTextEdit 的垂直滚动条单位是“行/块号”，不是像素；
        # 而 blockBoundingGeometry 返回的是像素，直接混用会让值溢出被
        # 截断到 0 或最大值（表现为总是跳到文件最顶/最底）。
        bar = self.verticalScrollBar()
        visible_lines = max(1, bar.pageStep())
        target = start - 1 - visible_lines // 2
        bar.setValue(max(0, min(target, bar.maximum())))
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(QColor(CUTE_THEME["highlight"]))
        selection.format.setProperty(
            QTextFormat.FullWidthSelection, True)
        self.setExtraSelections([selection])


class SourceTreeProxy(QSortFilterProxyModel):
    """隐藏构建产物等噪音目录，只保留可阅读的项目结构。"""

    IGNORED_DIRS = {
        "build", "builds", ".git", ".cache", "node_modules", "__pycache__",
        ".vscode", ".idea", "CMakeFiles", ".pytest_cache", "venv",
        "geographiclib-datasets", ".claude-console",
    }

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        if model.isDir(index):
            return model.fileName(index) not in self.IGNORED_DIRS
        return True


class StudyFloatWindow(QMainWindow):
    """把代码研读页放进独立窗口；关闭时回到主窗口页签。"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class StudyQaWindow(QMainWindow):
    """解释栏问答的悬浮窗：半透明、置顶，可关闭后按键召回。"""

    def __init__(self):
        # 不挂到研读页上：研读页在页签与独立窗口之间来回搬运时，
        # 问答窗作为独立顶层窗口可以一直保留，关闭只是隐藏。
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("解释栏问答 · Claude 控制台")
        self.setWindowIcon(make_app_icon())
        self.setWindowOpacity(0.9)
        self.resize(560, 660)


class ChatQaWindow(QMainWindow):
    """主对话选中内容问答的悬浮窗：半透明、置顶，关闭后按键召回。"""

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("选中内容问答 · Claude 控制台")
        self.setWindowIcon(make_app_icon())
        self.setWindowOpacity(0.9)
        self.resize(560, 660)


class CodeStudyPage(QWidget):
    """代码研读页：主对话框驱动总体讲解，选中解释文字可就地追问。"""

    float_requested = pyqtSignal()
    reveal_requested = pyqtSignal()
    project_requested = pyqtSignal(str)
    load_record_requested = pyqtSignal(dict)

    SOURCE_SUFFIXES = (
        "*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hh", "*.hpp", "*.hxx",
        "*.ipp", "*.inl", "*.msg", "*.srv", "*.py", "*.cmake", "*.txt",
        "*.md", "*.markdown", "*.yaml", "*.yml", "*.json", "*.xml", "*.sh",
        "*.kconfig", "*.params", "*.proto", "Kconfig", "CMakeLists.txt",
    )

    HELP_TEXT = (
        "在「对话」页勾选「研读模式」后提问，回答会显示在下方解释栏；"
        "在解释栏或代码区选中内容后点「针对选中内容提问」，"
        "会打开悬浮问答窗口追问（Ctrl+Shift+Q 可随时召回）。"
        "研读内容可用「保存研读 / 载入研读 / 返回刚才」管理，"
        "也可以直接为当前研读生成配图或视频。")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = ""
        self.permission_mode = "default"
        self.model = None
        self.effort = None
        self._explanation_markdown = ""
        self._qa_messages = []  # [{"id","role","text","merged","streaming"}]
        self._qa_next_id = 1
        self._qa_answer_id = None
        self._qa_live = ""
        self._qa_busy = False
        self._last_question = ""
        self._last_answer = ""
        self._pending_quote = ""
        self._study_goal = ""
        self._study_media = []  # [{"path","kind","caption"}]
        self._study_back_stack = []
        self._saved_study_name = ""
        # 当前载入/保存的研读记录文件；之后的修改会自动同步回这个文件。
        self._saved_study_path = None
        self._media_worker = None
        self._media_dialog = None
        self._media_busy = False
        # 最近一次产生选区的控件（解释栏或某个代码页），用于“针对选中内容提问”
        self._quote_source = None
        self._worker = None
        self._worker_cfg = None
        self._current_file = None
        self._current_line = 1
        self._code_tabs = []
        self._history = []
        self._history_pos = -1
        self._floating = False
        self._fs_model = None
        self._tree_proxy = None
        self._placeholder_visible = False
        self._build_ui()
        self._qa_timer = QTimer(self)
        self._qa_timer.setInterval(80)
        self._qa_timer.timeout.connect(self._flush_qa_live)
        # 载入记录后，内容变化稍后自动写回原记录文件。
        self._record_timer = QTimer(self)
        self._record_timer.setSingleShot(True)
        self._record_timer.setInterval(900)
        self._record_timer.timeout.connect(self._write_study_record)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        title = QLabel("代码研读")
        title.setObjectName("H2")
        self.root_label = QLabel("未选择项目")
        self.root_label.setObjectName("Muted")
        self.root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.save_study_btn = QPushButton("保存研读")
        self.save_study_btn.setObjectName("Flat")
        self.save_study_btn.setToolTip("把当前解释栏、问答和配图保存成一个研读记录")
        self.save_study_btn.clicked.connect(self._save_study)
        self.load_study_btn = QPushButton("载入研读")
        self.load_study_btn.setObjectName("Flat")
        self.load_study_btn.setToolTip("载入之前保存的研读记录")
        self.load_study_btn.clicked.connect(self._show_study_menu)
        self.back_study_btn = QPushButton("返回刚才")
        self.back_study_btn.setObjectName("Flat")
        self.back_study_btn.setToolTip("回到载入记录之前的那次研读")
        self.back_study_btn.setEnabled(False)
        self.back_study_btn.clicked.connect(self._restore_previous_study)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.setObjectName("Flat")
        self.clear_btn.clicked.connect(self.clear_all)
        self.float_btn = QPushButton("独立窗口")
        self.float_btn.setObjectName("Flat")
        self.float_btn.setToolTip("把代码研读页放进独立窗口；再次点击收回页签")
        self.float_btn.clicked.connect(self.float_requested.emit)
        bar.addWidget(title)
        bar.addWidget(self.root_label, 1)
        bar.addWidget(self.save_study_btn)
        bar.addWidget(self.load_study_btn)
        bar.addWidget(self.back_study_btn)
        bar.addWidget(self.float_btn)
        bar.addWidget(self.clear_btn)
        layout.addLayout(bar)

        self.goal_label = QLabel(self.HELP_TEXT)
        self.goal_label.setObjectName("Muted")
        self.goal_label.setWordWrap(True)
        layout.addWidget(self.goal_label)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(6)
        splitter.setChildrenCollapsible(False)

        # ---- 左：解释 + 问答 ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(6)

        explain_head = QHBoxLayout()
        explain_title = QLabel("AI 解释")
        explain_title.setObjectName("H2")
        self.ask_btn = QPushButton("针对选中内容提问")
        self.ask_btn.setToolTip(
            "先在解释栏或代码区选中一段内容，再点击这里打开悬浮问答窗并带入引用")
        self.ask_btn.clicked.connect(self._quote_selection)
        self.qa_open_btn = QPushButton("问答窗口")
        self.qa_open_btn.setObjectName("Flat")
        self.qa_open_btn.setToolTip(
            "打开/召回悬浮问答窗口（快捷键 Ctrl+Shift+Q）")
        self.qa_open_btn.clicked.connect(self._show_qa_window)
        self.study_image_btn = QPushButton("生成配图")
        self.study_image_btn.setObjectName("Flat")
        self.study_image_btn.setToolTip(
            "根据选中的解释/代码或当前研读主题生成一张图片，并附在解释栏下方")
        self.study_image_btn.clicked.connect(
            lambda: self.generate_media("image"))
        self.study_video_btn = QPushButton("生成视频")
        self.study_video_btn.setObjectName("Flat")
        self.study_video_btn.setToolTip(
            "根据当前研读主题生成一段短视频，生成后显示在解释栏下方")
        self.study_video_btn.clicked.connect(
            lambda: self.generate_media("video"))
        self.explain_status = QLabel("等待研读请求")
        self.explain_status.setObjectName("Muted")
        explain_head.addWidget(explain_title)
        explain_head.addWidget(self.ask_btn)
        explain_head.addWidget(self.qa_open_btn)
        explain_head.addWidget(self.study_image_btn)
        explain_head.addWidget(self.study_video_btn)
        explain_head.addStretch(1)
        explain_head.addWidget(self.explain_status)
        left_layout.addLayout(explain_head)

        self.explain_view = QTextBrowser()
        self.explain_view.setOpenLinks(False)
        self.explain_view.setOpenExternalLinks(False)
        self.explain_view.anchorClicked.connect(self._on_anchor_clicked)
        self.explain_view.selectionChanged.connect(
            lambda: self._remember_quote_source(self.explain_view))
        self.explain_view.setStyleSheet(
            "QTextBrowser { background: #ffffff; border: 1px solid #f6d9e6;"
            " border-radius: 8px; padding: 8px; color: #4b3742; }")
        # 解释栏直接拉伸到底，与右侧代码框底边对齐；按钮全部收进标题行
        left_layout.addWidget(self.explain_view, 1)

        self.study_media_container = QWidget()
        self.study_media_layout = QVBoxLayout(self.study_media_container)
        self.study_media_layout.setContentsMargins(0, 0, 0, 0)
        self.study_media_layout.setSpacing(6)
        self.study_media_container.setVisible(False)
        left_layout.addWidget(self.study_media_container)

        # 问答题面搬到独立悬浮窗，解释栏下方只保留解释正文
        self._build_qa_window()

        splitter.addWidget(left)

        # ---- 右：目录 + 多页源码 ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(6)

        file_row = QHBoxLayout()
        self.file_label = QLabel("尚未选择代码位置")
        self.file_label.setObjectName("H2")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.nav_back_btn = QToolButton()
        self.nav_back_btn.setText("←")
        self.nav_back_btn.setObjectName("Small")
        self.nav_back_btn.setToolTip("上一个代码位置")
        self.nav_back_btn.clicked.connect(self._go_back)
        self.nav_back_btn.setEnabled(False)
        self.nav_forward_btn = QToolButton()
        self.nav_forward_btn.setText("→")
        self.nav_forward_btn.setObjectName("Small")
        self.nav_forward_btn.setToolTip("下一个代码位置")
        self.nav_forward_btn.clicked.connect(self._go_forward)
        self.nav_forward_btn.setEnabled(False)

        self.explorer_btn = QPushButton("目录")
        self.explorer_btn.setObjectName("Flat")
        self.explorer_btn.setCheckable(True)
        self.explorer_btn.setChecked(True)
        self.explorer_btn.setToolTip("显示/隐藏代码目录")
        self.explorer_btn.toggled.connect(self._toggle_explorer)

        self.reload_btn = QPushButton("重新加载")
        self.reload_btn.setObjectName("Flat")
        self.reload_btn.clicked.connect(self._reload_current)
        self.editor_btn = QPushButton("VS Code")
        self.editor_btn.setObjectName("Flat")
        self.editor_btn.setToolTip("在 VS Code 中打开当前文件并定位到当前行")
        self.editor_btn.clicked.connect(self._open_in_editor)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.nav_back_btn)
        file_row.addWidget(self.nav_forward_btn)
        file_row.addWidget(self.explorer_btn)
        file_row.addWidget(self.reload_btn)
        file_row.addWidget(self.editor_btn)
        right_layout.addLayout(file_row)

        self.right_splitter = QSplitter(Qt.Horizontal)
        self.right_splitter.setHandleWidth(5)
        self.right_splitter.setChildrenCollapsible(False)

        self.explorer = QTreeView()
        self.explorer.setObjectName("SourceTree")
        self.explorer.setHeaderHidden(True)
        self.explorer.setMinimumWidth(150)
        self.explorer.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.explorer.setAnimated(True)
        self.explorer.doubleClicked.connect(self._open_tree_index)
        self.explorer.setStyleSheet(
            "QTreeView { background: #fceaf1; border: 1px solid #f4b4cc;"
            " border-radius: 6px; padding: 4px; color: #333333; }"
            "QTreeView::item { padding: 2px 4px; }"
            "QTreeView::item:hover { background: #fcdee9; }"
            "QTreeView::item:selected { background: #ffb1b7; color: #39007f; }")
        self.right_splitter.addWidget(self.explorer)

        self.code_tabs = QTabWidget()
        self.code_tabs.setObjectName("CodeTabs")
        self.code_tabs.setTabsClosable(True)
        self.code_tabs.setMovable(True)
        self.code_tabs.setDocumentMode(True)
        self.code_tabs.tabCloseRequested.connect(self._close_code_tab)
        self.code_tabs.currentChanged.connect(self._on_code_tab_changed)
        self.code_tabs.setStyleSheet(
            "QTabWidget#CodeTabs::pane { border: 1px solid #f4b4cc;"
            " background: #ffffff; }"
            "QTabBar::tab { background: #fceaf1; color: #333333;"
            " padding: 5px 12px; border: 1px solid #f4b4cc;"
            " border-bottom: none; margin-right: 2px; }"
            "QTabBar::tab:selected { background: #fcdee9; color: #39007f;"
            " font-weight: 600; }"
            "QTabBar::tab:hover { background: #fcdee9; }")
        self.code_placeholder = QLabel(
            "点击解释中的 📄 链接，或双击左侧目录里的文件\n"
            "打开的文件会保留为页签，可切换或关闭")
        self.code_placeholder.setObjectName("Empty")
        self.code_placeholder.setAlignment(Qt.AlignCenter)
        self.code_placeholder.setWordWrap(True)
        self.code_tabs.addTab(self.code_placeholder, "未打开文件")
        self._placeholder_visible = True
        self.right_splitter.addWidget(self.code_tabs)
        self.right_splitter.setStretchFactor(0, 0)
        self.right_splitter.setStretchFactor(1, 1)
        self.right_splitter.setSizes([210, 700])
        right_layout.addWidget(self.right_splitter, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 560])
        layout.addWidget(splitter, 1)

        self._update_qa_buttons()

    def _build_qa_window(self):
        """在独立悬浮窗里搭好解释栏问答的视图、输入框和发送按钮。"""
        window = StudyQaWindow()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("解释栏问答")
        title.setObjectName("H2")
        self.qa_quote_hint = QLabel("未选中文字")
        self.qa_quote_hint.setObjectName("Muted")
        recall_hint = QLabel("Ctrl+Shift+Q 召回")
        recall_hint.setObjectName("Muted")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.qa_quote_hint)
        head.addWidget(recall_hint)
        layout.addLayout(head)

        self.qa_view = QTextBrowser()
        self.qa_view.setOpenLinks(False)
        self.qa_view.setOpenExternalLinks(False)
        self.qa_view.anchorClicked.connect(self._on_anchor_clicked)
        self.qa_view.setStyleSheet(
            "QTextBrowser { background: #fffafc; border: 1px solid #f6d9e6;"
            " border-radius: 8px; padding: 8px; color: #4b3742; }")
        self.qa_view.setPlaceholderText(
            "选中解释或代码后提问，问题与回答会显示在这里")
        layout.addWidget(self.qa_view, 1)

        self.qa_input = ChatInput()
        self.qa_input.setObjectName("ChatInput")
        self.qa_input.setFixedHeight(84)
        self.qa_input.setPlaceholderText(
            "针对解释或代码的局部问题，Enter 发送，Shift+Enter 换行")
        self.qa_input.submitted.connect(self._send_question)
        layout.addWidget(self.qa_input)

        bottom = QHBoxLayout()
        self.qa_status = QLabel("")
        self.qa_status.setObjectName("Muted")
        bottom.addWidget(self.qa_status, 1)
        self.qa_send_btn = QPushButton("发送")
        self.qa_send_btn.setObjectName("Primary")
        self.qa_send_btn.clicked.connect(self._send_question)
        bottom.addWidget(self.qa_send_btn)
        layout.addLayout(bottom)

        window.setCentralWidget(container)
        self.qa_window = window

    def _show_qa_window(self):
        window = self.qa_window
        if not window.isVisible():
            # 第一次打开时贴在研读页所在窗口的右侧，避免正好盖住解释栏
            anchor = self.window().frameGeometry()
            window.move(
                max(anchor.left() + 40, anchor.right() - window.width() - 40),
                anchor.top() + 80)
        window.show()
        window.raise_()
        window.activateWindow()

    # ------------------------------------------------------------- project --
    def set_project(self, root):
        root = root or ""
        if root == self.root:
            self.root_label.setText(
                f"项目：{root}" if root else "未选择项目")
            return
        # 切换项目前把当前研读压入“返回刚才”栈，避免误切后内容全丢
        if self.root and self._has_study_content():
            self._study_back_stack.append(self._snapshot())
        self.root = root
        self.root_label.setText(f"项目：{root}" if root else "未选择项目")
        self._current_file = None
        self._current_line = 1
        self._close_all_code_tabs()
        self._history = []
        self._history_pos = -1
        self._update_history_buttons()
        self._set_explorer_root(root)
        self.file_label.setText("尚未选择代码位置")
        self._explanation_markdown = ""
        self.explain_view.clear()
        self._qa_messages = []
        self._qa_next_id = 1
        self._qa_answer_id = None
        self.qa_view.clear()
        self._qa_live = ""
        self._last_answer = ""
        self._study_goal = ""
        self._study_media = []
        self._render_media_results()
        self.goal_label.setText(self.HELP_TEXT)
        self.goal_label.show()
        self._pending_quote = ""
        if hasattr(self, "qa_quote_hint"):
            self.qa_quote_hint.setText("未选中文字")
        self.explain_status.setText("等待研读请求")
        self._update_study_buttons()
        if self._worker is not None:
            self._worker.reset()
        self._worker_cfg = None
        self._update_qa_buttons()

    def set_runtime(self, permission_mode, model, effort):
        self.permission_mode = permission_mode or "default"
        self.model = model
        self.effort = effort
        # 下次问答时按新配置重建会话
        if self._worker is not None:
            self._worker_cfg = None

    # --------------------------------------------------------- explanation --
    def begin_study(self, goal):
        # 开始新一轮研读前保留上一份内容，方便用「返回刚才」找回
        if self._explanation_markdown.strip() or self._qa_messages:
            self._study_back_stack.append(self._snapshot())
            del self._study_back_stack[:-15]
            self._update_study_buttons()
        self._study_goal = (goal or "").strip()
        # 研读进行中不显示「正在研读：…」这一行，节省顶部空间
        self.goal_label.hide()
        self.explain_status.setText("Claude 正在生成解释…")

    def preview_explanation(self, text):
        self._explanation_markdown = text
        self._render_explanation(scroll="bottom")

    def show_explanation(self, text):
        self._explanation_markdown = text or ""
        self._render_explanation(scroll="top")
        self.explain_status.setText("讲解已更新 · 可点击链接跳转源码")

    def append_explanation(self, text):
        if not text:
            return
        self._explanation_markdown = (
            self._explanation_markdown.rstrip() + "\n\n" + text.strip() + "\n")
        self._render_explanation(scroll="bottom")

    def append_note(self, text):
        self.append_explanation(f"> {text}")

    def _render_explanation(self, scroll="top"):
        self._schedule_record_save()
        rendered = study_refs_to_markdown(self._explanation_markdown)
        self.explain_view.setMarkdown(rendered)
        bar = self.explain_view.verticalScrollBar()
        if scroll == "bottom":
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
        else:
            QTimer.singleShot(0, lambda: bar.setValue(bar.minimum()))

    # ------------------------------------------------------- study records --
    def _has_study_content(self):
        return bool(self._explanation_markdown.strip()
                    or self._qa_messages or self._study_media)

    def _snapshot(self):
        return {
            "format": 1,
            "name": self._saved_study_name or self._study_goal or "未命名研读",
            "root": self.root,
            "goal": self._study_goal,
            "explanation": self._explanation_markdown,
            "qa": [
                {"role": m.get("role", "assistant"),
                 "text": m.get("text", ""),
                 "merged": bool(m.get("merged"))}
                for m in self._qa_messages
            ],
            "media": [dict(item) for item in self._study_media],
            "current_file": str(self._current_file) if self._current_file else "",
            "current_line": int(self._current_line or 1),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }

    def capture_state(self):
        """给主窗口用：保存当前项目研读页的完整内容（不落盘）。"""
        try:
            back_stack = json.loads(json.dumps(
                self._study_back_stack, ensure_ascii=False))
        except (TypeError, ValueError):
            back_stack = []
        return {
            "goal": self._study_goal,
            "saved_name": self._saved_study_name,
            "explanation": self._explanation_markdown,
            "qa": [
                {"role": m.get("role", "assistant"),
                 "text": m.get("text", ""),
                 "merged": bool(m.get("merged"))}
                for m in self._qa_messages
            ],
            "study_media": [dict(item) for item in self._study_media],
            "back_stack": back_stack,
            "last_answer": self._last_answer,
            "current_file": str(self._current_file) if self._current_file else "",
            "current_line": int(self._current_line or 1),
        }

    def restore_state(self, state):
        """给主窗口用：切回项目时恢复它自己的研读页内容。"""
        if not isinstance(state, dict):
            return
        self._saved_study_name = state.get("saved_name") or ""
        self._study_goal = state.get("goal") or ""
        self._explanation_markdown = state.get("explanation") or ""
        self._study_back_stack = [
            item for item in (state.get("back_stack") or [])
            if isinstance(item, dict)]
        self._qa_messages = []
        self._qa_next_id = 1
        for item in state.get("qa") or []:
            self._qa_messages.append({
                "id": self._qa_next_id,
                "role": item.get("role", "assistant"),
                "text": item.get("text", ""),
                "merged": bool(item.get("merged")),
                "streaming": False,
            })
            self._qa_next_id += 1
        self._qa_answer_id = None
        self._qa_live = ""
        self._last_answer = state.get("last_answer") or ""
        self._study_media = [
            dict(item) for item in (state.get("study_media") or [])
            if item.get("path") and Path(item["path"]).exists()
        ]
        if self._study_goal or self._saved_study_name:
            # 恢复已有研读内容时不再显示「正在研读 / 研读记录」这一行
            self.goal_label.hide()
        else:
            self.goal_label.setText(self.HELP_TEXT)
            self.goal_label.show()
        self._render_explanation(scroll="top")
        self._render_qa()
        self._render_media_results()
        self.explain_status.setText(
            "已恢复上次研读" if self._has_study_content() else "等待研读请求")
        current_file = state.get("current_file") or ""
        if current_file and Path(current_file).is_file():
            try:
                self.load_file(Path(current_file),
                               int(state.get("current_line") or 1),
                               None, remember=False)
            except (OSError, ValueError):
                pass
        self._update_study_buttons()
        self._update_qa_buttons()

    def _apply_snapshot(self, snapshot, push_current=True):
        if not isinstance(snapshot, dict):
            return
        if push_current and self._has_study_content():
            self._study_back_stack.append(self._snapshot())
        record_path = snapshot.get("_record_path")
        if record_path:
            self._saved_study_path = Path(record_path)
        self._saved_study_name = snapshot.get("name") or ""
        self._study_goal = snapshot.get("goal") or ""
        self._explanation_markdown = snapshot.get("explanation") or ""
        self._qa_messages = []
        self._qa_next_id = 1
        for item in snapshot.get("qa") or []:
            self._qa_messages.append({
                "id": self._qa_next_id,
                "role": item.get("role", "assistant"),
                "text": item.get("text", ""),
                "merged": bool(item.get("merged")),
                "streaming": False,
            })
            self._qa_next_id += 1
        self._qa_answer_id = None
        self._study_media = [
            dict(item) for item in (snapshot.get("media") or [])
            if item.get("path") and Path(item["path"]).exists()
        ]
        # 载入记录后同样不显示「研读记录：…」这一行
        self.goal_label.hide()
        self._render_explanation(scroll="top")
        self._render_qa()
        self._render_media_results()
        self.explain_status.setText("研读记录已载入")
        current_file = snapshot.get("current_file") or ""
        line = int(snapshot.get("current_line") or 1)
        if current_file and Path(current_file).is_file():
            self.load_file(Path(current_file), line, line, remember=False)
        self._update_study_buttons()

    def _study_records(self):
        records = []
        if not STUDY_DIR.is_dir():
            return records
        for path in sorted(STUDY_DIR.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            name = path.stem
            saved_at = ""
            root = ""
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                name = data.get("name") or name
                saved_at = str(data.get("saved_at") or "")
                root = str(data.get("root") or "")
            except (OSError, ValueError):
                continue
            records.append({"path": path, "name": name,
                            "saved_at": saved_at, "root": root})
        return records

    def _schedule_record_save(self):
        """载入过研读记录时，内容改动后自动同步回原记录文件。"""
        if self._saved_study_path is None:
            return
        timer = getattr(self, "_record_timer", None)
        if timer is not None:
            timer.start()

    def _write_study_record(self):
        path = self._saved_study_path
        if path is None:
            return
        data = self._snapshot()
        try:
            Path(path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            self.explain_status.setText(f"研读记录同步失败：{exc}")
            return
        self.explain_status.setText(f"研读记录已同步：{Path(path).name}")

    def _save_study(self):
        if not self._has_study_content():
            QMessageBox.information(
                self, "保存研读", "当前还没有可保存的研读内容。")
            return
        default_name = (self._saved_study_name or self._study_goal
                        or f"研读 {datetime.now():%m-%d %H:%M}")
        name, ok = QInputDialog.getText(
            self, "保存研读", "研读记录名称：", text=default_name)
        if not ok:
            return
        name = (name or default_name).strip() or default_name
        safe = re.sub(r"[^\w一-鿿.-]+", "-", name).strip(".-") or "study"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        STUDY_DIR.mkdir(parents=True, exist_ok=True)
        path = STUDY_DIR / f"{stamp}-{safe[:60]}.json"
        data = self._snapshot()
        data["name"] = name
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "保存研读", f"保存失败：{exc}")
            return
        self._saved_study_name = name
        self._saved_study_path = path
        self.explain_status.setText(f"研读已保存：{path.name}")
        self.status_note(f"研读已保存到 {path}")

    def _show_study_menu(self):
        records = self._study_records()
        menu = QMenu(self)
        act_save = menu.addAction("保存当前研读…")
        menu.addSeparator()
        actions = {}
        if records:
            for record in records[:20]:
                label = record["name"]
                if record["saved_at"]:
                    label += f"  ·  {record['saved_at'][:16].replace('T', ' ')}"
                action = menu.addAction(label)
                actions[action] = record
        else:
            menu.addAction("（还没有保存的研读）").setEnabled(False)
        menu.addSeparator()
        act_open = menu.addAction("打开研读记录目录")
        chosen = menu.exec_(
            self.load_study_btn.mapToGlobal(self.load_study_btn.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen is act_save:
            self._save_study()
        elif chosen is act_open:
            STUDY_DIR.mkdir(parents=True, exist_ok=True)
            self._open_study_dir()
        elif chosen in actions:
            self._load_study_path(actions[chosen]["path"])

    def _open_study_dir(self):
        win_compat.open_path(STUDY_DIR)

    def _load_study_path(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "载入研读", f"无法读取记录：{exc}")
            return
        data["_record_path"] = str(path)
        root = str(data.get("root") or "")
        if root and root != self.root:
            # 记录属于别的项目：交给主窗口切到那个项目自己的研读页再应用，
            # 不要污染当前项目（以及它可能已弹出的独立窗口）。
            self.load_record_requested.emit(data)
            return
        self._saved_study_path = Path(path)
        self._apply_snapshot(data)
        self._update_study_buttons()

    def _restore_previous_study(self):
        if not self._study_back_stack:
            return
        # 真正的“后退”：弹出上一份快照后不再把当前内容压回栈顶，
        # 否则会在两个页面之间来回互换、永远退不出去。
        snapshot = self._study_back_stack.pop()
        self._apply_snapshot(snapshot, push_current=False)
        self.explain_status.setText("已返回刚才的研读")
        self._update_study_buttons()

    def _update_study_buttons(self):
        self.back_study_btn.setEnabled(bool(self._study_back_stack))

    def clear_all(self):
        if self._explanation_markdown.strip() or self._qa_messages:
            self._study_back_stack.append(self._snapshot())
            del self._study_back_stack[:-15]
        self._explanation_markdown = ""
        self.explain_view.clear()
        self._qa_messages = []
        self._qa_next_id = 1
        self._qa_answer_id = None
        self.qa_view.clear()
        self._last_answer = ""
        self._pending_quote = ""
        self._study_goal = ""
        self._saved_study_name = ""
        # 清空页面视为开始新的研读，不把已保存的记录覆盖成空内容。
        self._saved_study_path = None
        timer = getattr(self, "_record_timer", None)
        if timer is not None:
            timer.stop()
        self._study_media = []
        self.goal_label.setText(self.HELP_TEXT)
        self.goal_label.show()
        self._render_media_results()
        self.qa_quote_hint.setText("未选中文字")
        self.explain_status.setText("等待研读请求")
        self._update_study_buttons()

    # ------------------------------------------------------------ media --
    def _render_media_results(self):
        self._schedule_record_save()
        while self.study_media_layout.count():
            item = self.study_media_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for item in self._study_media:
            widget = MediaResultWidget(
                item.get("path", ""), item.get("kind", "image"),
                item.get("caption", ""), self.study_media_container)
            self.study_media_layout.addWidget(widget)
        self.study_media_container.setVisible(bool(self._study_media))

    def _media_prompt_default(self):
        source = self._selection_source()
        selected = ""
        if source is not None:
            selected = source.textCursor().selectedText().replace(" ", "\n").strip()
        if selected:
            return selected[:1200]
        if self._study_goal:
            return self._study_goal
        plain = self.explain_view.toPlainText().strip()
        if plain:
            return plain[:1200]
        return ""

    def generate_media(self, kind="image", prompt=None):
        if not self.root:
            QMessageBox.information(self, "生成媒体", "请先选择一个项目目录。")
            return
        if self._media_busy:
            self.status_note("正在生成，请稍候…")
            return
        dialog = MediaPromptDialog(
            kind, prompt if prompt is not None else self._media_prompt_default(),
            parent=self)
        if dialog.exec_() != QDialog.Accepted:
            return
        values = dialog.values()
        self._start_media(kind, values)

    def _start_media(self, kind, values):
        backend = values.get("video_backend") or ""
        if kind == "video" and not backend:
            backend = ("siliconflow" if read_secret_key("SILICONFLOW_API_KEY")
                       else "agnes")
        if kind == "video" and backend == "siliconflow":
            script = BASE_DIR / "siliconflow_video.py"
            provider_text = "SiliconFlow"
        else:
            script_name = ("generate.py" if kind == "image"
                           else "generate_video.py")
            script = find_media_script(script_name)
            provider_text = "media-tools"
        out_dir = Path(self.root) / ".claude-console" / "generated"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "生成媒体", f"无法创建输出目录：{exc}")
            return
        suffix = ".png" if kind == "image" else ".mp4"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = out_dir / f"{kind}-{stamp}{suffix}"
        if kind == "image":
            args = [values["prompt"], str(output), values.get("size") or "1:1"]
        elif backend == "siliconflow":
            args = [values["prompt"], str(output),
                    "--seconds", str(values.get("seconds") or 5),
                    "--resolution", values.get("video_size") or "768p",
                    "--ratio", values.get("ratio") or "16:9"]
        else:
            args = [values["prompt"], str(output),
                    "--seconds", str(values.get("seconds") or 5),
                    "--size", values.get("video_size") or "720P",
                    "--ratio", values.get("ratio") or "16:9"]
        if script is None:
            QMessageBox.information(
                self, "缺少生成脚本",
                "没有找到 media-tools 技能。\n\n"
                "请到「技能与插件 → 技能库 → 从 GitHub 安装」安装：\n"
                "147-sys/dsh-media-skills")
            return
        progress = TaskProgressDialog(
            "生成图片" if kind == "image" else "生成视频",
            f"正在调用 {provider_text} 生成，请稍候…\n"
            + ("图片通常几秒到一分钟。" if kind == "image"
               else "视频通常需要几分钟。"), self)
        progress.set_busy("正在连接服务商…")
        progress.show()
        worker = MediaWorker(script, args, output, cwd=self.root, parent=self)
        self._media_worker = worker
        self._media_dialog = progress
        self._media_busy = True
        self._update_media_buttons()
        worker.log.connect(progress.set_busy)
        progress.canceled.connect(worker.cancel)

        def done(path):
            self._media_worker = None
            self._media_busy = False
            self._update_media_buttons()
            progress.mark_done()
            self._study_media.append({
                "path": path,
                "kind": kind,
                "caption": "研读配图" if kind == "image" else "研读视频",
            })
            self._render_media_results()
            self.explain_status.setText(
                "配图已生成" if kind == "image" else "视频已生成")
            QTimer.singleShot(900, progress.close)

        def failed(message):
            cancelled = worker._cancelled
            self._media_worker = None
            self._media_busy = False
            self._update_media_buttons()
            progress.close()
            if cancelled:
                self.status_note("已取消生成")
                return
            QMessageBox.warning(self, "生成失败", message)

        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _update_media_buttons(self):
        for button in (getattr(self, "study_image_btn", None),
                       getattr(self, "study_video_btn", None)):
            if button is not None:
                button.setEnabled(not self._media_busy)

    # ------------------------------------------------------------- QA chat --
    def _ensure_worker(self):
        if self._worker is None:
            self._worker = ClaudeWorker(self)
            self._worker.text_delta.connect(self._on_qa_delta)
            self._worker.assistant.connect(self._on_qa_assistant)
            self._worker.result.connect(self._on_qa_result)
            self._worker.error.connect(self._on_qa_error)
            self._worker.busy_changed.connect(self._on_qa_busy)
            self._worker.permission_asked.connect(self._on_qa_permission)
            self._worker.start()
        config = (self.root, self.permission_mode, self.model, self.effort)
        if self._worker_cfg != config:
            self._worker.configure(
                self.root, self.permission_mode,
                model=self.model, effort=self.effort)
            self._worker_cfg = config

    def _update_qa_buttons(self):
        ready = bool(self.root) and SDK_AVAILABLE
        self.qa_send_btn.setEnabled(ready and not self._qa_busy)
        self.ask_btn.setEnabled(bool(self.root))
        self.qa_status.setText("Claude 正在回答…" if self._qa_busy else "")

    def _remember_quote_source(self, widget):
        if widget.textCursor().hasSelection():
            self._quote_source = widget

    def _entry_for_view(self, widget):
        return next((e for e in self._code_tabs if e["view"] is widget), None)

    def _cursor_line_range(self, widget):
        cursor = widget.textCursor()
        document = widget.document()
        start = document.findBlock(cursor.selectionStart()).blockNumber() + 1
        last_pos = max(cursor.selectionStart(), cursor.selectionEnd() - 1)
        end = document.findBlock(last_pos).blockNumber() + 1
        return start, max(start, end)

    def _selection_source(self):
        """优先用最近一次产生选区的控件，其次当前代码页，最后解释栏。"""
        widget = self._quote_source
        if widget is not None and widget.textCursor().hasSelection():
            return widget
        active = self._active_entry()
        candidates = [active["view"]] if active is not None else []
        candidates.append(self.explain_view)
        for candidate in candidates:
            if candidate.textCursor().hasSelection():
                return candidate
        return None

    def _quote_selection(self):
        self._show_qa_window()
        source = self._selection_source()
        selected = source.textCursor().selectedText() if source else ""
        selected = selected.replace(" ", "\n").strip()
        if not selected:
            self.qa_quote_hint.setText("请先在解释栏或代码区选中一段内容")
            return
        self._pending_quote = selected
        shown = selected if len(selected) <= 40 else selected[:40] + "…"
        entry = self._entry_for_view(source)
        if entry is not None:
            start, end = self._cursor_line_range(source)
            where = f"{self._visible_name(entry['path'])}:{start}-{end}"
            header = f"关于这段代码（{where}）："
            self.qa_quote_hint.setText(f"已引用代码 {where}")
        else:
            header = "关于这段解释："
            self.qa_quote_hint.setText(f"已引用解释：{shown}")
        quote_lines = "\n".join(
            "> " + line for line in selected.splitlines() if line.strip())
        self.qa_input.setPlainText(f"{header}\n{quote_lines}\n\n")
        cursor = self.qa_input.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.qa_input.setTextCursor(cursor)
        self.qa_input.setFocus()

    def _send_question(self):
        text = self.qa_input.toPlainText().strip()
        if not text:
            return
        if not self.root:
            QMessageBox.information(self, "代码研读", "请先在左侧选择一个项目目录。")
            return
        if not SDK_AVAILABLE:
            QMessageBox.warning(
                self, "缺少对话组件",
                "未安装 Claude Agent SDK，无法进行问答。\n\n"
                f"{SDK_IMPORT_ERROR}")
            return
        if self._qa_busy:
            return
        self._ensure_worker()
        quote = self._pending_quote
        self._pending_quote = ""
        self.qa_quote_hint.setText("未选中文字")
        self.qa_input.clear()

        self._last_question = text
        self.qa_status.setText("")
        user_id = self._qa_next_id
        self._qa_next_id += 1
        answer_id = self._qa_next_id
        self._qa_next_id += 1
        self._qa_messages.append({
            "id": user_id, "role": "user", "text": text,
            "merged": False, "streaming": False})
        self._qa_messages.append({
            "id": answer_id, "role": "assistant",
            "text": "_正在回答…_", "merged": False, "streaming": True})
        self._qa_answer_id = answer_id
        self._qa_live = ""
        self._render_qa()
        self._qa_busy = True
        self._update_qa_buttons()

        explanation = self.explain_view.toPlainText()
        if len(explanation) > 9000:
            explanation = explanation[:9000] + "\n…（前面内容已截断）"
        code_map = project_code_map(self.root, limit_chars=5000)
        parts = [
            "【代码研读 · 局部问答】",
            f"项目根目录：{self.root}",
            "",
            "当前解释栏内容（只读上下文，用来理解用户正在看什么）：",
            "<<<",
            explanation or "（空）",
            ">>>",
        ]
        if code_map:
            parts += [
                "",
                "项目符号索引（真实文件与行号，可直接引用）：",
                "<<<",
                code_map,
                ">>>",
            ]
        if quote:
            parts += ["", "用户选中的内容（解释或代码）：", "<<<", quote, ">>>"]
        parts += [
            "",
            "用户的问题：",
            text,
            "",
            "请直接、简洁地回答这个局部疑问，用中文和 Markdown。",
            "如果需要指向源码，请使用 "
            "[[code:相对路径:起行-止行|关键函数或类名]] 的格式"
            "（路径相对项目根目录，行号必须来自实际文件，不要估算），"
            "不要大段粘贴代码。",
        ]
        self._worker.send("\n".join(parts))

    # ----------------------------------------------------- QA worker events --
    def _on_qa_delta(self, chunk):
        self._qa_live += chunk
        if not self._qa_timer.isActive():
            self._qa_timer.start()

    def _qa_message(self, qa_id):
        return next((m for m in self._qa_messages if m.get("id") == qa_id),
                    None)

    def _qa_index(self, qa_id):
        return next((i for i, m in enumerate(self._qa_messages)
                     if m.get("id") == qa_id), -1)

    def _flush_qa_live(self):
        message = self._qa_message(self._qa_answer_id)
        if message is None:
            self._qa_timer.stop()
            return
        message["text"] = self._qa_live + ("\n\n▍" if self._qa_live else "")
        message["streaming"] = True
        self._render_qa()
        if not self._qa_live:
            self._qa_timer.stop()

    def _on_qa_assistant(self, text, _tools):
        self._qa_timer.stop()
        segment = (text or "").strip()
        # 一轮回答里可能有多个 AssistantMessage（工具调用前后各有一段），
        # 流式累计的 _qa_live 才是整轮正文；只取 text 会丢掉前一段。
        answer = self._qa_live
        if segment and segment not in answer:
            answer = f"{answer.rstrip()}\n\n{segment}" if answer else segment
        if not answer:
            return
        message = self._qa_message(self._qa_answer_id)
        if message is not None:
            message["text"] = answer
            message["streaming"] = True
        self._last_answer = answer
        self._render_qa()

    def _on_qa_result(self, info):
        self._qa_timer.stop()
        message = self._qa_message(self._qa_answer_id)
        # 结果到达时以整轮累计正文为准，避免最后一段覆盖前面的内容。
        answer = self._qa_live or self._last_answer
        if answer:
            self._last_answer = answer
            if message is not None:
                message["text"] = answer
                message["streaming"] = False
            self._render_qa()
        elif message is not None:
            message["streaming"] = False
        self._qa_live = ""
        self._qa_busy = False
        self._update_qa_buttons()
        if info.get("is_error") and not self._last_answer:
            self._qa_messages.append({
                "id": self._qa_next_id, "role": "error",
                "text": info.get("result") or "Claude 返回了错误",
                "merged": False, "streaming": False})
            self._qa_next_id += 1
            self._render_qa()
            return
        self._render_qa()

    def _on_qa_error(self, message):
        self._qa_timer.stop()
        self._qa_busy = False
        self._qa_messages.append({
            "id": self._qa_next_id, "role": "error",
            "text": message, "merged": False, "streaming": False})
        self._qa_next_id += 1
        self._render_qa()
        self._update_qa_buttons()

    def _on_qa_busy(self, busy):
        self._qa_busy = busy
        self._update_qa_buttons()

    def _on_qa_permission(self, request_id, tool_name, input_text):
        dialog = PermissionDialog(tool_name, input_text, self)
        allowed = dialog.exec_() == QDialog.Accepted
        self._worker.respond_permission(request_id, allowed)

    def _merge_qa_answer(self, qa_id):
        index = self._qa_index(qa_id)
        if index < 0:
            return
        message = self._qa_messages[index]
        if message.get("role") != "assistant":
            return
        if message.get("merged"):
            self.qa_status.setText("这条回答已经在解释栏里了")
            return
        answer = str(message.get("text") or "").strip()
        if not answer:
            return
        question = ""
        for previous in reversed(self._qa_messages[:index]):
            if previous.get("role") == "user":
                question = str(previous.get("text") or "")
                break
        question_text = question.strip().replace("\n", "\n> ")
        section = (
            "---\n\n### 补充解释（来自解释栏问答）\n\n"
            f"> 问：{question_text}\n\n{answer}\n")
        self.append_explanation(section)
        message["merged"] = True
        self._render_qa()
        self.explain_status.setText("已把这条回答补充进解释栏")

    def _delete_qa_answer(self, qa_id):
        index = self._qa_index(qa_id)
        if index < 0:
            return
        message = self._qa_messages[index]
        if message.get("role") != "assistant":
            return
        # 连同紧挨着的问题一起删除，避免留下孤儿问题
        if index > 0 and self._qa_messages[index - 1].get("role") == "user":
            del self._qa_messages[index - 1:index + 1]
        else:
            del self._qa_messages[index]
        self._render_qa()
        self.qa_status.setText("已删除这条问答")

    def _render_qa(self):
        self._schedule_record_save()
        if not self._qa_messages:
            self.qa_view.clear()
            return
        blocks = []
        for message in visible_qa_messages(self._qa_messages):
            role = message.get("role")
            prefix = {"user": "**你**", "assistant": "**Claude**",
                      "error": "**出错**"}.get(role, "**Claude**")
            text = str(message.get("text") or "")
            if role == "assistant" and not message.get("streaming"):
                qa_id = message.get("id")
                if message.get("merged"):
                    actions = (f"✅ 已添加到解释栏 · "
                               f"**[删除](qa://delete/{qa_id})**")
                else:
                    actions = (f"**[➕ 添加到解释栏](qa://merge/{qa_id})** · "
                               f"**[🗑 删除](qa://delete/{qa_id})**")
                text = text.rstrip() + "\n\n" + actions
            blocks.append(f"{prefix}\n\n{text}")
        rendered = study_refs_to_markdown("\n\n---\n\n".join(blocks))
        self.qa_view.setMarkdown(rendered)
        bar = self.qa_view.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    # ---------------------------------------------------------- code view --
    def _on_anchor_clicked(self, url):
        href = url.toString()
        if href.startswith("qa://"):
            match = re.match(r"qa://(merge|delete)/(\d+)", href)
            if match:
                qa_id = int(match.group(2))
                if match.group(1) == "merge":
                    self._merge_qa_answer(qa_id)
                else:
                    self._delete_qa_answer(qa_id)
            return
        if href.startswith("file://"):
            QDesktopServices.openUrl(url)
            return
        if href.startswith(("px4code://", "code://")):
            # 问答窗是独立顶层窗口，点链接时先把研读页/独立窗口带到前面
            self.reveal_requested.emit()
            self.open_ref(href)
        else:
            QDesktopServices.openUrl(url)

    def open_ref(self, href):
        body = href
        for prefix in ("px4code://", "code://"):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        rel, _sep, fragment = body.partition("#")
        rel, _qsep, query = rel.partition("?")
        rel = unquote(rel).lstrip("/")
        symbol = ""
        try:
            symbol = (parse_qs(query).get("sym") or [""])[0].strip()
        except ValueError:
            symbol = ""
        match = re.match(r"L?(\d+)(?:-L?(\d+))?", fragment or "")
        start = int(match.group(1)) if match else 1
        end = int(match.group(2)) if match and match.group(2) else start
        path = self._resolve_path(rel, symbol=symbol)
        if path is None:
            self.file_label.setText(f"找不到文件：{rel}")
            self.status_note(f"找不到文件：{rel}（链接里带的是符号名时，"
                             "可先确认文件是否被重命名/移动）")
            return
        self.load_file(path, start, end, symbol=symbol)

    @staticmethod
    def _block_end(lines, index):
        """从定义行出发，估算这个符号占用的最后一行（1 基）。"""
        if not 0 <= index < len(lines):
            return index + 1
        line = lines[index]
        if "{" in line:
            depth = 0
            for i in range(index, min(len(lines), index + 800)):
                depth += lines[i].count("{") - lines[i].count("}")
                if depth <= 0 and i > index:
                    return i + 1
            return min(len(lines), index + 800)
        indent = len(line) - len(line.lstrip())
        last = index
        for i in range(index + 1, min(len(lines), index + 400)):
            stripped = lines[i].strip()
            if not stripped:
                continue
            current = len(lines[i]) - len(lines[i].lstrip())
            if current <= indent:
                break
            last = i
        return last + 1 if last > index else index + 1

    @classmethod
    def _symbol_span(cls, lines, symbol):
        """在文件里找符号定义，返回 (起行, 止行)；找不到返回 None。"""
        bare = symbol.strip().rstrip("()").strip()
        if not bare:
            return None
        token = re.split(r"[.:]+", bare)[-1] or bare
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", token):
            return None
        pattern = re.compile(
            r"(?<![\w$])" + re.escape(token) + r"(?![\w$])")
        definition = re.compile(
            r"(?:def|class|struct|enum|function|fn|func|interface|trait|"
            r"protocol|type|const|let|var)\s+" + re.escape(token) + r"\b")
        fallback = None
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*", "/*")):
                continue
            if fallback is None:
                fallback = index
            if definition.search(line) or re.match(
                    r"^[^=;]*\b" + re.escape(token) + r"\s*\(", stripped):
                return index + 1, cls._block_end(lines, index)
        if fallback is not None:
            return fallback + 1, cls._block_end(lines, fallback)
        return None

    def _active_entry(self):
        widget = self.code_tabs.currentWidget()
        for entry in self._code_tabs:
            if entry["view"] is widget:
                return entry
        return None

    def _remove_placeholder(self):
        if not self._placeholder_visible:
            return
        index = self.code_tabs.indexOf(self.code_placeholder)
        if index >= 0:
            self.code_tabs.removeTab(index)
        self._placeholder_visible = False

    def _show_placeholder(self):
        if self._placeholder_visible:
            return
        index = self.code_tabs.addTab(self.code_placeholder, "未打开文件")
        self.code_tabs.setCurrentIndex(index)
        self._placeholder_visible = True

    def _visible_name(self, path):
        try:
            return str(path.relative_to(Path(self.root).resolve()))
        except (ValueError, OSError):
            return str(path)

    def _remember_location(self, path, start, end):
        entry = (str(path), int(start), int(end))
        if (self._history_pos >= 0
                and self._history[self._history_pos] == entry):
            self._update_history_buttons()
            return
        del self._history[self._history_pos + 1:]
        self._history.append(entry)
        self._history_pos = len(self._history) - 1
        self._update_history_buttons()

    def _update_history_buttons(self):
        self.nav_back_btn.setEnabled(self._history_pos > 0)
        self.nav_forward_btn.setEnabled(
            0 <= self._history_pos < len(self._history) - 1)

    def _go_back(self):
        self._navigate_history(self._history_pos - 1)

    def _go_forward(self):
        self._navigate_history(self._history_pos + 1)

    def _navigate_history(self, position):
        if not 0 <= position < len(self._history):
            return
        self._history_pos = position
        path, start, end = self._history[position]
        self.load_file(Path(path), start, end, remember=False)
        self._update_history_buttons()

    def _reveal_in_tree(self, path):
        if self._fs_model is None or self._tree_proxy is None:
            return
        index = self._fs_model.index(str(path))
        if not index.isValid():
            return
        proxy_index = self._tree_proxy.mapFromSource(index)
        if not proxy_index.isValid():
            return
        parent = proxy_index.parent()
        while parent.isValid():
            self.explorer.expand(parent)
            parent = parent.parent()
        self.explorer.setCurrentIndex(proxy_index)
        self.explorer.scrollTo(proxy_index)

    def _open_tree_index(self, proxy_index):
        if self._fs_model is None or self._tree_proxy is None:
            return
        source = self._tree_proxy.mapToSource(proxy_index)
        if not source.isValid() or self._fs_model.isDir(source):
            return
        path = Path(self._fs_model.filePath(source))
        self.load_file(path, 1, 1)

    def _set_explorer_root(self, root):
        if not root:
            return
        if self._fs_model is None:
            self._fs_model = QFileSystemModel(self)
            self._fs_model.setFilter(
                QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot)
            self._fs_model.setNameFilters(list(self.SOURCE_SUFFIXES))
            self._fs_model.setNameFilterDisables(False)
            self._tree_proxy = SourceTreeProxy(self)
            self._tree_proxy.setSourceModel(self._fs_model)
            self.explorer.setModel(self._tree_proxy)
            for column in range(1, 4):
                self.explorer.setColumnHidden(column, True)
        self._fs_model.setRootPath(root)
        source_root = self._fs_model.index(root)
        if source_root.isValid():
            proxy_root = self._tree_proxy.mapFromSource(source_root)
            self.explorer.setRootIndex(proxy_root)
            self.explorer.setCurrentIndex(proxy_root)
            self.explorer.expand(proxy_root)

    def _toggle_explorer(self, checked):
        self.explorer.setVisible(bool(checked))

    def _on_code_tab_changed(self, _index):
        entry = self._active_entry()
        if entry is None:
            if self._placeholder_visible:
                self.file_label.setText("尚未选择代码位置")
            return
        self._current_file = entry["path"]
        self._current_line = entry["line"]
        self._refresh_file_label()

    def _close_code_tab(self, index):
        widget = self.code_tabs.widget(index)
        if widget is self.code_placeholder:
            return
        entry = next((e for e in self._code_tabs if e["view"] is widget), None)
        if entry is not None:
            self._code_tabs.remove(entry)
            if self._quote_source is entry["view"]:
                self._quote_source = None
        self.code_tabs.removeTab(index)
        if widget is not None:
            widget.deleteLater()
        if not self._code_tabs:
            self._current_file = None
            self._current_line = 1
            self._show_placeholder()
            self.file_label.setText("尚未选择代码位置")
        else:
            self._on_code_tab_changed(self.code_tabs.currentIndex())

    def _close_all_code_tabs(self):
        self.code_tabs.blockSignals(True)
        for entry in list(self._code_tabs):
            index = self.code_tabs.indexOf(entry["view"])
            if index >= 0:
                self.code_tabs.removeTab(index)
            entry["view"].deleteLater()
        self._code_tabs = []
        self._quote_source = None
        self.code_tabs.blockSignals(False)
        self._show_placeholder()

    def _refresh_file_label(self):
        if self._current_file is None:
            self.file_label.setText("尚未选择代码位置")
            return
        entry = self._active_entry()
        end = entry["end"] if entry is not None else self._current_line
        start = self._current_line
        line_text = (f"第 {start} 行" if end == start
                     else f"第 {start}–{end} 行")
        self.file_label.setText(
            f"{self._visible_name(self._current_file)} · {line_text}")

    def _resolve_path(self, rel, symbol=""):
        if not self.root:
            return None
        rel = (rel or "").strip().replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        if not rel:
            return None
        root = Path(self.root).resolve()
        raw = Path(rel)
        if raw.is_absolute():
            try:
                resolved = raw.resolve()
            except OSError:
                return None
            return resolved if resolved.is_file() else None

        parts = Path(rel).parts
        if not parts:
            return None
        # 1) 直接命中；模型可能把项目根目录名也写进了相对路径，逐层剥掉再试
        for i in range(len(parts)):
            try:
                resolved = (root / Path(*parts[i:])).resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return resolved

        # 2) 后缀匹配：模型可能省略中间目录，或路径层级写得不准。
        #    同名文件常常分布在多个模块里，不能随便取第一个。
        wanted = tuple(parts)
        matches = []
        scanned = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in SourceTreeProxy.IGNORED_DIRS
                    and not d.startswith("."))
                for name in filenames:
                    scanned += 1
                    try:
                        relpath = (Path(dirpath) / name).relative_to(root)
                    except ValueError:
                        continue
                    rel_parts = relpath.parts
                    if (len(rel_parts) >= len(wanted)
                            and rel_parts[-len(wanted):] == wanted):
                        matches.append(relpath)
                if scanned >= 60000:
                    break
        except OSError:
            pass
        if not matches:
            return None

        # 3) 有符号名时优先选真正包含该符号的文件；否则取层级最浅的
        def score(relpath):
            has_symbol = 0
            if symbol:
                try:
                    text = (root / relpath).read_text(
                        encoding="utf-8", errors="ignore")[:400000]
                except OSError:
                    text = ""
                if text and self._symbol_span(text.splitlines(), symbol):
                    has_symbol = 1
            return (has_symbol, -len(relpath.parts), -len(str(relpath)))

        matches.sort(key=score, reverse=True)
        return (root / matches[0]).resolve()

    @classmethod
    def _refine_range(cls, lines, start, end, symbol=""):
        """把模型给的行号校正到真实文件里的符号块。

        行号来自模型估算时容易偏；如果链接带了函数/类名，就以符号定义为锚点。
        """
        count = len(lines)
        if count <= 0:
            return 1, 1
        start = min(max(1, int(start or 1)), count)
        end = min(max(start, int(end or start)), count)
        if not symbol:
            return start, end
        span = cls._symbol_span(lines, symbol)
        if span is None:
            return start, end
        hit_start, hit_end = span
        span_len = hit_end - hit_start + 1
        cited_len = end - start + 1
        # 引用范围包含符号定义且没有离谱地大，就尊重原范围；
        # 否则用符号块纠正，避免“跳到了文件里的别处”。
        if start <= hit_start <= end and cited_len <= max(span_len * 3, 120):
            return start, end
        return hit_start, hit_end

    def load_file(self, path, start=1, end=None, remember=True, symbol=""):
        path = Path(path)
        start = max(1, int(start or 1))
        end = max(start, int(end or start))
        entry = next((e for e in self._code_tabs if e["path"] == path), None)
        if entry is None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self.file_label.setText(f"无法读取：{path.name}")
                self.status_note(f"无法读取：{exc}")
                return
            if "\x00" in text[:2000]:
                self.file_label.setText(f"{path.name} · 二进制文件")
                self.status_note("这是二进制文件，未显示内容。")
                return
            start, end = self._refine_range(
                text.splitlines(), start, end, symbol)
            view = StudyCodeView()
            view.selectionChanged.connect(
                lambda v=view: self._remember_quote_source(v))
            view.setPlainText(text)
            entry = {"path": path, "view": view, "line": start, "end": end}
            self._code_tabs.append(entry)
            self._remove_placeholder()
            index = self.code_tabs.addTab(view, path.name)
            self.code_tabs.setTabToolTip(index, str(path))
            self.code_tabs.setCurrentIndex(index)
        else:
            start, end = self._refine_range(
                entry["view"].toPlainText().splitlines(),
                start, end, symbol)
            index = self.code_tabs.indexOf(entry["view"])
            if index >= 0:
                self.code_tabs.setCurrentIndex(index)
            entry["line"] = start
            entry["end"] = end
        entry["view"].goto_line(start, end)
        self._current_file = path
        self._current_line = start
        self._refresh_file_label()
        self._reveal_in_tree(path)
        if remember:
            self._remember_location(path, start, end)

    def _reload_current(self):
        entry = self._active_entry()
        if entry is None:
            self.status_note("还没有打开文件")
            return
        path = entry["path"]
        start, end = entry["line"], entry["end"]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.status_note(f"无法读取：{exc}")
            return
        entry["view"].setPlainText(text)
        entry["view"].goto_line(start, end)
        self.status_note(f"已重新加载：{path.name}")

    def _open_in_editor(self):
        entry = self._active_entry()
        if entry is None:
            self.status_note("还没有打开文件")
            return
        if not win_compat.open_editor(entry["path"], entry["line"]):
            self.status_note("未找到 VS Code 的 code 命令")

    def set_floating(self, floating):
        self._floating = bool(floating)
        self.float_btn.setText("收回页签" if self._floating else "独立窗口")
        self.float_btn.setToolTip(
            "把代码研读页收回主窗口页签" if self._floating
            else "把代码研读页放进独立窗口")

    def status_note(self, text):
        self.explain_status.setText(text)

    # ------------------------------------------------------------ teardown --
    def shutdown(self):
        self._qa_timer.stop()
        media_worker = getattr(self, "_media_worker", None)
        if media_worker is not None:
            media_worker.cancel()
            media_worker.wait(3000)
            self._media_worker = None
        window = getattr(self, "qa_window", None)
        if window is not None:
            window.close()
            window.deleteLater()
            self.qa_window = None
        if self._worker is not None:
            self._worker.shutdown()


# --------------------------------------------------------------------------- #
# 代码开发页：多文件夹、三种修改模式、我的改动检查、改前/改后确认
# --------------------------------------------------------------------------- #
DEV_MODES = (
    ("自动覆盖（不确认）", "all",
     "AI 对代码的修改直接覆盖原文件，不弹确认窗口，也不弹权限窗口；"
     "之后仍可用 ↶ 撤回"),
    ("每轮确认一次", "function",
     "每轮 AI 修改结束后统一确认一次，确认后覆盖本轮所有改动"),
    ("每次都确认", "each",
     "每个文件的修改都进入改前/改后预览，逐个确认后才覆盖"),
)
DEV_SOURCE_SUFFIXES = SOURCE_SUFFIXES | {
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".md",
    ".markdown", ".toml", ".ini", ".cfg", ".conf", ".env", ".gradle",
    ".cmake", ".txt", ".tex",
}
DEV_SKIP_DIRS = SourceTreeProxy.IGNORED_DIRS | {
    "vendor", "third_party", "thirdparty", "external", "deps", "dist",
    "out", "target", "obj", "bin", "site-packages", "Pods",
    "bower_components", "packages", "coverage", "logs", "tmp",
}
DEV_CONFIG_PATH = CONFIG_DIR / "dev_state.json"
DEV_SNAPSHOT_PATH = CONFIG_DIR / "dev_snapshot.json"


def dev_scan_files(roots, max_files=4000, max_file_size=1_000_000,
                   max_total=8_000_000):
    """扫描允许修改的文件夹，返回 {绝对路径: 文本}（只收源码文件）。"""
    files = {}
    total = 0
    for root in roots or []:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in DEV_SKIP_DIRS and not d.startswith("."))
                for name in sorted(filenames):
                    if Path(name).suffix.lower() not in DEV_SOURCE_SUFFIXES:
                        continue
                    path = Path(dirpath) / name
                    try:
                        if path.stat().st_size > max_file_size:
                            continue
                        text = path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    files[str(path)] = text
                    total += len(text)
                    if len(files) >= max_files or total >= max_total:
                        return files
        except OSError:
            continue
    return files


def dev_line_changes(before, after):
    """返回 (改前行号集合, 改后行号集合, opcodes)。"""
    old_lines = (before or "").splitlines()
    new_lines = (after or "").splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    before_lines, after_lines = set(), set()
    hunks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before_lines.update(range(i1 + 1, i2 + 1))
        after_lines.update(range(j1 + 1, j2 + 1))
        hunks.append((tag, i1, i2, j1, j2))
    return before_lines, after_lines, hunks


def dev_unified_diff(before, after, name="文件", limit=6000):
    lines = list(difflib.unified_diff(
        (before or "").splitlines(), (after or "").splitlines(),
        fromfile=f"{name}（改前）", tofile=f"{name}（改后）", lineterm="", n=3))
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n…（diff 已截断）"
    return text


class DevCodeView(StudyCodeView):
    """开发页代码视图：可编辑、按行高亮改动并在行号旁显示红色感叹号。"""

    def __init__(self, editable=False, parent=None):
        super().__init__(parent)
        self.setReadOnly(not editable)
        self.change_lines = set()
        self.change_color = "#332f9e63"
        self._editable = editable

    def _indent_unit(self):
        return "\t" if "\t" in self._current_indent() else "    "

    def _current_indent(self):
        block = self.textCursor().block()
        line = block.text()
        indent = re.match(r"[ \t]*", line).group(0)
        if indent or line.strip():
            return indent
        # 空行：沿用上一行的缩进，避免在代码块中间换行时突然顶格。
        previous = block.previous()
        if previous.isValid():
            return re.match(r"[ \t]*", previous.text()).group(0)
        return ""

    def keyPressEvent(self, event):
        """回车时延续当前行的缩进，并在 {([ 或 : 之后再多缩进一级。"""
        if (self._editable
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not self.textCursor().hasSelection()):
            cursor = self.textCursor()
            line = cursor.block().text()
            before = line[:cursor.positionInBlock()]
            indent = self._current_indent()
            stripped = before.rstrip()
            if stripped and stripped[-1] in "{([:":
                indent += self._indent_unit()
            super().keyPressEvent(event)
            if indent:
                self.textCursor().insertText(indent)
            return
        super().keyPressEvent(event)

    def line_number_area_width(self):
        return super().line_number_area_width() + 18

    def set_change_lines(self, lines, color=None):
        self.change_lines = {int(x) for x in (lines or [])}
        if color:
            self.change_color = color
        self.line_number_area.update()
        self._apply_change_highlights()

    def _apply_change_highlights(self):
        selections = []
        for line in sorted(self.change_lines):
            block = self.document().findBlockByNumber(line - 1)
            if not block.isValid():
                continue
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(block)
            selection.format.setBackground(QColor(self.change_color))
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selections.append(selection)
        self.setExtraSelections(selections)

    def paint_line_numbers(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(CUTE_THEME["gutter_bg"]))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                line = number + 1
                if line in self.change_lines:
                    painter.setFont(self.font())
                    painter.setPen(QColor("#d9457a"))
                    painter.drawText(2, int(top), 16, height,
                                     Qt.AlignLeft | Qt.AlignVCenter, "❗")
                painter.setFont(self.font())
                painter.setPen(QColor(CUTE_THEME["line_number"]))
                painter.drawText(
                    16, int(top), self.line_number_area.width() - 20,
                    height, Qt.AlignRight | Qt.AlignVCenter, str(line))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1


class DevDiffPanel(QWidget):
    """内嵌在代码开发页里的改前/改后预览，不再另开窗口。

    显示时右侧会暂时收起代码目录，把整块空间让给对比；
    左侧对话仍然保留，可以继续提出修改要求。
    """

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self._loading = False
        self._refreshing = False
        self._current_path = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("AI 修改预览")
        title.setObjectName("H2")
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(280)
        self.file_combo.currentIndexChanged.connect(self._on_file_changed)
        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        reload_btn = QPushButton("重新读取磁盘")
        reload_btn.setObjectName("Flat")
        reload_btn.setToolTip("丢弃右侧尚未确认的编辑，重新读取文件当前内容")
        reload_btn.clicked.connect(self._reload_disk)
        back_btn = QPushButton("返回代码")
        back_btn.setObjectName("Flat")
        back_btn.setToolTip("收起修改对比，回到代码目录和编辑器")
        back_btn.clicked.connect(self.page._hide_diff_view)
        top.addWidget(title)
        top.addWidget(self.file_combo, 1)
        top.addWidget(self.summary)
        top.addWidget(reload_btn)
        top.addWidget(back_btn)
        layout.addLayout(top)

        heads = QHBoxLayout()
        self.before_head = QLabel("修改前")
        self.before_head.setObjectName("H2")
        self.after_head = QLabel("修改后（可直接编辑）")
        self.after_head.setObjectName("H2")
        heads.addWidget(self.before_head, 1)
        heads.addWidget(self.after_head, 1)
        layout.addLayout(heads)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(6)
        self.before_view = DevCodeView(editable=False)
        self.after_view = DevCodeView(editable=True)
        self.before_view.set_change_lines([], "#33d9536f")
        self.after_view.set_change_lines([], "#332f9e63")
        splitter.addWidget(self.before_view)
        splitter.addWidget(self.after_view)
        splitter.setSizes([600, 600])
        layout.addWidget(splitter, 1)

        hint = QLabel(
            "红色 ❗ 标出改动行。可以直接在右侧修改 AI 写的代码，"
            "再点最下方「确认覆盖原代码」；也可以回到左侧对话继续提出修改要求。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        self.quote_btn = QPushButton("继续对话修改这段")
        self.quote_btn.setObjectName("Flat")
        self.quote_btn.setToolTip("把右侧选中的代码带入左侧对话，向 AI 提第二轮要求")
        self.quote_btn.clicked.connect(self._quote_after_selection)
        self.cancel_all_btn = QPushButton("全部取消")
        self.cancel_all_btn.setObjectName("Danger")
        self.cancel_all_btn.clicked.connect(self.page.cancel_all_changes)
        self.confirm_all_btn = QPushButton("全部确认")
        self.confirm_all_btn.setObjectName("Primary")
        self.confirm_all_btn.clicked.connect(self.page.confirm_all_changes)
        self.cancel_btn = QPushButton("取消并还原")
        self.cancel_btn.setObjectName("Danger")
        self.cancel_btn.clicked.connect(self.page.cancel_current_change)
        self.confirm_btn = QPushButton("确认覆盖原代码")
        self.confirm_btn.setObjectName("Primary")
        self.confirm_btn.clicked.connect(self.page.confirm_current_change)
        bottom.addWidget(self.status)
        bottom.addWidget(self.quote_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.cancel_all_btn)
        bottom.addWidget(self.confirm_all_btn)
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(self.confirm_btn)
        layout.addLayout(bottom)

    def refresh(self):
        pending = self.page._pending
        current = self.file_combo.currentData()
        self._refreshing = True
        self._loading = True
        self.file_combo.clear()
        for path in sorted(pending):
            self.file_combo.addItem(self.page._display_path(path), path)
        index = self.file_combo.findData(current)
        if index < 0:
            index = 0
        if self.file_combo.count():
            self.file_combo.setCurrentIndex(index)
        self._loading = False
        self._on_file_changed(self.file_combo.currentIndex())
        self._refreshing = False

    def _on_file_changed(self, _index):
        if self._loading:
            return
        if not self._refreshing and self._current_path is not None:
            entry = self.page._pending.get(self._current_path)
            if entry is not None:
                entry["after"] = self.after_view.toPlainText()
        path = self.file_combo.currentData()
        self._current_path = path
        entry = self.page._pending.get(path)
        if entry is None:
            self.before_view.clear()
            self.after_view.clear()
            return
        before = entry.get("original")
        before_text = before if isinstance(before, str) else ""
        after = entry.get("after", "")
        self.before_view.setPlainText(before_text)
        self.after_view.setPlainText(after)
        before_lines, after_lines, hunks = dev_line_changes(before_text, after)
        self.before_view.set_change_lines(before_lines, "#33d9536f")
        self.after_view.set_change_lines(after_lines, "#332f9e63")
        self.file_combo.setToolTip(path)
        self.summary.setText(
            f"{len(hunks)} 处改动 · 改前 {len(before_text.splitlines())} 行 / "
            f"改后 {len(after.splitlines())} 行")
        self.status.setText(path)
        count = len(self.page._pending)
        if self.page.mode == "function":
            # 每轮确认一次：只保留「全部确认 / 全部取消」，一次覆盖本轮全部改动。
            self.confirm_all_btn.setText("确认覆盖本轮全部")
            self.cancel_all_btn.setText("取消本轮全部")
            self.confirm_all_btn.setVisible(True)
            self.cancel_all_btn.setVisible(True)
            self.confirm_btn.setVisible(False)
            self.cancel_btn.setVisible(False)
        else:
            multiple = count > 1
            self.confirm_all_btn.setText("全部确认")
            self.cancel_all_btn.setText("全部取消")
            self.confirm_all_btn.setVisible(multiple)
            self.cancel_all_btn.setVisible(multiple)
            self.confirm_btn.setVisible(True)
            self.cancel_btn.setVisible(True)
        self.after_view.setFocus()

    def store_editor(self):
        path = self.file_combo.currentData()
        entry = self.page._pending.get(path)
        if entry is not None:
            entry["after"] = self.after_view.toPlainText()

    def _reload_disk(self):
        path = self.file_combo.currentData()
        entry = self.page._pending.get(path)
        if entry is None:
            return
        try:
            entry["after"] = Path(path).read_text(
                encoding="utf-8", errors="ignore")
        except OSError as exc:
            QMessageBox.warning(self, "重新读取", f"读取失败：{exc}")
            return
        self._on_file_changed(self.file_combo.currentIndex())

    def current_path(self):
        return self.file_combo.currentData()

    def select_file(self, path):
        """刷新后定位到指定文件（用于双击目录树里的待确认文件）。"""
        self.refresh()
        index = self.file_combo.findData(path)
        if index >= 0:
            self.file_combo.setCurrentIndex(index)

    def _quote_after_selection(self):
        cursor = self.after_view.textCursor()
        text = cursor.selectedText().replace(" ", "\n") if cursor.hasSelection() else ""
        entry_text = self.after_view.toPlainText()
        if not text:
            # 没选就引用所有改动行
            lines = entry_text.splitlines()
            changed = sorted(self.after_view.change_lines)
            if changed:
                text = "\n".join(lines[i - 1] for i in changed if 0 < i <= len(lines))
            else:
                text = entry_text
        path = self.file_combo.currentData() or ""
        self.page.quote_for_chat(path, text, pending=True)


class DevQaWindow(QMainWindow):
    """开发页悬浮问答窗：半透明、置顶，关闭后按键召回。"""

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("开发问答 · Claude 控制台")
        self.setWindowIcon(make_app_icon())
        self.setWindowOpacity(0.9)
        self.resize(560, 660)


class DevFloatWindow(QMainWindow):
    """把代码开发页放进独立窗口；关闭时回到主窗口页签。"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class DevFolderDialog(QDialog):
    """移除文件夹：打开已添加列表，多选后一次删除。"""

    def __init__(self, roots, parent=None):
        super().__init__(parent)
        self.setWindowTitle("移除文件夹")
        self.setWindowIcon(make_app_icon())
        self.resize(600, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        hint = QLabel("选择要移除的文件夹（可按住 Ctrl/Shift 多选）。"
                      "只从允许修改列表中移除，不会删除磁盘上的文件。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for root in roots or []:
            item = QListWidgetItem(f"📁 {Path(root).name}")
            item.setData(Qt.UserRole, root)
            item.setToolTip(root)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        layout.addWidget(self.list, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("Flat")
        cancel_btn.clicked.connect(self.reject)
        remove_btn = QPushButton("移除所选")
        remove_btn.setObjectName("Danger")
        remove_btn.clicked.connect(self.accept)
        bottom.addWidget(cancel_btn)
        bottom.addWidget(remove_btn)
        layout.addLayout(bottom)

    def selected_roots(self):
        return [item.data(Qt.UserRole) for item in self.list.selectedItems()]


class CodeDevPage(QWidget):
    """代码开发页：左侧对话，右侧文件与源码；AI 改动需确认后覆盖。"""

    float_requested = pyqtSignal()
    reveal_requested = pyqtSignal()

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.config = config if isinstance(config, dict) else {}
        # 允许修改的文件夹不跨进程恢复：每次重新打开程序都从空的文件夹列表开始，
        # 只有主动「载入开发」才带回保存记录里的文件夹。
        self.roots = []
        saved_mode = self.config.get("dev_mode") or "function"
        if saved_mode not in {m[1] for m in DEV_MODES}:
            saved_mode = "function"
        self.mode = saved_mode
        self.model = None
        self.effort = None
        self._worker = None
        self._worker_cfg = None
        self._busy = False
        self._messages = []  # [{"role","text"}]
        self._live = ""
        self._chat_dirty = False
        self._pending = {}  # {path: {"original","after"}}
        # 确认覆盖后的文件级撤回/前进记录：
        # 每项为 [(path, before_text_or_None, after_text), ...]
        self._file_undo = []
        self._file_redo = []
        self._snapshot = {}
        self._snapshot_roots = []
        self._turn_files = {}
        self._current_file = None
        self._perm_context = {}
        self._tree_items = {}
        self._diff_panel = None
        self._qa_window = None
        self._qa_view = None
        self._qa_input = None
        self._qa_send = None
        self._qa_status = None
        # 问答窗口使用自己的消息记录和后台会话，不与主 AI 对话同步。
        self._qa_messages = []
        self._qa_live = ""
        self._qa_dirty = False
        self._qa_busy = False
        self._qa_worker = None
        self._qa_worker_cfg = None
        self._review_pending = False
        self._floating = False
        self._saved_dev_name = ""
        # 当前载入/保存的开发记录文件；之后的修改会自动同步回这个文件。
        self._saved_dev_path = None
        # 右侧打开的文件页签：[{"path", "view"}]
        self._code_tabs = []
        self._placeholder_visible = True
        self._build_ui()
        self._load_state()
        self._refresh_roots_list()
        self._refresh_tree()
        self._update_buttons()

        self._chat_timer = QTimer(self)
        self._chat_timer.setInterval(80)
        self._chat_timer.timeout.connect(self._flush_chat)

        self._qa_timer = QTimer(self)
        self._qa_timer.setInterval(80)
        self._qa_timer.timeout.connect(self._flush_qa_chat)

        # 用户在当前文件里输入时，稍后刷新“我的改动”行标记
        self._mark_timer = QTimer(self)
        self._mark_timer.setSingleShot(True)
        self._mark_timer.setInterval(400)
        self._mark_timer.timeout.connect(self._refresh_viewer_marks)

        # 代码编辑自动保存：停止输入约 0.7 秒后写回磁盘。
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(700)
        self._autosave_timer.timeout.connect(self._autosave_current)

        # 载入开发记录后，对话/改动等变化稍后自动写回原记录文件。
        self._record_timer = QTimer(self)
        self._record_timer.setSingleShot(True)
        self._record_timer.setInterval(900)
        self._record_timer.timeout.connect(self._write_dev_record)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 所有文件夹/记录/检查/查看/清空按键都收在同一行。
        bar = QHBoxLayout()
        bar.setSpacing(5)
        mode_label = QLabel("修改模式")
        mode_label.setObjectName("Muted")
        bar.addWidget(mode_label)
        self.mode_combo = QComboBox()
        for label, value, tip in DEV_MODES:
            self.mode_combo.addItem(label, value)
            self.mode_combo.setItemData(
                self.mode_combo.count() - 1, tip, Qt.ToolTipRole)
        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData(self.mode)))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        bar.addWidget(self.mode_combo)

        # 文件夹相关的所有按键（以及记录/检查/查看/清空）都放在同一行。
        self.add_folder_btn = QPushButton("＋ 添加")
        self.add_folder_btn.setToolTip(
            "添加允许修改的代码文件夹（可一次多选，之后还能继续添加）")
        self.add_folder_btn.clicked.connect(self._add_folders)
        self.remove_folder_btn = QPushButton("− 移除")
        self.remove_folder_btn.setObjectName("Flat")
        self.remove_folder_btn.setToolTip(
            "打开已添加的文件夹列表，选中后一次移除（不会删除磁盘文件）")
        self.remove_folder_btn.clicked.connect(self._remove_folder)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("Flat")
        self.refresh_btn.setToolTip("刷新左侧代码目录")
        self.refresh_btn.clicked.connect(self._refresh_tree)
        self.mark_btn = QPushButton("标记节点")
        self.mark_btn.setToolTip(
            "把当前所有文件记录为「新代码起点」：之后自己改动的行会标 ❗，"
            "「检查新代码」也只检查这个节点之后的改动。"
            "系统不会自动挪动起点，需要时手动再点一次。")
        self.mark_btn.clicked.connect(self._mark_new_code_start)
        self.check_btn = QPushButton("检查新代码")
        self.check_btn.setToolTip(
            "只把「标记节点」之后你自己写的新代码发给 AI，"
            "让它指出问题（只读，不会改文件）")
        self.check_btn.clicked.connect(self._check_my_code)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.setObjectName("Flat")
        self.clear_btn.setToolTip("清空左侧 AI 对话")
        self.clear_btn.clicked.connect(self._clear_chat)
        bar.addWidget(self.add_folder_btn)
        bar.addWidget(self.remove_folder_btn)
        bar.addWidget(self.refresh_btn)
        bar.addWidget(self.mark_btn)
        bar.addWidget(self.check_btn)
        bar.addWidget(self.clear_btn)
        bar.addStretch(1)
        self.save_dev_btn = QPushButton("保存开发")
        self.save_dev_btn.setObjectName("Flat")
        self.save_dev_btn.setToolTip(
            "把当前开发对话、允许修改的文件夹和待确认改动保存成开发记录")
        self.save_dev_btn.clicked.connect(self._save_dev)
        self.load_dev_btn = QPushButton("载入开发")
        self.load_dev_btn.setObjectName("Flat")
        self.load_dev_btn.setToolTip("载入之前保存的开发记录")
        self.load_dev_btn.clicked.connect(self._show_dev_menu)
        bar.addWidget(self.save_dev_btn)
        bar.addWidget(self.load_dev_btn)
        self.float_btn = QPushButton("独立窗口")
        self.float_btn.setObjectName("Flat")
        self.float_btn.setToolTip("把代码开发页放进独立窗口；再次点击收回页签")
        self.float_btn.clicked.connect(self.float_requested.emit)
        bar.addWidget(self.float_btn)
        layout.addLayout(bar)

        # 操作反馈仍然显示，但挪到聊天标题右侧，不再单独占一行按键空间。
        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.setMinimumWidth(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(6)
        splitter.setChildrenCollapsible(False)

        # ---- 左：对话栏 ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(6)
        chat_head = QHBoxLayout()
        chat_title = QLabel("AI 对话")
        chat_title.setObjectName("H2")
        self.qa_open_btn = QPushButton("问答窗口")
        self.qa_open_btn.setObjectName("Flat")
        self.qa_open_btn.setToolTip("打开/召回悬浮问答窗口（Ctrl+Shift+E）")
        self.qa_open_btn.clicked.connect(self._show_qa_window)
        self.qa_select_btn = QPushButton("针对选中提问")
        self.qa_select_btn.setObjectName("Flat")
        self.qa_select_btn.setToolTip(
            "先在右侧源码或对话里选中一段内容，再点这里，\n"
            "把这段引用直接带入下方对话输入框（不弹独立窗口）")
        self.qa_select_btn.clicked.connect(self._quote_for_qa)
        self.chat_status = QLabel("等待指令")
        self.chat_status.setObjectName("Muted")
        chat_head.addWidget(chat_title)
        chat_head.addWidget(self.qa_open_btn)
        chat_head.addWidget(self.qa_select_btn)
        chat_head.addStretch(1)
        chat_head.addWidget(self.status_label)
        chat_head.addWidget(self.chat_status)
        left_layout.addLayout(chat_head)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenLinks(False)
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setStyleSheet(
            "QTextBrowser { background: #ffffff; border: 1px solid #f6d9e6;"
            " border-radius: 8px; padding: 8px; color: #4b3742; }")
        self.chat_view.setPlaceholderText(
            "描述你想开发/修改的功能；也可以先在右侧选中一段代码，"
            "点「对话改进这段」再提问")
        left_layout.addWidget(self.chat_view, 1)

        self.chat_input = ChatInput()
        self.chat_input.setObjectName("ChatInput")
        self.chat_input.setFixedHeight(96)
        self.chat_input.setPlaceholderText(
            "描述要开发或修改的功能，Enter 发送，Shift+Enter 换行")
        self.chat_input.submitted.connect(self._send_chat)
        left_layout.addWidget(self.chat_input)

        bottom = QHBoxLayout()
        self.quote_btn = QPushButton("对话改进这段")
        self.quote_btn.setObjectName("Flat")
        self.quote_btn.setToolTip(
            "先在右侧源码（或在 AI 修改预览的「修改后」一栏）选中一段代码，"
            "再点这里，把这段代码带入对话")
        self.quote_btn.clicked.connect(self._quote_selection)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._interrupt)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("Primary")
        self.send_btn.clicked.connect(self._send_chat)
        bottom.addStretch(1)
        bottom.addWidget(self.quote_btn)
        bottom.addWidget(self.stop_btn)
        bottom.addWidget(self.send_btn)
        left_layout.addLayout(bottom)
        splitter.addWidget(left)

        # ---- 右：目录 + 源码 ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(6)
        self.file_row_widget = QWidget()
        file_row_layout = QVBoxLayout(self.file_row_widget)
        file_row_layout.setContentsMargins(0, 0, 0, 0)
        file_row_layout.setSpacing(0)
        file_row = QHBoxLayout()
        self.file_label = QLabel("尚未打开文件")
        self.file_label.setObjectName("H2")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("Flat")
        self.save_btn.setToolTip("保存当前文件（Ctrl+S）")
        self.save_btn.clicked.connect(self._save_current)
        self.reload_file_btn = QPushButton("重新加载")
        self.reload_file_btn.setObjectName("Flat")
        self.reload_file_btn.clicked.connect(self._reload_current)
        self.undo_btn = QPushButton("↶")
        self.undo_btn.setObjectName("Flat")
        self.undo_btn.setFixedWidth(34)
        self.undo_btn.setToolTip("撤回上一次修改（Ctrl+Z）")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self._undo_current)
        self.redo_btn = QPushButton("↷")
        self.redo_btn.setObjectName("Flat")
        self.redo_btn.setFixedWidth(34)
        self.redo_btn.setToolTip("前进到下一次修改（Ctrl+Y / Ctrl+Shift+Z）")
        self.redo_btn.setEnabled(False)
        self.redo_btn.clicked.connect(self._redo_current)
        self.vscode_btn = QPushButton("VS Code")
        self.vscode_btn.setObjectName("Flat")
        self.vscode_btn.clicked.connect(self._open_in_vscode)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.save_btn)
        file_row.addWidget(self.reload_file_btn)
        file_row.addWidget(self.undo_btn)
        file_row.addWidget(self.redo_btn)
        file_row.addWidget(self.vscode_btn)
        file_row_layout.addLayout(file_row)
        right_layout.addWidget(self.file_row_widget)

        # 普通代码视图 / 内嵌修改对比 共用右侧空间；对比时自动隐藏目录栏。
        self.right_stack = QStackedWidget()
        right_splitter = QSplitter(Qt.Horizontal)
        right_splitter.setHandleWidth(5)
        right_splitter.setChildrenCollapsible(False)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(170)
        self.tree.itemDoubleClicked.connect(self._on_tree_activated)
        self.tree.itemExpanded.connect(self._on_tree_expanded)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        self.tree.setStyleSheet(
            "QTreeWidget { background: #fceaf1; border: 1px solid #f4b4cc;"
            " border-radius: 6px; padding: 4px; color: #333333; }"
            "QTreeWidget::item { padding: 2px 4px; }"
            "QTreeWidget::item:hover { background: #fcdee9; }"
            "QTreeWidget::item:selected { background: #ffb1b7; color: #39007f; }")
        right_splitter.addWidget(self.tree)
        # 打开的文件保留为页签，可切换、拖动或关闭（与代码研读一致）。
        self.code_tabs = QTabWidget()
        self.code_tabs.setObjectName("CodeTabs")
        self.code_tabs.setTabsClosable(True)
        self.code_tabs.setMovable(True)
        self.code_tabs.setDocumentMode(True)
        self.code_tabs.tabCloseRequested.connect(self._close_code_tab)
        self.code_tabs.currentChanged.connect(self._on_code_tab_changed)
        self.code_tabs.setStyleSheet(
            "QTabWidget#CodeTabs::pane { border: 1px solid #f4b4cc;"
            " background: #ffffff; }"
            "QTabBar::tab { background: #fceaf1; color: #333333;"
            " padding: 5px 12px; border: 1px solid #f4b4cc;"
            " border-bottom: none; margin-right: 2px; }"
            "QTabBar::tab:selected { background: #fcdee9; color: #39007f;"
            " font-weight: 600; }"
            "QTabBar::tab:hover { background: #fcdee9; }")
        self.code_placeholder = QLabel(
            "双击左侧目录里的文件打开\n打开的文件会保留为页签，可切换或关闭")
        self.code_placeholder.setObjectName("Empty")
        self.code_placeholder.setAlignment(Qt.AlignCenter)
        self.code_placeholder.setWordWrap(True)
        self.code_tabs.addTab(self.code_placeholder, "未打开文件")
        self._placeholder_visible = True
        right_splitter.addWidget(self.code_tabs)
        right_splitter.setStretchFactor(0, 0)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setSizes([230, 700])
        self.right_splitter = right_splitter
        self.right_stack.addWidget(right_splitter)
        self.diff_panel = DevDiffPanel(self)
        self._diff_panel = self.diff_panel
        self.right_stack.addWidget(self.diff_panel)
        self.right_stack.setCurrentWidget(right_splitter)
        right_layout.addWidget(self.right_stack, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        # 左侧 AI 对话默认宽度为原来的 3/4（560 -> 420）。
        splitter.setSizes([420, 780])
        layout.addWidget(splitter, 1)

        save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        save_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        save_shortcut.activated.connect(self._save_current)

    # ------------------------------------------------------------- folders --
    def _refresh_roots_list(self):
        # 文件夹不再单独占一行显示；数量与待确认文件数只放在按钮提示里。
        self.remove_folder_btn.setEnabled(bool(self.roots))
        self.remove_folder_btn.setToolTip(
            "打开已添加的文件夹列表，选中后一次移除（不会删除磁盘文件）"
            if self.roots else "还没有添加文件夹")
        self._update_buttons()

    @staticmethod
    def _is_under(path, root):
        try:
            return os.path.commonpath([str(path), str(root)]) == str(root)
        except ValueError:
            return False

    def _add_folders(self):
        dialog = QFileDialog(self, "选择代码文件夹（可按住 Ctrl/Shift 多选）")
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        for view_name in ("listView", "treeView"):
            view = dialog.findChild(QListView if view_name == "listView"
                                    else QTreeView, view_name)
            if view is not None:
                view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if dialog.exec_() != QDialog.Accepted:
            return
        paths = []
        list_view = dialog.findChild(QListView, "listView")
        model = list_view.model() if list_view is not None else None
        selection = (list_view.selectionModel()
                     if list_view is not None else None)
        if model is not None and selection is not None:
            for index in selection.selectedIndexes():
                if hasattr(model, "filePath"):
                    paths.append(model.filePath(index))
        if not paths:
            paths.extend(dialog.selectedFiles())
        added = 0
        for path in paths:
            if not path:
                continue
            try:
                resolved = str(Path(path).expanduser().resolve())
            except OSError:
                continue
            if not Path(resolved).is_dir() or resolved in self.roots:
                continue
            self.roots.append(resolved)
            added += 1
        if added:
            self._persist()
            self._refresh_roots_list()
            self._refresh_tree()
            if self._worker is not None:
                self._worker.set_dev_policy(self.roots, self.mode)
            self._schedule_dev_record_save()
            self.status_label.setText(
                f"已添加 {added} 个文件夹，可点「标记节点」记录新代码起点")

    def _remove_folder(self):
        if not self.roots:
            self.status_label.setText("还没有添加文件夹")
            return
        dialog = DevFolderDialog(self.roots, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        selected = set(dialog.selected_roots())
        if not selected:
            return
        self.roots = [r for r in self.roots if r not in selected]
        self._persist()
        self._refresh_roots_list()
        self._refresh_tree()
        if self._worker is not None:
            self._worker.set_dev_policy(self.roots, self.mode)
        self._schedule_dev_record_save()
        self.status_label.setText(f"已移除 {len(selected)} 个文件夹")

    def _persist(self):
        # 只记住修改模式；允许修改的文件夹不跨进程保存，重开程序时为空。
        self.config["dev_mode"] = self.mode
        try:
            save_config(self.config)
        except OSError:
            pass

    def _on_mode_changed(self, _index):
        self.mode = self.mode_combo.currentData() or "function"
        self._persist()
        if self._worker is not None:
            self._worker.set_dev_policy(self.roots, self.mode)
        label = self.mode_combo.currentText()
        self._schedule_dev_record_save()
        self.status_label.setText(f"修改模式：{label}")

    # ------------------------------------------------------- dev records --
    def reset_for_new_chat(self):
        """新对话/关闭对话时收起这次开发会话（不删除磁盘文件）。"""
        if self._busy and self._worker is not None:
            self._worker.interrupt()
        if self._qa_busy and self._qa_worker is not None:
            self._qa_worker.interrupt()
        self._messages = []
        self._live = ""
        self._chat_dirty = False
        self._qa_messages = []
        self._qa_live = ""
        self._qa_dirty = False
        self._pending = {}
        self._turn_files = {}
        self._review_pending = False
        self._saved_dev_name = ""
        self._saved_dev_path = None
        timer = getattr(self, "_record_timer", None)
        if timer is not None:
            timer.stop()
        self.chat_input.clear()
        if self._qa_input is not None:
            self._qa_input.clear()
        if self._qa_window is not None:
            self._qa_window.close()
        self._hide_diff_view()
        self._refresh_tree_marks()
        self._refresh_viewer_marks()
        self._render_chat()
        self._render_qa_chat()
        self.chat_status.setText("等待指令")
        self.status_label.setText("")
        self._update_buttons()

    def _dev_record_snapshot(self):
        messages = [dict(m) for m in self._messages]
        if self._live.strip():
            messages.append({"role": "assistant", "text": self._live})
        pending = {
            path: {
                "original": entry.get("original"),
                "after": entry.get("after", ""),
            }
            for path, entry in self._pending.items()
        }
        return {
            "format": 1,
            "name": self._saved_dev_name or "未命名开发",
            "roots": list(self.roots),
            "mode": self.mode,
            "messages": messages,
            "current_file": self._current_file or "",
            "pending": pending,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _dev_records(self):
        records = []
        if not DEV_RECORDS_DIR.is_dir():
            return records
        for path in sorted(DEV_RECORDS_DIR.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            name = path.stem
            saved_at = ""
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                name = data.get("name") or name
                saved_at = str(data.get("saved_at") or "")
            except (OSError, ValueError):
                continue
            records.append({"path": path, "name": name, "saved_at": saved_at})
        return records

    def _schedule_dev_record_save(self):
        """载入过开发记录时，内容改动后自动同步回原记录文件。"""
        if self._saved_dev_path is None:
            return
        timer = getattr(self, "_record_timer", None)
        if timer is not None:
            timer.start()

    def _write_dev_record(self):
        path = self._saved_dev_path
        if path is None:
            return
        data = self._dev_record_snapshot()
        try:
            Path(path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            self.status_label.setText(f"开发记录同步失败：{exc}")
            return
        self.status_label.setText(f"开发记录已同步：{Path(path).name}")

    def _save_dev(self):
        if not (self._messages or self._pending):
            QMessageBox.information(
                self, "保存开发", "当前还没有可保存的开发对话或修改。")
            return
        default_name = (self._saved_dev_name
                        or f"开发 {datetime.now():%m-%d %H:%M}")
        name, ok = QInputDialog.getText(
            self, "保存开发", "开发记录名称：", text=default_name)
        if not ok:
            return
        name = (name or default_name).strip() or default_name
        safe = re.sub(r"[^\w一-鿿.-]+", "-", name).strip(".-") or "dev"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        DEV_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        path = DEV_RECORDS_DIR / f"{stamp}-{safe[:60]}.json"
        data = self._dev_record_snapshot()
        data["name"] = name
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "保存开发", f"保存失败：{exc}")
            return
        self._saved_dev_name = name
        self._saved_dev_path = path
        self.status_label.setText(f"开发已保存：{path.name}")
        self.chat_status.setText(f"开发已保存：{name}")

    def _show_dev_menu(self):
        records = self._dev_records()
        menu = QMenu(self)
        act_save = menu.addAction("保存当前开发…")
        menu.addSeparator()
        actions = {}
        if records:
            for record in records[:20]:
                label = record["name"]
                if record["saved_at"]:
                    label += f"  ·  {record['saved_at'][:16].replace('T', ' ')}"
                action = menu.addAction(label)
                actions[action] = record
        else:
            menu.addAction("（还没有保存的开发记录）").setEnabled(False)
        menu.addSeparator()
        act_open = menu.addAction("打开开发记录目录")
        chosen = menu.exec_(
            self.load_dev_btn.mapToGlobal(self.load_dev_btn.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen is act_save:
            self._save_dev()
        elif chosen is act_open:
            DEV_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
            win_compat.open_path(DEV_RECORDS_DIR)
        elif chosen in actions:
            self._load_dev_path(actions[chosen]["path"])

    def _load_dev_path(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "载入开发", f"无法读取记录：{exc}")
            return
        self._saved_dev_path = Path(path)
        self._apply_dev_record(data)

    def _apply_dev_record(self, data):
        if not isinstance(data, dict):
            return
        if self._busy and self._worker is not None:
            self._worker.interrupt()
        roots = []
        for root in data.get("roots") or []:
            if not isinstance(root, str):
                continue
            try:
                resolved = str(Path(root).expanduser().resolve())
            except OSError:
                continue
            if Path(resolved).is_dir() and resolved not in roots:
                roots.append(resolved)
        mode = data.get("mode") or "function"
        if mode not in {item[1] for item in DEV_MODES}:
            mode = "function"
        self.roots = roots
        self.mode = mode
        self.mode_combo.blockSignals(True)
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
        self._messages = []
        for item in data.get("messages") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            role = "user" if item.get("role") == "user" else "assistant"
            self._messages.append({"role": role, "text": text})
        self._live = ""
        self._chat_dirty = False
        self._pending = {}
        for path, entry in (data.get("pending") or {}).items():
            if not isinstance(path, str) or not isinstance(entry, dict):
                continue
            after = entry.get("after")
            if not isinstance(after, str):
                continue
            original = entry.get("original")
            if not isinstance(original, str):
                original = None
            self._pending[path] = {"original": original, "after": after}
        self._turn_files = {}
        self._saved_dev_name = str(data.get("name") or "")
        self._persist()
        if self._worker is not None:
            self._worker.set_dev_policy(self.roots, self.mode)
        self._refresh_roots_list()
        self._refresh_tree()
        self._render_chat()
        current = str(data.get("current_file") or "")
        self._close_all_code_tabs()
        if current and Path(current).is_file():
            self._open_file(current)
        self._refresh_viewer_marks()
        if self._pending:
            self._show_diff_view()
        else:
            self._hide_diff_view()
        self.chat_status.setText("已载入开发记录")
        self.status_label.setText(
            f"开发记录：{self._saved_dev_name or '已载入'}"
            "（可点「标记节点」记录新代码起点）")
        self._update_buttons()

    # ---------------------------------------------------------------- tree --
    def _refresh_tree(self):
        self.tree.clear()
        self._tree_items = {}
        for root in self.roots:
            top = QTreeWidgetItem([Path(root).name])
            top.setData(0, Qt.UserRole, ("dir", root))
            top.setToolTip(0, root)
            self.tree.addTopLevelItem(top)
            placeholder = QTreeWidgetItem(["…"])
            placeholder.setData(0, Qt.UserRole, ("placeholder", root))
            top.addChild(placeholder)
        self._refresh_tree_marks()

    def _on_tree_expanded(self, item):
        data = item.data(0, Qt.UserRole)
        if not isinstance(data, tuple) or len(data) != 2:
            return
        kind, path = data
        if kind != "dir":
            return
        self._load_tree_children(item, path)

    def _load_tree_children(self, item, path):
        if item.childCount() == 1:
            only = item.child(0)
            data = only.data(0, Qt.UserRole)
            if isinstance(data, tuple) and data[0] == "placeholder":
                item.removeChild(only)
            else:
                return
        try:
            entries = sorted(
                Path(path).iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.name in DEV_SKIP_DIRS:
                    continue
                child = QTreeWidgetItem([entry.name])
                child.setData(0, Qt.UserRole, ("dir", str(entry)))
                child.setToolTip(0, str(entry))
                placeholder = QTreeWidgetItem(["…"])
                placeholder.setData(0, Qt.UserRole, ("placeholder", str(entry)))
                child.addChild(placeholder)
                item.addChild(child)
            elif entry.suffix.lower() in DEV_SOURCE_SUFFIXES:
                child = QTreeWidgetItem([entry.name])
                child.setData(0, Qt.UserRole, ("file", str(entry)))
                child.setToolTip(0, str(entry))
                item.addChild(child)
        self._refresh_tree_marks()

    def _on_tree_activated(self, item, _column):
        data = item.data(0, Qt.UserRole)
        if not isinstance(data, tuple) or len(data) != 2:
            return
        kind, path = data
        if kind == "file":
            if path in self._pending:
                # 「查看修改」按键已移除：双击带 ❗ 的文件直接打开对比。
                self._show_diff_view()
                if self._diff_panel is not None:
                    self._diff_panel.select_file(path)
            else:
                self._open_file(path)
        elif kind == "dir":
            item.setExpanded(not item.isExpanded())

    # ---------------------------------------------------- tree file actions --
    def _tree_target(self, item):
        """返回 (kind, path)；点空白处返回 (None, "")。"""
        if item is None:
            return None, ""
        data = item.data(0, Qt.UserRole)
        if isinstance(data, tuple) and len(data) == 2 \
                and data[0] in ("file", "dir"):
            return data[0], str(data[1])
        return None, ""

    def _tree_menu_dir(self, kind, path):
        if kind == "dir":
            return Path(path)
        if kind == "file":
            return Path(path).parent
        if self.roots:
            return Path(self.roots[0])
        return None

    def _is_root_path(self, path):
        target = str(Path(path))
        return any(target == str(Path(root)) for root in self.roots)

    def _tree_context_menu(self, pos):
        if not self.roots:
            return
        item = self.tree.itemAt(pos)
        kind, path = self._tree_target(item)
        directory = self._tree_menu_dir(kind, path)
        menu = QMenu(self)
        act_new_file = menu.addAction("新建文件")
        act_new_folder = menu.addAction("新建文件夹")
        act_rename = None
        act_delete = None
        if kind in ("file", "dir") and not self._is_root_path(path):
            menu.addSeparator()
            act_rename = menu.addAction("重命名")
            act_delete = menu.addAction("删除")
        menu.addSeparator()
        act_refresh = menu.addAction("刷新")
        chosen = menu.exec_(
            self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_new_file:
            self._tree_create_entry(directory, is_dir=False)
        elif chosen is act_new_folder:
            self._tree_create_entry(directory, is_dir=True)
        elif act_rename is not None and chosen is act_rename:
            self._tree_rename(path)
        elif act_delete is not None and chosen is act_delete:
            self._tree_delete(path)
        elif chosen is act_refresh:
            self._refresh_tree()

    def _tree_create_entry(self, directory, is_dir):
        if directory is None:
            return
        title = "新建文件夹" if is_dir else "新建文件"
        name, ok = QInputDialog.getText(
            self, title, "名称：", text="新建文件夹" if is_dir else "新建文件.txt")
        if not ok or not (name or "").strip():
            return
        name = name.strip()
        if "/" in name or "\\" in name:
            QMessageBox.warning(self, title, "名称里不能包含路径分隔符。")
            return
        target = directory / name
        try:
            if is_dir:
                target.mkdir(parents=False, exist_ok=False)
            else:
                target.touch(exist_ok=False)
        except FileExistsError:
            QMessageBox.warning(self, title, f"已存在：{name}")
            return
        except OSError as exc:
            QMessageBox.warning(self, title, f"创建失败：{exc}")
            return
        self._refresh_tree()
        if not is_dir:
            self._open_file(str(target))
        self.status_label.setText(f"已创建：{self._display_path(str(target))}")

    def _tree_rename(self, path):
        old = Path(path)
        name, ok = QInputDialog.getText(
            self, "重命名", "新名称：", text=old.name)
        if not ok or not (name or "").strip():
            return
        name = name.strip()
        if "/" in name or "\\" in name:
            QMessageBox.warning(self, "重命名", "名称里不能包含路径分隔符。")
            return
        target = old.with_name(name)
        if target == old:
            return
        try:
            old.rename(target)
        except FileExistsError:
            QMessageBox.warning(self, "重命名", f"已存在：{name}")
            return
        except OSError as exc:
            QMessageBox.warning(self, "重命名", f"重命名失败：{exc}")
            return
        # 已打开的页签跟着改名，避免之后保存到旧路径。
        entry = self._entry_for_path(str(old))
        if entry is not None:
            entry["path"] = str(target)
            index = self.code_tabs.indexOf(entry["view"])
            if index >= 0:
                self.code_tabs.setTabText(index, target.name)
                self.code_tabs.setTabToolTip(index, str(target))
            if self._current_file == str(old):
                self._current_file = str(target)
                self.file_label.setText(self._display_path(str(target)))
                self.file_label.setToolTip(str(target))
        self._refresh_tree()
        self.status_label.setText(f"已重命名：{old.name} → {name}")

    def _tree_delete(self, path):
        target = Path(path)
        answer = QMessageBox.question(
            self, "删除",
            f"确定删除「{target.name}」吗？\n"
            + ("文件夹及其全部内容会被删除。" if target.is_dir()
               else "该文件会被删除。")
            + "\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "删除", f"删除失败：{exc}")
            return
        # 关掉该路径（或目录下）的页签，并清掉对应的待确认改动。
        for entry in list(self._code_tabs):
            if self._is_under(str(entry["path"]), str(target)):
                self._close_tab_for_path(str(entry["path"]))
        for pending_path in list(self._pending):
            if self._is_under(pending_path, str(target)):
                self._pending.pop(pending_path, None)
        self._refresh_tree()
        self._refresh_viewer_marks()
        self._update_buttons()
        self.status_label.setText(f"已删除：{target.name}")

    def _refresh_tree_marks(self):
        pending = set(self._pending)
        # 递归遍历（树通常不大）
        def walk(item):
            data = item.data(0, Qt.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                kind, target = data
                if kind == "placeholder":
                    return
                name = Path(target).name
                if kind == "file":
                    marked = target in pending
                elif kind == "dir":
                    marked = any(self._is_under(path, target)
                                 for path in pending)
                else:
                    marked = False
                item.setText(0, ("❗ " if marked else "") + name)
            for index in range(item.childCount()):
                walk(item.child(index))
        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))
        self._refresh_roots_list()

    # --------------------------------------------------------------- file --
    def _display_path(self, path):
        for root in self.roots:
            if self._is_under(path, root):
                try:
                    return str(Path(path).relative_to(root))
                except ValueError:
                    pass
        return str(path)

    def _entry_for_path(self, path):
        target = str(path)
        return next((e for e in self._code_tabs
                     if str(e["path"]) == target), None)

    def _active_entry(self):
        widget = self.code_tabs.currentWidget()
        return next((e for e in self._code_tabs
                     if e["view"] is widget), None)

    def _active_view(self):
        entry = self._active_entry()
        return entry["view"] if entry is not None else None

    def _open_file(self, path):
        path = str(path)
        entry = self._entry_for_path(path)
        if entry is None:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                QMessageBox.warning(self, "打开文件", f"读取失败：{exc}")
                return
            view = DevCodeView(editable=True)
            view.set_change_lines([], "#33e4638c")
            view.setPlainText(text)
            view.document().setModified(False)
            view.textChanged.connect(self._schedule_marks)
            view.textChanged.connect(self._schedule_autosave)
            view.undoAvailable.connect(
                lambda v=view: self._update_undo_buttons(v))
            view.redoAvailable.connect(
                lambda v=view: self._update_undo_buttons(v))
            entry = {"path": path, "view": view}
            self._code_tabs.append(entry)
            self._remove_code_placeholder()
            index = self.code_tabs.addTab(view, Path(path).name)
            self.code_tabs.setTabToolTip(index, path)
        index = self.code_tabs.indexOf(entry["view"])
        if index >= 0:
            self.code_tabs.setCurrentIndex(index)
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
        entry["view"].document().setModified(False)
        cursor = entry["view"].textCursor()
        cursor.movePosition(QTextCursor.Start)
        entry["view"].setTextCursor(cursor)
        self._current_file = path
        self.file_label.setText(self._display_path(path))
        self.file_label.setToolTip(path)
        self._refresh_viewer_marks()
        self.status_label.setText(f"已打开：{self._display_path(path)}")
        self._schedule_dev_record_save()
        self._update_undo_buttons()

    def _on_code_tab_changed(self, _index):
        entry = self._active_entry()
        if entry is None:
            self._current_file = None
            self.file_label.setText("尚未打开文件")
            self.file_label.setToolTip("")
            self._update_undo_buttons()
            return
        self._current_file = str(entry["path"])
        self.file_label.setText(self._display_path(entry["path"]))
        self.file_label.setToolTip(str(entry["path"]))
        self._refresh_viewer_marks()
        self._schedule_dev_record_save()
        self._update_undo_buttons()

    def _close_code_tab(self, index):
        widget = self.code_tabs.widget(index)
        if widget is self.code_placeholder:
            return
        entry = next((e for e in self._code_tabs if e["view"] is widget), None)
        if entry is not None:
            self._code_tabs.remove(entry)
        self.code_tabs.removeTab(index)
        if widget is not None:
            widget.deleteLater()
        if not self._code_tabs:
            self._current_file = None
            self.file_label.setText("尚未打开文件")
            self.file_label.setToolTip("")
            self.code_tabs.addTab(self.code_placeholder, "未打开文件")
            self._placeholder_visible = True
            self._update_undo_buttons()
        else:
            self._on_code_tab_changed(self.code_tabs.currentIndex())

    def _close_all_code_tabs(self):
        self.code_tabs.blockSignals(True)
        for entry in list(self._code_tabs):
            index = self.code_tabs.indexOf(entry["view"])
            if index >= 0:
                self.code_tabs.removeTab(index)
            entry["view"].deleteLater()
        self._code_tabs = []
        self.code_tabs.blockSignals(False)
        self._remove_code_placeholder()
        self._show_code_placeholder()
        self._current_file = None
        self.file_label.setText("尚未打开文件")
        self.file_label.setToolTip("")
        self._update_undo_buttons()

    def _remove_code_placeholder(self):
        index = self.code_tabs.indexOf(self.code_placeholder)
        if index >= 0:
            self.code_tabs.removeTab(index)
        self._placeholder_visible = False

    def _show_code_placeholder(self):
        if self.code_tabs.indexOf(self.code_placeholder) < 0:
            self.code_tabs.addTab(self.code_placeholder, "未打开文件")
        self._placeholder_visible = True
        self.code_tabs.setCurrentWidget(self.code_placeholder)

    def _schedule_marks(self, *_args):
        self._mark_timer.start()

    def _refresh_viewer_marks(self):
        for entry in self._code_tabs:
            path = str(entry["path"])
            view = entry["view"]
            pending = self._pending.get(path)
            if pending is not None:
                _before, lines, _hunks = dev_line_changes(
                    pending.get("original", ""), pending.get("after", ""))
                view.set_change_lines(lines, "#332f9e63")
                color_pending = True
            else:
                old = self._snapshot.get(path)
                current = view.toPlainText()
                if not self._snapshot:
                    # 还没有“我的起点”，不把所有行都标成改动
                    lines = set()
                elif old is None:
                    lines = (set(range(1, len(current.splitlines()) + 1))
                             if current else set())
                elif old != current:
                    _before, lines, _hunks = dev_line_changes(old, current)
                else:
                    lines = set()
                view.set_change_lines(lines, "#33e4638c")
                color_pending = False
            index = self.code_tabs.indexOf(view)
            if index >= 0:
                name = Path(path).name
                self.code_tabs.setTabText(
                    index, ("❗ " if color_pending else "") + name)

    def _save_current(self, quiet=False, view=None):
        entry = self._active_entry()
        if view is not None:
            entry = next((e for e in self._code_tabs
                          if e["view"] is view), None)
        if entry is None:
            return False
        path = entry["path"]
        text = entry["view"].toPlainText()
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            if not quiet:
                QMessageBox.warning(self, "保存文件", f"保存失败：{exc}")
            self.status_label.setText(f"保存失败：{exc}")
            return False
        entry["view"].document().setModified(False)
        if not quiet:
            self.status_label.setText(f"已保存：{self._display_path(path)}")
        self._refresh_viewer_marks()
        self._refresh_tree_marks()
        return True

    def _schedule_autosave(self, *_args):
        # 载入文件时 setPlainText 也会触发 textChanged，此时文档未被修改。
        if any(e["view"].document().isModified() for e in self._code_tabs):
            self._autosave_timer.start()

    def _autosave_current(self):
        saved = []
        for entry in list(self._code_tabs):
            if not entry["view"].document().isModified():
                continue
            if self._save_current(quiet=True, view=entry["view"]):
                saved.append(self._display_path(entry["path"]))
        if saved:
            self.status_label.setText(f"已自动保存：{'、'.join(saved)}")

    def _update_undo_buttons(self, view=None):
        if view is not None and view is not self._active_view():
            return
        entry = self._active_entry()
        doc = entry["view"].document() if entry is not None else None
        self.undo_btn.setEnabled(
            (doc is not None and doc.isUndoAvailable())
            or bool(self._file_undo))
        self.redo_btn.setEnabled(
            (doc is not None and doc.isRedoAvailable())
            or bool(self._file_redo))

    def _undo_current(self):
        # 先在编辑器内撤回手动输入；编辑器没有可撤回内容时，
        # 再撤回「确认覆盖原代码」造成的文件写入。
        entry = self._active_entry()
        doc = entry["view"].document() if entry is not None else None
        if doc is not None and doc.isUndoAvailable():
            entry["view"].undo()
            self._schedule_autosave()
        else:
            self._undo_confirmed_change()
        self._update_undo_buttons()

    def _redo_current(self):
        entry = self._active_entry()
        doc = entry["view"].document() if entry is not None else None
        if doc is not None and doc.isRedoAvailable():
            entry["view"].redo()
            self._schedule_autosave()
        else:
            self._redo_confirmed_change()
        self._update_undo_buttons()

    # ---------------------------------------------- confirmed-change undo --
    @staticmethod
    def _write_history_text(path, text):
        if text is None:
            Path(path).unlink(missing_ok=True)
        else:
            Path(path).write_text(text, encoding="utf-8")

    def _push_file_history(self, changes):
        changes = [item for item in changes if item[1] != item[2]]
        if not changes:
            return
        self._file_undo.append(changes)
        del self._file_undo[:-50]
        self._file_redo.clear()
        self._update_undo_buttons()

    def _undo_confirmed_change(self):
        if not self._file_undo:
            return False
        changes = self._file_undo.pop()
        done = []
        for path, before, after in changes:
            try:
                self._write_history_text(path, before)
            except OSError as exc:
                QMessageBox.warning(self, "撤回修改", f"{path}\n{exc}")
                continue
            done.append((path, before, after))
        if not done:
            self._file_undo.append(changes)
            self._update_undo_buttons()
            return False
        self._file_redo.append(done)
        self._refresh_after_file_history("已撤回确认覆盖")
        return True

    def _redo_confirmed_change(self):
        if not self._file_redo:
            return False
        changes = self._file_redo.pop()
        done = []
        for path, before, after in changes:
            try:
                self._write_history_text(path, after)
            except OSError as exc:
                QMessageBox.warning(self, "前进修改", f"{path}\n{exc}")
                continue
            done.append((path, before, after))
        if not done:
            self._file_redo.append(changes)
            self._update_undo_buttons()
            return False
        self._file_undo.append(done)
        self._refresh_after_file_history("已前进到确认覆盖后的版本")
        return True

    def _refresh_after_file_history(self, message):
        self._refresh_tree_marks()
        if self._current_file and Path(self._current_file).exists():
            self._reload_file_tab(self._current_file)
        elif self._current_file:
            self._close_tab_for_path(self._current_file)
        self._schedule_dev_record_save()
        self.status_label.setText(message)
        self._update_buttons()
        self._update_undo_buttons()

    def _close_tab_for_path(self, path):
        entry = self._entry_for_path(path)
        if entry is None:
            return
        index = self.code_tabs.indexOf(entry["view"])
        if index >= 0:
            self._close_code_tab(index)

    def _reload_file_tab(self, path):
        """重新读取磁盘内容，覆盖该文件页签里尚未保存的编辑。"""
        entry = self._entry_for_path(path)
        if entry is None:
            self._open_file(path)
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            QMessageBox.warning(self, "重新加载", f"读取失败：{exc}")
            return
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
        entry["view"].setPlainText(text)
        entry["view"].document().setModified(False)
        cursor = entry["view"].textCursor()
        cursor.movePosition(QTextCursor.Start)
        entry["view"].setTextCursor(cursor)
        if self._active_entry() is entry:
            self._current_file = str(entry["path"])
            self.file_label.setText(self._display_path(entry["path"]))
            self.file_label.setToolTip(str(entry["path"]))
        self._refresh_viewer_marks()
        self._update_undo_buttons()
        self.status_label.setText(f"已重新加载：{self._display_path(path)}")

    def _reload_current(self):
        if self._current_file:
            self._reload_file_tab(self._current_file)

    def _open_in_vscode(self):
        if self._current_file:
            win_compat.open_editor(self._current_file)

    # --------------------------------------------------------------- chat --
    def _markdown(self):
        parts = []
        for message in self._messages:
            who = "🟣 你" if message.get("role") == "user" else "🤖 Claude"
            parts.append(f"### {who}\n\n{message.get('text', '')}")
        if self._live:
            parts.append(f"### 🤖 Claude\n\n{self._live}")
        return "\n\n---\n\n".join(parts) if parts else ""

    def _render_chat(self):
        text = _escape_markdown_angles(self._markdown())
        self.chat_view.setMarkdown(text)
        bar = self.chat_view.verticalScrollBar()
        QTimer.singleShot(0, lambda b=bar: b.setValue(b.maximum()))

    def _flush_chat(self):
        if not self._chat_dirty:
            self._chat_timer.stop()
            return
        self._chat_dirty = False
        self._render_chat()

    def _qa_markdown(self):
        parts = []
        for message in visible_qa_messages(self._qa_messages):
            who = "🟣 你" if message.get("role") == "user" else "🤖 Claude"
            parts.append(f"### {who}\n\n{message.get('text', '')}")
        if self._qa_live:
            parts.append(f"### 🤖 Claude\n\n{self._qa_live}")
        return _escape_markdown_angles(
            "\n\n---\n\n".join(parts)) if parts else ""

    def _render_qa_chat(self):
        if self._qa_view is None:
            return
        self._qa_view.setMarkdown(self._qa_markdown())
        bar = self._qa_view.verticalScrollBar()
        QTimer.singleShot(0, lambda b=bar: b.setValue(b.maximum()))

    def _flush_qa_chat(self):
        if not self._qa_dirty:
            self._qa_timer.stop()
            return
        self._qa_dirty = False
        self._render_qa_chat()

    def _append_qa_message(self, role, text):
        if not (text or "").strip():
            return
        self._qa_messages.append({"role": role, "text": text.strip()})
        # 开发问答没有落盘需求，内存里也只保留窗口可见的最近若干条。
        self._qa_messages[:] = list(visible_qa_messages(self._qa_messages))
        self._qa_dirty = True
        if not self._qa_timer.isActive():
            self._qa_timer.start()

    def _append_message(self, role, text):
        if not (text or "").strip():
            return
        self._messages.append({"role": role, "text": text.strip()})
        if len(self._messages) > 200:
            del self._messages[:50]
        self._schedule_dev_record_save()
        self._chat_dirty = True
        if not self._chat_timer.isActive():
            self._chat_timer.start()

    def _clear_chat(self):
        self._messages = []
        self._live = ""
        self._schedule_dev_record_save()
        self._render_chat()
        self.chat_status.setText("对话已清空")

    def _ensure_worker(self):
        if self._worker is None:
            worker = ClaudeWorker(self)
            worker.text_delta.connect(self._on_delta)
            worker.assistant.connect(self._on_assistant)
            worker.result.connect(self._on_result)
            worker.error.connect(self._on_error)
            worker.busy_changed.connect(self._on_busy)
            worker.permission_detail.connect(self._on_permission_detail)
            worker.permission_asked.connect(self._on_permission_asked)
            self._worker = worker
            worker.start()
        self._worker.set_dev_policy(self.roots, self.mode)
        root = self.roots[0] if self.roots else ""
        cfg = (root, self.model, self.effort)
        if self._worker_cfg != cfg:
            self._worker.configure(
                root, "default", model=self.model, effort=self.effort)
            self._worker_cfg = cfg
            self._worker.set_dev_policy(self.roots, self.mode)

    def set_runtime(self, model=None, effort=None, permission=None):
        if model != self.model or effort != self.effort:
            self.model = model
            self.effort = effort
            if self._worker is not None:
                self._worker_cfg = None
            if self._qa_worker is not None:
                self._qa_worker_cfg = None

    def set_floating(self, floating):
        """更新独立窗口按钮的外观（页面搬到独立窗口后变成「收回页签」）。"""
        self._floating = bool(floating)
        self.float_btn.setText("收回页签" if self._floating else "独立窗口")
        self.float_btn.setToolTip(
            "把代码开发页收回主窗口页签" if self._floating
            else "把代码开发页放进独立窗口；再次点击收回页签")

    def _update_buttons(self):
        ready = bool(self.roots) and SDK_AVAILABLE
        self.send_btn.setEnabled(ready and not self._busy)
        self.stop_btn.setEnabled(self._busy)
        self.mark_btn.setEnabled(ready and not self._busy)
        self.check_btn.setEnabled(ready and not self._busy)
        self._update_undo_buttons()
        if self._qa_send is not None:
            self._qa_send.setEnabled(ready and not self._qa_busy)

    def _chat_context(self):
        lines = ["【代码开发模式】", "允许直接修改的文件夹："]
        lines.extend(f"- {root}" for root in self.roots)
        if self.mode == "all":
            how = "修改会直接覆盖原文件，不会弹确认窗口。"
        elif self.mode == "function":
            how = "每轮修改结束后会统一确认一次再覆盖。"
        else:
            how = "每个文件的修改都要逐个确认后才覆盖。"
        lines.append(
            f"修改处理方式：{self.mode_combo.currentText()}。{how}"
            "不会再单独请求权限。")
        if self._current_file:
            lines.append(f"当前打开文件：{self._current_file}")
        lines.append(
            "请直接使用读写/编辑工具完成修改；只需说明改了什么以及为什么。")
        return "\n".join(lines)

    def _start_send(self, prompt, display, deny_writes=False):
        if self._busy:
            return False
        if not self.roots:
            QMessageBox.information(
                self, "代码开发", "请先添加至少一个允许修改的代码文件夹。")
            return False
        if not SDK_AVAILABLE:
            QMessageBox.warning(
                self, "缺少对话组件",
                f"未安装 Claude Agent SDK，无法在界面内开发。\n\n{SDK_IMPORT_ERROR}")
            return False
        self._ensure_worker()
        # 本轮开始前的文件内容：Edit/Write 由工作线程精确抓取；
        # 这里再兜底一次，Bash 等方式造成的改动也能进入改前/改后预览。
        self._turn_files = dev_scan_files(self.roots)
        self._worker.begin_dev_turn(deny_writes=deny_writes)
        self._review_pending = bool(deny_writes)
        self._append_message("user", display)
        self._live = ""
        self._busy = True
        self.chat_status.setText("Claude 正在处理…")
        self._update_buttons()
        if not self._worker.send(prompt):
            self._busy = False
            self.chat_status.setText("发送失败")
            self._update_buttons()
            return False
        return True

    def _send_chat(self):
        text = self.chat_input.toPlainText().strip()
        if not text:
            return
        prompt = self._chat_context() + "\n\n用户要求：\n" + text
        if self._start_send(prompt, text):
            self.chat_input.clear()

    def _quote_selection(self):
        # 正在看 AI 修改预览时，引用「修改后」一栏里尚未确认的新代码；
        # 否则引用当前打开的源码文件。
        in_diff = (self._diff_panel is not None
                   and self.right_stack.currentWidget() is self._diff_panel)
        if in_diff:
            view = self.diff_panel.after_view
            path = self.diff_panel.current_path() or ""
        else:
            view = self._active_view()
            path = self._current_file or ""
        if view is None:
            self.status_label.setText("请先在右侧打开一个代码文件")
            return
        cursor = view.textCursor()
        if not cursor.hasSelection():
            self.status_label.setText(
                "请先在右侧「修改后」一栏选中一段新代码"
                if in_diff else "请先在右侧源码里选中一段代码")
            return
        start = view.document().findBlock(
            cursor.selectionStart()).blockNumber() + 1
        last_pos = max(cursor.selectionStart(), cursor.selectionEnd() - 1)
        end = view.document().findBlock(last_pos).blockNumber() + 1
        selected = cursor.selectedText().replace(" ", "\n")
        self.quote_for_chat(path, selected, start, end, pending=in_diff)

    def _quote_for_qa(self):
        """把源码或对话里的选中内容带入悬浮问答窗。"""
        text = ""
        where = "选中内容"
        suffix = ""
        active_view = self._active_view()
        cursor = active_view.textCursor() if active_view is not None else None
        if cursor is not None and cursor.hasSelection():
            selected = cursor.selectedText().replace(" ", "\n").strip()
            if selected:
                start = active_view.document().findBlock(
                    cursor.selectionStart()).blockNumber() + 1
                last_pos = max(cursor.selectionStart(), cursor.selectionEnd() - 1)
                end = active_view.document().findBlock(last_pos).blockNumber() + 1
                name = (self._display_path(self._current_file)
                        if self._current_file else "当前文件")
                where = f"{name}:{start}-{end}"
                suffix = Path(self._current_file or "").suffix.lstrip(".")
                text = selected
        if not text:
            cursor = self.chat_view.textCursor()
            if cursor.hasSelection():
                text = cursor.selectedText().replace(" ", "\n").strip()
                where = "对话内容"
        if not text:
            self.status_label.setText("请先在右侧源码或对话里选中一段内容")
            return
        # 不再弹出独立问答窗：直接把引用带入下方对话输入框。
        block = f"```{suffix}\n{text}\n```"
        if len(block) > 8000:
            block = block[:8000] + "\n…（引用已截断）\n```"
        header = f"关于这段内容（{where}）：\n{block}\n\n我的问题："
        self.chat_input.setPlainText(header)
        cursor = self.chat_input.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_input.setTextCursor(cursor)
        self.chat_input.setFocus()
        self.status_label.setText("已把选中内容带入下方对话，输入问题后发送")

    def quote_for_chat(self, path, text, start=None, end=None, pending=False):
        name = self._display_path(path) if path else "当前文件"
        where = f"{name}:{start}-{end}" if start else name
        if pending:
            where += "（AI 修改后，尚未覆盖）"
        suffix = Path(path).suffix.lstrip(".") if path else ""
        block = f"```{suffix}\n{text.strip()}\n```"
        if len(block) > 8000:
            block = block[:8000] + "\n…（引用已截断）\n```"
        header = f"关于这段代码（{where}）：\n{block}\n\n我希望："
        self.chat_input.setPlainText(header)
        cursor = self.chat_input.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_input.setTextCursor(cursor)
        self.chat_input.setFocus()
        self.show()
        self.raise_()

    def _interrupt(self):
        if self._worker is not None and self._busy:
            self._worker.interrupt()
            self.chat_status.setText("正在停止…")

    # ------------------------------------------------------------- worker --
    def _on_delta(self, chunk):
        self._live += chunk
        self._chat_dirty = True
        if not self._chat_timer.isActive():
            self._chat_timer.start()

    def _on_assistant(self, text, _tools):
        if (text or "").strip():
            self._live = ""
            self._append_message("assistant", text)

    def _on_busy(self, busy):
        self._busy = bool(busy)
        if busy:
            self.chat_status.setText("Claude 正在处理…")
        else:
            self.chat_status.setText("就绪")
        self._update_buttons()

    def _on_error(self, message):
        self._live = ""
        self._append_message("assistant", f"⚠️ {message}")
        self.chat_status.setText("出错")

    def _on_result(self, _info):
        self._live = ""
        self._merge_ai_changes()
        self._busy = False
        self.chat_status.setText("就绪")
        self._schedule_dev_record_save()
        self._render_chat()
        self._update_buttons()

    def _on_permission_detail(self, request_id, path, outside):
        self._perm_context[request_id] = (path, outside)

    def _on_permission_asked(self, request_id, tool_name, input_text):
        _path, outside = self._perm_context.pop(request_id, ("", False))
        note = ""
        if outside:
            note = ("⚠️ 该文件不在你允许修改的文件夹内。"
                    "按照设置，文件夹外的修改必须逐次申请。")
        if self._worker is None:
            return
        dialog = PermissionDialog(tool_name, input_text, self, note=note)
        allowed = dialog.exec_() == QDialog.Accepted
        self._worker.respond_permission(request_id, allowed)
        self.status_label.setText("已允许本次操作" if allowed else "已拒绝本次操作")

    # ------------------------------------------------------ AI change flow --
    def _merge_ai_changes(self):
        if self._worker is None:
            return
        pre_images = self._worker.take_dev_pre_images()
        current = dev_scan_files(self.roots)
        turn_files = self._turn_files or {}
        changed = False
        removed = False
        handled = set()
        for path, before in pre_images.items():
            handled.add(path)
            try:
                after = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if isinstance(before, str) and before == after:
                continue
            entry = self._pending.get(path)
            if entry is None:
                self._pending[path] = {"original": before, "after": after}
                changed = True
            else:
                entry["after"] = after
                changed = True
                if entry.get("after") == entry.get("original"):
                    self._pending.pop(path, None)
                    removed = True
        # Bash 等未经过写入工具拦截的改动：用本轮开始前的快照兜底
        for path, after in current.items():
            if path in handled:
                continue
            before = turn_files.get(path)
            if before == after:
                continue
            entry = self._pending.get(path)
            if entry is None:
                self._pending[path] = {"original": before, "after": after}
                changed = True
            else:
                entry["after"] = after
                changed = True
                if entry.get("after") == entry.get("original"):
                    self._pending.pop(path, None)
                    removed = True
        self._turn_files = {}
        if not changed and not removed:
            return
        self._refresh_tree_marks()
        self._refresh_viewer_marks()
        if self.mode == "all":
            # 自动覆盖模式：不弹预览、不等确认，直接写入磁盘。
            self._auto_apply_pending()
        else:
            self._show_diff_view()
        # 起点只由用户点「标记节点」决定，AI 修改后不再自动挪动。
        self._schedule_dev_record_save()

    def _auto_apply_pending(self):
        """把当前所有待确认改动直接写回磁盘，并记入 ↶ 撤回历史。"""
        history = []
        for path, entry in list(self._pending.items()):
            after = entry.get("after", "")
            try:
                before = Path(path).read_text(
                    encoding="utf-8", errors="ignore")
            except FileNotFoundError:
                before = None
            except OSError:
                before = entry.get("original")
            try:
                Path(path).write_text(after, encoding="utf-8")
            except OSError as exc:
                self.status_label.setText(f"自动覆盖失败：{exc}")
                continue
            history.append((path, before, after))
            self._pending.pop(path, None)
        self._push_file_history(history)
        if history:
            self._after_bulk_change(f"已自动覆盖 {len(history)} 个文件")
        else:
            self._hide_diff_view()
        self._update_buttons()

    def _show_diff_view(self):
        """在开发页内显示改前/改后对比，并暂时收起代码目录栏。"""
        if not self._pending:
            self.status_label.setText("当前没有待确认的修改")
            self._hide_diff_view()
            return
        self.diff_panel.refresh()
        self.file_row_widget.setVisible(False)
        self.right_stack.setCurrentWidget(self.diff_panel)
        if not self.isVisible():
            self.reveal_requested.emit()

    def _hide_diff_view(self):
        """收起对比，恢复代码目录和编辑器。"""
        if self._diff_panel is not None:
            self.right_stack.setCurrentWidget(self.right_splitter)
        self.file_row_widget.setVisible(True)

    def _store_diff_editor(self):
        if self._diff_panel is not None:
            self._diff_panel.store_editor()

    def confirm_current_change(self):
        self._store_diff_editor()
        path = (self._diff_panel.current_path()
                if self._diff_panel is not None else None)
        if path is None:
            return
        entry = self._pending.get(path)
        if entry is None:
            return
        after = entry.get("after", "")
        try:
            before = Path(path).read_text(encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            before = None
        except OSError:
            before = entry.get("original")
        try:
            Path(path).write_text(after, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "确认修改", f"写入失败：{exc}")
            return
        self._push_file_history([(path, before, after)])
        self._pending.pop(path, None)
        self._after_change_removed(path, f"已确认并覆盖：{self._display_path(path)}")

    def cancel_current_change(self):
        path = (self._diff_panel.current_path()
                if self._diff_panel is not None else None)
        if path is None:
            return
        entry = self._pending.get(path)
        if entry is None:
            return
        original = entry.get("original")
        try:
            if original is None:
                Path(path).unlink(missing_ok=True)
            else:
                Path(path).write_text(original, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "取消修改", f"还原失败：{exc}")
            return
        self._pending.pop(path, None)
        self._after_change_removed(path, f"已还原：{self._display_path(path)}")

    def confirm_all_changes(self):
        self._store_diff_editor()
        paths = list(self._pending)
        history = []
        for path in paths:
            entry = self._pending.get(path)
            if entry is None:
                continue
            after = entry.get("after", "")
            try:
                before = Path(path).read_text(encoding="utf-8", errors="ignore")
            except FileNotFoundError:
                before = None
            except OSError:
                before = entry.get("original")
            try:
                Path(path).write_text(after, encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "确认修改", f"{path}\n{exc}")
                continue
            history.append((path, before, after))
            self._pending.pop(path, None)
        self._push_file_history(history)
        self._after_bulk_change(f"已确认 {len(paths)} 个文件的修改")

    def cancel_all_changes(self):
        paths = list(self._pending)
        for path in paths:
            entry = self._pending.get(path)
            if entry is None:
                continue
            original = entry.get("original")
            try:
                if original is None:
                    Path(path).unlink(missing_ok=True)
                else:
                    Path(path).write_text(original, encoding="utf-8")
            except OSError:
                continue
            self._pending.pop(path, None)
        self._after_bulk_change(f"已还原 {len(paths)} 个文件的修改")

    def _after_change_removed(self, path, message):
        self.status_label.setText(message)
        self._refresh_tree_marks()
        if self._entry_for_path(path) is not None:
            if Path(path).exists():
                self._reload_file_tab(path)
            else:
                self._close_tab_for_path(path)
        if self._pending:
            self.diff_panel.refresh()
        else:
            self._hide_diff_view()
        self._schedule_dev_record_save()
        self._update_buttons()
        self.status_label.setText(message)

    def _after_bulk_change(self, message):
        self.status_label.setText(message)
        self._refresh_tree_marks()
        for entry in list(self._code_tabs):
            path = str(entry["path"])
            if Path(path).exists():
                self._reload_file_tab(path)
            else:
                self._close_tab_for_path(path)
        self._hide_diff_view()
        self._schedule_dev_record_save()
        self._update_buttons()
        self.status_label.setText(message)

    # --------------------------------------------------- my-code snapshot --
    def _load_state(self):
        if not DEV_SNAPSHOT_PATH.is_file():
            return
        try:
            data = json.loads(DEV_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if list(data.get("roots") or []) != self.roots:
            return
        files = data.get("files")
        if isinstance(files, dict):
            self._snapshot = {
                str(k): str(v) for k, v in files.items()
                if isinstance(k, str) and isinstance(v, str)}
            self._snapshot_roots = list(self.roots)

    def _save_snapshot(self, files=None):
        if not self.roots:
            return
        if files is None:
            files = dev_scan_files(self.roots)
        data = {
            "roots": list(self.roots),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "files": files,
        }
        try:
            DEV_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            DEV_SNAPSHOT_PATH.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            return
        self._snapshot = files
        self._snapshot_roots = list(self.roots)
        self._refresh_viewer_marks()

    def _snapshot_diff(self, current):
        added, changed, removed = [], [], []
        parts = []
        for path in sorted(current):
            old = self._snapshot.get(path)
            if old is None:
                added.append(path)
                body = current[path]
                if len(body) > 4000:
                    body = body[:4000] + "\n…（已截断）"
                parts.append(f"### 新文件 {self._display_path(path)}\n{body}")
            elif old != current[path]:
                changed.append(path)
                parts.append(
                    f"### 改动文件 {self._display_path(path)}\n" +
                    dev_unified_diff(old, current[path],
                                     self._display_path(path)))
        for path in sorted(self._snapshot):
            if path not in current:
                removed.append(path)
                parts.append(f"### 删除文件 {self._display_path(path)}")
        return parts, added, changed, removed

    def _mark_new_code_start(self):
        """手动记录「新代码起点」：此后的改动才会被标 ❗ 和送去检查。"""
        if not self.roots:
            QMessageBox.information(
                self, "标记节点", "请先添加代码文件夹。")
            return
        # 先把编辑器里未落盘的内容写回磁盘，保证节点记录的是当前代码。
        if any(e["view"].document().isModified() for e in self._code_tabs):
            self._autosave_current()
        self._save_snapshot()
        self._refresh_tree_marks()
        self._schedule_dev_record_save()
        self.status_label.setText(
            f"已标记新代码起点 · {datetime.now():%H:%M:%S}")

    def _check_my_code(self):
        if self._busy:
            return
        if not self.roots:
            QMessageBox.information(
                self, "检查我的新代码", "请先添加代码文件夹。")
            return
        # 自动保存后再扫描，避免刚改完的代码还没落盘就被漏掉。
        if any(e["view"].document().isModified() for e in self._code_tabs):
            self._autosave_current()
        current = dev_scan_files(self.roots)
        if not self._snapshot:
            self.status_label.setText(
                "还没有标记新代码起点，请先点上方「标记节点」")
            QMessageBox.information(
                self, "检查我的新代码",
                "还没有标记新代码起点。\n\n"
                "请先点顶部「标记节点」记录当前代码，"
                "之后再改动的内容才会被检查。")
            return
        parts, added, changed, removed = self._snapshot_diff(current)
        if not parts:
            self.status_label.setText(
                "标记节点之后没有检测到你的新改动")
            return
        body = "\n\n".join(parts)
        if len(body) > 26000:
            body = body[:26000] + "\n…（检查内容已截断）"
        prompt = (
            "【代码检查（只读）】\n"
            "下面是用户在「标记节点」之后自己写的新代码改动。\n"
            "请检查这些改动有没有问题：逻辑错误、边界条件、异常处理、"
            "并发/资源泄漏、命名与可维护性、与现有接口不一致等。\n"
            "不要修改文件，不要调用 Write/Edit/Bash；只输出问题清单。\n"
            "每条问题给出：文件与行号、严重程度（高/中/低）、原因、修改建议。"
            "如果没有明显问题，也请明确说明。\n\n"
            f"<<<\n{body}\n>>>")
        display = (f"检查我的新代码：{len(changed)} 个修改文件、"
                   f"{len(added)} 个新文件、{len(removed)} 个删除文件")
        # 检查不会自动挪动起点；需要下次从这里重新算时，手动点「标记节点」。
        self._start_send(prompt, display, deny_writes=True)

    # ------------------------------------------------------------- QA window --
    def _show_qa_window(self):
        window = self._qa_window
        if window is None:
            self._build_qa_window()
            window = self._qa_window
        if not window.isVisible():
            anchor = self.window().frameGeometry()
            window.move(
                max(anchor.left() + 40, anchor.right() - window.width() - 40),
                anchor.top() + 80)
        window.show()
        window.raise_()
        window.activateWindow()

    def _build_qa_window(self):
        window = DevQaWindow()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("开发问答")
        title.setObjectName("H2")
        recall = QLabel("Ctrl+Shift+E 召回；发送后对话同步到主页面")
        recall.setObjectName("Muted")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(recall)
        layout.addLayout(head)

        self._qa_view = QTextBrowser()
        self._qa_view.setStyleSheet(
            "QTextBrowser { background: #fffafc; border: 1px solid #f6d9e6;"
            " border-radius: 8px; padding: 8px; color: #4b3742; }")
        self._qa_view.setPlaceholderText(
            "可以在这里直接提问，也可以先选中一段代码再问")
        layout.addWidget(self._qa_view, 1)

        self._qa_input = ChatInput()
        self._qa_input.setObjectName("ChatInput")
        self._qa_input.setFixedHeight(84)
        self._qa_input.setPlaceholderText("Enter 发送，Shift+Enter 换行")
        self._qa_input.submitted.connect(self._send_qa)
        layout.addWidget(self._qa_input)

        bottom = QHBoxLayout()
        self._qa_status = QLabel("")
        self._qa_status.setObjectName("Muted")
        bottom.addWidget(self._qa_status, 1)
        self._qa_send = QPushButton("发送")
        self._qa_send.setObjectName("Primary")
        self._qa_send.clicked.connect(self._send_qa)
        bottom.addWidget(self._qa_send)
        layout.addLayout(bottom)

        window.setCentralWidget(container)
        self._qa_window = window
        self._render_qa_chat()
        self._update_buttons()

    def _qa_context(self):
        lines = ["【代码开发 · 独立问答（只读）】", "可访问的文件夹："]
        lines.extend(f"- {root}" for root in self.roots)
        if self._current_file:
            lines.append(f"当前文件：{self._current_file}")
        lines.append(
            "这是一个与主开发对话相互独立的问答会话。"
            "不要修改文件，不要调用 Write/Edit/Bash；只根据代码回答问题。")
        return "\n".join(lines)

    def _ensure_qa_worker(self):
        if self._qa_worker is None:
            worker = ClaudeWorker(self)
            worker.text_delta.connect(self._on_qa_delta)
            worker.assistant.connect(self._on_qa_assistant)
            worker.result.connect(self._on_qa_result)
            worker.error.connect(self._on_qa_error)
            worker.busy_changed.connect(self._on_qa_busy)
            self._qa_worker = worker
            worker.start()
        self._qa_worker.set_dev_policy(self.roots, self.mode)
        root = self.roots[0] if self.roots else ""
        cfg = (root, self.model, self.effort)
        if self._qa_worker_cfg != cfg:
            self._qa_worker.configure(
                root, "default", model=self.model, effort=self.effort)
            self._qa_worker_cfg = cfg
            self._qa_worker.set_dev_policy(self.roots, self.mode)
        return self._qa_worker

    def _on_qa_delta(self, chunk):
        self._qa_live += chunk
        self._qa_dirty = True
        if not self._qa_timer.isActive():
            self._qa_timer.start()

    def _on_qa_assistant(self, text, _tools):
        if (text or "").strip():
            self._qa_live = ""
            self._append_qa_message("assistant", text)

    def _on_qa_result(self, _info):
        self._qa_live = ""
        self._qa_busy = False
        self._qa_status.setText("就绪")
        self._render_qa_chat()
        self._update_buttons()

    def _on_qa_error(self, message):
        self._qa_live = ""
        self._append_qa_message("assistant", f"⚠️ {message}")
        self._qa_status.setText("出错")

    def _on_qa_busy(self, busy):
        self._qa_busy = bool(busy)
        self._qa_status.setText("Claude 正在回答…" if busy else "就绪")
        self._update_buttons()

    def _send_qa(self):
        text = self._qa_input.toPlainText().strip()
        if not text or self._qa_busy:
            return
        if not self.roots:
            QMessageBox.information(
                self, "开发问答", "请先添加至少一个允许访问的代码文件夹。")
            return
        if not SDK_AVAILABLE:
            QMessageBox.warning(
                self, "缺少对话组件",
                f"未安装 Claude Agent SDK，无法在界面内提问。\n\n{SDK_IMPORT_ERROR}")
            return
        worker = self._ensure_qa_worker()
        worker.begin_dev_turn(deny_writes=True)
        self._append_qa_message("user", text)
        self._qa_live = ""
        self._qa_busy = True
        self._qa_status.setText("Claude 正在回答…")
        self._update_buttons()
        self._qa_input.clear()
        prompt = self._qa_context() + "\n\n用户问题：\n" + text
        if not worker.send(prompt):
            self._qa_busy = False
            self._qa_status.setText("发送失败")
            self._update_buttons()

    # ------------------------------------------------------------- shutdown --
    def shutdown(self):
        timer = getattr(self, "_chat_timer", None)
        if timer is not None:
            timer.stop()
        qa_timer = getattr(self, "_qa_timer", None)
        if qa_timer is not None:
            qa_timer.stop()
        worker = self._worker
        if worker is not None:
            worker.shutdown()
            self._worker = None
        qa_worker = self._qa_worker
        if qa_worker is not None:
            qa_worker.shutdown()
            self._qa_worker = None
        if self._qa_window is not None:
            self._qa_window.close()
            self._qa_window.deleteLater()
        self._qa_window = None
        self._qa_view = None
        self._diff_panel = None




class PermissionDialog(QDialog):
    def __init__(self, tool_name, input_text, parent=None, note=""):
        super().__init__(parent)
        self.setWindowTitle("权限确认")
        self.setModal(True)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        title = QLabel("Claude 请求执行一个操作")
        title.setObjectName("H1")
        layout.addWidget(title)

        if note:
            note_label = QLabel(note)
            note_label.setWordWrap(True)
            note_label.setStyleSheet(
                "QLabel { background: #fff4e5; border: 1px solid #f3c98b;"
                " border-radius: 8px; padding: 8px; color: #8a5a13; }")
            layout.addWidget(note_label)

        label = TOOL_LABELS.get(tool_name, tool_name)
        detail = QLabel(f"工具：{label}（{tool_name}）")
        detail.setObjectName("H2")
        layout.addWidget(detail)

        editor = QPlainTextEdit()
        editor.setObjectName("PermissionInput")
        editor.setReadOnly(True)
        editor.setPlainText(input_text)
        editor.setMaximumHeight(280)
        editor.setMinimumHeight(120)
        layout.addWidget(editor)

        hint = QLabel("允许后将按上述参数执行；拒绝则 Claude 会收到拒绝结果。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_deny = QPushButton("拒绝")
        btn_deny.clicked.connect(self.reject)
        btn_allow = QPushButton("允许")
        btn_allow.setObjectName("Primary")
        btn_allow.clicked.connect(self.accept)
        btn_deny.setDefault(True)
        buttons.addWidget(btn_deny)
        buttons.addWidget(btn_allow)
        layout.addLayout(buttons)


def make_app_icon(size=256):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    margin = size * 0.06
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawRoundedRect(int(margin), int(margin),
                            int(size - 2 * margin), int(size - 2 * margin),
                            size * 0.22, size * 0.22)
    font = QFont("DejaVu Sans Mono")
    font.setPixelSize(int(size * 0.36))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(pm.rect(), Qt.AlignCenter, ">_")
    painter.end()
    return QIcon(pm)


class WorkerThread(QThread):
    """在后台执行耗时操作（下载技能、安装插件等）。"""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, func, *args, parent=None, progress_arg=False,
                 cancel_arg=False):
        super().__init__(parent)
        self._func = func
        self._args = args
        self._progress_arg = progress_arg
        self._cancel_arg = cancel_arg
        self._cancel = threading.Event()

    def request_cancel(self):
        self._cancel.set()

    def is_cancelled(self):
        return self._cancel.is_set()

    def _report(self, done, total, stage=""):
        self.progress.emit(int(done), int(total), str(stage))

    def run(self):
        kwargs = {}
        if self._progress_arg:
            kwargs["progress"] = self._report
        if self._cancel_arg:
            kwargs["should_cancel"] = self._cancel.is_set
        try:
            self.succeeded.emit(self._func(*self._args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - 错误交给界面提示
            if self._cancel.is_set():
                self.failed.emit("操作已取消")
            else:
                self.failed.emit(f"{type(exc).__name__}: {exc}")


class TaskProgressDialog(QDialog):
    """下载/安装任务的进度窗口：下载阶段显示真实百分比，其余阶段显示步骤。"""

    canceled = pyqtSignal()

    def __init__(self, title, heading, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        self.heading = QLabel(heading)
        self.heading.setWordWrap(True)
        self.heading.setObjectName("H2")
        layout.addWidget(self.heading)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # 0/0 = 不确定进度的忙碌条纹
        self.bar.setTextVisible(True)
        self.bar.setMinimumHeight(22)
        layout.addWidget(self.bar)

        self.detail = QLabel("准备中…")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self._request_cancel)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

        self._canceling = False
        self._last_percent = 0

    def _request_cancel(self):
        if self._canceling:
            return
        self._canceling = True
        self.cancel_btn.setEnabled(False)
        self.detail.setText("正在取消…")
        self.canceled.emit()

    def set_progress(self, done, total, stage=""):
        if self._canceling:
            return
        stage = stage or "处理中"
        if "下载" in stage and total > 0:
            ratio = max(0.0, min(1.0, done / total))
            percent = int(5 + ratio * 80)
            if total >= 1024:
                self.detail.setText(
                    f"{stage}：{done / 1048576:.1f} MB / "
                    f"{total / 1048576:.1f} MB")
            else:
                self.detail.setText(f"{stage}：{done} / {total} 字节")
        elif "下载" in stage:
            # 服务器没给 Content-Length：显示忙碌条纹和已下载体积
            self.bar.setRange(0, 0)
            self.detail.setText(f"{stage}：{done / 1048576:.1f} MB")
            return
        elif "解压" in stage:
            percent = 88
            self.detail.setText(stage + "…")
        elif "安装技能" in stage:
            ratio = 1.0 if total <= 0 else done / total
            percent = int(90 + max(0.0, min(1.0, ratio)) * 10)
            self.detail.setText(f"{stage}（{done}/{total}）")
        elif "完成" in stage:
            percent = 100
            self.detail.setText(stage)
        else:
            percent = 100 if total > 0 and done >= total else 50
            self.detail.setText(stage)
        self._last_percent = max(self._last_percent, min(100, percent))
        self.bar.setRange(0, 100)
        self.bar.setValue(self._last_percent)

    def set_busy(self, text):
        if self._canceling:
            return
        self.bar.setRange(0, 0)
        self.detail.setText(text)

    def mark_done(self):
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.detail.setText("完成")
        QTimer.singleShot(150, self.accept)


class CodexSessionDialog(QDialog):
    """预览 Codex 会话内容；可转到终端用 codex resume 继续。"""

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.resume_requested = False
        self.setWindowTitle("Codex 会话")
        self.setMinimumSize(680, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel(entry.get("title") or "Codex 会话")
        title.setObjectName("H1")
        title.setWordWrap(True)
        layout.addWidget(title)

        cwd = entry.get("cwd") or "（未记录目录）"
        meta = QLabel(f"目录：{cwd}\n会话 ID：{entry.get('sid', '')}")
        meta.setObjectName("Muted")
        meta.setWordWrap(True)
        meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(meta)

        viewer = QPlainTextEdit()
        viewer.setObjectName("PermissionInput")
        viewer.setReadOnly(True)
        messages = shared_agents.load_codex_messages(entry["path"], limit=60)
        if messages:
            lines = []
            for role, text in messages:
                who = "你" if role == "user" else "Codex"
                body = text if len(text) <= 4000 else text[:4000] + "…"
                lines.append(f"【{who}】\n{body}")
            viewer.setPlainText("\n\n".join(lines))
        else:
            viewer.setPlainText("（未能解析出文本消息，可在终端中继续查看）")
        layout.addWidget(viewer, 1)

        buttons = QHBoxLayout()
        btn_copy = QPushButton("复制会话 ID")
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(
            entry.get("sid", "")))
        btn_term = QPushButton("在终端中继续")
        btn_term.setObjectName("Primary")
        btn_term.clicked.connect(self._accept_resume)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        buttons.addWidget(btn_copy)
        buttons.addStretch(1)
        buttons.addWidget(btn_close)
        buttons.addWidget(btn_term)
        layout.addLayout(buttons)

    def _accept_resume(self):
        self.resume_requested = True
        self.accept()


class PluginPickerDialog(QDialog):
    """从市场列表里搜索并选择要安装的插件。"""

    MAX_RESULTS = 300

    def __init__(self, items, title="安装插件", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(720, 560)
        self._items = sorted(
            items,
            key=lambda item: (-int(item.get("installCount") or 0),
                              str(item.get("name") or "").lower()))
        self._selected = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(9)

        heading = QLabel(title)
        heading.setObjectName("H1")
        layout.addWidget(heading)
        hint = QLabel("按名称或简介搜索；双击或选中后点「安装」。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索插件…")
        self.search.textChanged.connect(self._populate)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.currentItemChanged.connect(self._show_detail)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)

        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(52)
        layout.addWidget(self.detail)

        buttons = QHBoxLayout()
        self.count_label = QLabel("")
        self.count_label.setObjectName("Muted")
        buttons.addWidget(self.count_label)
        buttons.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_install = QPushButton("安装")
        btn_install.setObjectName("Primary")
        btn_install.clicked.connect(self.accept)
        buttons.addWidget(btn_cancel)
        buttons.addWidget(btn_install)
        layout.addLayout(buttons)

        self._populate()

    def _populate(self):
        query = self.search.text().strip().lower()
        self.list.clear()
        shown = 0
        total = 0
        for item in self._items:
            haystack = " ".join(str(item.get(key) or "") for key in
                                ("name", "pluginId", "description",
                                 "marketplaceName")).lower()
            if query and query not in haystack:
                continue
            total += 1
            if shown >= self.MAX_RESULTS:
                continue
            shown += 1
            widget_item = QListWidgetItem()
            widget_item.setData(Qt.UserRole, item)
            widget_item.setSizeHint(QSize(0, 54))
            marketplace = item.get("marketplaceName") or ""
            subtitle = marketplace
            install_count = item.get("installCount")
            if install_count:
                subtitle += f" · {install_count} 次安装"
            if item.get("enabled") or item.get("installed"):
                subtitle += " · 已安装"
            self.list.addItem(widget_item)
            self.list.setItemWidget(widget_item, ListItemWidget(
                item.get("name") or item.get("pluginId", ""), subtitle))
        if shown:
            self.list.setCurrentRow(0)
        else:
            self.detail.setText("没有匹配的插件。")
        suffix = f"（仅显示前 {self.MAX_RESULTS} 个）" if total > shown else ""
        self.count_label.setText(f"匹配 {total} 个{suffix}")

    def _show_detail(self, current, _previous):
        if current is None:
            self.detail.setText("")
            return
        item = current.data(Qt.UserRole) or {}
        description = " ".join(str(item.get("description") or "").split())
        self.detail.setText(description[:500] or "（没有简介）")

    def accept(self):
        current = self.list.currentItem()
        if current is None:
            QMessageBox.information(self, "安装插件", "请先选择一个插件。")
            return
        self._selected = current.data(Qt.UserRole)
        super().accept()

    def selected_plugin_id(self):
        if not self._selected:
            return None
        return self._selected.get("pluginId") or self._selected.get("name")


class ProgressPopup(QFrame):
    """「进度查询」按钮上方的小浮窗，点击浮窗以外的任意位置关闭。"""

    def __init__(self, parent=None):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setObjectName("ProgressPopup")
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._anchor = None
        self._filtering = False
        self._label = QLabel(self)
        self._label.setTextFormat(Qt.RichText)
        self._label.setWordWrap(True)
        self._label.setMinimumWidth(300)
        self._label.setMaximumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(self._label)
        self.setStyleSheet(
            "QFrame#ProgressPopup { background-color: #ffffff;"
            " border: 1px solid #f0c9da; border-radius: 10px; }"
            "QFrame#ProgressPopup QLabel { color: #5a3f4b; font-size: 12px;"
            " background: transparent; }")

    def show_for(self, anchor: QWidget, markup: str) -> None:
        self._anchor = anchor
        self._label.setText(markup)
        self.adjustSize()
        pos = anchor.mapToGlobal(QPoint(anchor.width() // 2, 0))
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        x = pos.x() - self.width() // 2
        y = pos.y() - self.height() - 8
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 8,
                    min(x, area.right() - self.width() - 8))
            if y < area.top() + 8:
                # 上方放不下时改到按钮下方
                y = anchor.mapToGlobal(
                    QPoint(anchor.width() // 2, anchor.height())).y() + 8
            y = max(area.top() + 8,
                    min(y, area.bottom() - self.height() - 8))
        self.move(x, y)
        self.show()
        self.raise_()
        self._attach_filter()

    def _attach_filter(self) -> None:
        app = QApplication.instance()
        if app is not None and not self._filtering:
            app.installEventFilter(self)
            self._filtering = True

    def _detach_filter(self) -> None:
        if self._filtering:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._filtering = False

    def _is_inside_anchor(self, global_pos: QPoint) -> bool:
        anchor = self._anchor
        if anchor is None or not anchor.isVisible():
            return False
        rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        return rect.contains(global_pos)

    def eventFilter(self, obj, event):
        if self.isVisible():
            etype = event.type()
            if etype == QEvent.MouseButtonPress:
                # 兼容 Qt5/Qt6 的全局坐标接口
                global_pos = event.globalPos() if hasattr(event, "globalPos") \
                    else event.globalPosition().toPoint()
                if not self.geometry().contains(global_pos) \
                        and not self._is_inside_anchor(global_pos):
                    self.hide()
            elif etype == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        self._detach_filter()
        super().hideEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(make_app_icon())
        self.config = load_config()
        self.current_project = None
        self.sessions = []
        self._claude_proc = None
        # 每个项目一份对话状态：切走后后台会话继续运行，切回来原样恢复。
        self._chat_states = {}

        saved_mode = self.config.get("permission_mode", "default")
        self._permission_mode = saved_mode
        self._last_perm_index = 0
        self._model = self.config.get("model") or default_model_choice()
        self._effort = self.config.get("effort") or "high"
        if not isinstance(self._effort, str) or not self._effort.strip():
            self._effort = "high"
        # 双 AI 协作状态：主/从模型、工作档次、余额查询线程
        self._dual_master = dual_ai.RoleSpec.from_dict(
            self.config.get("dual_master"), "claude")
        self._dual_worker = dual_ai.RoleSpec.from_dict(
            self.config.get("dual_worker"), "codex")
        # 旧配置只有占比；一次性换算成档次，之后不再按比例调度。
        tier_value = self.config.get("dual_tier")
        if not tier_value:
            tier_value = self.config.get("dual_share", 30)
        self._dual_tier = dual_ai.normalize_tier(tier_value)
        if self.config.get("dual_tier") != self._dual_tier:
            self.config["dual_tier"] = self._dual_tier
            save_config(self.config)
        self._balance_workers = []
        self._balance_cache = {}
        # 正在查询的余额键 -> 开始时间；用于去重和超时提示，
        # 避免运行任务时反复点「↻」堆积请求、界面一直停在“查询中…”。
        self._balance_pending = {}
        self._balance_timer = QTimer(self)
        self._balance_timer.setInterval(5 * 60 * 1000)
        # 定时刷新允许命中 5 分钟缓存；手动「↻」仍强制查询。
        self._balance_timer.timeout.connect(
            lambda: self._refresh_balances(force=False))
        self._balance_watch = QTimer(self)
        self._balance_watch.setInterval(1000)
        self._balance_watch.timeout.connect(self._check_balance_timeouts)
        self._balance_watch.start()
        # 从服务商自动获取的模型目录：{模型 ID: {name, efforts, ...}}
        self._model_catalog = {m["id"]: dict(m) for m in FALLBACK_MODELS}
        self._catalog_thread = None
        self._catalog_loaded = False
        self._pending_catalog = None
        self._settings_mtime = provider_settings_mtime()
        self._provider_sig = provider_signature()
        self._chat_cwd = None
        self._chat_resume_id = None
        self._chat_session_id = None
        self._needs_reconfigure = True
        self._chat_busy = False
        self._study_pending = False
        self._live_wrapper = None
        self._live_bubble = None
        self._stream_text = ""
        self._stream_dirty = False
        self._closing = False
        self._chat_empty = None
        # 切换项目后的定位：渲染代次隔离过期滚动回调；批量重建期间
        # 抑制逐项滚到底，等布局稳定后把最近一轮用户提问顶到视口顶部。
        self._chat_render_gen = 0
        self._chat_bulk_render = False
        self._chat_last_wrapper = None
        self._chat_last_user_wrapper = None
        # 显式滚到底的序号：用于判断切换定位是否已被新的聊天滚动取代。
        self._chat_scroll_seq = 0
        # 对话页不在前台时暂存定位，切回对话页再应用。
        self._pending_chat_anchor = None
        self._plugin_worker = None
        self._skill_worker = None
        self._plugins_loaded = False
        self._skills = []
        self._skill_filter = "all"
        self._quote_target = None
        self._chat_media_worker = None
        self._chat_media_dialog = None
        self._chat_media_busy = False
        # 主对话里的「选中内容问答」：独立悬浮窗 + 独立会话
        self._chat_bubbles = []
        self._chat_selection = ("", "")
        self._chat_qa_worker = None
        self._chat_qa_worker_cfg = None
        self._chat_qa_busy = False
        self._chat_qa_messages = []
        self._chat_qa_next_id = 1
        self._chat_qa_answer_id = None
        self._chat_qa_live = ""
        self._chat_qa_pending_quote = ""
        # 每个项目独立的研读模式与研读页内容（解释、问答、配图等）
        self._study_states = {}
        self._study_state_path = None
        # 每个项目自己的一份研读页；独立窗口也按项目各自持有。
        # key 为项目路径（未选择项目时是空字符串）。
        self._study_pages = {}
        self._study_windows = {}
        self._pending_study_record = None
        # 每个项目自己的一份代码开发页；独立窗口也按项目各自持有。
        # key 为项目路径（未选择项目时是空字符串）。
        self._dev_pages = {}
        self._dev_windows = {}

        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(60)
        self._stream_timer.timeout.connect(self._flush_stream)
        # 双 AI 流式输出按 80ms 合并刷新，避免每个 token 都重排气泡导致界面卡顿。
        self._dual_stream_timer = QTimer(self)
        self._dual_stream_timer.setInterval(80)
        self._dual_stream_timer.timeout.connect(self._flush_dual_stream)
        self._chat_qa_timer = QTimer(self)
        self._chat_qa_timer.setInterval(80)
        self._chat_qa_timer.timeout.connect(self._flush_chat_qa_live)

        # 进度浮窗：每秒刷新一次，读取真实后台进程的进度/网速/ETA
        self._progress_refresh_timer = QTimer(self)
        self._progress_refresh_timer.setInterval(1000)
        self._progress_refresh_timer.timeout.connect(self._refresh_progress_popup)
        self._progress_refresh_timer.start()
        # 后台任务完成提醒：记录已见过的任务，消失时提示"已完成"
        self._bg_seen_tasks = {}
        self._bg_watch_timer = QTimer(self)
        self._bg_watch_timer.setInterval(1000)
        self._bg_watch_timer.timeout.connect(self._watch_background_tasks)
        self._bg_watch_timer.start()

        self._build_ui()
        self._connect_worker()
        self._populate_projects()
        self._refresh_favorites()
        self._restore_geometry()
        self._probe_claude()
        try:
            shared_agents.ensure_shared_layout()
        except OSError:
            pass
        self._refresh_skills()

        QTimer.singleShot(0, self._select_first_project)
        QTimer.singleShot(200, self._refresh_model_catalog)

    # ------------------------------------------------- per-project chat state --
    def _new_chat_state(self, path):
        """一个项目自己的一份对话：会话进程、消息记录、流式文本和状态。"""
        return {
            "path": path or "",
            "worker": None,
            "blocks": [],       # [{"kind": "user"/"assistant"/"note"/"tool", ...}]
            "live": "",         # 正在流式输出的文本
            "busy": False,
            "status": "就绪" if path else "未选择项目",
            "resume_id": None,
            "session_id": None,
            "cwd": None,
            "needs_reconfigure": True,
            "study_pending": False,
            "study_goal": "",   # 后台研读请求的目标，回答归档时用
            "tab": "chat",      # 该项目上次停留的主页签（对话/研读/历史…）
        }

    def _chat_state(self, path=None):
        states = getattr(self, "_chat_states", None)
        if states is None:
            states = {}
            self._chat_states = states
        if path is None:
            path = getattr(self, "current_project", None)
        key = path or ""
        state = states.get(key)
        if state is None:
            state = self._new_chat_state(key)
            states[key] = state
        return state

    def _is_active_chat_state(self, state):
        return state is self._chat_state()

    # ------------------------------------------- per-project study page state --
    def _study_state(self, path):
        states = getattr(self, "_study_states", None)
        if states is None:
            states = {}
            self._study_states = states
        key = path or ""
        state = states.get(key)
        if state is None:
            state = {"mode": False, "snapshot": None}
            states[key] = state
        return state

    def _study_page_for(self, path):
        """取（或创建）某个项目自己的研读页。

        每个项目一张页，独立窗口也挂在对应的页上，所以切换项目时
        已弹出的窗口不会被别的项目覆盖。
        """
        key = path or ""
        page = self._study_pages.get(key)
        if page is not None:
            return page
        page = CodeStudyPage()
        page.project_key = key
        page.float_requested.connect(
            lambda p=page: self._toggle_study_float_for(p))
        page.reveal_requested.connect(
            lambda p=page: self._show_study_page_for(p))
        page.project_requested.connect(self._select_project_path)
        page.load_record_requested.connect(self._on_study_record_requested)
        if key:
            page.set_project(key)
        self._study_pages[key] = page
        return page

    def _dev_page_for(self, path):
        """取（或创建）某个项目自己的代码开发页。

        每个项目一张页、一个独立窗口，切换项目时互不覆盖。
        """
        key = path or ""
        page = self._dev_pages.get(key)
        if page is not None:
            return page
        page = CodeDevPage(self.config)
        page.project_key = key
        page.set_runtime(self._current_model(), self._current_effort())
        page.float_requested.connect(
            lambda p=page: self._toggle_dev_float_for(p))
        page.reveal_requested.connect(
            lambda p=page: self._show_dev_page_for(p))
        self._dev_pages[key] = page
        return page

    def _dev_page_path(self, page):
        return getattr(page, "project_key", "") if page is not None else ""

    def _capture_study_state(self, path):
        """切走项目前，把该项目研读页当前内容与模式存到它自己名下。"""
        page = self._study_pages.get(path or "")
        if page is None:
            return
        state = self._study_state(path)
        state["snapshot"] = page.capture_state()
        if (path or "") == (getattr(self, "current_project", None) or ""):
            state["mode"] = self.btn_study_mode.isChecked()

    @property
    def _chat_cwd(self):
        return self._chat_state()["cwd"]

    @_chat_cwd.setter
    def _chat_cwd(self, value):
        self._chat_state()["cwd"] = value

    @property
    def _chat_resume_id(self):
        return self._chat_state()["resume_id"]

    @_chat_resume_id.setter
    def _chat_resume_id(self, value):
        self._chat_state()["resume_id"] = value

    @property
    def _chat_session_id(self):
        return self._chat_state()["session_id"]

    @_chat_session_id.setter
    def _chat_session_id(self, value):
        self._chat_state()["session_id"] = value

    @property
    def _needs_reconfigure(self):
        return self._chat_state()["needs_reconfigure"]

    @_needs_reconfigure.setter
    def _needs_reconfigure(self, value):
        self._chat_state()["needs_reconfigure"] = bool(value)

    @property
    def _chat_busy(self):
        return bool(self._chat_state()["busy"])

    @_chat_busy.setter
    def _chat_busy(self, value):
        self._chat_state()["busy"] = bool(value)

    @property
    def _study_pending(self):
        return bool(self._chat_state()["study_pending"])

    @_study_pending.setter
    def _study_pending(self, value):
        self._chat_state()["study_pending"] = bool(value)

    @property
    def worker(self):
        """当前项目自己的 Claude 会话工作线程。"""
        return self._ensure_worker_for(self._chat_state())

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        self.setMinimumSize(960, 660)
        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, central)
        splitter.setHandleWidth(5)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter)

        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_content())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([250, 910])

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setMinimumWidth(210)
        sidebar.setMaximumWidth(340)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 16, 10, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(8, 0, 4, 0)
        title = QLabel("项目")
        title.setObjectName("H2")
        header.addWidget(title)
        header.addStretch(1)
        self.btn_add_project = QToolButton()
        self.btn_add_project.setText("+")
        self.btn_add_project.setObjectName("Small")
        self.btn_add_project.setToolTip("添加项目目录")
        self.btn_add_project.clicked.connect(self._add_project)
        header.addWidget(self.btn_add_project)
        self.btn_remove_project = QToolButton()
        self.btn_remove_project.setText("−")
        self.btn_remove_project.setObjectName("Small")
        self.btn_remove_project.setToolTip("从列表移除选中项目")
        self.btn_remove_project.clicked.connect(self._remove_project)
        header.addWidget(self.btn_remove_project)
        layout.addLayout(header)

        self.project_list = QListWidget()
        self.project_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.project_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.project_list.currentItemChanged.connect(self._on_project_changed)
        self.project_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(self._project_menu)
        layout.addWidget(self.project_list, 1)

        self.version_label = QLabel("正在检测 Claude Code…")
        self.version_label.setObjectName("Muted")
        self.version_label.setContentsMargins(8, 0, 8, 0)
        self.version_label.setWordWrap(True)
        layout.addWidget(self.version_label)
        return sidebar

    def _build_content(self):
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(12)

        self.header_name = QLabel("未选择项目")
        self.header_name.setObjectName("H1")
        self.header_path = QLabel("在左侧添加或选择一个项目目录")
        self.header_path.setObjectName("Muted")
        self.header_path.setTextInteractionFlags(Qt.TextSelectableByMouse)

        header_actions = QHBoxLayout()
        header_actions.setSpacing(6)
        self.btn_open_folder = QPushButton("打开文件夹")
        self.btn_open_folder.setObjectName("Flat")
        self.btn_open_folder.clicked.connect(self._open_folder)
        self.btn_open_terminal = QPushButton("打开终端")
        self.btn_open_terminal.setObjectName("Flat")
        self.btn_open_terminal.clicked.connect(lambda: self._run_in_terminal("", quiet=True))
        self.btn_copy_path = QPushButton("复制路径")
        self.btn_copy_path.setObjectName("Flat")
        self.btn_copy_path.clicked.connect(self._copy_path)
        for btn in (self.btn_open_folder, self.btn_open_terminal, self.btn_copy_path):
            header_actions.addWidget(btn)
        self.btn_open_editor = QPushButton("VS Code")
        self.btn_open_editor.setObjectName("Flat")
        self.btn_open_editor.clicked.connect(self._open_editor)
        self.btn_open_editor.setVisible(bool(win_compat.find_editor()))
        header_actions.addWidget(self.btn_open_editor)

        header_row = QHBoxLayout()
        header_left = QVBoxLayout()
        header_left.setSpacing(2)
        header_left.addWidget(self.header_name)
        header_left.addWidget(self.header_path)
        header_row.addLayout(header_left, 1)
        header_row.addLayout(header_actions)
        layout.addLayout(header_row)

        self.tabs = QTabWidget()
        self.chat_tab = self._build_chat_tab()
        self.tabs.addTab(self.chat_tab, "对话")
        self._study_tab_index = 1
        # 未选择项目时也有一张研读页，占住页签位置
        self.study_page = self._study_page_for("")
        self.tabs.addTab(self.study_page, "代码研读")
        self.dev_tab = self._dev_page_for("")
        self.tabs.addTab(self.dev_tab, "代码开发")
        self.history_tab = self._build_history_tab()
        self.tabs.addTab(self.history_tab, "会话历史")
        self.assets_tab = self._build_assets_tab()
        self.tabs.addTab(self.assets_tab, "技能与插件")
        self.commands_tab = self._build_commands_tab()
        self.tabs.addTab(self.commands_tab, "运行命令")
        self.tabs.currentChanged.connect(self._on_main_tab_changed)
        layout.addWidget(self.tabs, 1)

        self.statusBar().showMessage("就绪")

        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._new_chat)
        QShortcut(QKeySequence("F5"), self, activated=self._refresh_sessions)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._refresh_sessions)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.cmd_input.setFocus)
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.chat_input.setFocus)
        QShortcut(QKeySequence("Escape"), self, activated=self._interrupt_chat)
        # 问答悬浮窗关闭后，用应用级快捷键从任意窗口召回
        qa_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Q"), self)
        qa_shortcut.setContext(Qt.ApplicationShortcut)
        qa_shortcut.activated.connect(self._show_current_qa_window)
        chat_qa_shortcut = QShortcut(QKeySequence("Ctrl+Shift+W"), self)
        chat_qa_shortcut.setContext(Qt.ApplicationShortcut)
        chat_qa_shortcut.activated.connect(self._show_chat_qa_window)
        dev_qa_shortcut = QShortcut(QKeySequence("Ctrl+Shift+E"), self)
        dev_qa_shortcut.setContext(Qt.ApplicationShortcut)
        dev_qa_shortcut.activated.connect(self._show_dev_qa_window)

        return content

    def _build_chat_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        self.chat_toolbar = QWidget()
        bar = QHBoxLayout(self.chat_toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        perm_label = QLabel("权限")
        perm_label.setObjectName("Muted")
        bar.addWidget(perm_label)

        self.perm_combo = QComboBox()
        for label, mode, tip in PERMISSION_MODES:
            self.perm_combo.addItem(label, mode)
            self.perm_combo.setItemData(
                self.perm_combo.count() - 1, tip, Qt.ToolTipRole)
        target_index = 0
        for index, (_label, mode, _tip) in enumerate(PERMISSION_MODES):
            if mode == self._permission_mode:
                target_index = index
                break
        self.perm_combo.setCurrentIndex(target_index)
        self._last_perm_index = target_index
        self.perm_combo.currentIndexChanged.connect(self._on_permission_mode_changed)
        self.perm_combo.setToolTip(
            "请求批准：每次敏感操作都询问\n"
            "帮我批准：自动接受文件编辑\n"
            "完全访问：跳过所有权限确认")
        bar.addWidget(self.perm_combo)

        self.btn_new_chat = QPushButton("新对话")
        self.btn_new_chat.setObjectName("Primary")
        self.btn_new_chat.clicked.connect(self._new_chat)
        bar.addWidget(self.btn_new_chat)

        self.btn_continue_chat = QPushButton("继续上次")
        self.btn_continue_chat.clicked.connect(self._continue_last_chat)
        bar.addWidget(self.btn_continue_chat)

        self.btn_study_mode = QPushButton("研读模式")
        self.btn_study_mode.setCheckable(True)
        self.btn_study_mode.setToolTip(
            "开启后，在主对话框的提问会作为代码研读请求：\n"
            "自动切到「代码研读」页，并把回答写入解释栏。\n"
            "回答里带代码位置链接，点击即可跳到右侧源码。")
        self.btn_study_mode.toggled.connect(self._on_study_mode_toggled)
        bar.addWidget(self.btn_study_mode)

        self.btn_study_float = QPushButton("独立窗口")
        self.btn_study_float.setCheckable(True)
        self.btn_study_float.setToolTip(
            "研读页默认是主窗口里的页签；勾选后放到独立窗口，\n"
            "方便和主对话框并排看。")
        self.btn_study_float.toggled.connect(self._on_study_float_toggled)
        bar.addWidget(self.btn_study_float)

        self.btn_media = QToolButton()
        self.btn_media.setText("生成 ▾")
        self.btn_media.setToolTip(
            "生成图片或视频；研读模式下会附到「代码研读」页。\n"
            "也可以直接在输入框输入 /img 描述 或 /video 描述")
        self.btn_media.setPopupMode(QToolButton.InstantPopup)
        media_menu = QMenu(self.btn_media)
        act_image = media_menu.addAction("🖼 生成图片…")
        act_image.triggered.connect(lambda: self._prompt_media("image"))
        act_video = media_menu.addAction("🎬 生成视频…")
        act_video.triggered.connect(lambda: self._prompt_media("video"))
        self.btn_media.setMenu(media_menu)
        bar.addWidget(self.btn_media)

        bar.addStretch(1)
        self.chat_status = QLabel("就绪")
        self.chat_status.setObjectName("Muted")
        bar.addWidget(self.chat_status)
        # 窄窗口横向滚动整条工具栏，不折行或挤压模型控件。
        self.chat_toolbar_scroll = QScrollArea()
        self.chat_toolbar_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_toolbar_scroll.setWidgetResizable(True)
        self.chat_toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_toolbar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.chat_toolbar_scroll.setWidget(self.chat_toolbar)
        self.chat_toolbar_scroll.setFixedHeight(
            bar.sizeHint().height()
            + self.chat_toolbar_scroll.horizontalScrollBar().sizeHint().height() + 4)
        layout.addWidget(self.chat_toolbar_scroll)
        QTimer.singleShot(1200, self._refresh_balances)
        self._balance_timer.start()

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("ChatScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_body_widget = QWidget()
        self.chat_body_widget.setObjectName("ChatBody")
        self.chat_layout = QVBoxLayout(self.chat_body_widget)
        self.chat_layout.setContentsMargins(10, 6, 10, 6)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch(1)
        self.chat_scroll.setWidget(self.chat_body_widget)
        layout.addWidget(self.chat_scroll, 1)

        # 粘贴图片后在上方显示缩略图与识别状态
        self.chat_attachments = []
        self._image_seq = 0
        self.attachment_bar = QWidget()
        self.attachment_bar.setObjectName("AttachmentBar")
        self.attachment_layout = QHBoxLayout(self.attachment_bar)
        self.attachment_layout.setContentsMargins(0, 0, 0, 4)
        self.attachment_layout.setSpacing(8)
        self.attachment_layout.addStretch(1)
        self.attachment_bar.setVisible(False)
        layout.addWidget(self.attachment_bar)

        self.quote_bar = QFrame()
        self.quote_bar.setObjectName("QuoteBar")
        quote_row = QHBoxLayout(self.quote_bar)
        quote_row.setContentsMargins(10, 6, 8, 6)
        quote_row.setSpacing(8)
        self.quote_bar_label = QLabel("")
        self.quote_bar_label.setObjectName("QuoteBarText")
        self.quote_bar_label.setWordWrap(True)
        quote_row.addWidget(self.quote_bar_label, 1)
        quote_cancel = QToolButton()
        quote_cancel.setObjectName("Small")
        quote_cancel.setText("✕")
        quote_cancel.setToolTip("取消引用")
        quote_cancel.clicked.connect(self._clear_quote)
        quote_row.addWidget(quote_cancel)
        self.quote_bar.setVisible(False)
        layout.addWidget(self.quote_bar)

        self.chat_input = ChatInput()
        self.chat_input.setObjectName("ChatInput")
        self.chat_input.setPlaceholderText(
            "输入消息，Enter 发送，Shift+Enter 换行（可粘贴图片自动识别；"
            "输入 /img 描述 或 /video 描述 可直接生成图片/视频）")
        self.chat_input.setFixedHeight(92)
        self.chat_input.submitted.connect(self._send_chat_message)
        self.chat_input.image_pasted.connect(self._add_pasted_image)
        layout.addWidget(self.chat_input)

        # 模型配置默认收起：底栏「模型配置」按钮控制显隐。
        # 控件只创建一次，展开 / 收起不会重建控件或重置选择。
        self.model_config_panel = QWidget()
        config_row = QHBoxLayout(self.model_config_panel)
        config_row.setContentsMargins(0, 0, 0, 0)
        config_row.setSpacing(6)

        self.single_model_panel = QWidget()
        single = QHBoxLayout(self.single_model_panel)
        single.setContentsMargins(0, 0, 0, 0)
        single.setSpacing(6)
        model_label = QLabel("模型")
        model_label.setObjectName("Muted")
        single.addWidget(model_label)
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(190)
        self.model_combo.setToolTip(
            "模型列表从当前服务商自动获取（cc-switch 的 settings.json）。\n"
            "切换后会重建会话，并保留当前对话的上下文。")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        single.addWidget(self.model_combo)
        effort_label = QLabel("思考")
        effort_label.setObjectName("Muted")
        single.addWidget(effort_label)
        self.effort_combo = QComboBox()
        self.effort_combo.setMinimumWidth(80)
        self.effort_combo.setToolTip("思考档位，随所选模型支持的档位变化")
        self.effort_combo.currentIndexChanged.connect(self._on_effort_changed)
        single.addWidget(self.effort_combo)
        config_row.addWidget(self.single_model_panel)
        self._build_dual_panel(config_row)

        self.btn_refresh_models = QToolButton()
        self.btn_refresh_models.setText("↻")
        self.btn_refresh_models.setObjectName("Small")
        self.btn_refresh_models.setToolTip("重新获取服务商和 Codex 模型及思考档位")
        self.btn_refresh_models.clicked.connect(self._refresh_model_catalog)
        config_row.addWidget(self.btn_refresh_models)
        config_row.addStretch(1)

        # 窄窗口横向滚动配置行，保证主 / 从等长配置仍可访问。
        self.model_config_scroll = QScrollArea()
        self.model_config_scroll.setFrameShape(QFrame.NoFrame)
        self.model_config_scroll.setWidgetResizable(True)
        self.model_config_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.model_config_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.model_config_scroll.setWidget(self.model_config_panel)
        self.model_config_scroll.setFixedHeight(
            config_row.sizeHint().height()
            + self.model_config_scroll.horizontalScrollBar().sizeHint().height() + 20)
        self.model_config_scroll.setVisible(False)
        layout.addWidget(self.model_config_scroll)
        self._populate_model_combo()
        self._populate_dual_combos()

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        # 快捷键提示已写在输入框占位文字里；这里把空间留给余额 / 额度，
        # 保证底栏在 960 宽窗口下也能完整显示。
        self.btn_chat_qa = QPushButton("问答窗口")
        self.btn_chat_qa.setObjectName("Flat")
        self.btn_chat_qa.setToolTip(
            "直接打开对话问答窗口，不需要先选中内容（Ctrl+Shift+W 可随时召回）")
        self.btn_chat_qa.clicked.connect(self._show_chat_qa_window)
        bottom.addWidget(self.btn_chat_qa)
        self.btn_ask_selection = QPushButton("选中提问")
        self.btn_ask_selection.setObjectName("Flat")
        self.btn_ask_selection.setToolTip(
            "先在对话气泡里选中一段内容，再点这里打开悬浮问答窗"
            "（Ctrl+Shift+W 召回）")
        self.btn_ask_selection.clicked.connect(self._ask_chat_selection)
        bottom.addWidget(self.btn_ask_selection)
        self.btn_progress = QPushButton("进度查询")
        self.btn_progress.setObjectName("Flat")
        self.btn_progress.setToolTip(
            "查看当前任务的权限是否正常、执行进度和预计剩余时间（点击框外任意位置关闭）")
        self.btn_progress.clicked.connect(self._show_progress_popup)
        bottom.addWidget(self.btn_progress)
        bottom.addStretch(1)
        # 底栏顺序：双 AI 协作开关 → 模型配置按钮 → 额度显示 → 停止 → 发送。
        self.dual_check = QCheckBox("双 AI 协作")
        self.dual_check.setToolTip(
            "主 AI 负责规划、疑难处理和验收，执行 AI 负责范围内实现。\n"
            "主 AI 占比 0 / 100 时仅调用一个角色。")
        self.dual_check.setChecked(bool(self.config.get("dual_mode")))
        self.dual_check.toggled.connect(self._on_dual_toggled)
        bottom.addWidget(self.dual_check)
        self.btn_model_config = QPushButton("模型配置 ▾")
        self.btn_model_config.setObjectName("Flat")
        self.btn_model_config.setCheckable(True)
        self.btn_model_config.setToolTip(
            "展开 / 收起模型、思考档位、协作档次、交换主从和模型目录刷新")
        self.btn_model_config.toggled.connect(self._toggle_model_config)
        bottom.addWidget(self.btn_model_config)
        # 余额 / 额度紧贴停止按钮左侧：协作模式显示主从，单模型显示当前模型。
        bottom.addWidget(self.dual_balance_panel)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._interrupt_chat)
        bottom.addWidget(self.btn_stop)
        self.btn_send = QPushButton("发送")
        self.btn_send.setObjectName("Primary")
        self.btn_send.clicked.connect(self._send_chat_message)
        bottom.addWidget(self.btn_send)
        layout.addLayout(bottom)

        # 底栏控件创建完毕后再同步一次模式 / 忙碌状态。
        self._update_dual_controls()
        self._build_chat_qa_window()
        self._update_chat_qa_buttons()
        self._show_empty_state("开始新的对话吧\n在下方输入消息")
        return panel

    def _build_history_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)

        card, card_layout = self._make_card()
        history_header = QHBoxLayout()
        history_title = QLabel("会话历史")
        history_title.setObjectName("H2")
        self.session_count = QLabel("")
        self.session_count.setObjectName("Muted")
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setObjectName("Flat")
        self.btn_refresh.clicked.connect(self._refresh_sessions)
        history_header.addWidget(history_title)
        history_header.addWidget(self.session_count)

        self.history_scope_combo = QComboBox()
        self.history_scope_combo.addItem("当前项目", "project")
        self.history_scope_combo.addItem("全部项目", "all")
        self.history_scope_combo.setToolTip("选择只查看当前项目，还是查看全部工作历史")
        self.history_scope_combo.currentIndexChanged.connect(self._refresh_sessions)
        history_header.addWidget(self.history_scope_combo)

        self.history_source_combo = QComboBox()
        self.history_source_combo.addItem("全部来源", "all")
        self.history_source_combo.addItem("Claude", "claude")
        self.history_source_combo.addItem("Codex", "codex")
        self.history_source_combo.setToolTip("Claude 与 Codex 的会话会合并显示")
        self.history_source_combo.currentIndexChanged.connect(self._refresh_sessions)
        history_header.addWidget(self.history_source_combo)

        history_header.addStretch(1)
        btn_shared = QPushButton("共享目录")
        btn_shared.setObjectName("Flat")
        btn_shared.setToolTip("打开 ~/.agents：共享技能库与两边历史的统一入口")
        btn_shared.clicked.connect(
            lambda: self._open_path(shared_agents.SHARED_ROOT))
        history_header.addWidget(btn_shared)
        history_header.addWidget(self.btn_refresh)
        card_layout.addLayout(history_header)

        self.session_stack = QStackedWidget()
        self.session_list = QListWidget()
        self.session_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.session_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.session_list.itemDoubleClicked.connect(self._resume_session_item)
        self.session_list.itemSelectionChanged.connect(self._update_session_button)
        self.session_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._session_menu)
        self.empty_label = QLabel("暂无会话记录\n切换到「对话」标签开始新对话")
        self.empty_label.setObjectName("Empty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.session_stack.addWidget(self.session_list)
        self.session_stack.addWidget(self.empty_label)
        card_layout.addWidget(self.session_stack, 1)

        footer = QHBoxLayout()
        self.session_hint = QLabel(
            "Claude 会话可在界面中继续；Codex 会话双击后预览，并可在终端继续")
        self.session_hint.setObjectName("Muted")
        self.btn_resume_selected = QPushButton("在对话中继续")
        self.btn_resume_selected.setEnabled(False)
        self.btn_resume_selected.clicked.connect(self._resume_selected_session)
        footer.addWidget(self.session_hint)
        footer.addStretch(1)
        footer.addWidget(self.btn_resume_selected)
        card_layout.addLayout(footer)

        layout.addWidget(card, 1)
        return panel

    def _build_commands_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)

        card, cmd_layout = self._make_card()
        cmd_title = QLabel("在项目目录中运行命令")
        cmd_title.setObjectName("H2")
        cmd_hint = QLabel("命令会在外部终端中执行，适合 git、构建等操作")
        cmd_hint.setObjectName("Muted")
        cmd_layout.addWidget(cmd_title)
        cmd_layout.addWidget(cmd_hint)

        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(8)
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("输入命令后回车运行，例如 git status")
        self.cmd_input.returnPressed.connect(self._run_custom_command)
        self.history_model = QStringListModel(self.config["history"])
        completer = QCompleter(self.history_model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.cmd_input.setCompleter(completer)
        self.btn_run = QPushButton("运行")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self._run_custom_command)
        self.btn_star = QPushButton("★ 收藏")
        self.btn_star.setToolTip("把当前命令加入常用命令")
        self.btn_star.clicked.connect(self._add_favorite)
        cmd_row.addWidget(self.cmd_input, 1)
        cmd_row.addWidget(self.btn_run)
        cmd_row.addWidget(self.btn_star)
        cmd_layout.addLayout(cmd_row)

        self.fav_hint = QLabel("常用命令 · 双击运行，右键删除")
        self.fav_hint.setObjectName("Muted")
        cmd_layout.addWidget(self.fav_hint)
        self.fav_list = QListWidget()
        self.fav_list.setObjectName("Favorites")
        self.fav_list.setMaximumHeight(120)
        self.fav_list.itemDoubleClicked.connect(self._run_favorite)
        self.fav_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.fav_list.customContextMenuRequested.connect(self._favorite_menu)
        cmd_layout.addWidget(self.fav_list)
        cmd_layout.addStretch(1)

        layout.addWidget(card, 1)
        return panel

    def _tab_key(self, widget):
        """把主页签映射成可持久化到项目状态的短名字。"""
        if widget is None:
            return ""
        if isinstance(widget, CodeStudyPage):
            return "study"
        if isinstance(widget, CodeDevPage):
            return "dev"
        for key in ("chat", "history", "assets", "commands"):
            if widget is getattr(self, f"{key}_tab", None):
                return key
        return ""

    def _remember_active_tab(self):
        """把当前项目正停留的页签记到它自己的会话状态里。"""
        if getattr(self, "_suppress_tab_remember", False):
            return
        if not getattr(self, "tabs", None) or not self.current_project:
            return
        key = self._tab_key(self.tabs.currentWidget())
        if key:
            self._chat_state(self.current_project)["tab"] = key

    def _restore_active_tab(self):
        """切回项目时只恢复它自己的工作页签，不污染别的项目。"""
        if not getattr(self, "tabs", None) or not self.current_project:
            return
        key = self._chat_state(self.current_project).get("tab") or "chat"
        widget = None
        if key == "study":
            # 研读页可能被搬到独立窗口；此时回到对话页即可
            if self.tabs.indexOf(self.study_page) >= 0:
                widget = self.study_page
        elif key == "dev":
            # 开发页也可能搬到独立窗口；不在页签里就不要硬切
            dev = getattr(self, "dev_tab", None)
            if self.tabs.indexOf(dev) >= 0:
                widget = dev
        else:
            widget = getattr(self, f"{key}_tab", None)
        if widget is None:
            widget = getattr(self, "chat_tab", None)
        index = self.tabs.indexOf(widget) if widget is not None else -1
        if index < 0:
            widget = getattr(self, "chat_tab", None)
            index = self.tabs.indexOf(widget) if widget is not None else -1
        if index < 0:
            return
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(index)
        self.tabs.blockSignals(False)
        self._stabilize_page_layout(widget)

    def _stabilize_page_layout(self, widget=None):
        """页签重挂/切换后主动排版一次，避免停在上一个窗口的旧尺寸。"""
        targets = []
        for target in (widget, self.tabs.currentWidget()):
            if target is not None and target not in targets:
                targets.append(target)
        for target in targets:
            target.updateGeometry()
            layout = target.layout()
            if layout is not None:
                layout.activate()
            for splitter in target.findChildren(QSplitter):
                splitter.updateGeometry()

    def _on_main_tab_changed(self, index):
        self._remember_active_tab()
        widget = self.tabs.widget(index)
        if widget is not None:
            self._stabilize_page_layout(widget)
            QTimer.singleShot(
                0, lambda w=widget: self._stabilize_page_layout(w))
        if widget is getattr(self, "chat_tab", None):
            # 切回对话页时补上切换项目时因页面隐藏而暂存的定位。
            QTimer.singleShot(0, self._apply_pending_chat_anchor)
        if widget is self.assets_tab:
            self._refresh_skills()
            if not self._plugins_loaded:
                self._refresh_plugins()

    # ------------------------------------------------------- skills/plugins --
    def _build_assets_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)
        inner = QTabWidget()
        inner.addTab(self._build_skills_panel(), "技能库")
        inner.addTab(self._build_plugins_panel(), "插件")
        layout.addWidget(inner, 1)
        return panel

    def _build_skills_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)

        card, card_layout = self._make_card()
        header = QHBoxLayout()
        title = QLabel("共享技能库")
        title.setObjectName("H2")
        self.skill_count = QLabel("")
        self.skill_count.setObjectName("Muted")
        self.skill_filter = QComboBox()
        self.skill_filter.addItem("全部分类", "all")
        for key, label in SKILL_CATEGORIES:
            self.skill_filter.addItem(label, key)
        self.skill_filter.setToolTip("按类别筛选技能；右键技能可修改分类")
        self.skill_filter.currentIndexChanged.connect(
            lambda _index: self._refresh_skills())
        btn_refresh = QPushButton("刷新")
        btn_refresh.setObjectName("Flat")
        btn_refresh.clicked.connect(self._refresh_skills)
        header.addWidget(title)
        header.addWidget(self.skill_count)
        header.addStretch(1)
        header.addWidget(self.skill_filter)
        header.addWidget(btn_refresh)
        card_layout.addLayout(header)

        hint = QLabel(
            "技能实体存放在 ~/.agents/skills，Claude 与 Codex 共用同一份；"
            "安装一次，两边都能使用。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        card_layout.addWidget(hint)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        btn_github = QPushButton("从 GitHub 安装")
        btn_github.setObjectName("Primary")
        btn_github.clicked.connect(self._install_skill_github)
        btn_folder = QPushButton("从文件夹安装")
        btn_folder.clicked.connect(self._install_skill_folder)
        btn_sync = QPushButton("同步到两端")
        btn_sync.setToolTip("收编直接装在 Claude/Codex 技能目录里的技能，并补齐软链接")
        btn_sync.clicked.connect(self._sync_skills)
        btn_open = QPushButton("打开共享目录")
        btn_open.setObjectName("Flat")
        btn_open.clicked.connect(lambda: self._open_path(shared_agents.SHARED_SKILLS))
        for button in (btn_github, btn_folder, btn_sync, btn_open):
            actions.addWidget(button)
        actions.addStretch(1)
        card_layout.addLayout(actions)

        self.skill_list = QListWidget()
        self.skill_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.skill_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.skill_list.itemDoubleClicked.connect(self._open_skill_item)
        self.skill_list.itemSelectionChanged.connect(self._show_skill_detail)
        self.skill_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.skill_list.customContextMenuRequested.connect(self._skill_menu)
        card_layout.addWidget(self.skill_list, 1)

        self.skill_detail = QLabel("")
        self.skill_detail.setObjectName("Muted")
        self.skill_detail.setWordWrap(True)
        self.skill_detail.setMinimumHeight(44)
        card_layout.addWidget(self.skill_detail)

        footer = QLabel("双击打开技能目录 · 右键可重新链接或删除")
        footer.setObjectName("Muted")
        card_layout.addWidget(footer)

        layout.addWidget(card, 1)
        return panel

    def _build_plugins_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(9)

        hint = QLabel(
            "Claude 与 Codex 的插件格式不同（.claude-plugin / .codex-plugin），"
            "不能直接互相链接；这里把两边的插件放在一起管理。"
            "技能则通过「技能库」真正共用。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self._build_plugin_card("claude"), 1)
        row.addWidget(self._build_plugin_card("codex"), 1)
        layout.addLayout(row, 1)
        return panel

    def _build_plugin_card(self, kind):
        card, card_layout = self._make_card()
        is_claude = kind == "claude"
        title = QLabel("Claude Code 插件" if is_claude else "Codex 插件")
        title.setObjectName("H2")
        status = QLabel("尚未加载")
        status.setObjectName("Muted")
        btn_refresh = QPushButton("刷新")
        btn_refresh.setObjectName("Flat")
        btn_refresh.clicked.connect(self._refresh_plugins)
        btn_install = QPushButton("安装…")
        btn_install.clicked.connect(lambda: self._install_plugin(kind))
        header = QHBoxLayout()
        header.addWidget(title)
        header.addWidget(status)
        header.addStretch(1)
        header.addWidget(btn_refresh)
        header.addWidget(btn_install)
        card_layout.addLayout(header)

        plugin_list = QListWidget()
        plugin_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        plugin_list.setContextMenuPolicy(Qt.CustomContextMenu)
        plugin_list.customContextMenuRequested.connect(
            lambda pos, k=kind: self._plugin_menu(k, pos))
        card_layout.addWidget(plugin_list, 1)

        if is_claude:
            self.claude_plugin_list = plugin_list
            self.claude_plugin_status = status
        else:
            self.codex_plugin_list = plugin_list
            self.codex_plugin_status = status
        return card

    def _plugin_list(self, kind):
        return (self.claude_plugin_list if kind == "claude"
                else self.codex_plugin_list)

    def _plugin_status(self, kind):
        return (self.claude_plugin_status if kind == "claude"
                else self.codex_plugin_status)

    def _refresh_skills(self):
        try:
            skills = shared_agents.list_skills()
        except OSError as exc:
            self.skill_count.setText(f"读取失败：{exc}")
            return
        self._skills = skills
        selected = self.skill_list.currentItem()
        selected_data = (selected.data(Qt.UserRole) or {}) if selected else {}
        selected_name = (None if selected_data.get("__category__")
                         else selected_data.get("dir_name"))
        filter_key = self.skill_filter.currentData()
        self.skill_list.clear()
        grouped = {}
        for skill in skills:
            category = skill_category(skill, self.config)
            skill["category"] = category
            grouped.setdefault(category, []).append(skill)
        first_selectable = None
        for key, label in SKILL_CATEGORIES:
            if filter_key not in (None, "all") and key != filter_key:
                continue
            entries = sorted(grouped.get(key) or [],
                             key=lambda s: str(s.get("name", "")).lower())
            if not entries:
                continue
            header = QListWidgetItem()
            header.setFlags(Qt.NoItemFlags)
            header.setData(Qt.UserRole, {"__category__": True, "key": key})
            header.setSizeHint(QSize(0, 28))
            self.skill_list.addItem(header)
            header_label = QLabel(f"{label}  ({len(entries)})")
            header_label.setObjectName("SkillCategory")
            self.skill_list.setItemWidget(header, header_label)
            for skill in entries:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, skill)
                item.setSizeHint(QSize(0, 58))
                links = []
                for link_label, ok in skill["links"].items():
                    links.append(f"{link_label} ✓" if ok
                                 else f"{link_label} 未链接")
                subtitle = " · ".join(links)
                if skill.get("version"):
                    subtitle += f" · v{skill['version']}"
                widget = ListItemWidget(skill["name"], subtitle)
                widget.setToolTip(
                    f"{skill['description']}\n{subtitle}\n{skill['path']}")
                self.skill_list.addItem(item)
                self.skill_list.setItemWidget(item, widget)
                if first_selectable is None:
                    first_selectable = item
                if skill["dir_name"] == selected_name:
                    self.skill_list.setCurrentItem(item)
        visible = sum(len(grouped.get(key) or [])
                      for key, _label in SKILL_CATEGORIES
                      if filter_key in (None, "all") or key == filter_key)
        self.skill_count.setText(
            f"{visible}/{len(skills)} 个技能" if skills else "")
        if self.skill_list.currentItem() is None and first_selectable is not None:
            self.skill_list.setCurrentItem(first_selectable)
        self._show_skill_detail()

    def _open_skill_item(self, item):
        data = item.data(Qt.UserRole) or {}
        if data.get("__category__"):
            return
        self._open_path(data.get("path", ""))

    def _show_skill_detail(self):
        item = self.skill_list.currentItem()
        if item is None:
            self.skill_detail.setText("")
            return
        skill = item.data(Qt.UserRole) or {}
        if skill.get("__category__"):
            self.skill_detail.setText("")
            return
        description = skill.get("description") or "（没有简介）"
        category = dict(SKILL_CATEGORIES).get(
            skill.get("category") or skill_category(skill, self.config),
            "📦 其他")
        self.skill_detail.setText(
            f"分类：{category}\n{description}\n共享目录：{skill.get('path', '')}")

    def _set_skill_category(self, skill, category):
        categories = self.config.setdefault("skill_categories", {})
        categories[skill["dir_name"]] = category
        save_config(self.config)
        self._refresh_skills()
        label = dict(SKILL_CATEGORIES).get(category, category)
        self.statusBar().showMessage(
            f"已把「{skill['name']}」归到 {label}", 4000)

    def _skill_menu(self, pos):
        item = self.skill_list.itemAt(pos)
        if item is None:
            return
        skill = item.data(Qt.UserRole) or {}
        if skill.get("__category__"):
            return
        current_category = skill.get("category") or skill_category(
            skill, self.config)
        menu = QMenu(self)
        act_open = menu.addAction("打开技能目录")
        act_copy = menu.addAction("复制路径")
        category_menu = menu.addMenu("设置分类")
        category_actions = {}
        for key, label in SKILL_CATEGORIES:
            action = category_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == current_category)
            category_actions[action] = key
        menu.addSeparator()
        act_sync = menu.addAction("在两端重新链接")
        act_remove = menu.addAction("删除技能")
        action = menu.exec_(self.skill_list.viewport().mapToGlobal(pos))
        if action is act_open:
            self._open_path(skill["path"])
        elif action is act_copy:
            QApplication.clipboard().setText(str(skill["path"]))
            self.statusBar().showMessage("已复制技能路径", 2500)
        elif action in category_actions:
            self._set_skill_category(skill, category_actions[action])
        elif action is act_sync:
            self._sync_skills()
        elif action is act_remove:
            self._remove_skill(skill)

    def _open_path(self, path):
        target = Path(path)
        if not target.exists():
            self.statusBar().showMessage("路径不存在", 3000)
            return
        if not win_compat.open_path(target):
            self.statusBar().showMessage("无法打开该路径", 3000)

    def _sync_skills(self):
        messages = []
        try:
            messages.extend(shared_agents.ensure_shared_skills())
            messages.extend(shared_agents.adopt_existing_skills())
            messages.extend(shared_agents.ensure_shared_skills())
        except OSError as exc:
            QMessageBox.warning(self, "同步技能", f"同步失败：{exc}")
            return
        self._refresh_skills()
        if messages:
            self.statusBar().showMessage("；".join(messages[-2:]), 6000)
        else:
            self.statusBar().showMessage("两端技能链接已是最新", 4000)

    def _remove_skill(self, skill):
        answer = QMessageBox.question(
            self, "删除技能",
            f"从共享库删除「{skill['name']}」？\n"
            f"{skill['path']}\n\n该操作会同时移除两端的链接。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            shared_agents.remove_skill(skill["dir_name"])
        except OSError as exc:
            QMessageBox.warning(self, "删除技能", f"删除失败：{exc}")
            return
        self._refresh_skills()
        self.statusBar().showMessage(f"已删除技能：{skill['name']}", 4000)

    def _install_skill_folder(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择技能目录（需包含 SKILL.md）", str(Path.home()))
        if not directory:
            return
        try:
            name = shared_agents.install_skill_dir(Path(directory))
        except Exception as exc:  # noqa: BLE001 - 面向用户的提示
            QMessageBox.warning(self, "安装技能", str(exc))
            return
        self._refresh_skills()
        QMessageBox.information(self, "安装技能", f"已安装并链接到两端：{name}")

    def _install_skill_github(self):
        source, ok = QInputDialog.getText(
            self, "从 GitHub 安装技能",
            "输入 owner/repo，或完整地址（可带 /tree/分支/子目录）：\n"
            "例如 hugohe3/ppt-master",
        )
        if not ok or not source.strip():
            return
        self._start_skill_install(source.strip())

    def _start_skill_install(self, source):
        progress = TaskProgressDialog(
            "安装技能", f"正在从 GitHub 下载并安装：\n{source}", self)
        progress.set_busy("正在连接 GitHub…")
        progress.show()
        worker = WorkerThread(
            shared_agents.install_skill_from_github, source, parent=self,
            progress_arg=True, cancel_arg=True)
        self._skill_worker = worker
        worker.progress.connect(progress.set_progress)
        progress.canceled.connect(worker.request_cancel)

        def done(names):
            self._skill_worker = None
            progress.mark_done()
            self._refresh_skills()
            QMessageBox.information(
                self, "安装技能",
                "已安装并链接到两端：\n" + "\n".join(names))

        def failed(message):
            self._skill_worker = None
            progress.close()
            if worker.is_cancelled():
                self.statusBar().showMessage("已取消安装技能", 4000)
                return
            QMessageBox.warning(self, "安装技能", message)

        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _refresh_plugins(self):
        self._plugins_loaded = True
        for kind in ("claude", "codex"):
            try:
                data = (shared_agents.claude_plugins(include_available=False)
                        if kind == "claude"
                        else shared_agents.codex_plugins(include_available=False))
            except Exception as exc:  # noqa: BLE001 - CLI 可能不可用
                self._plugin_status(kind).setText(f"读取失败：{exc}"[:90])
                self._plugin_list(kind).clear()
                continue
            installed = data.get("installed") or []
            self._populate_plugin_list(kind, installed)

    def _populate_plugin_list(self, kind, plugins):
        widget = self._plugin_list(kind)
        widget.clear()
        for plugin in plugins:
            plugin_id = plugin.get("pluginId") or plugin.get("name", "")
            enabled = bool(plugin.get("enabled", True))
            name = plugin.get("name") or plugin_id
            marketplace = plugin.get("marketplaceName") or ""
            version = plugin.get("version") or ""
            detail = []
            if version:
                detail.append(f"v{version}")
            detail.append("已启用" if enabled else "已禁用")
            if marketplace:
                detail.append(marketplace)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, plugin)
            item.setSizeHint(QSize(0, 54))
            line = ListItemWidget(name, " · ".join(detail))
            description = " ".join(str(plugin.get("description") or "").split())
            line.setToolTip(f"{description}\n{plugin_id}")
            widget.addItem(item)
            widget.setItemWidget(item, line)
        status = self._plugin_status(kind)
        status.setText(f"{len(plugins)} 个已安装" if plugins else "未安装任何插件")

    def _plugin_menu(self, kind, pos):
        widget = self._plugin_list(kind)
        item = widget.itemAt(pos)
        if item is None:
            return
        plugin = item.data(Qt.UserRole) or {}
        plugin_id = plugin.get("pluginId") or plugin.get("name", "")
        enabled = bool(plugin.get("enabled", True))
        menu = QMenu(self)
        act_toggle = menu.addAction("禁用" if enabled else "启用")
        act_remove = menu.addAction("卸载")
        act_copy = menu.addAction("复制插件 ID")
        action = menu.exec_(widget.viewport().mapToGlobal(pos))
        if action is act_toggle:
            verb = "disable" if enabled else "enable"
            label = "禁用" if enabled else "启用"
            self._run_plugin_action(kind, verb, plugin_id,
                                    f"{label} {plugin_id}")
        elif action is act_remove:
            answer = QMessageBox.question(
                self, "卸载插件",
                f"卸载「{plugin_id}」？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer == QMessageBox.Yes:
                self._run_plugin_action(kind, "uninstall", plugin_id,
                                        f"卸载 {plugin_id}")
        elif action is act_copy:
            QApplication.clipboard().setText(plugin_id)
            self.statusBar().showMessage("已复制插件 ID", 2500)

    def _install_plugin(self, kind):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            data = (shared_agents.claude_plugins(include_available=True)
                    if kind == "claude"
                    else shared_agents.codex_plugins(include_available=True))
        except Exception as exc:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "安装插件", f"读取插件市场失败：{exc}")
            return
        QApplication.restoreOverrideCursor()
        installed_ids = {item.get("pluginId")
                         for item in data.get("installed") or []}
        available = data.get("available") or []
        for item in available:
            item["installed"] = item.get("pluginId") in installed_ids
        if not available:
            QMessageBox.information(self, "安装插件", "插件市场里没有可安装的插件。")
            return
        title = "安装 Claude Code 插件" if kind == "claude" else "安装 Codex 插件"
        dialog = PluginPickerDialog(available, title, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        plugin_id = dialog.selected_plugin_id()
        if not plugin_id:
            return
        answer = QMessageBox.question(
            self, "安装插件", f"安装「{plugin_id}」？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        self._run_plugin_action(kind, "install", plugin_id,
                                f"安装 {plugin_id}")

    def _run_plugin_action(self, kind, action, plugin_id, description):
        if kind == "codex" and action in ("enable", "disable"):
            func, args = (shared_agents.codex_set_enabled,
                          (plugin_id, action == "enable"))
        elif kind == "claude":
            func, args = (shared_agents.claude_plugin_action,
                          (action, plugin_id))
        else:
            cli_action = "add" if action == "install" else "remove"
            func, args = (shared_agents.codex_plugin_action,
                          (cli_action, plugin_id))

        progress = TaskProgressDialog("插件管理", description + "…", self)
        progress.set_busy("正在执行 claude / codex 插件命令…")
        progress.show()
        worker = WorkerThread(func, *args, parent=self)
        self._plugin_worker = worker

        started = time.monotonic()
        elapsed_timer = QTimer(progress)

        def tick():
            seconds = int(time.monotonic() - started)
            progress.set_busy(f"正在执行插件命令…已用 {seconds} 秒")

        elapsed_timer.timeout.connect(tick)
        elapsed_timer.start(1000)

        def done(result):
            elapsed_timer.stop()
            progress.close()
            self._plugin_worker = None
            ok, message = result if isinstance(result, tuple) else (bool(result), "")
            self._refresh_plugins()
            self._refresh_skills()
            if ok:
                self.statusBar().showMessage(f"{description} 完成", 5000)
            else:
                QMessageBox.warning(
                    self, "插件管理",
                    f"{description} 失败：\n{message or '未知错误'}")

        def failed(message):
            elapsed_timer.stop()
            progress.close()
            self._plugin_worker = None
            QMessageBox.warning(self, "插件管理",
                                f"{description} 失败：\n{message}")

        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _make_card(self):
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)
        return card, layout

    # ------------------------------------------------------------ projects --
    def _populate_projects(self, select_path=None):
        self.project_list.blockSignals(True)
        self.project_list.clear()
        selected_row = -1
        for idx, project in enumerate(self.config["projects"]):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, project)
            item.setSizeHint(QSize(0, 52))
            self.project_list.addItem(item)
            self.project_list.setItemWidget(
                item, ListItemWidget(project["name"], project["path"]))
            if select_path and project["path"] == select_path:
                selected_row = idx
        self.project_list.blockSignals(False)
        if not self.config["projects"]:
            self.current_project = None
            self._set_header(None)
            self._reset_chat_for_project()
            self._switch_study_page_to(None)
            self._switch_dev_page_to(None)
            self._refresh_sessions()
            return
        if selected_row < 0:
            selected_row = 0
        self.project_list.setCurrentRow(selected_row)

    def _select_first_project(self):
        if self.config["projects"] and self.project_list.currentRow() < 0:
            self.project_list.setCurrentRow(0)

    def _on_project_changed(self, current, _previous):
        # 先把上一个项目自己的研读内容、模式与页签存起来，再切换
        self._remember_active_tab()
        # 切页签过程中 removeTab 会触发 currentChanged，此时不能把临时页签
        # 记到新项目头上，否则会覆盖它自己的记忆
        self._suppress_tab_remember = True
        try:
            old_path = getattr(self, "_study_state_path", None)
            if old_path is not None:
                self._capture_study_state(old_path)
            project = current.data(Qt.UserRole) if current else None
            self.current_project = project["path"] if project else None
            self._set_header(project)
            self._reset_chat_for_project()
            # 每个项目一张常驻研读页：切走只是换页签，内容不会被清空；
            # 已独立的窗口也不参与切换。
            self._switch_study_page_to(self.current_project)
            self._switch_dev_page_to(self.current_project)
            self._set_study_mode_silently(
                bool(self._study_state(self.current_project)["mode"]))
            self._study_state_path = self.current_project or ""
            self._apply_pending_study_record()
            self._restore_active_tab()
            self._refresh_sessions()
        finally:
            self._suppress_tab_remember = False

    def _on_study_record_requested(self, data):
        """载入研读记录：记录属于别的项目时，切到那个项目再应用。"""
        if not isinstance(data, dict):
            return
        root = str(data.get("root") or "")
        if root and root != (self.current_project or ""):
            self._pending_study_record = data
            self._select_project_path(root)
            return
        self.study_page._apply_snapshot(data)
        self.study_page._update_study_buttons()

    def _apply_pending_study_record(self):
        data = getattr(self, "_pending_study_record", None)
        if not isinstance(data, dict):
            return
        root = str(data.get("root") or "")
        if root != (self.current_project or ""):
            return
        self._pending_study_record = None
        self.study_page._apply_snapshot(data)
        self.study_page._update_study_buttons()

    def _set_header(self, project):
        if project is None:
            self.header_name.setText("未选择项目")
            self.header_path.setText("在左侧添加或选择一个项目目录")
        else:
            self.header_name.setText(project["name"])
            self.header_path.setText(project["path"])
        has_project = project is not None
        for btn in (self.btn_open_folder, self.btn_open_terminal,
                    self.btn_copy_path, self.btn_open_editor, self.btn_run,
                    self.btn_new_chat, self.btn_continue_chat):
            btn.setEnabled(has_project)
        self._update_chat_buttons()
        self._update_session_button()

    def _add_project(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择项目目录", str(Path.home()))
        if not directory:
            return
        directory = str(Path(directory).resolve())
        existing = next((p for p in self.config["projects"]
                         if p["path"] == directory), None)
        if existing:
            self._select_project_path(directory)
            self.statusBar().showMessage("该项目已在列表中", 3000)
            return
        name = Path(directory).name or directory
        self.config["projects"].append({"name": name, "path": directory})
        save_config(self.config)
        self._populate_projects(select_path=directory)
        self.statusBar().showMessage(f"已添加项目：{name}", 3000)

    def _remove_project(self):
        item = self.project_list.currentItem()
        if item is None:
            return
        project = item.data(Qt.UserRole)
        answer = QMessageBox.question(
            self, "移除项目",
            f"从列表中移除「{project['name']}」？\n不会删除任何文件。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.config["projects"] = [p for p in self.config["projects"]
                                   if p["path"] != project["path"]]
        save_config(self.config)
        if project["path"] == (self.current_project or ""):
            self._reset_dev_for_new_chat()
        self._populate_projects()
        self.statusBar().showMessage("已移除项目", 3000)

    def _select_project_path(self, path):
        for row in range(self.project_list.count()):
            item = self.project_list.item(row)
            if item.data(Qt.UserRole)["path"] == path:
                self.project_list.setCurrentRow(row)
                return
        # 研读记录可能指向尚未添加的项目目录，自动补进侧边栏
        if path and Path(path).is_dir():
            name = Path(path).name or path
            self.config["projects"].append({"name": name, "path": path})
            save_config(self.config)
            self._populate_projects(select_path=path)

    def _project_menu(self, pos):
        item = self.project_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        act_open = menu.addAction("打开文件夹")
        act_term = menu.addAction("在终端中打开")
        menu.addSeparator()
        act_remove = menu.addAction("从列表移除")
        action = menu.exec_(self.project_list.viewport().mapToGlobal(pos))
        if action is act_open:
            self.project_list.setCurrentItem(item)
            self._open_folder()
        elif action is act_term:
            self.project_list.setCurrentItem(item)
            self._run_in_terminal("", quiet=True)
        elif action is act_remove:
            self.project_list.setCurrentItem(item)
            self._remove_project()

    # ------------------------------------------------------------ sessions --
    def _refresh_sessions(self):
        scope = "project"
        source = "all"
        if hasattr(self, "history_scope_combo"):
            scope = self.history_scope_combo.currentData() or "project"
        if hasattr(self, "history_source_combo"):
            source = self.history_source_combo.currentData() or "all"

        if scope == "project" and not self.current_project:
            self.sessions = []
            self.session_list.clear()
            self.session_count.setText("")
            self.session_stack.setCurrentWidget(self.empty_label)
            self.empty_label.setText("尚未选择项目\n可切换到「全部项目」查看两边历史")
            self._update_session_button()
            return

        entries = []
        if source in ("all", "claude"):
            sessions = (load_project_sessions(self.current_project)
                        if scope == "project" else load_all_claude_sessions())
            entries.extend(session_to_entry(session) for session in sessions)
        if source in ("all", "codex"):
            try:
                if scope == "project":
                    infos = shared_agents.codex_sessions_for_cwd(
                        self.current_project)
                else:
                    infos = shared_agents.all_codex_sessions()
                entries.extend(codex_entry(info) for info in infos)
            except OSError:
                pass

        entries.sort(key=lambda entry: entry["updated"], reverse=True)
        self.sessions = entries
        self.session_list.clear()
        claude_count = 0
        codex_count = 0
        for entry in entries:
            is_codex = entry["source"] == "codex"
            if is_codex:
                codex_count += 1
                source_label = "Codex"
            else:
                claude_count += 1
                source_label = "Claude"
            item = QListWidgetItem()
            item.setData(Qt.UserRole, entry)
            item.setSizeHint(QSize(0, 58))
            meta = f"{humanize(entry['updated'])} · {entry['rounds']} 轮"
            if entry.get("cwd") and scope == "all":
                meta += f" · {entry['cwd']}"
            widget = ListItemWidget(f"{source_label} · {entry['title']}", meta)
            widget.setToolTip(
                f"{entry['title']}\n{meta}\n会话 ID：{entry['sid']}")
            self.session_list.addItem(item)
            self.session_list.setItemWidget(item, widget)

        count = len(entries)
        if count:
            self.session_count.setText(
                f"共 {count} 个（Claude {claude_count} · Codex {codex_count}）")
            self.session_stack.setCurrentWidget(self.session_list)
        else:
            self.session_count.setText("")
            if scope == "all":
                self.empty_label.setText("还没有任何工作历史")
            elif source == "codex":
                self.empty_label.setText("当前项目还没有 Codex 会话")
            elif source == "claude":
                self.empty_label.setText("当前项目还没有 Claude 会话")
            else:
                self.empty_label.setText("暂无会话记录\n切换到「对话」标签开始新对话")
            self.session_stack.setCurrentWidget(self.empty_label)
        self._update_session_button()

    def _selected_session_entry(self):
        item = self.session_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _update_session_button(self):
        entry = (self._selected_session_entry()
                 if hasattr(self, "session_list") else None)
        self.btn_resume_selected.setEnabled(entry is not None)
        if entry and entry.get("source") == "codex":
            self.btn_resume_selected.setText("预览 / 终端继续")
        else:
            self.btn_resume_selected.setText("在对话中继续")

    def _resume_selected_session(self):
        entry = self._selected_session_entry()
        if not entry:
            return
        if entry["source"] == "codex":
            self._preview_codex_session(entry)
        else:
            self._resume_in_chat(entry["sid"])

    def _resume_session_item(self, item):
        entry = item.data(Qt.UserRole)
        if not entry:
            return
        if entry["source"] == "codex":
            self._preview_codex_session(entry)
        else:
            self._resume_in_chat(entry["sid"])

    def _session_menu(self, pos):
        item = self.session_list.itemAt(pos)
        if item is None:
            return
        entry = item.data(Qt.UserRole)
        is_codex = entry.get("source") == "codex"
        menu = QMenu(self)
        act_resume = menu.addAction(
            "预览会话" if is_codex else "在对话中恢复")
        act_terminal = menu.addAction("在终端中继续") if is_codex else None
        act_copy = menu.addAction("复制会话 ID")
        act_folder = (menu.addAction("打开会话所在目录")
                      if entry.get("cwd") else None)
        action = menu.exec_(self.session_list.viewport().mapToGlobal(pos))
        if action is act_resume:
            self.session_list.setCurrentItem(item)
            self._resume_selected_session()
        elif act_terminal is not None and action is act_terminal:
            self._open_codex_session(entry)
        elif action is act_copy:
            QApplication.clipboard().setText(entry["sid"])
            self.statusBar().showMessage("已复制会话 ID", 2500)
        elif act_folder is not None and action is act_folder:
            win_compat.open_path(entry["cwd"])

    def _preview_codex_session(self, entry):
        dialog = CodexSessionDialog(entry, self)
        if dialog.exec_() == QDialog.Accepted and dialog.resume_requested:
            self._open_codex_session(entry)

    def _open_codex_session(self, entry):
        executable = shared_agents.find_codex_cli()
        if not executable:
            QMessageBox.warning(
                self, "找不到 codex",
                "无法定位 codex 命令，请确认 Codex 已正确安装。")
            return
        cwd = entry.get("cwd") or self.current_project or str(Path.home())
        if not Path(cwd).is_dir():
            cwd = str(Path.home())
        command = (f"{win_compat.quote_arg(executable)} resume "
                   f"{win_compat.quote_arg(entry['sid'])}")
        try:
            terminal = launch_terminal(cwd, command)
        except Exception as exc:  # noqa: BLE001 - 面向用户的错误提示
            QMessageBox.critical(self, "无法打开终端", str(exc))
            return
        self.statusBar().showMessage(
            f"已在 {terminal} 中继续 Codex 会话", 5000)

    # ------------------------------------------------------------ commands --
    def _run_custom_command(self):
        command = self.cmd_input.text().strip()
        if not command:
            return
        self._remember_command(command)
        self._run_in_terminal(command)

    def _remember_command(self, command):
        history = [c for c in self.config["history"] if c != command]
        history.insert(0, command)
        self.config["history"] = history[:50]
        self.history_model.setStringList(self.config["history"])
        save_config(self.config)

    def _add_favorite(self):
        command = self.cmd_input.text().strip()
        if not command:
            self.statusBar().showMessage("先输入一条命令再收藏", 3000)
            return
        if command in self.config["favorites"]:
            self.statusBar().showMessage("这条命令已在常用命令中", 3000)
            return
        self.config["favorites"].append(command)
        save_config(self.config)
        self._refresh_favorites()
        self.statusBar().showMessage("已加入常用命令", 3000)

    def _refresh_favorites(self):
        self.fav_list.clear()
        for command in self.config["favorites"]:
            self.fav_list.addItem(command)
        has_any = bool(self.config["favorites"])
        self.fav_hint.setVisible(has_any)
        self.fav_list.setVisible(has_any)

    def _run_favorite(self, item):
        command = item.text()
        self.cmd_input.setText(command)
        self._remember_command(command)
        self._run_in_terminal(command)

    def _favorite_menu(self, pos):
        item = self.fav_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        act_run = menu.addAction("运行")
        act_remove = menu.addAction("删除")
        action = menu.exec_(self.fav_list.viewport().mapToGlobal(pos))
        if action is act_run:
            self._run_favorite(item)
        elif action is act_remove:
            self.config["favorites"].remove(item.text())
            save_config(self.config)
            self._refresh_favorites()

    # --------------------------------------------------------------- chat --
    def _connect_worker(self):
        # 工作线程按项目懒创建；这里只确保当前项目已经有一份。
        if self.current_project:
            self._ensure_worker_for(self._chat_state())

    def _ensure_worker_for(self, state):
        worker = state.get("worker")
        if worker is not None:
            return worker
        worker = ClaudeWorker(self)
        worker.text_delta.connect(
            lambda chunk, s=state: self._on_chat_delta(s, chunk))
        worker.assistant.connect(
            lambda text, tools, s=state: self._on_chat_assistant(s, text, tools))
        worker.result.connect(
            lambda info, s=state: self._on_chat_result(s, info))
        worker.error.connect(
            lambda message, s=state: self._on_chat_error(s, message))
        worker.busy_changed.connect(
            lambda busy, s=state: self._on_chat_busy(s, busy))
        worker.permission_asked.connect(
            lambda rid, name, text, s=state:
            self._on_permission_asked(s, rid, name, text))
        state["worker"] = worker
        worker.start()
        return worker

    # ------------------------------------------------------ 双 AI 协作面板 --
    def _build_dual_panel(self, layout):
        # 与单模型组共用原模型位置；整个组隐藏后标签也不占空间。
        self.dual_panel = QWidget()
        row = QHBoxLayout(self.dual_panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.dual_tier_label = QLabel("档次")
        self.dual_tier_label.setObjectName("Muted")
        self.dual_tier_combo = QComboBox()
        self.dual_tier_combo.setFixedWidth(150)
        self.dual_tier_combo.setToolTip(
            "按工作部件难度分工，而不是按比例：\n"
            + "\n".join(f"{p.id} {p.label}：{p.summary}" for p in dual_ai.tier_choices()))
        for policy in dual_ai.tier_choices():
            self.dual_tier_combo.addItem(policy.label, policy.id)
        index = self.dual_tier_combo.findData(self._dual_tier)
        self.dual_tier_combo.setCurrentIndex(index if index >= 0 else
                                             self.dual_tier_combo.findData(dual_ai.L2))
        self.dual_tier_combo.currentIndexChanged.connect(
            self._on_dual_tier_changed)

        for role, label, handler in (
                ("master", "主 AI", self._on_dual_master_changed),
                ("worker", "执行 AI", self._on_dual_worker_changed)):
            if role == "worker":
                row.addWidget(self.dual_tier_label)
                row.addWidget(self.dual_tier_combo)
                self.dual_swap_btn = QToolButton()
                self.dual_swap_btn.setText("⇄")
                self.dual_swap_btn.setObjectName("Small")
                self.dual_swap_btn.setToolTip("交换两个角色的模型及思考档位")
                self.dual_swap_btn.clicked.connect(self._swap_dual_roles)
                row.addWidget(self.dual_swap_btn)
                # 复查开关紧跟协作档次：简单任务可取消主 AI 独立复查省额度。
                self.review_check = QCheckBox("复查")
                self.review_check.setToolTip(
                    "勾选（默认）：按协作档次执行复查策略；L1/L2 完成后验收，"
                    "L3 失败时审查，L0/L4 无独立复查。\n"
                    "取消：跳过独立复查以节省额度；执行者自检、失败接管和"
                    "任务结果判定不受影响。")
                self.review_check.setChecked(
                    bool(self.config.get("dual_review_enabled", True)))
                self.review_check.toggled.connect(self._on_dual_review_toggled)
                row.addWidget(self.review_check)
            title = QLabel(label)
            title.setObjectName("Muted")
            row.addWidget(title)
            combo = QComboBox()
            combo.setFixedWidth(165)
            combo.setToolTip(label + "模型：支持 Claude 服务商与 Codex 登录模型")
            combo.currentIndexChanged.connect(handler)
            setattr(self, "dual_" + role + "_combo", combo)
            row.addWidget(combo)
            title = QLabel("思考")
            title.setObjectName("Muted")
            row.addWidget(title)
            effort = QComboBox()
            # 覆盖全局下拉框最小宽度，保证短档位控件保持紧凑。
            effort.setStyleSheet("QComboBox { min-width: 72px; max-width: 72px; }")
            effort.currentIndexChanged.connect(
                lambda _index, r=role: self._on_dual_effort_changed(r))
            setattr(self, "dual_" + role + "_effort_combo", effort)
            row.addWidget(effort)

        layout.addWidget(self.dual_panel)

        # 余额容器放在输入框下方、停止按钮左侧。
        # 协作模式显示主 / 从两个额度，单模型模式只显示当前模型余额。
        self.dual_balance_panel = QWidget()
        balances = QHBoxLayout(self.dual_balance_panel)
        balances.setContentsMargins(0, 0, 0, 0)
        balances.setSpacing(6)
        self.dual_master_balance = QLabel("主：额度 —")
        self.dual_master_balance.setObjectName("DualBalance")
        balances.addWidget(self.dual_master_balance)
        self.dual_worker_balance = QLabel("从：额度 —")
        self.dual_worker_balance.setObjectName("DualBalance")
        balances.addWidget(self.dual_worker_balance)
        self.single_balance = QLabel("余额 —")
        self.single_balance.setObjectName("DualBalance")
        balances.addWidget(self.single_balance)
        self.dual_balance_btn = QToolButton()
        self.dual_balance_btn.setText("↻")
        self.dual_balance_btn.setObjectName("Small")
        self.dual_balance_btn.setToolTip("刷新当前模式的余额 / 额度")
        self.dual_balance_btn.clicked.connect(self._refresh_balances)
        balances.addWidget(self.dual_balance_btn)

        self._update_dual_controls()

    def _fill_dual_combo(self, combo, current, default_source="claude"):
        combo.blockSignals(True)
        combo.clear()
        claude_models = list(self._model_catalog.values())
        if not claude_models:
            claude_models = [dict(item) for item in FALLBACK_MODELS]
        codex_models = dual_ai.load_codex_models()

        def header(text):
            combo.addItem(text, None)
            item = combo.model().item(combo.count() - 1)
            if item is not None:
                item.setEnabled(False)

        header("── Claude 服务商 ──")
        for meta in claude_models:
            model_id = str(meta.get("id") or "")
            name = str(meta.get("name") or model_id)
            combo.addItem(name, {"source": "claude", "model": model_id,
                                 "name": name,
                                 "efforts": meta.get("efforts", FALLBACK_EFFORTS),
                                 "efforts_known": "efforts" in meta,
                                 "default_effort": meta.get("default_effort")})
        header("── Codex 登录模型 ──")
        for meta in codex_models:
            model_id = str(meta.get("id") or "")
            name = "Codex · " + str(meta.get("name") or model_id)
            combo.addItem(name, {"source": "codex", "model": model_id,
                                 "name": name, "efforts": meta.get("efforts") or [],
                                 "default_effort": meta.get("default_effort")})

        target_source = current.source if isinstance(
            current, dual_ai.RoleSpec) else "claude"
        target_model = current.model if isinstance(
            current, dual_ai.RoleSpec) else ""
        # 服务商目录异步加载：保存的自定义模型不能先被内置兜底覆盖。
        if target_model and not any(
                isinstance(combo.itemData(i), dict)
                and (combo.itemData(i).get("source"),
                     combo.itemData(i).get("model")) == (target_source, target_model)
                for i in range(combo.count())):
            combo.addItem(current.label or target_model,
                          {"source": target_source, "model": target_model,
                           "name": current.label, "efforts": [],
                           "efforts_known": False})
        selected = -1
        for index in range(combo.count()):
            data = combo.itemData(index)
            if (isinstance(data, dict)
                    and data.get("source") == target_source
                    and data.get("model") == target_model):
                selected = index
                break
        if selected < 0:
            # 已保存过来源时尊重它；只有从未选择过才用默认来源。
            # 默认：主 AI 跟随当前 Claude 模型，执行 AI 用 Codex 登录模型。
            source = target_source if target_source in (
                "claude", "codex") else default_source
            if source == "codex":
                fallback = dual_ai.default_codex_model()
                wanted = ("codex", fallback["id"] if fallback else "")
            else:
                wanted = ("claude", target_model or self._model)
            for index in range(combo.count()):
                data = combo.itemData(index)
                if (isinstance(data, dict)
                        and (data.get("source"), data.get("model")) == wanted):
                    selected = index
                    break
        if selected < 0:
            for index in range(combo.count()):
                if isinstance(combo.itemData(index), dict):
                    selected = index
                    break
        combo.setCurrentIndex(max(0, selected))
        combo.blockSignals(False)

    def _populate_dual_combos(self):
        if not hasattr(self, "dual_master_combo"):
            return
        changed = False
        for role, source in (("master", "claude"), ("worker", "codex")):
            current = getattr(self, "_dual_" + role)
            # 旧配置首次迁移时，主模型跟随单模型（包括尚未加载的自定义模型）。
            current = dual_ai.RoleSpec(
                source=current.source,
                model=current.model or (self._model if current.source == "claude" else ""),
                name=current.name,
                effort=self._effort if current.effort is None else current.effort)
            combo = getattr(self, "dual_" + role + "_combo")
            self._fill_dual_combo(combo, current, default_source=source)
            self._populate_dual_effort(role, current.effort)
            spec = self._dual_spec_from_combo(role)
            if (not self._catalog_loaded and spec.source == "claude"
                    and spec.model == current.model and current.effort is not None
                    and not (combo.currentData() or {}).get("efforts_known")):
                # 显示临时支持的选项，但保留持久化偏好供目录到达后恢复。
                spec.effort = current.effort
            setattr(self, "_dual_" + role, spec)
            key = "dual_" + role
            if self.config.get(key) != spec.to_dict():
                self.config[key] = spec.to_dict()
                changed = True
        if changed:
            save_config(self.config)
        self._update_dual_controls()

    @staticmethod
    def _combo_spec(combo):
        data = combo.currentData()
        if not isinstance(data, dict):
            return dual_ai.RoleSpec()
        return dual_ai.RoleSpec(
            source=str(data.get("source") or "claude"),
            model=str(data.get("model") or ""),
            name=str(data.get("name") or ""),
            efforts=tuple(data.get("efforts") or ()))

    def _populate_dual_effort(self, role, preferred=None):
        model_combo = getattr(self, "dual_" + role + "_combo")
        combo = getattr(self, "dual_" + role + "_effort_combo")
        spec = self._combo_spec(model_combo)
        levels = dual_ai.supported_efforts(spec)
        model_combo.setToolTip(f"{spec.label}\n后端：{spec.source}\n模型：{spec.model}")
        data = model_combo.currentData() or {}
        # 老配置只迁移一次全局档位，此后两个角色完全独立。
        wanted = self._effort if preferred is None else preferred
        if wanted not in levels:
            wanted = data.get("default_effort")
        if wanted not in levels:
            wanted = "high" if "high" in levels else (levels[0] if levels else "")
        combo.blockSignals(True)
        combo.clear()
        for level in levels:
            combo.addItem(level, level)
        if not levels:
            combo.addItem("默认", "")
        combo.setCurrentIndex(max(0, combo.findData(wanted)))
        combo.blockSignals(False)
        combo.setToolTip("该模型支持：" + " / ".join(levels) if levels
                         else "该模型未报告可选思考档位，使用模型默认值")

    def _on_dual_effort_changed(self, role):
        spec = self._dual_spec_from_combo(role)
        setattr(self, "_dual_" + role, spec)
        self.config["dual_" + role] = spec.to_dict()
        save_config(self.config)

    def _set_combo_to_spec(self, combo, spec):
        combo.blockSignals(True)
        for index in range(combo.count()):
            data = combo.itemData(index)
            if (isinstance(data, dict) and spec is not None
                    and data.get("source") == spec.source
                    and data.get("model") == spec.model):
                combo.setCurrentIndex(index)
                break
        combo.blockSignals(False)

    def _on_dual_toggled(self, checked):
        self.config["dual_mode"] = bool(checked)
        save_config(self.config)
        self._update_chat_buttons()
        # 切换模式后按新模式刷新底栏额度；5 分钟内的缓存可直接复用，
        # 过期结果会在 _refresh_balance 中重新查询。
        self._refresh_balances(force=False)
        if checked:
            policy = dual_ai.tier_policy(self._dual_tier)
            self.statusBar().showMessage(
                f"双 AI 协作已开启（档次 {policy.label}）："
                "按工作部件难度分工，压缩跨模型交接，节省额度", 5000)
        else:
            self.statusBar().showMessage("已切回单模型对话", 3000)

    def _on_dual_master_changed(self, _index):
        self._populate_dual_effort("master", self._dual_master.effort)
        spec = self._dual_spec_from_combo("master")
        if not spec.model:
            return
        self._dual_master = spec
        self.config["dual_master"] = spec.to_dict()
        save_config(self.config)
        self._update_dual_controls()
        self._refresh_balance("master", force=True)
        self.statusBar().showMessage(f"主 AI：{spec.label}", 2500)

    def _on_dual_worker_changed(self, _index):
        self._populate_dual_effort("worker", self._dual_worker.effort)
        spec = self._dual_spec_from_combo("worker")
        if not spec.model:
            return
        self._dual_worker = spec
        self.config["dual_worker"] = spec.to_dict()
        save_config(self.config)
        self._update_dual_controls()
        self._refresh_balance("worker", force=True)
        self.statusBar().showMessage(f"执行 AI：{spec.label}", 2500)

    def _swap_dual_roles(self):
        master = self._dual_spec_from_combo("master")
        worker = self._dual_spec_from_combo("worker")
        self._set_combo_to_spec(self.dual_master_combo, worker)
        self._set_combo_to_spec(self.dual_worker_combo, master)
        self._populate_dual_effort("master", worker.effort)
        self._populate_dual_effort("worker", master.effort)
        self._dual_master = self._dual_spec_from_combo("master")
        self._dual_worker = self._dual_spec_from_combo("worker")
        self.config["dual_master"] = self._dual_master.to_dict()
        self.config["dual_worker"] = self._dual_worker.to_dict()
        save_config(self.config)
        self._update_dual_controls()
        self._refresh_balances()
        self.statusBar().showMessage(
            f"已交换：主 AI = {self._dual_master.label}，"
            f"执行 AI = {self._dual_worker.label}", 3500)

    def _on_dual_review_toggled(self, checked):
        self.config["dual_review_enabled"] = bool(checked)
        save_config(self.config)
        self.statusBar().showMessage(
            "主 AI 复查已开启：按当前协作档次执行验收策略"
            if checked else
            "已关闭主 AI 复查：跳过独立验收调用；执行者自检与失败接管保留",
            4000)

    def _on_dual_tier_changed(self, _index):
        tier = self.dual_tier_combo.currentData() or dual_ai.L2
        self._dual_tier = dual_ai.normalize_tier(tier)
        self.config["dual_tier"] = self._dual_tier
        save_config(self.config)
        policy = dual_ai.tier_policy(self._dual_tier)
        self._update_dual_controls()
        self.statusBar().showMessage(
            f"协作档次：{policy.label} — {policy.short}", 3500)

    def _toggle_model_config(self, checked):
        """展开 / 收起模型配置；只切换显隐，不重建控件、不重置选择。"""
        self.model_config_scroll.setVisible(bool(checked))
        self.btn_model_config.setText(
            "模型配置 ▴" if checked else "模型配置 ▾")

    def _update_dual_controls(self):
        if not hasattr(self, "dual_check"):
            return
        on = self.dual_check.isChecked()
        self.single_model_panel.setVisible(not on)
        self.dual_panel.setVisible(on)
        busy = bool(self._chat_busy)
        self.dual_check.setEnabled(not busy)
        for widget in (self.dual_master_combo, self.dual_worker_combo,
                       self.dual_swap_btn, self.dual_tier_combo,
                       self.review_check):
            widget.setEnabled(on and not busy)
        for role in ("master", "worker"):
            combo = getattr(self, "dual_" + role + "_effort_combo")
            combo.setEnabled(on and not busy and bool(combo.currentData()))
        if hasattr(self, "btn_refresh_models"):
            self.btn_refresh_models.setEnabled(not busy and self._catalog_thread is None)
        # 余额查询在后台线程进行，运行任务时也应可随时刷新查看。
        self.dual_balance_btn.setEnabled(True)
        # 底栏余额标签随模式切换：协作看主从，单模型看当前模型。
        if hasattr(self, "single_balance"):
            self.single_balance.setVisible(not on)
            self.dual_balance_btn.setToolTip(
                "刷新两个模型的余额 / 额度" if on
                else "刷新当前模型的余额 / 额度")
        self.dual_master_balance.setVisible(on)
        self.dual_worker_balance.setVisible(on)
        # 双 AI 模式下，顶部单模型选择让位给主 / 从两个下拉框
        if hasattr(self, "model_combo") and hasattr(self, "effort_combo"):
            self.model_combo.setEnabled(not busy and not on)
            self.effort_combo.setEnabled(not busy and not on)

    def _dual_spec_from_combo(self, role):
        spec = self._combo_spec(getattr(self, "dual_" + role + "_combo"))
        spec.effort = getattr(self, "dual_" + role + "_effort_combo").currentData() or ""
        return spec

    def _dual_mode_on(self):
        return bool(getattr(self, "dual_check", None)
                    and self.dual_check.isChecked())

    def _balance_spec(self, role):
        """取某余额角色当前对应的模型；single 用单模型下拉框。"""
        if role == "single":
            model = self.model_combo.currentData() or self._model
            if not model:
                return dual_ai.RoleSpec(source="claude")
            return dual_ai.RoleSpec(
                source="claude", model=str(model),
                name=self.model_combo.currentText() or str(model),
                effort=self._current_effort())
        return self._dual_spec_from_combo(role)

    def _balance_label(self, role):
        if role == "master":
            return self.dual_master_balance
        if role == "worker":
            return self.dual_worker_balance
        return self.single_balance

    def _refresh_balances(self, _checked=False, force=True):
        if not hasattr(self, "single_balance"):
            return
        if self._dual_mode_on():
            self._refresh_balance("master", force=force)
            self._refresh_balance("worker", force=force)
        else:
            self._refresh_balance("single", force=force)

    # 余额查询的最长等待时间；超时后释放标签，避免一直卡在“查询中…”。
    BALANCE_TIMEOUT = 25

    def _refresh_balance(self, role, force=False):
        if not hasattr(self, "single_balance"):
            return
        spec = self._balance_spec(role)
        if not spec.model:
            return
        label = self._balance_label(role)
        prefix = {"master": "主", "worker": "从"}.get(role, "")
        key = (spec.source, spec.model)
        cached = self._balance_cache.get(key)
        if not force and cached and time.monotonic() - cached[0] < 300:
            self._apply_balance(role, label, prefix, cached[1])
            return
        # 自动刷新不重复发起同一模型的查询；手动「↻」仍允许主动重试。
        if not force and key in self._balance_pending:
            return
        self._balance_pending[key] = time.monotonic()
        label.setText(f"{prefix} 查询中…" if prefix else "查询中…")
        label.setStyleSheet("")
        worker = dual_ai.BalanceWorker(role, spec, provider_env(), self)
        worker.ready.connect(
            lambda role, result, selected=spec: self._on_balance_ready(role, result, selected))
        worker.done.connect(
            lambda w=worker: self._cleanup_balance_worker(w))
        self._balance_workers.append(worker)
        worker.start()

    def _check_balance_timeouts(self):
        """查询长时间未返回时恢复标签，用户可立即重试。"""
        if not self._balance_pending:
            return
        now = time.monotonic()
        for key, since in list(self._balance_pending.items()):
            if now - since < self.BALANCE_TIMEOUT:
                continue
            self._balance_pending.pop(key, None)
            for role in ("single", "master", "worker"):
                if not hasattr(self, "single_balance"):
                    continue
                spec = self._balance_spec(role)
                if (spec.source, spec.model) != key:
                    continue
                label = self._balance_label(role)
                if "查询中" not in label.text():
                    continue
                prefix = {"master": "主", "worker": "从"}.get(role, "")
                label.setText(
                    f"{prefix} 查询超时 · 点 ↻ 重试" if prefix
                    else "查询超时 · 点 ↻ 重试")
                label.setToolTip(
                    f"余额 / 额度查询超过 {self.BALANCE_TIMEOUT} 秒未返回，"
                    "可稍后点击 ↻ 重试")
                label.setStyleSheet("color:#b05a5a;")

    def _cleanup_balance_worker(self, worker):
        if worker in self._balance_workers:
            self._balance_workers.remove(worker)
        worker.deleteLater()

    def _on_balance_ready(self, role, result, queried_spec=None):
        label = self._balance_label(role)
        prefix = {"master": "主", "worker": "从"}.get(role, "")
        spec = self._balance_spec(role)
        key = (spec.source, spec.model)
        queried_key = ((queried_spec.source, queried_spec.model)
                       if queried_spec is not None else key)
        self._balance_cache[queried_key] = (time.monotonic(), result)
        self._balance_pending.pop(queried_key, None)
        # 只应用仍对应当前所选模型的结果，避免旧查询覆盖新额度。
        if queried_key == key:
            self._apply_balance(role, label, prefix, result)

    @staticmethod
    def _apply_balance(role, label, prefix, result):
        text = str((result or {}).get("text") or "—").strip()
        if role == "master":
            # 底栏空间有限：去掉冗余的「余额」并只保留「主」前缀。
            text = text.removeprefix("余额 ").strip()
            text = f"{prefix} {text}"
        elif role == "worker":
            # 「Plus · 5h 6% · 周 1%」压缩后仍保留全部用量数字。
            text = text.replace(" · ", " ")
            text = re.sub(r"\s+(?=\d+%)", "", text)
            text = f"{prefix} {text}"
        label.setText(text)
        label.setToolTip(str((result or {}).get("detail") or ""))
        if not (result or {}).get("ok"):
            label.setStyleSheet("color:#b05a5a;")
        else:
            label.setStyleSheet("")

    def _send_dual_message(self, prompt, display):
        state = self._chat_state()
        thread = state.get("dual_thread")
        if thread is not None and thread.isRunning():
            self.statusBar().showMessage("协作任务正在运行，请先停止或等待完成", 3000)
            return
        master = self._dual_spec_from_combo("master")
        worker = self._dual_spec_from_combo("worker")
        tier = self.dual_tier_combo.currentData() or self._dual_tier
        policy = dual_ai.tier_policy(tier)
        active_specs = ([master] if policy.solo_role == dual_ai.MASTER
                        else [worker] if policy.solo_role == dual_ai.WORKER
                        else [master, worker])
        if any(not spec.model for spec in active_specs):
            QMessageBox.warning(self, "双 AI 协作",
                                "请先选择主 AI 和执行 AI 的模型。")
            return
        codex_bin = dual_ai.find_codex_binary()
        claude_bin = find_claude() or ""
        if not claude_bin and any(spec.source == "claude" for spec in active_specs):
            QMessageBox.warning(
                self, "双 AI 协作",
                "找不到 claude 命令，无法调用 Claude 服务商模型。")
            return
        if not codex_bin and any(spec.source == "codex" for spec in active_specs):
            QMessageBox.warning(
                self, "双 AI 协作",
                "找不到 Codex 命令行，无法调用 Codex 登录模型。\n\n"
                "请确认 ChatGPT/Codex 桌面端已安装并完成登录。")
            return
        self._append_chat_block(
            {"kind": "user", "text": display, "plain": True})
        self.chat_input.clear()
        self._clear_attachments()
        self._reset_live_stream()
        self._dual_master, self._dual_worker = master, worker
        self._dual_tier = dual_ai.normalize_tier(tier)
        thread = dual_ai.DualRunThread(
            parent=self,
            cwd=self.current_project,
            request=prompt,
            # 当前请求另行发送；历史完整落盘，由角色首次接手读取，不重复塞入每个任务。
            context=dual_ai.history_context((state.get("blocks") or [])[:-1], limit=None),
            master=master,
            worker=worker,
            tier=self._dual_tier,
            permission_mode=self._current_permission_mode(),
            claude_path=claude_bin,
            codex_path=codex_bin,
            claude_env=provider_env(),
            previous_state=state.get("dual_state_dir", ""),
            # 发送时快照：运行中即使改动勾选也不影响本轮。
            review_enabled=self.review_check.isChecked(),
        )
        state["dual_thread"] = thread
        state["dual_live"] = ""
        state["dual_live_bubble"] = None
        state["dual_live_wrapper"] = None
        state["dual_last_role"] = ""
        if (self._current_permission_mode() == "default"
                and not state.get("dual_perm_noted")):
            state["dual_perm_noted"] = True
            self._on_dual_note(
                state,
                "提示：双 AI 协作使用命令行无交互模式，"
                "「请求批准」会按“自动批准文件编辑”执行；"
                "需要完全放开时请选择「完全访问」。")
        thread.status.connect(
            lambda text, s=state: self._on_dual_status(s, text))
        thread.note.connect(
            lambda text, s=state: self._on_dual_note(s, text))
        thread.plan_ready.connect(
            lambda plan, s=state: self._on_dual_plan(s, plan))
        thread.role_started.connect(
            lambda role, label, s=state:
            self._on_dual_role_started(s, role, label))
        thread.delta.connect(
            lambda role, chunk, s=state:
            self._on_dual_delta(s, role, chunk))
        thread.message.connect(
            lambda role, text, tools, s=state:
            self._on_dual_message(s, role, text, tools))
        thread.tool.connect(
            lambda role, name, data, s=state:
            self._on_dual_tool(s, role, name, data))
        thread.completed.connect(
            lambda info, s=state: self._on_dual_completed(s, info))
        thread.failed.connect(
            lambda message, s=state: self._on_dual_failed(s, message))
        thread.finished.connect(
            lambda s=state: self._on_dual_thread_finished(s))
        self._set_dual_busy(True)
        self.chat_status.setText("双 AI 协作中…")
        thread.start()

    def _set_dual_busy(self, busy):
        self._chat_busy = bool(busy)
        self._update_chat_buttons()
        if not busy:
            self._update_dual_controls()

    def _on_dual_status(self, state, text):
        state["status"] = text
        if self._is_active_chat_state(state):
            self.chat_status.setText(text)

    def _on_dual_note(self, state, text):
        state["blocks"].append({"kind": "note", "text": text})
        if self._is_active_chat_state(state):
            self._render_chat_note(text)

    def _on_dual_plan(self, state, plan):
        tasks = plan.get("tasks") or []
        tier_label = plan.get("tier_label") or self._dual_tier
        lines = [f"主 AI 计划（档次 {plan.get('tier')} · {tier_label}）："
                 f"{plan.get('summary') or '（无摘要）'}"]
        for task in tasks:
            role = "主 AI" if task.get("role") == "master" else "执行 AI"
            ops = "/".join(dual_ai.WORK_OPS.get(x, x) for x in task.get("ops") or [])
            lines.append(
                f"  {task.get('id')} · {role} · {task.get('points')} 点"
                + (f" · {ops}" if ops else "")
                + f" · {task.get('title')}")
        lines.append(
            f"分工按工作部件难度，不按比例凑点；"
            f"预估主 AI 工作量 {plan.get('actual_share')}%"
            f"（{plan.get('master_points')}/{plan.get('total_points')} 点，仅供参考）")
        text = "\n".join(lines)
        state["blocks"].append({"kind": "note", "text": text})
        if self._is_active_chat_state(state):
            self._render_chat_note(text)

    def _on_dual_role_started(self, state, role, label):
        if self._is_active_chat_state(state):
            # 先把上一角色尚未刷新的文本提交，再开始新气泡。
            self._flush_dual_stream()
            self._dual_stream_timer.stop()
        state["dual_last_role"] = role
        state["dual_live"] = ""
        if not self._is_active_chat_state(state):
            return
        self._discard_dual_live(state)
        wrapper, bubble = self._add_bubble(role, "", plain=True)
        state["dual_live_wrapper"] = wrapper
        state["dual_live_bubble"] = bubble
        self._on_dual_note(state, label)

    def _on_dual_delta(self, state, _role, chunk):
        state["dual_live"] = (state.get("dual_live") or "") + chunk
        if not self._is_active_chat_state(state):
            return
        # 只标记有更新，由定时器批量刷新文本；高频 delta 不再逐条重排。
        if not self._dual_stream_timer.isActive():
            self._dual_stream_timer.start()

    def _flush_dual_stream(self):
        """把当前项目的流式文本一次性提交到气泡。"""
        state = self._chat_state()
        if not self._is_active_chat_state(state):
            self._dual_stream_timer.stop()
            return
        text = state.get("dual_live") or ""
        if not text:
            self._dual_stream_timer.stop()
            return
        bubble = state.get("dual_live_bubble")
        if bubble is None:
            wrapper, bubble = self._add_bubble(
                state.get("dual_last_role") or "assistant", "", plain=True)
            state["dual_live_wrapper"] = wrapper
            state["dual_live_bubble"] = bubble
        bubble.set_plain(text)
        self._scroll_to_bottom()

    def _discard_dual_live(self, state):
        wrapper = state.get("dual_live_wrapper")
        if wrapper is not None:
            wrapper.deleteLater()
        state["dual_live_wrapper"] = None
        state["dual_live_bubble"] = None
        state["dual_live"] = ""

    def _on_dual_message(self, state, role, text, tools):
        stream_text = state.get("dual_live") or ""
        if self._is_active_chat_state(state):
            bubble = state.get("dual_live_bubble")
            if bubble is not None:
                if text:
                    bubble.set_markdown(text)
                elif stream_text:
                    bubble.set_markdown(stream_text)
            elif text:
                self._add_bubble(role, text)
        state["dual_live_wrapper"] = None
        state["dual_live_bubble"] = None
        state["dual_live"] = ""
        final_text = text or stream_text
        if final_text:
            state["blocks"].append(
                {"kind": role, "text": final_text})
        for tool in tools or []:
            block = {"kind": "tool", "name": tool.get("name", "工具"),
                     "data": tool.get("input") or {}}
            state["blocks"].append(block)
            if self._is_active_chat_state(state):
                self._render_tool_chip(block["name"], block["data"])
        if self._is_active_chat_state(state):
            self._scroll_to_bottom()

    def _on_dual_tool(self, state, _role, name, data):
        block = {"kind": "tool", "name": name, "data": data or {}}
        state["blocks"].append(block)
        if self._is_active_chat_state(state):
            self._render_tool_chip(name, data or {})

    def _on_dual_completed(self, state, info):
        self._discard_dual_live(state)
        if info.get("state_dir"):
            state["dual_state_dir"] = info["state_dir"]
        stats = info.get("stats") or {}
        master = stats.get("master") or {}
        worker = stats.get("worker") or {}

        def call_text(item):
            parts = [f"{item.get('calls', 0)} 次调用"]
            cost = float(item.get("cost") or 0)
            if cost > 0:
                parts.append(f"${cost:.4f}")
            tokens = int(item.get("tokens") or 0)
            if tokens > 0:
                parts.append(f"{tokens:,} tokens")
            seconds = float(item.get("seconds") or 0)
            if seconds > 0:
                parts.append(f"{seconds:.0f} 秒")
            return " · ".join(parts)

        takeovers = sum(
            1 for record in info.get("records") or []
            if record.get("takeover") is not None)
        takeover_text = f"　主 AI 接管 {takeovers} 次" if takeovers else ""
        asks = int(info.get("asks") or 0)
        ask_text = f"　中途询问 {asks} 次" if asks else ""
        before = int(info.get("context_before") or 0)
        after = int(info.get("context_after") or before)
        context_text = (f"　上下文 {before:,}→{after:,} 字符"
                        if after < before else "")

        if info.get("cancelled"):
            note = "协作已停止。"
        else:
            headline = "协作完成" if info.get("ok") else "协作结束（有未完成项）"
            review_text = ("" if info.get("review_enabled", True)
                           else "　复查已关闭")
            note = (
                f"{headline} · 档次 {info.get('tier')} · "
                f"{info.get('tier_label') or '按工作部件分工'}"
                f"{review_text}{takeover_text}{ask_text}{context_text}\n"
                f"主 AI：{call_text(master)}　执行 AI：{call_text(worker)}\n"
                f"跨模型提示 {int(info.get('prompt_chars') or 0):,} 字符"
                f"（按档次收敛调用次数与交接长度）")
        if info.get("state_dir"):
            note += f"\n交接协议 v{info.get('protocol_version', 3)} · 完整记录：{info['state_dir']}"
        state["status"] = (
            "协作完成" if (info.get("ok") and not info.get("cancelled"))
            else "已停止" if info.get("cancelled") else "协作结束")
        state["blocks"].append({"kind": "note", "text": note})
        if self._is_active_chat_state(state):
            self._render_chat_note(note)
            self.chat_status.setText(state["status"])
        QTimer.singleShot(600, self._refresh_balances)

    def _on_dual_failed(self, state, message):
        self._discard_dual_live(state)
        text = f"协作出错：{message}"
        state["status"] = "出错"
        state["blocks"].append({"kind": "note", "text": text})
        if self._is_active_chat_state(state):
            self._render_chat_note(text)
            self.chat_status.setText("出错")

    def _on_dual_thread_finished(self, state):
        thread = state.get("dual_thread")
        if thread is not None:
            thread.deleteLater()
            state["dual_thread"] = None
        state["busy"] = False
        if self._is_active_chat_state(state):
            self._dual_stream_timer.stop()
            self._update_chat_buttons()
            self._flush_stream()
            if self._pending_catalog is not None:
                models, error = self._pending_catalog
                self._pending_catalog = None
                self._on_catalog_loaded(models, error)

    def _current_permission_mode(self):
        return self.perm_combo.currentData() or "default"

    def _current_model(self):
        return self.model_combo.currentData() or self._model

    def _current_effort(self):
        return self.effort_combo.currentData() or self._effort

    # ------------------------------------------------------ 模型目录 --
    def _populate_model_combo(self, preferred=None):
        """用当前模型目录重建下拉框，并保持原选择（若仍存在）。"""
        current = preferred or self._model
        models = list(self._model_catalog.values())
        if not models:
            models = [dict(m) for m in FALLBACK_MODELS]
            self._model_catalog = {m["id"]: dict(m) for m in models}

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        selected = -1
        for meta in models:
            label = meta.get("name") or meta["id"]
            self.model_combo.addItem(label, meta["id"])
            index = self.model_combo.count() - 1
            tips = [f"模型 ID：{meta['id']}"]
            context = meta.get("context")
            if isinstance(context, int) and context > 0:
                tips.append("上下文：" + (
                    f"{context / 1000000:g}M" if context >= 1000000
                    else f"{context // 1000}K"))
            efforts = meta.get("efforts") or []
            if efforts:
                tips.append("思考档位：" + " / ".join(str(e) for e in efforts))
            self.model_combo.setItemData(
                index, "\n".join(tips), Qt.ToolTipRole)
            if meta["id"] == current:
                selected = index

        if selected < 0:
            # 保存的模型不在当前服务商的列表里，优先跟随服务商配置
            env_model = provider_env().get("ANTHROPIC_MODEL", "")
            selected = self.model_combo.findData(env_model)
        if selected < 0 and not self._catalog_loaded:
            # 目录尚未取回时保留配置里的自定义模型名
            self.model_combo.addItem(current, current)
            selected = self.model_combo.count() - 1
        if selected < 0:
            selected = 0
        self.model_combo.setCurrentIndex(selected)
        self._model = self.model_combo.currentData()
        self.model_combo.blockSignals(False)
        self._populate_effort_combo()

    def _populate_effort_combo(self, preferred=None):
        """按所选模型在服务商目录里报告的档位重建思考下拉框（显示英文原值）。"""
        meta = self._model_catalog.get(self._model, {})
        levels = [str(e) for e in (meta.get("efforts") or [])
                  if str(e) in EFFORT_CLI_LEVELS]
        if not levels:
            # 服务商没有报告该模型的档位时才使用兜底
            levels = list(FALLBACK_EFFORTS)
        pending_effort = not self._catalog_loaded and "efforts" not in meta
        if (pending_effort and self._effort in EFFORT_CLI_LEVELS
                and self._effort not in levels):
            levels.append(self._effort)
        wanted = preferred or self._effort
        if wanted not in levels:
            wanted = meta.get("default_effort")
        if wanted not in levels:
            wanted = "high" if "high" in levels else levels[0]

        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        for effort in levels:
            self.effort_combo.addItem(effort, effort)
        self.effort_combo.setCurrentIndex(
            max(0, self.effort_combo.findData(wanted)))
        self.effort_combo.blockSignals(False)
        source = meta.get("source")
        tip = f"思考档位（服务商返回 {len(levels)} 档）：" + " / ".join(levels)
        tip += ("\n来源：" + source) if source else "\n来源：内置兜底列表"
        self.effort_combo.setToolTip(tip)

        effort = self.effort_combo.currentData()
        # 异步目录到达前，兜底选项不能覆盖已保存的自定义模型档位。
        if effort and effort != self._effort and not pending_effort:
            self._effort = effort
            self.config["effort"] = effort
            save_config(self.config)
            self.worker.set_effort(effort, resume=self._chat_resume_id)

    def _refresh_model_catalog(self, _checked=False):
        if self._catalog_thread is not None and self._catalog_thread.isRunning():
            return
        self._settings_mtime = provider_settings_mtime()
        self.btn_refresh_models.setEnabled(False)
        self.btn_refresh_models.setText("…")
        thread = ModelCatalogWorker(self)
        thread.loaded.connect(self._on_catalog_loaded)
        thread.finished.connect(self._on_catalog_finished)
        thread.finished.connect(thread.deleteLater)
        self._catalog_thread = thread
        thread.start()

    def _on_catalog_finished(self):
        self._catalog_thread = None
        self.btn_refresh_models.setText("↻")
        self._update_dual_controls()

    def _on_catalog_loaded(self, models, error):
        if self._chat_busy:
            # 正在回复时不要切换模型/思考强度，等空闲后再应用
            self._pending_catalog = (models, error)
            return
        if error or not models:
            # Codex 目录来自本地缓存，不依赖服务商网络请求是否成功。
            self._populate_dual_combos()
            self._refresh_balances()
            self.statusBar().showMessage(
                f"模型列表获取失败，继续使用内置列表：{error}"[:180], 6000)
            return
        old_model, old_effort = self._model, self._effort
        self._model_catalog = {m["id"]: m for m in models}
        self._catalog_loaded = True
        self._populate_model_combo()
        self._populate_dual_combos()
        self._refresh_balances()
        if self._model != old_model:
            self.config["model"] = self._model
            save_config(self.config)
            self.worker.set_model(self._model, resume=self._chat_resume_id)
        if self._effort != old_effort:
            self.config["effort"] = self._effort
            save_config(self.config)
            self.worker.set_effort(self._effort, resume=self._chat_resume_id)
        signature = provider_signature()
        if signature != self._provider_sig:
            # cc-switch 换服务商后，旧的 SDK 子进程仍连着原服务商
            self._provider_sig = signature
            self.worker.reconnect(resume=self._chat_resume_id)
        hosts = {m.get("source", "").split("/")[2] for m in models
                 if m.get("source", "").startswith("http")}
        level_sets = {tuple(m.get("efforts") or []) for m in models}
        level_sets.discard(())
        if len(level_sets) == 1:
            levels_text = " / ".join(next(iter(level_sets)))
        elif level_sets:
            levels_text = "各模型不同"
        else:
            levels_text = "服务商未声明，使用兜底"
        self.statusBar().showMessage(
            f"已从 {'/'.join(hosts) or '服务商'} 获取 {len(models)} 个模型"
            f" · 思考档位 {levels_text}", 6000)

    def changeEvent(self, event):
        super().changeEvent(event)
        # cc-switch 切换服务商会改写 settings.json，窗口重新激活时自动刷新
        if event.type() == QEvent.ActivationChange:
            if not self.isActiveWindow():
                # 切到其他应用时，进度浮窗也算“点击框外”，一并关闭
                popup = getattr(self, "_progress_popup", None)
                if popup is not None and popup.isVisible():
                    popup.hide()
            elif provider_settings_mtime() != self._settings_mtime:
                QTimer.singleShot(0, self._refresh_model_catalog)

    def _on_model_changed(self, index):
        model = self.model_combo.itemData(index)
        if not model:
            return
        self._model = model
        self.config["model"] = model
        save_config(self.config)
        self._populate_effort_combo()
        self.worker.set_model(model, resume=self._chat_resume_id)
        self._sync_study_runtime(model=model)
        if not self._dual_mode_on():
            self._refresh_balance("single", force=True)
        self.statusBar().showMessage(
            f"模型：{self.model_combo.currentText()}（已应用到对话）", 3000)

    def _on_effort_changed(self, index):
        effort = self.effort_combo.itemData(index)
        if not effort or effort == self._effort:
            return
        self._effort = effort
        self.config["effort"] = effort
        save_config(self.config)
        self.worker.set_effort(effort, resume=self._chat_resume_id)
        self._sync_study_runtime(effort=effort)
        self.statusBar().showMessage(
            f"思考强度：{effort}（已应用到对话）", 3000)

    def _on_permission_mode_changed(self, index):
        mode = self.perm_combo.itemData(index) or "default"
        if mode == "bypassPermissions":
            answer = QMessageBox.warning(
                self, "完全访问模式",
                "完全访问会跳过所有权限确认，Claude 可以直接修改文件、"
                "执行任意命令。\n\n仅在你完全信任当前项目时使用。是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.perm_combo.blockSignals(True)
                self.perm_combo.setCurrentIndex(self._last_perm_index)
                self.perm_combo.blockSignals(False)
                return
        self._last_perm_index = index
        self._permission_mode = mode
        self.config["permission_mode"] = mode
        save_config(self.config)
        self.worker.set_permission_mode(mode)
        self._sync_study_runtime(permission=mode)
        self.statusBar().showMessage(
            f"权限模式：{permission_display(mode)}", 3000)

    def _update_chat_buttons(self):
        has_project = bool(self.current_project)
        self.btn_send.setEnabled(
            has_project and not self._chat_busy
            and (SDK_AVAILABLE or self._dual_mode_on()))
        self.btn_stop.setEnabled(self._chat_busy)
        self.btn_new_chat.setEnabled(has_project)
        self.btn_continue_chat.setEnabled(has_project and not self._chat_busy)
        if hasattr(self, "btn_study_mode"):
            self.btn_study_mode.setEnabled(has_project and not self._chat_busy)
        if hasattr(self, "btn_media"):
            self.btn_media.setEnabled(
                has_project and not self._chat_busy
                and not self._chat_media_busy)
        self.perm_combo.setEnabled(not self._chat_busy)
        self.model_combo.setEnabled(not self._chat_busy)
        self.effort_combo.setEnabled(not self._chat_busy)
        self._update_dual_controls()
        self._update_chat_qa_buttons()

    def _show_empty_state(self, text):
        if self._chat_empty is not None:
            self._chat_empty.setText(text)
            self._chat_empty.show()
            return
        label = QLabel(text)
        label.setObjectName("ChatEmpty")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setContentsMargins(0, 60, 0, 0)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, label)
        self._chat_empty = label

    def _hide_empty_state(self):
        if self._chat_empty is not None:
            self._chat_empty.hide()

    def _clear_chat_view(self):
        # 使尚未执行的滚动回调失效，避免旧项目 / 旧渲染滚到新内容。
        self._chat_render_gen += 1
        self._chat_bulk_render = False
        self._chat_last_wrapper = None
        self._chat_last_user_wrapper = None
        self._pending_chat_anchor = None
        self._stream_timer.stop()
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._live_wrapper = None
        self._live_bubble = None
        self._stream_text = ""
        self._stream_dirty = False
        self._chat_empty = None
        self._chat_bubbles = []
        self._chat_selection = ("", "")
        # 选中问答跟着当前对话走：新对话/切换项目时清空，避免上下文串台
        self._chat_qa_messages = []
        self._chat_qa_next_id = 1
        self._chat_qa_answer_id = None
        self._chat_qa_live = ""
        self._chat_qa_pending_quote = ""
        if hasattr(self, "chat_qa_view"):
            self.chat_qa_view.clear()
            self.chat_qa_quote_hint.setText("可直接提问")

    def _add_bubble(self, role, text="", plain=False):
        """只创建界面控件；消息记录由 _append_chat_block 负责。"""
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        bubble = ChatBubble(role, text, plain=plain or role == "user",
                            base_dir=self.current_project or "")
        bubble.link_activated.connect(self._on_code_link)
        bubble.quote_requested.connect(self._quote_bubble)
        bubble.selection_changed.connect(self._remember_chat_selection)
        bubble.selection_asked.connect(self._ask_chat_selection_from)
        self._chat_bubbles.append(bubble)
        if role == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, wrapper)
        self._hide_empty_state()
        if self._chat_bulk_render:
            self._chat_last_wrapper = wrapper
            if role == "user":
                self._chat_last_user_wrapper = wrapper
        else:
            self._scroll_to_bottom()
        return wrapper, bubble

    def _append_chat_block(self, block):
        """把一条对话内容记到当前项目并显示出来。"""
        self._chat_state()["blocks"].append(block)
        self._render_chat_block(block)

    def _render_chat_block(self, block):
        kind = block.get("kind")
        if kind in ("user", "assistant", "master", "worker"):
            self._add_bubble(
                kind, block.get("text", ""),
                plain=bool(block.get("plain", kind == "user")))
        elif kind == "note":
            self._render_chat_note(block.get("text", ""))
        elif kind == "tool":
            self._render_tool_chip(
                block.get("name", "工具"), block.get("data") or {})

    def _render_chat_state(self):
        """按当前项目的记录重建对话区（切换项目或恢复时使用）。"""
        state = self._chat_state()
        self._clear_chat_view()
        gen = self._chat_render_gen
        project = self.current_project or ""
        # 批量重建期间不逐条滚到底，否则最后一帧会停在最下面。
        self._chat_bulk_render = True
        self._chat_last_wrapper = None
        self._chat_last_user_wrapper = None
        try:
            for block in state["blocks"]:
                self._render_chat_block(block)
            if state["live"]:
                self._live_wrapper, self._live_bubble = self._add_bubble(
                    "assistant", "", plain=True)
                self._live_bubble.set_plain(state["live"])
            if state.get("dual_live"):
                role = state.get("dual_last_role") or "assistant"
                wrapper, bubble = self._add_bubble(role, "", plain=True)
                bubble.set_plain(state["dual_live"])
                state["dual_live_wrapper"] = wrapper
                state["dual_live_bubble"] = bubble
        finally:
            self._chat_bulk_render = False
        target = self._chat_last_user_wrapper or self._chat_last_wrapper
        if target is not None:
            # 定位到最近一轮用户提问的顶部；没有用户消息时退回最后一条消息。
            self._scroll_to_wrapper_top(
                target, gen, project, self._chat_scroll_seq)
        if not state["blocks"] and not state["live"]:
            if state["path"]:
                self._show_empty_state("开始新的对话吧\n在下方输入消息")
            else:
                self._show_empty_state("请先在左侧选择一个项目")

    def _render_chat_note(self, text):
        label = QLabel(text)
        label.setObjectName("ChatNote")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, label)
        self._hide_empty_state()
        if self._chat_bulk_render:
            self._chat_last_wrapper = label
        else:
            self._scroll_to_bottom()

    def _add_chat_note(self, text):
        self._append_chat_block({"kind": "note", "text": text})

    def _render_tool_chip(self, name, data):
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(2, 0, 0, 0)
        row.setSpacing(0)
        chip = QFrame()
        chip.setObjectName("ToolChip")
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(10, 5, 10, 5)
        chip_layout.setSpacing(6)
        label = TOOL_LABELS.get(name, name)
        summary = tool_summary(name, data)
        text = f"⚙ {label}" + (f" · {summary}" if summary else "")
        text_label = QLabel(text)
        text_label.setObjectName("ToolText")
        chip_layout.addWidget(text_label)
        chip.setToolTip(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        row.addWidget(chip)
        row.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, wrapper)
        self._hide_empty_state()
        if self._chat_bulk_render:
            self._chat_last_wrapper = wrapper
        else:
            self._scroll_to_bottom()

    def _add_tool_chip(self, name, data):
        self._append_chat_block(
            {"kind": "tool", "name": name, "data": data})

    def _scroll_to_bottom(self):
        if self._chat_bulk_render:
            return
        self._chat_scroll_seq += 1
        bar = self.chat_scroll.verticalScrollBar()
        gen = self._chat_render_gen
        QTimer.singleShot(0, lambda: self._apply_scroll_bottom(bar, gen, final=False))

    def _apply_scroll_bottom(self, bar, gen, final=True):
        # 渲染代次已变（切换项目 / 重建）时丢弃过期回调。
        if gen != self._chat_render_gen:
            return
        bar.setValue(bar.maximum())
        if not final:
            # 新内容插入后布局可能再长一帧；延后校准到真正的底部。
            QTimer.singleShot(
                0, lambda: self._apply_scroll_bottom(bar, gen, final=True))

    def _scroll_to_wrapper_top(self, wrapper, gen, project, seq):
        """把某个气泡（最近一轮用户提问）定位到聊天视口顶部。"""
        QTimer.singleShot(
            0, lambda: self._apply_wrapper_top(
                wrapper, gen, project, seq, final=False))

    def _apply_pending_chat_anchor(self):
        """切回对话页后应用此前暂存的定位。"""
        pending = self._pending_chat_anchor
        if pending is None:
            return
        wrapper, gen, project, seq = pending
        if (gen != self._chat_render_gen
                or project != (self.current_project or "")
                or seq != self._chat_scroll_seq):
            self._pending_chat_anchor = None
            return
        if wrapper.isHidden() or not wrapper.isVisible():
            return
        self._apply_wrapper_top(wrapper, gen, project, seq, final=True)

    def _apply_wrapper_top(self, wrapper, gen, project, seq, final=True):
        if gen != self._chat_render_gen or project != (self.current_project or ""):
            return
        # 期间用户已发送新消息 / 流式输出要滚到底时，让位给新的滚动意图。
        if seq != self._chat_scroll_seq:
            return
        if wrapper is None or wrapper.isHidden():
            return
        if not wrapper.isVisible():
            # 对话页不在前台（例如切到代码开发页）：等切回对话页再定位。
            self._pending_chat_anchor = (wrapper, gen, project, seq)
            return
        self._pending_chat_anchor = None
        # 先让刚插入的气泡完成布局，第二次回调再校准最终位置。
        self.chat_layout.activate()
        self.chat_body_widget.adjustSize()
        bar = self.chat_scroll.verticalScrollBar()
        y = wrapper.mapTo(self.chat_body_widget, QPoint(0, 0)).y()
        bar.setValue(max(0, min(y, bar.maximum())))
        if not final:
            QTimer.singleShot(
                0, lambda: self._apply_wrapper_top(
                    wrapper, gen, project, seq, final=True))

    def _prepare_worker(self):
        mode = self._current_permission_mode()
        if self._needs_reconfigure or self._chat_cwd != self.current_project:
            self.worker.configure(
                self.current_project, mode, resume=self._chat_resume_id,
                model=self._current_model(), effort=self._current_effort())
            self._chat_cwd = self.current_project
            self._needs_reconfigure = False
        else:
            self.worker.set_permission_mode(mode)

    def _send_chat_message(self):
        text = self.chat_input.toPlainText().strip()
        if not text and not self.chat_attachments:
            return
        if not self.current_project:
            self.statusBar().showMessage("请先选择一个项目", 3000)
            return
        media_command = self._parse_media_command(text)
        if media_command is not None:
            self._handle_media_command(*media_command)
            return
        if not SDK_AVAILABLE and not self.dual_check.isChecked():
            QMessageBox.warning(
                self, "缺少对话组件",
                "未安装 Claude Agent SDK，无法在界面内对话。\n\n"
                f"{SDK_IMPORT_ERROR}")
            return
        if self._chat_busy:
            return
        if self._attachments_pending():
            self.statusBar().showMessage("图片还在识别中，请稍候再发送…", 3000)
            return
        prompt, display = self._compose_message(text)
        quote_prompt, quote_display = self._quote_prefix()
        if quote_prompt:
            prompt = quote_prompt + prompt
            display = quote_display + display
            self._clear_quote()
        if self.btn_study_mode.isChecked():
            if self.dual_check.isChecked():
                self.statusBar().showMessage(
                    "研读模式使用单模型；已按单模型流程发送", 3500)
            self._send_study_learning(prompt, display)
            self._clear_attachments()
            return
        if self.dual_check.isChecked():
            self._study_pending = False
            self._send_dual_message(prompt, display)
            return
        self._study_pending = False
        self._prepare_worker()
        self._append_chat_block(
            {"kind": "user", "text": display, "plain": True})
        self.chat_input.clear()
        self._clear_attachments()
        self._reset_live_stream()
        self.worker.send(prompt)

    def _reset_live_stream(self):
        self._chat_state()["live"] = ""
        self._live_wrapper = None
        self._live_bubble = None
        self._stream_text = ""
        self._stream_dirty = False

    # ------------------------------------------------------ quote & media --
    def _quote_bubble(self, role, text):
        text = (text or "").strip()
        if not text:
            return
        self._quote_target = {"role": role, "text": text}
        snippet = " ".join(text.split())
        if len(snippet) > 100:
            snippet = snippet[:100] + "…"
        who = "你" if role == "user" else "Claude"
        self.quote_bar_label.setText(f"↩ 引用{who}：{snippet}")
        self.quote_bar.setVisible(True)
        self.chat_input.setFocus()

    def _clear_quote(self):
        self._quote_target = None
        self.quote_bar.setVisible(False)
        self.quote_bar_label.setText("")

    def _quote_prefix(self):
        target = self._quote_target
        if not target:
            return "", ""
        raw = str(target.get("text") or "").strip()
        if not raw:
            return "", ""
        who = {"user": "你", "master": "主 AI",
               "worker": "执行 AI"}.get(target.get("role"), "Claude")
        snippet = " ".join(raw.split())
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        if len(raw) > 4000:
            raw = raw[:4000] + "\n…（引用内容已截断）"
        prompt = (
            f"【引用回复】用户正在回复下面这条{who}之前的消息：\n"
            f"<<<\n{raw}\n>>>\n\n")
        return prompt, f"↩ 引用{who}：{snippet}\n\n"

    # -------------------------------------------- 主对话选中内容问答 --
    def _build_chat_qa_window(self):
        """主对话的选中内容问答悬浮窗，交互与研读页的解释栏问答一致。"""
        window = ChatQaWindow()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("对话问答")
        title.setObjectName("H2")
        self.chat_qa_quote_hint = QLabel("可直接提问")
        self.chat_qa_quote_hint.setObjectName("Muted")
        recall_hint = QLabel("Ctrl+Shift+W 召回")
        recall_hint.setObjectName("Muted")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.chat_qa_quote_hint)
        head.addWidget(recall_hint)
        layout.addLayout(head)

        self.chat_qa_view = QTextBrowser()
        self.chat_qa_view.setOpenLinks(False)
        self.chat_qa_view.setOpenExternalLinks(False)
        self.chat_qa_view.anchorClicked.connect(self._on_chat_qa_anchor_clicked)
        self.chat_qa_view.setStyleSheet(
            "QTextBrowser { background: #fffafc; border: 1px solid #f6d9e6;"
            " border-radius: 8px; padding: 8px; color: #4b3742; }")
        self.chat_qa_view.setPlaceholderText(
            "可以直接在下方提问；若先在对话里选中一段内容，问题会自动带上引用")
        layout.addWidget(self.chat_qa_view, 1)

        # 问答窗口也支持粘贴/拖入图片：先 OCR，再把识别结果随问题发送
        self.chat_qa_attachments = []
        self.chat_qa_attachment_bar = QWidget()
        self.chat_qa_attachment_bar.setObjectName("AttachmentBar")
        self.chat_qa_attachment_layout = QHBoxLayout(
            self.chat_qa_attachment_bar)
        self.chat_qa_attachment_layout.setContentsMargins(0, 0, 0, 4)
        self.chat_qa_attachment_layout.setSpacing(8)
        self.chat_qa_attachment_layout.addStretch(1)
        self.chat_qa_attachment_bar.setVisible(False)
        layout.addWidget(self.chat_qa_attachment_bar)

        self.chat_qa_input = ChatInput()
        self.chat_qa_input.setObjectName("ChatInput")
        self.chat_qa_input.setFixedHeight(84)
        self.chat_qa_input.setPlaceholderText(
            "直接输入问题，Enter 发送，Shift+Enter 换行；"
            "可粘贴/拖入图片自动识别（可选：先在对话里选中内容）")
        self.chat_qa_input.submitted.connect(self._send_chat_qa)
        self.chat_qa_input.image_pasted.connect(self._add_chat_qa_image)
        layout.addWidget(self.chat_qa_input)

        bottom = QHBoxLayout()
        self.chat_qa_status = QLabel("")
        self.chat_qa_status.setObjectName("Muted")
        bottom.addWidget(self.chat_qa_status, 1)
        self.chat_qa_send_btn = QPushButton("发送")
        self.chat_qa_send_btn.setObjectName("Primary")
        self.chat_qa_send_btn.clicked.connect(self._send_chat_qa)
        bottom.addWidget(self.chat_qa_send_btn)
        layout.addLayout(bottom)

        window.setCentralWidget(container)
        self.chat_qa_window = window

    def _show_chat_qa_window(self):
        window = getattr(self, "chat_qa_window", None)
        if window is None:
            return
        if not window.isVisible():
            anchor = self.frameGeometry()
            window.move(
                max(anchor.left() + 40, anchor.right() - window.width() - 40),
                anchor.top() + 90)
        window.show()
        window.raise_()
        window.activateWindow()

    def _remember_chat_selection(self, text, role):
        text = (text or "").strip()
        self._chat_selection = (text, role or "") if text else ("", "")

    def _selected_chat_text(self):
        """当前对话气泡里的选区优先，其次是最近一次记录，最后是输入框。"""
        for bubble in reversed(self._chat_bubbles):
            text = bubble.selected_text()
            if text:
                return text, bubble.role
        text, role = self._chat_selection
        if text:
            return text, role
        cursor = self.chat_input.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText().replace(
                " ", "\n").strip()
            return selected, "user"
        return "", ""

    def _ask_chat_selection(self, text=None, role=None):
        if isinstance(text, bool):  # QPushButton.clicked 的 checked 参数
            text = None
        self._show_chat_qa_window()
        if not text:
            text, role = self._selected_chat_text()
        text = (text or "").replace(" ", "\n").strip()
        if not text:
            self.chat_qa_quote_hint.setText("请先在对话气泡里选中一段内容")
            self.chat_qa_input.setFocus()
            return
        self._chat_qa_pending_quote = text
        who = "你" if role == "user" else "Claude"
        shown = text if len(text) <= 40 else text[:40] + "…"
        self.chat_qa_quote_hint.setText(f"已引用{who}的消息：{shown}")
        quote_lines = "\n".join(
            "> " + line for line in text.splitlines() if line.strip())
        self.chat_qa_input.setPlainText(f"关于{who}的这段内容：\n{quote_lines}\n\n")
        cursor = self.chat_qa_input.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_qa_input.setTextCursor(cursor)
        self.chat_qa_input.setFocus()

    def _ask_chat_selection_from(self, text, role):
        self._ask_chat_selection(text, role)

    # ------------------------------------------------------------ 进度查询 --
    @staticmethod
    def _dual_running_snapshot(state):
        """双 AI 协作线程正在运行时返回它的实时快照。

        协作模式不使用单模型 worker，所以必须单独采集，否则「进度查询」
        会误报“没有运行中的任务”。
        """
        thread = (state or {}).get("dual_thread")
        if thread is None:
            return None
        try:
            if not thread.isRunning():
                return None
            snapshot = (thread.status_snapshot()
                        if hasattr(thread, "status_snapshot") else {"busy": True})
        except RuntimeError:
            # QThread 已销毁
            return None
        snapshot = dict(snapshot or {})
        snapshot["busy"] = True
        return snapshot

    def _progress_worker_snapshot(self):
        """优先报告正在运行的对话：当前项目 → 其他项目 → 问答窗口。

        每个项目有独立的单模型 worker 和协作线程，任一在运行都应报告；
        全都空闲时才回落为“无任务”。
        """
        state = self._chat_state()
        worker = state.get("worker")
        worker_snapshot = worker.status_snapshot() if worker is not None else None

        snapshot = self._dual_running_snapshot(state)
        if snapshot is not None:
            return snapshot, "主对话 · 协作"
        if worker_snapshot is not None and worker_snapshot.get("busy"):
            return worker_snapshot, "主对话"

        qa_worker = getattr(self, "_chat_qa_worker", None)
        qa_snapshot = qa_worker.status_snapshot() if qa_worker is not None else None
        if qa_snapshot is not None and qa_snapshot.get("busy"):
            return qa_snapshot, "问答窗口"

        # 后台项目仍在运行：切换项目后也应能看到真实进度。
        for key, other in list(getattr(self, "_chat_states", {}).items()):
            if other is state:
                continue
            name = os.path.basename(str(key or "").rstrip("/")) or "其他项目"
            snapshot = self._dual_running_snapshot(other)
            if snapshot is not None:
                return snapshot, f"{name} · 协作"
            other_worker = other.get("worker")
            if other_worker is not None:
                other_snapshot = other_worker.status_snapshot()
                if other_snapshot.get("busy"):
                    return other_snapshot, f"{name} · 主对话"

        if worker_snapshot is not None:
            return worker_snapshot, "主对话"
        if qa_snapshot is not None:
            return qa_snapshot, "问答窗口"
        return None, "主对话"

    def _progress_markup(self, snapshot, channel):
        def fmt_time(seconds):
            seconds = int(max(0, seconds or 0))
            return f"{seconds // 60:02d}:{seconds % 60:02d}"

        busy = bool(snapshot.get("busy"))
        pending = int(snapshot.get("pending_permissions") or 0)
        mode = snapshot.get("permission_mode") or "default"
        dev_mode = bool(snapshot.get("dev_mode"))
        tool = str(snapshot.get("last_tool") or "")
        pending_tools = [
            str(item) for item in (snapshot.get("pending_tools") or []) if item]
        if pending_tools:
            tool = pending_tools[-1] or tool
        permission_tool = str(snapshot.get("permission_tool") or tool)
        since = snapshot.get("since_activity")
        elapsed = snapshot.get("elapsed") or 0.0
        tool_count = int(snapshot.get("tool_count") or 0)
        last_error = str(snapshot.get("last_error") or "")
        # 后台进程（编译/打包/下载）不受对话回合结束影响，单独采样
        try:
            bg_snapshot = background_monitor.snapshot()
        except Exception:
            bg_snapshot = {}
        bg_tasks = bg_snapshot.get("tasks") or []

        if dev_mode:
            permission = "✅ 正常 · 开发模式自动放行"
        elif mode == "bypassPermissions":
            permission = "✅ 正常 · 完全访问（不会等待权限）"
        else:
            permission = f"✅ 正常 · {permission_display(mode)}"
            if busy and not pending:
                permission += "（当前无待确认请求）"
        if pending:
            permission = (
                "⚠️ 等待权限确认：<b>"
                f"{html.escape(permission_tool or '未知工具')}</b>")

        if not busy:
            if bg_tasks:
                progress = "对话回合已结束，但仍有后台任务在运行（见下方）"
                eta = "请以后台任务的预计剩余时间为准"
            else:
                progress = "当前没有运行中的任务"
                eta = "—"
            if last_error:
                progress = f"上一轮出错：{html.escape(last_error[:120])}"
        elif pending:
            progress = "⏸ 已暂停，等待你批准或拒绝权限"
            eta = "授权后约 1–3 分钟（粗略）"
        else:
            if since is None:
                progress = "⏳ 已启动，等待首个输出"
                eta = "约 1–2 分钟"
            elif since <= 10:
                progress = f"✅ 正常推进 · {since:.0f} 秒前仍有输出"
                eta = "约 1–2 分钟"
            elif since <= 45:
                progress = f"✅ 正常 · 最近 {since:.0f} 秒无新输出"
                eta = "约 2–5 分钟"
            elif since <= 120:
                progress = (
                    f"⚠️ 较慢 · 已 {since:.0f} 秒无输出，可能在执行长任务")
                eta = "约 3–10 分钟，建议继续观察"
            else:
                progress = f"⚠️ 可能卡住 · 已 {since:.0f} 秒无输出"
                eta = "无法估计，建议检查权限或停止重试"
            if tool:
                progress += (
                    f"<br>最近工具：{html.escape(tool)}"
                    f"（本轮 {tool_count} 次）")
            if last_error:
                progress += (
                    f"<br>最近错误：{html.escape(last_error[:120])}")

        activity = str(snapshot.get("activity_label") or "")
        lines = [
            f"<b>进度查询</b> · {html.escape(channel)}",
            f"权限：{permission}",
            f"进度：{progress}",
            f"预计：{eta}",
        ]
        if snapshot.get("dual"):
            role_label = str(snapshot.get("role_label") or "")
            task_label = str(snapshot.get("task_label") or "")
            tier = str(snapshot.get("tier") or "")
            parts = ([f"档次 {html.escape(tier)}"] if tier else []) + [
                html.escape(x) for x in (role_label, task_label) if x]
            if "review_enabled" in snapshot:
                parts.append("复查 开" if snapshot["review_enabled"]
                             else "复查 关")
            if parts:
                lines.insert(1, "协作：" + " · ".join(parts))
        if busy:
            lines.append(f"已运行：{fmt_time(elapsed)}")
            if activity:
                lines.append(f"最近活动：{html.escape(activity)}")
        lines.extend(background_monitor.render_markup(bg_snapshot))
        return "<br>".join(lines)

    def _progress_markup_now(self):
        snapshot, channel = self._progress_worker_snapshot()
        if snapshot is None:
            # 还没创建会话进程时也给出一致的空闲状态，而不是“无可用进程”。
            snapshot = {
                "busy": False,
                "permission_mode": self._current_permission_mode(),
            }
            channel = "主对话"
        return self._progress_markup(snapshot, channel)

    def _show_progress_popup(self):
        markup = self._progress_markup_now()
        popup = getattr(self, "_progress_popup", None)
        if popup is None:
            popup = ProgressPopup(self)
            self._progress_popup = popup
        popup.show_for(self.btn_progress, markup)

    def _refresh_progress_popup(self):
        """浮窗打开期间持续刷新，显示实时进度、网速和预计剩余时间。"""
        popup = getattr(self, "_progress_popup", None)
        if popup is None or not popup.isVisible():
            return
        popup.show_for(self.btn_progress, self._progress_markup_now())

    def _watch_background_tasks(self):
        """后台任务消失时主动提示，避免回合结束后用户还得反复询问。"""
        try:
            snap = background_monitor.snapshot()
        except Exception:
            return
        now = time.monotonic()
        current = {}
        for task in snap.get("tasks") or []:
            pid = task.get("root_pid")
            if pid is None:
                continue
            current[pid] = task
        for pid, task in current.items():
            seen = self._bg_seen_tasks.get(pid)
            if seen is None:
                self._bg_seen_tasks[pid] = {
                    "label": task.get("label") or "后台任务",
                    "start": now - float(task.get("elapsed") or 0.0),
                }
            else:
                seen["label"] = task.get("label") or seen["label"]
        finished = []
        for pid in list(self._bg_seen_tasks):
            if pid in current:
                continue
            info = self._bg_seen_tasks.pop(pid)
            duration = now - info.get("start", now)
            if duration < 5:
                continue
            text = (f"后台任务已完成：{info.get('label') or '任务'}"
                    f"（用时 {background_monitor.format_duration(duration)}）")
            finished.append(text)
            self.statusBar().showMessage(text, 12000)
            self._notify_desktop(APP_NAME, text)
        if finished and not current:
            # 最后一个后台任务结束，恢复对话状态文字
            try:
                if self.chat_status.text().startswith("回合已完成"):
                    self.chat_status.setText("已完成（后台任务已结束）")
            except Exception:
                pass

    def _notify_desktop(self, title, text):
        try:
            if shutil.which("notify-send"):
                subprocess.Popen(
                    ["notify-send", "--app-name", APP_NAME, title, text],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except Exception:
            pass
        QApplication.alert(self)

    def _recent_chat_context(self, max_chars=9000):
        lines = []
        for bubble in self._chat_bubbles:
            text = (bubble.raw_text or "").strip()
            if not text:
                continue
            who = "用户" if bubble.role == "user" else "Claude"
            lines.append(f"{who}：{text}")
        context = "\n\n".join(lines)
        if len(context) > max_chars:
            context = "…（更早内容已省略）\n" + context[-max_chars:]
        return context

    def _ensure_chat_qa_worker(self):
        if self._chat_qa_worker is None:
            worker = ClaudeWorker(self)
            worker.text_delta.connect(self._on_chat_qa_delta)
            worker.assistant.connect(self._on_chat_qa_assistant)
            worker.result.connect(self._on_chat_qa_result)
            worker.error.connect(self._on_chat_qa_error)
            worker.busy_changed.connect(self._on_chat_qa_busy)
            worker.permission_asked.connect(self._on_chat_qa_permission)
            worker.start()
            self._chat_qa_worker = worker
        config = (self.current_project, self._current_permission_mode(),
                  self._current_model(), self._current_effort())
        if self._chat_qa_worker_cfg != config:
            self._chat_qa_worker.configure(
                self.current_project, self._current_permission_mode(),
                model=self._current_model(), effort=self._current_effort())
            self._chat_qa_worker_cfg = config
        return self._chat_qa_worker

    def _update_chat_qa_buttons(self):
        has_ui = hasattr(self, "chat_qa_send_btn")
        if not has_ui:
            return
        ready = bool(self.current_project) and SDK_AVAILABLE
        pending = self._chat_qa_attachments_pending()
        self.chat_qa_send_btn.setEnabled(
            ready and not self._chat_qa_busy and not pending)
        if self._chat_qa_busy:
            self.chat_qa_status.setText("Claude 正在回答…")
        elif pending:
            self.chat_qa_status.setText("图片还在识别中…")
        else:
            self.chat_qa_status.setText("")

    def _send_chat_qa(self):
        text = self.chat_qa_input.toPlainText().strip()
        if not text and not self.chat_qa_attachments:
            return
        if not self.current_project:
            QMessageBox.information(
                self, "选中内容问答", "请先在左侧选择一个项目目录。")
            return
        if not SDK_AVAILABLE:
            QMessageBox.warning(
                self, "缺少对话组件",
                "未安装 Claude Agent SDK，无法进行问答。\n\n"
                f"{SDK_IMPORT_ERROR}")
            return
        if self._chat_qa_busy:
            return
        if self._chat_qa_attachments_pending():
            self.chat_qa_status.setText("图片还在识别中，请稍候再发送…")
            return
        prompt_text, display_text = self._chat_qa_compose_message(text)
        self._ensure_chat_qa_worker()
        quote = self._chat_qa_pending_quote
        self._chat_qa_pending_quote = ""
        self.chat_qa_quote_hint.setText("未选中文字")
        self.chat_qa_input.clear()
        self._clear_chat_qa_attachments()

        user_id = self._chat_qa_next_id
        self._chat_qa_next_id += 1
        answer_id = self._chat_qa_next_id
        self._chat_qa_next_id += 1
        self._chat_qa_messages.append({
            "id": user_id, "role": "user", "text": display_text,
            "merged": False, "streaming": False})
        self._chat_qa_messages.append({
            "id": answer_id, "role": "assistant",
            "text": "_正在回答…_", "merged": False, "streaming": True})
        self._chat_qa_answer_id = answer_id
        self._chat_qa_live = ""
        self._render_chat_qa()
        self._chat_qa_busy = True
        self._update_chat_qa_buttons()

        parts = [
            "【主对话 · 问答窗口】" if not quote
            else "【主对话 · 选中内容问答】",
            f"项目根目录：{self.current_project}",
            "",
            "用户直接打开了问答窗口提问。" if not quote
            else "用户在主对话里选中了一段内容，并针对它提问。",
            "最近对话上下文（只读，用来理解指代，不要逐条复述）：",
            "<<<",
            self._recent_chat_context() or "（空）",
            ">>>",
        ]
        if quote:
            parts += ["", "用户选中的内容：", "<<<", quote, ">>>"]
        code_map = project_code_map(self.current_project, limit_chars=5000)
        if code_map:
            parts += [
                "",
                "项目符号索引（真实文件与行号，可直接引用）：",
                "<<<",
                code_map,
                ">>>",
            ]
        parts += [
            "",
            "用户的问题：",
            prompt_text,
            "",
            "请直接、简洁地回答这个局部疑问，用中文和 Markdown。",
            "如果需要指向源码，请使用 "
            "[[code:相对路径:起行-止行|关键函数或类名]] 的格式"
            "（路径相对项目根目录，行号必须来自实际文件，不要估算），"
            "不要大段粘贴代码。",
        ]
        self._chat_qa_worker.send("\n".join(parts))

    def _chat_qa_message(self, qa_id):
        return next(
            (m for m in self._chat_qa_messages if m.get("id") == qa_id), None)

    def _chat_qa_index(self, qa_id):
        return next(
            (i for i, m in enumerate(self._chat_qa_messages)
             if m.get("id") == qa_id), -1)

    def _render_chat_qa(self):
        if not self._chat_qa_messages:
            self.chat_qa_view.clear()
            return
        blocks = []
        for message in visible_qa_messages(self._chat_qa_messages):
            role = message.get("role")
            prefix = {"user": "**你**", "assistant": "**Claude**",
                      "error": "**出错**"}.get(role, "**Claude**")
            text = str(message.get("text") or "")
            if role == "assistant" and not message.get("streaming"):
                qa_id = message.get("id")
                if message.get("merged"):
                    actions = (f"✅ 已插入对话 · "
                               f"**[删除](chatqa://delete/{qa_id})**")
                else:
                    actions = (f"**[➕ 插入对话](chatqa://insert/{qa_id})** · "
                               f"**[🗑 删除](chatqa://delete/{qa_id})**")
                text = text.rstrip() + "\n\n" + actions
            blocks.append(f"{prefix}\n\n{text}")
        rendered = study_refs_to_markdown("\n\n---\n\n".join(blocks))
        self.chat_qa_view.setMarkdown(rendered)
        bar = self.chat_qa_view.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def _on_chat_qa_delta(self, chunk):
        self._chat_qa_live += chunk
        if not self._chat_qa_timer.isActive():
            self._chat_qa_timer.start()

    def _flush_chat_qa_live(self):
        message = self._chat_qa_message(self._chat_qa_answer_id)
        if message is None:
            self._chat_qa_timer.stop()
            return
        message["text"] = self._chat_qa_live + (
            "\n\n▍" if self._chat_qa_live else "")
        message["streaming"] = True
        self._render_chat_qa()
        if not self._chat_qa_live:
            self._chat_qa_timer.stop()

    def _on_chat_qa_assistant(self, text, _tools):
        self._chat_qa_timer.stop()
        answer = text or self._chat_qa_live
        if not answer:
            return
        message = self._chat_qa_message(self._chat_qa_answer_id)
        if message is not None:
            message["text"] = answer
            message["streaming"] = False
        self._chat_qa_live = ""
        self._render_chat_qa()

    def _on_chat_qa_result(self, info):
        self._chat_qa_timer.stop()
        message = self._chat_qa_message(self._chat_qa_answer_id)
        if message is not None:
            message["streaming"] = False
            if message.get("text") in ("", "_正在回答…_"):
                message["text"] = (
                    info.get("result")
                    or ("（没有返回内容）" if not info.get("is_error")
                        else ""))
        self._chat_qa_busy = False
        self._update_chat_qa_buttons()
        if info.get("is_error"):
            self._chat_qa_messages.append({
                "id": self._chat_qa_next_id, "role": "error",
                "text": info.get("result") or "Claude 返回了错误",
                "merged": False, "streaming": False})
            self._chat_qa_next_id += 1
        self._render_chat_qa()

    def _on_chat_qa_error(self, message):
        self._chat_qa_timer.stop()
        self._chat_qa_busy = False
        self._chat_qa_messages.append({
            "id": self._chat_qa_next_id, "role": "error",
            "text": message, "merged": False, "streaming": False})
        self._chat_qa_next_id += 1
        self._render_chat_qa()
        self._update_chat_qa_buttons()

    def _on_chat_qa_busy(self, busy):
        self._chat_qa_busy = busy
        self._update_chat_qa_buttons()

    def _on_chat_qa_permission(self, request_id, tool_name, input_text):
        dialog = PermissionDialog(
            tool_name, input_text, getattr(self, "chat_qa_window", self))
        allowed = dialog.exec_() == QDialog.Accepted
        self._chat_qa_worker.respond_permission(request_id, allowed)

    def _on_chat_qa_anchor_clicked(self, url):
        href = url.toString()
        match = re.match(r"chatqa://(insert|delete)/(\d+)", href)
        if match:
            qa_id = int(match.group(2))
            if match.group(1) == "insert":
                self._insert_chat_qa_answer(qa_id)
            else:
                self._delete_chat_qa_answer(qa_id)
            return
        if href.startswith("file://"):
            QDesktopServices.openUrl(url)
            return
        if href.startswith(("px4code://", "code://")):
            self._on_code_link(href)
        else:
            QDesktopServices.openUrl(url)

    def _insert_chat_qa_answer(self, qa_id):
        index = self._chat_qa_index(qa_id)
        if index < 0:
            return
        message = self._chat_qa_messages[index]
        if message.get("role") != "assistant":
            return
        if message.get("merged"):
            self.chat_qa_status.setText("这条回答已经插入对话了")
            return
        answer = str(message.get("text") or "").strip()
        if not answer:
            return
        self._append_chat_block({"kind": "assistant", "text": answer})
        message["merged"] = True
        self._render_chat_qa()
        self.chat_qa_status.setText("已把这条回答插入主对话")
        self.statusBar().showMessage("选中问答回答已插入主对话", 4000)

    def _delete_chat_qa_answer(self, qa_id):
        index = self._chat_qa_index(qa_id)
        if index < 0:
            return
        message = self._chat_qa_messages[index]
        if message.get("role") != "assistant":
            return
        if index > 0 and self._chat_qa_messages[index - 1].get("role") == "user":
            del self._chat_qa_messages[index - 1:index + 1]
        else:
            del self._chat_qa_messages[index]
        self._render_chat_qa()
        self.chat_qa_status.setText("已删除这条问答")

    def _parse_media_command(self, text):
        stripped = (text or "").strip()
        lowered = stripped.lower()
        pairs = (("image", IMAGE_COMMAND_PREFIXES),
                 ("video", VIDEO_COMMAND_PREFIXES))
        for kind, prefixes in pairs:
            for prefix in prefixes:
                if lowered.startswith(prefix.lower()):
                    prompt = stripped[len(prefix):].strip()
                    if prompt:
                        return kind, prompt
        return None

    @staticmethod
    def _default_media_values(kind, prompt):
        if kind == "image":
            return {"prompt": prompt, "size": "1:1"}
        backend = ("siliconflow" if read_secret_key("SILICONFLOW_API_KEY")
                   else "agnes")
        return {
            "prompt": prompt,
            "video_backend": backend,
            "seconds": 5,
            "video_size": "768p" if backend == "siliconflow" else "720P",
            "ratio": "16:9",
        }

    def _handle_media_command(self, kind, prompt):
        self.chat_input.clear()
        self._clear_attachments()
        self._clear_quote()
        if self.btn_study_mode.isChecked():
            self.study_page.set_project(self.current_project)
            self.study_page.set_runtime(
                self._current_permission_mode(),
                self._current_model(), self._current_effort())
            self._show_study_page()
            self.study_page.generate_media(kind, prompt)
            return
        self._start_chat_media(kind, self._default_media_values(kind, prompt))

    def _prompt_media(self, kind):
        if not self.current_project:
            self.statusBar().showMessage("请先选择一个项目", 3000)
            return
        default = self.chat_input.toPlainText().strip()
        parsed = self._parse_media_command(default)
        if parsed is not None:
            default = parsed[1]
        dialog = MediaPromptDialog(kind, default, parent=self)
        if dialog.exec_() != QDialog.Accepted:
            return
        values = dialog.values()
        if self.btn_study_mode.isChecked():
            self.study_page.set_project(self.current_project)
            self.study_page.set_runtime(
                self._current_permission_mode(),
                self._current_model(), self._current_effort())
            self._show_study_page()
            self.study_page._start_media(kind, values)
            return
        self._start_chat_media(kind, values)

    def _start_chat_media(self, kind, values):
        if self._chat_media_busy:
            self.statusBar().showMessage("正在生成，请稍候…", 3000)
            return
        if not self.current_project:
            self.statusBar().showMessage("请先选择一个项目", 3000)
            return
        backend = values.get("video_backend") or ""
        if kind == "video" and not backend:
            backend = ("siliconflow" if read_secret_key("SILICONFLOW_API_KEY")
                       else "agnes")
        if kind == "video" and backend == "siliconflow":
            script = BASE_DIR / "siliconflow_video.py"
            provider_text = "SiliconFlow"
        else:
            script_name = ("generate.py" if kind == "image"
                           else "generate_video.py")
            script = find_media_script(script_name)
            provider_text = "media-tools"
        if script is None:
            QMessageBox.information(
                self, "缺少生成脚本",
                "没有找到 media-tools 技能。\n\n"
                "请到「技能与插件 → 技能库 → 从 GitHub 安装」安装：\n"
                "147-sys/dsh-media-skills")
            return
        out_dir = Path(self.current_project) / ".claude-console" / "generated"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "生成媒体", f"无法创建输出目录：{exc}")
            return
        suffix = ".png" if kind == "image" else ".mp4"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = out_dir / f"{kind}-{stamp}{suffix}"
        prompt = values.get("prompt") or ""
        display_prompt = values.get("display_prompt") or prompt
        if kind == "image":
            args = [prompt, str(output), values.get("size") or "1:1"]
        elif backend == "siliconflow":
            args = [prompt, str(output),
                    "--seconds", str(values.get("seconds") or 5),
                    "--resolution", values.get("video_size") or "768p",
                    "--ratio", values.get("ratio") or "16:9"]
        else:
            args = [prompt, str(output),
                    "--seconds", str(values.get("seconds") or 5),
                    "--size", values.get("video_size") or "720P",
                    "--ratio", values.get("ratio") or "16:9"]
        self._append_chat_block(
            {"kind": "user",
             "text": ("🖼 生成图片：" if kind == "image" else "🎬 生成视频：")
             + display_prompt, "plain": True})
        progress = TaskProgressDialog(
            "生成图片" if kind == "image" else "生成视频",
            f"正在调用 {provider_text} 生成，请稍候…\n"
            + ("图片通常几秒到一分钟。" if kind == "image"
               else "视频通常需要几分钟。"), self)
        progress.set_busy("正在连接服务商…")
        progress.show()
        worker = MediaWorker(
            script, args, output, cwd=self.current_project, parent=self)
        self._chat_media_worker = worker
        self._chat_media_dialog = progress
        self._chat_media_busy = True
        self._update_chat_buttons()
        worker.log.connect(progress.set_busy)
        progress.canceled.connect(worker.cancel)

        def done(path):
            self._chat_media_worker = None
            self._chat_media_busy = False
            self._update_chat_buttons()
            progress.mark_done()
            self._add_media_bubble(kind, path)
            self.chat_status.setText(
                "图片已生成" if kind == "image" else "视频已生成")
            QTimer.singleShot(900, progress.close)

        def failed(message):
            cancelled = worker._cancelled
            self._chat_media_worker = None
            self._chat_media_busy = False
            self._update_chat_buttons()
            progress.close()
            if cancelled:
                self.statusBar().showMessage("已取消生成", 4000)
                return
            QMessageBox.warning(self, "生成失败", message)

        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _add_media_bubble(self, kind, path):
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        widget = MediaResultWidget(
            path, kind,
            "已生成图片" if kind == "image" else "已生成视频", wrapper)
        row.addWidget(widget)
        row.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, wrapper)
        self._hide_empty_state()
        self._scroll_to_bottom()

    def _cancel_chat_media(self):
        worker = self._chat_media_worker
        if worker is not None:
            worker.cancel()
        self._chat_media_worker = None
        self._chat_media_busy = False
        dialog = self._chat_media_dialog
        if dialog is not None:
            dialog.close()
        self._chat_media_dialog = None
        self._update_chat_buttons()

    # ------------------------------------------------------ image attachments --
    @staticmethod
    def _png_bytes(image):
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        data = bytes(buffer.data())
        buffer.close()
        return data

    def _add_pasted_image(self, image):
        if image is None or image.isNull():
            return
        if len(self.chat_attachments) >= 8:
            self.statusBar().showMessage("一次最多附带 8 张图片", 3000)
            return
        self._image_seq += 1
        attachment = {
            "seq": self._image_seq,
            "png": self._png_bytes(image),
            "text": "",
            "status": "pending",
            "error": "",
            "widget": None,
            "worker": None,
            "preview": None,
        }

        chip = QFrame()
        chip.setObjectName("AttachmentChip")
        chip.setFixedHeight(58)
        row = QHBoxLayout(chip)
        row.setContentsMargins(6, 5, 6, 5)
        row.setSpacing(7)

        preview = QLabel()
        preview.setFixedSize(46, 46)
        preview.setAlignment(Qt.AlignCenter)
        preview.setPixmap(QPixmap.fromImage(image).scaled(
            46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        row.addWidget(preview)

        info = QVBoxLayout()
        info.setSpacing(1)
        title = QLabel(f"图片 {attachment['seq']}")
        title.setObjectName("AttachmentTitle")
        status = QLabel("识别中…")
        status.setObjectName("AttachmentStatus")
        info.addWidget(title)
        info.addWidget(status)
        row.addLayout(info)

        remove = QToolButton()
        remove.setObjectName("Small")
        remove.setText("✕")
        remove.setToolTip("移除这张图片")
        remove.clicked.connect(
            lambda _checked=False, att=attachment: self._remove_attachment(att))
        row.addWidget(remove)

        attachment["widget"] = chip
        attachment["preview"] = preview
        attachment["status_label"] = status
        self.chat_attachments.append(attachment)
        self.attachment_layout.insertWidget(
            self.attachment_layout.count() - 1, chip)
        self.attachment_bar.setVisible(True)

        ok, reason = image_ocr.available()
        if not ok:
            self._finish_ocr(attachment, "", f"识别组件不可用：{reason}")
            return
        worker = WorkerThread(image_ocr.recognize_bytes, attachment["png"],
                              parent=self)
        attachment["worker"] = worker
        worker.succeeded.connect(
            lambda result, att=attachment: self._finish_ocr(att, result, ""))
        worker.failed.connect(
            lambda message, att=attachment: self._finish_ocr(att, "", message))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _finish_ocr(self, attachment, text, error):
        attachment["worker"] = None
        if attachment not in self.chat_attachments:
            return
        label = attachment.get("status_label")
        if error or not str(text).strip():
            attachment["status"] = "error" if error else "empty"
            attachment["error"] = error
            attachment["text"] = ""
            if label is not None:
                label.setText("未识别出文字" if not error else "识别失败")
                label.setToolTip(error)
            if attachment.get("widget") is not None:
                attachment["widget"].setToolTip(
                    error or "没有从图片中识别出文字")
            if error:
                self.statusBar().showMessage(f"图片识别失败：{error}", 5000)
            return
        attachment["status"] = "done"
        attachment["text"] = str(text)
        char_count = len(str(text).replace("\n", ""))
        if label is not None:
            label.setText(f"已识别 {char_count} 字")
            label.setToolTip(str(text)[:2000])
        if attachment.get("widget") is not None:
            attachment["widget"].setToolTip(str(text)[:2000])

    def _remove_attachment(self, attachment):
        worker = attachment.get("worker")
        if worker is not None:
            worker.request_cancel()
        widget = attachment.get("widget")
        if attachment in self.chat_attachments:
            self.chat_attachments.remove(attachment)
        if widget is not None:
            self.attachment_layout.removeWidget(widget)
            widget.deleteLater()
        self.attachment_bar.setVisible(bool(self.chat_attachments))

    def _clear_attachments(self):
        for attachment in list(getattr(self, "chat_attachments", [])):
            self._remove_attachment(attachment)

    def _attachments_pending(self):
        return any(att.get("status") == "pending"
                   for att in self.chat_attachments)

    def _compose_message(self, text):
        """把输入文字和图片识别结果合成发给模型的完整提示词。"""
        pieces = []
        for attachment in self.chat_attachments:
            seq = attachment["seq"]
            recognized = attachment.get("text", "").strip()
            if recognized:
                pieces.append(f"【图片 {seq} 的文字识别结果】\n{recognized}")
            else:
                pieces.append(f"【图片 {seq}】未识别出文字。")
        prompt = text
        if pieces:
            prompt = (text + "\n\n" if text else "") + "\n\n".join(pieces)
        display = text
        if self.chat_attachments:
            note = f"[已附带 {len(self.chat_attachments)} 张图片，识别结果随消息发送]"
            display = (display + "\n\n" if display else "") + note
        return prompt, display

    # ------------------------------------------- 问答窗口图片附件 --
    def _chat_qa_attachments_pending(self):
        return any(att.get("status") == "pending"
                   for att in getattr(self, "chat_qa_attachments", []))

    def _add_chat_qa_image(self, image):
        if image is None or image.isNull():
            return
        attachments = getattr(self, "chat_qa_attachments", None)
        layout = getattr(self, "chat_qa_attachment_layout", None)
        bar = getattr(self, "chat_qa_attachment_bar", None)
        if attachments is None or layout is None or bar is None:
            return
        if len(attachments) >= 8:
            self.statusBar().showMessage("一次最多附带 8 张图片", 3000)
            return
        self._image_seq = getattr(self, "_image_seq", 0) + 1
        attachment = {
            "seq": self._image_seq,
            "png": self._png_bytes(image),
            "text": "",
            "status": "pending",
            "error": "",
            "widget": None,
            "worker": None,
        }

        chip = QFrame()
        chip.setObjectName("AttachmentChip")
        chip.setFixedHeight(58)
        row = QHBoxLayout(chip)
        row.setContentsMargins(6, 5, 6, 5)
        row.setSpacing(7)

        preview = QLabel()
        preview.setFixedSize(46, 46)
        preview.setAlignment(Qt.AlignCenter)
        preview.setPixmap(QPixmap.fromImage(image).scaled(
            46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        row.addWidget(preview)

        info = QVBoxLayout()
        info.setSpacing(1)
        title = QLabel(f"图片 {attachment['seq']}")
        title.setObjectName("AttachmentTitle")
        status = QLabel("识别中…")
        status.setObjectName("AttachmentStatus")
        info.addWidget(title)
        info.addWidget(status)
        row.addLayout(info)

        remove = QToolButton()
        remove.setObjectName("Small")
        remove.setText("✕")
        remove.setToolTip("移除这张图片")
        remove.clicked.connect(
            lambda _checked=False, att=attachment:
            self._remove_chat_qa_attachment(att))
        row.addWidget(remove)

        attachment["widget"] = chip
        attachment["status_label"] = status
        attachments.append(attachment)
        layout.insertWidget(layout.count() - 1, chip)
        bar.setVisible(True)
        self._update_chat_qa_buttons()

        ok, reason = image_ocr.available()
        if not ok:
            self._finish_chat_qa_ocr(
                attachment, "", f"识别组件不可用：{reason}")
            return
        worker = WorkerThread(
            image_ocr.recognize_bytes, attachment["png"], parent=self)
        attachment["worker"] = worker
        worker.succeeded.connect(
            lambda result, att=attachment:
            self._finish_chat_qa_ocr(att, result, ""))
        worker.failed.connect(
            lambda message, att=attachment:
            self._finish_chat_qa_ocr(att, "", message))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _finish_chat_qa_ocr(self, attachment, text, error):
        attachment["worker"] = None
        if attachment not in getattr(self, "chat_qa_attachments", []):
            return
        label = attachment.get("status_label")
        if error or not str(text).strip():
            attachment["status"] = "error" if error else "empty"
            attachment["error"] = error
            attachment["text"] = ""
            if label is not None:
                label.setText("未识别出文字" if not error else "识别失败")
                label.setToolTip(error)
            if error:
                self.statusBar().showMessage(
                    f"图片识别失败：{error}", 5000)
        else:
            attachment["status"] = "done"
            attachment["text"] = str(text)
            char_count = len(str(text).replace("\n", ""))
            if label is not None:
                label.setText(f"已识别 {char_count} 字")
                label.setToolTip(str(text)[:2000])
        self._update_chat_qa_buttons()

    def _remove_chat_qa_attachment(self, attachment):
        worker = attachment.get("worker")
        if worker is not None:
            worker.request_cancel()
        widget = attachment.get("widget")
        attachments = getattr(self, "chat_qa_attachments", [])
        if attachment in attachments:
            attachments.remove(attachment)
        layout = getattr(self, "chat_qa_attachment_layout", None)
        if widget is not None and layout is not None:
            layout.removeWidget(widget)
            widget.deleteLater()
        bar = getattr(self, "chat_qa_attachment_bar", None)
        if bar is not None:
            bar.setVisible(bool(attachments))
        self._update_chat_qa_buttons()

    def _clear_chat_qa_attachments(self):
        for attachment in list(getattr(self, "chat_qa_attachments", [])):
            self._remove_chat_qa_attachment(attachment)

    def _chat_qa_compose_message(self, text):
        pieces = []
        attachments = getattr(self, "chat_qa_attachments", [])
        for attachment in attachments:
            seq = attachment["seq"]
            recognized = attachment.get("text", "").strip()
            if recognized:
                pieces.append(
                    f"【图片 {seq} 的文字识别结果】\n{recognized}")
            else:
                pieces.append(f"【图片 {seq}】未识别出文字。")
        prompt = text
        if pieces:
            prompt = (text + "\n\n" if text else "") + "\n\n".join(pieces)
        display = text
        if attachments:
            note = (
                f"[已附带 {len(attachments)} 张图片，识别结果随消息发送]")
            display = (display + "\n\n" if display else "") + note
        return prompt, display

    def _style_study_mode_button(self, checked):
        # 当前会话启用研读模式时把按钮标绿，一眼能看出消息会被路由到解释栏
        if checked:
            self.btn_study_mode.setStyleSheet(
                "QPushButton { background-color: #2f9e63; border: 1px solid #268a55;"
                " border-radius: 8px; padding: 8px 14px; color: #ffffff;"
                " font-weight: 600; }"
                "QPushButton:hover { background-color: #37ad6e;"
                " border-color: #268a55; }"
                "QPushButton:pressed { background-color: #248a54; }")
        else:
            self.btn_study_mode.setStyleSheet("")

    def _set_study_mode_silently(self, checked):
        """切换项目时恢复该项目的研读模式，不触发切换副作用。"""
        btn = getattr(self, "btn_study_mode", None)
        if btn is None:
            return
        btn.blockSignals(True)
        btn.setChecked(bool(checked))
        btn.blockSignals(False)
        self._style_study_mode_button(bool(checked))

    def _on_study_mode_toggled(self, checked):
        self._style_study_mode_button(checked)
        # 模式按项目记忆：切到别的项目不会跟着带过去
        self._study_state(self.current_project)["mode"] = bool(checked)
        if checked:
            self.study_page.set_project(self.current_project)
            self.study_page.set_runtime(
                self._current_permission_mode(),
                self._current_model(), self._current_effort())
            self.statusBar().showMessage(
                "研读模式已开启：在下方提问，回答会写入「代码研读」页", 5000)
        else:
            self.statusBar().showMessage("研读模式已关闭", 2500)

    def _send_study_learning(self, text, display_text=None):
        """主对话框的研读请求：仍用主会话，回答同步进代码研读解释栏。"""
        if not self.study_page.root:
            self.study_page.set_project(self.current_project)
        self.study_page.set_runtime(
            self._current_permission_mode(),
            self._current_model(), self._current_effort())
        self.study_page.begin_study(text)
        self._show_study_page()
        self._study_pending = True
        self._chat_state()["study_goal"] = text
        self._prepare_worker()
        self._append_chat_block(
            {"kind": "user", "text": display_text if display_text else text,
             "plain": True})
        self.chat_input.clear()
        self._reset_live_stream()
        root = self.current_project or ""
        code_map = project_code_map(root)
        prompt = (
            "【代码研读模式】\n"
            f"项目根目录：{root}\n"
            f"我想学习的内容：{text}\n\n"
            "项目符号索引（路径相对项目根目录，行号取自真实文件，可直接用来引用；"
            "索引没覆盖到的位置请先读取文件再引用）：\n"
            "<<<\n"
            f"{code_map or '（未扫描到源码文件，请先浏览项目目录）'}\n"
            ">>>\n\n"
            "请面向“读懂代码”来解释，回答会显示在代码研读页的解释栏里：\n"
            "1. 先用分层/流程图式的小标题说明每一层在算什么、输入什么、输出什么；\n"
            "2. 每个关键步骤后面标注源码位置，格式必须严格为 "
            "[[code:相对路径:起行-止行|关键函数或类名]]"
            "（路径相对项目根目录，不带反引号；行号必须来自你实际读取的文件"
            "或上面的符号索引，不要估算）；\n"
            "3. 指出关键变量、数据结构/消息名和调用关系，必要时说明容易混淆的点；\n"
            "4. 用中文和 Markdown，不要大段粘贴源码。")
        self.worker.send(prompt)

    def _on_code_link(self, href):
        self.study_page.set_project(self.current_project)
        self._show_study_page()
        self.study_page.open_ref(href)

    # ------------------------------------------------- 每个项目各自的研读页 --
    def _study_page_path(self, page):
        return getattr(page, "project_key", "") if page is not None else ""

    def _sync_study_runtime(self, permission=None, model=None, effort=None):
        """把全局权限/模型/思考档位同步给所有项目的研读页。"""
        perm = (self._current_permission_mode()
                if permission is None else permission)
        mdl = self._current_model() if model is None else model
        eff = self._current_effort() if effort is None else effort
        for page in self._study_pages.values():
            page.set_runtime(perm, mdl, eff)
        for page in getattr(self, "_dev_pages", {}).values():
            page.set_runtime(mdl, eff, perm)

    def _show_dev_page(self):
        self._show_dev_page_for(getattr(self, "dev_tab", None))

    def _show_dev_page_for(self, page):
        """把某项目的代码开发页带到前台（独立窗口则前置窗口，否则切页签）。"""
        if page is None:
            return
        path = self._dev_page_path(page)
        window = self._dev_windows.get(path)
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()
            return
        if path != (self.current_project or ""):
            self._select_project_path(path)
        index = self.tabs.indexOf(page)
        if index >= 0:
            self.tabs.setCurrentIndex(index)

    def _reset_dev_for_new_chat(self):
        """新对话时收起当前项目的代码开发页，像代码研读一样回到干净状态。"""
        dev = getattr(self, "dev_tab", None)
        path = self._dev_page_path(dev)
        if path in self._dev_windows and path == (self.current_project or ""):
            self._reattach_dev_page(path)
        if dev is not None:
            dev.reset_for_new_chat()

    def _show_dev_qa_window(self):
        dev = getattr(self, "dev_tab", None)
        if dev is None:
            return
        window = self._dev_windows.get(self._dev_page_path(dev))
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()
        else:
            self.tabs.setCurrentWidget(dev)
        dev._show_qa_window()

    def _show_current_qa_window(self):
        page = self.study_page
        if page is not None:
            page._show_qa_window()

    def _attach_study_tab(self, page):
        """把某项目的研读页挂进主窗口页签（如果它已经在页签里就不动）。"""
        if page is None or self.tabs.indexOf(page) >= 0:
            return
        # 固定在「代码开发」页签之前，避免反复挂载后页签顺序漂移。
        dev_index = self.tabs.indexOf(getattr(self, "dev_tab", None))
        index = dev_index if dev_index >= 0 else 1
        index = max(1, min(index, self.tabs.count()))
        self.tabs.insertTab(index, page, "代码研读")
        self._study_tab_index = self.tabs.indexOf(page)
        self.tabs.setTabToolTip(index, f"代码研读 · {page.root or '未选择项目'}")
        # 不要在这里 page.show()：QTabWidget 只会显示当前页。强行 show 会让
        # 非当前的研读页和代码开发页同时可见、文字互相叠在一起，切换项目后
        # 看起来就像“排版错乱”。
        page.setVisible(self.tabs.currentWidget() is page)
        self._stabilize_page_layout(page)
        QTimer.singleShot(0, lambda p=page: self._stabilize_page_layout(p))

    def _detach_study_tab(self, page):
        """把研读页从主窗口页签里取下（页面对象继续保留）。"""
        if page is None:
            return
        index = self.tabs.indexOf(page)
        if index < 0:
            return
        self._study_tab_index = index
        # removeTab 会顺带改当前页签，别让这个中间状态写进项目记忆
        previous = getattr(self, "_suppress_tab_remember", False)
        self._suppress_tab_remember = True
        try:
            self.tabs.removeTab(index)
            page.hide()
        finally:
            self._suppress_tab_remember = previous

    def _switch_study_page_to(self, path):
        """切换主窗口当前显示的项目研读页。

        已独立成窗口的页面不参与切换，窗口内容保持原样。
        """
        key = path or ""
        new_page = self._study_page_for(key)
        old_page = getattr(self, "study_page", None)
        if old_page is not new_page:
            if old_page is not None and self.tabs.indexOf(old_page) >= 0:
                self._detach_study_tab(old_page)
            self.study_page = new_page
        floating = self._study_windows.get(key) is not None
        if not floating:
            self._attach_study_tab(new_page)
        new_page.set_runtime(
            self._current_permission_mode(),
            self._current_model(), self._current_effort())
        self._sync_study_float_button(floating)

    def _switch_dev_page_to(self, path):
        """切换主窗口当前显示的项目代码开发页。

        已独立成窗口的页面不参与切换，窗口内容保持原样。
        """
        key = path or ""
        new_page = self._dev_page_for(key)
        old_page = getattr(self, "dev_tab", None)
        if old_page is not new_page:
            if old_page is not None and self.tabs.indexOf(old_page) >= 0:
                self._detach_dev_tab(old_page)
            self.dev_tab = new_page
        floating = self._dev_windows.get(key) is not None
        if not floating:
            self._attach_dev_tab(new_page)
        new_page.set_runtime(
            self._current_model(), self._current_effort(),
            self._current_permission_mode())

    def _show_study_page(self):
        self._show_study_page_for(self.study_page)

    def _show_study_page_for(self, page):
        if page is None:
            return
        path = self._study_page_path(page)
        window = self._study_windows.get(path)
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()
            return
        if path != (self.current_project or ""):
            # 后台项目的问答窗请求回到研读页：切到它自己的项目再看
            self._select_project_path(path)
        index = self.tabs.indexOf(page)
        if index >= 0:
            self.tabs.setCurrentIndex(index)

    def _on_study_float_toggled(self, checked):
        page = self.study_page
        if page is None:
            return
        path = self._study_page_path(page)
        if checked and path not in self._study_windows:
            self._detach_study_page(path)
        elif not checked and path in self._study_windows:
            self._reattach_study_page(path)

    def _toggle_study_float(self):
        self._toggle_study_float_for(self.study_page)

    def _toggle_study_float_for(self, page):
        if page is None:
            return
        path = self._study_page_path(page)
        if path in self._study_windows:
            # 在后台项目的窗口里点「收回页签」时，顺带把主窗口切到该项目，
            # 否则页签收回来了却看不见
            if path != (self.current_project or ""):
                self._select_project_path(path)
            self._reattach_study_page(path)
        else:
            self._detach_study_page(path)

    def _detach_study_page(self, path=None):
        key = (self.current_project or "") if path is None else path
        page = self._study_page_for(key)
        if key in self._study_windows:
            return
        if self.tabs.indexOf(page) >= 0:
            self._detach_study_tab(page)
        if key == (self.current_project or ""):
            # 页面搬去独立窗口后，主窗口回到对话页，避免停在别的页签上
            self.tabs.blockSignals(True)
            self.tabs.setCurrentWidget(self.chat_tab)
            self.tabs.blockSignals(False)
            self._chat_state(key)["tab"] = "chat"
        window = StudyFloatWindow(self)
        window.setWindowTitle(
            f"代码研读 · {Path(key).name}" if key else "代码研读 · Claude 控制台")
        window.setWindowIcon(make_app_icon())
        window.resize(1350, 820)
        window.closed.connect(lambda p=key: self._reattach_study_page(p))
        # 独立窗口四周留一点页边距，避免页面紧贴窗口边缘。
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(10, 10, 10, 10)
        holder_layout.setSpacing(0)
        page.setParent(holder)
        holder_layout.addWidget(page)
        window.setCentralWidget(holder)
        window._page = page
        # removeTab/setParent 会把页面显式隐藏；QMainWindow.show() 不会自动
        # 重新显示被显式隐藏的子控件，必须手动 show，否则独立窗口一片空白。
        page.show()
        self._study_windows[key] = window
        page.set_floating(True)
        if key == (self.current_project or ""):
            self._sync_study_float_button(True)
        window.show()
        window.raise_()

    def _reattach_study_page(self, path=None):
        key = (self.current_project or "") if path is None else path
        window = self._study_windows.pop(key, None)
        if window is None:
            return
        window.blockSignals(True)
        holder = window.takeCentralWidget()
        page = getattr(window, "_page", None)
        window._page = None
        if page is not None:
            page.setParent(self.tabs)
            page.set_floating(False)
            if key == (self.current_project or ""):
                self._attach_study_tab(page)
                self.tabs.setCurrentWidget(page)
            else:
                # 后台项目的窗口收起来了：页签等切到该项目时再挂
                page.hide()
        if holder is not None:
            holder.deleteLater()
        if key == (self.current_project or ""):
            self._sync_study_float_button(False)
        window.close()
        window.deleteLater()

    def _sync_study_float_button(self, floating):
        if not hasattr(self, "btn_study_float"):
            return
        self.btn_study_float.blockSignals(True)
        self.btn_study_float.setChecked(floating)
        self.btn_study_float.setText("收回页签" if floating else "独立窗口")
        self.btn_study_float.blockSignals(False)

    # ------------------------------------------- 代码开发页独立窗口 --
    def _toggle_dev_float(self):
        self._toggle_dev_float_for(getattr(self, "dev_tab", None))

    def _toggle_dev_float_for(self, page):
        if page is None:
            return
        path = self._dev_page_path(page)
        if path in self._dev_windows:
            if path != (self.current_project or ""):
                self._select_project_path(path)
            self._reattach_dev_page(path)
        else:
            self._detach_dev_page(path)

    def _attach_dev_tab(self, page=None):
        """把某项目的开发页挂回主窗口页签，固定在研读页之后。"""
        dev = page if page is not None else getattr(self, "dev_tab", None)
        if dev is None or self.tabs.indexOf(dev) >= 0:
            return
        study_index = self.tabs.indexOf(getattr(self, "study_page", None))
        index = study_index + 1 if study_index >= 0 else 1
        index = max(1, min(index, self.tabs.count()))
        self.tabs.insertTab(index, dev, "代码开发")
        self.tabs.setTabToolTip(
            index, f"代码开发 · {Path(self._dev_page_path(dev)).name}"
            if self._dev_page_path(dev) else "代码开发")
        dev.setVisible(self.tabs.currentWidget() is dev)
        self._stabilize_page_layout(dev)
        QTimer.singleShot(0, lambda: self._stabilize_page_layout(dev))

    def _detach_dev_tab(self, page):
        """把开发页从主窗口页签里取下（页面对象继续保留）。"""
        if page is None:
            return
        index = self.tabs.indexOf(page)
        if index < 0:
            return
        previous = getattr(self, "_suppress_tab_remember", False)
        self._suppress_tab_remember = True
        try:
            self.tabs.removeTab(index)
            page.hide()
        finally:
            self._suppress_tab_remember = previous

    def _detach_dev_page(self, path=None):
        key = (self.current_project or "") if path is None else path
        page = self._dev_page_for(key)
        if key in self._dev_windows:
            return
        if self.tabs.indexOf(page) >= 0:
            self._detach_dev_tab(page)
        if key == (self.current_project or ""):
            self.tabs.blockSignals(True)
            self.tabs.setCurrentWidget(self.chat_tab)
            self.tabs.blockSignals(False)
            self._chat_state(key)["tab"] = "chat"
        window = DevFloatWindow(self)
        window.setWindowTitle(
            f"代码开发 · {Path(key).name}" if key else f"代码开发 · {APP_NAME}")
        window.setWindowIcon(make_app_icon())
        window.resize(1350, 820)
        window.closed.connect(lambda p=key: self._reattach_dev_page(p))
        # 独立窗口四周留一点页边距，避免页面紧贴窗口边缘。
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(10, 10, 10, 10)
        holder_layout.setSpacing(0)
        page.setParent(holder)
        holder_layout.addWidget(page)
        window.setCentralWidget(holder)
        window._page = page
        # removeTab/setParent 会显式隐藏页面，必须手动 show 才不是空白窗口。
        page.show()
        page.set_floating(True)
        self._dev_windows[key] = window
        window.show()
        window.raise_()
        window.activateWindow()

    def _reattach_dev_page(self, path=None):
        key = (self.current_project or "") if path is None else path
        window = self._dev_windows.pop(key, None)
        if window is None:
            return
        window.blockSignals(True)
        holder = window.takeCentralWidget()
        dev = getattr(window, "_page", None)
        window._page = None
        if dev is not None:
            dev.setParent(self.tabs)
            dev.set_floating(False)
            if key == (self.current_project or ""):
                self._attach_dev_tab(dev)
                self.tabs.setCurrentWidget(dev)
                self._stabilize_page_layout(dev)
            else:
                # 后台项目的窗口收起来了：等切到该项目时再挂页签
                dev.hide()
        if holder is not None:
            holder.deleteLater()
        window.close()
        window.deleteLater()

    def _ensure_live_bubble(self):
        if self._live_bubble is None:
            self._live_wrapper, self._live_bubble = self._add_bubble(
                "assistant", "", plain=True)
        return self._live_bubble

    def _on_chat_delta(self, state, chunk):
        # 后台项目的输出只记进它自己的状态，不打扰当前正在看的项目。
        state["live"] += chunk
        if not self._is_active_chat_state(state):
            return
        self._ensure_live_bubble()
        self._stream_dirty = True
        if not self._stream_timer.isActive():
            self._stream_timer.start()
        self._scroll_to_bottom()

    def _flush_stream(self):
        state = self._chat_state()
        if not self._stream_dirty:
            self._stream_timer.stop()
            return
        if self._live_bubble is not None:
            self._live_bubble.set_plain(state["live"])
        if state["study_pending"] and state["live"]:
            self._study_page_for(state["path"]).preview_explanation(
                state["live"])
        self._stream_dirty = False

    def _discard_live_bubble(self):
        if self._live_wrapper is not None:
            self._live_wrapper.deleteLater()
        self._live_wrapper = None
        self._live_bubble = None
        self._stream_text = ""
        self._stream_dirty = False

    def _store_background_study(self, state, text):
        """把研读回答写回发起请求的项目自己的研读页。

        每个项目一张常驻页（可能已经独立成窗口），所以不会串到别的项目。
        """
        page = self._study_page_for(state.get("path") or "")
        page.show_explanation(text)

    def _on_chat_assistant(self, state, text, tools):
        active = self._is_active_chat_state(state)
        stream_text = state["live"]
        had_stream = bool(stream_text)
        if active:
            self._stream_timer.stop()
            if self._live_bubble is not None:
                if text:
                    self._live_bubble.set_markdown(text)
                elif not had_stream:
                    self._discard_live_bubble()
            elif text:
                self._add_bubble("assistant", text)
            if self._live_bubble is not None and not text and had_stream:
                # 流式内容已完整显示，保留现有文本（按 Markdown 渲染，
                # 这样回复里的本地图片也能直接显示出来）
                self._live_bubble.set_markdown(stream_text)
            self._live_wrapper = None
            self._live_bubble = None
            self._stream_text = ""
            self._stream_dirty = False
        final_text = text or (stream_text if had_stream else "")
        if final_text:
            state["blocks"].append(
                {"kind": "assistant", "text": final_text})
        state["live"] = ""
        for tool in tools:
            block = {"kind": "tool", "name": tool.get("name", "工具"),
                     "data": tool.get("input") or {}}
            state["blocks"].append(block)
            if active:
                self._render_tool_chip(block["name"], block["data"])
        if state["study_pending"] and final_text:
            # 写回发起请求的那个项目自己的研读页；切到别的项目也不会串台，
            # 已独立成窗口的页面同样能收到结果。
            self._store_background_study(state, final_text)
        if active:
            self._scroll_to_bottom()

    def _on_chat_result(self, state, info):
        active = self._is_active_chat_state(state)
        if active:
            self._stream_timer.stop()
        if info.get("is_error"):
            message = (info.get("result") or "").strip() or "Claude 返回了错误"
            state["blocks"].append(
                {"kind": "note", "text": f"出错：{message[:600]}"})
            if active:
                self._render_chat_note(f"出错：{message[:600]}")
            state["status"] = "出错"
            if state["study_pending"]:
                page = self._study_page_for(state.get("path") or "")
                page.append_note(f"研读出错：{message[:600]}")
                page.explain_status.setText("研读出错")
        else:
            label = "已完成"
            # 回合结束 ≠ 后台任务结束：编译/打包/下载可能还在跑
            try:
                bg_tasks = background_monitor.snapshot().get("tasks") or []
            except Exception:
                bg_tasks = []
            if bg_tasks:
                label = f"回合已完成 · 后台 {len(bg_tasks)} 个任务运行中"
            cost = info.get("cost")
            if isinstance(cost, (int, float)) and cost > 0:
                label += f" · ${cost:.4f}"
            state["status"] = label
        if active:
            self.chat_status.setText(state["status"])
        state["study_pending"] = False
        sid = info.get("session_id")
        if sid:
            state["session_id"] = sid
            state["resume_id"] = sid
            state["needs_reconfigure"] = False
        QTimer.singleShot(400, self._refresh_sessions)

    def _on_chat_error(self, state, message):
        active = self._is_active_chat_state(state)
        state["blocks"].append(
            {"kind": "note", "text": f"连接出错：{message}"})
        if active:
            self._stream_timer.stop()
            self._discard_live_bubble()
            self._render_chat_note(f"连接出错：{message}")
            self.chat_status.setText("出错")
        if state["study_pending"]:
            page = self._study_page_for(state.get("path") or "")
            page.append_note(f"连接出错：{message}")
            page.explain_status.setText("研读出错")
        # 流式内容已经在错误信息里体现，清掉避免和正式回答混在一起
        state["live"] = ""
        state["study_pending"] = False
        state["status"] = "出错"

    def _on_chat_busy(self, state, busy):
        state["busy"] = busy
        if not self._is_active_chat_state(state):
            return
        self._update_chat_buttons()
        if busy:
            self.chat_status.setText("Claude 正在回复…")
        else:
            self._flush_stream()
            self._stream_timer.stop()
            if self.chat_status.text().startswith("Claude 正在回复"):
                self.chat_status.setText(state["status"] or "就绪")
            if self._pending_catalog is not None:
                models, error = self._pending_catalog
                self._pending_catalog = None
                QTimer.singleShot(
                    0, lambda: self._on_catalog_loaded(models, error))

    def _on_permission_asked(self, state, request_id, tool_name, input_text):
        worker = state.get("worker") or self.worker
        if self._closing:
            worker.respond_permission(request_id, False)
            return
        dialog = PermissionDialog(tool_name, input_text, self)
        allowed = dialog.exec_() == QDialog.Accepted
        worker.respond_permission(request_id, allowed)
        self.statusBar().showMessage(
            "已允许本次操作" if allowed else "已拒绝本次操作", 3000)

    def _interrupt_chat(self):
        state = self._chat_state()
        thread = state.get("dual_thread")
        if thread is not None and thread.isRunning():
            thread.cancel()
            self.chat_status.setText("正在停止协作…")
            return
        if self._chat_busy:
            self.worker.interrupt()
            self.chat_status.setText("正在停止…")

    def _new_chat(self):
        if not self.current_project:
            self.statusBar().showMessage("请先选择一个项目", 3000)
            return
        state = self._chat_state()
        thread = state.get("dual_thread")
        if thread is not None and thread.isRunning():
            thread.cancel()
            self.chat_status.setText("正在停止协作，请结束后再新建对话…")
            return
        if state.get("worker") is not None:
            state["worker"].reset()
        state["blocks"] = []
        state["live"] = ""
        state["dual_live"] = ""
        state["dual_live_bubble"] = None
        state["dual_live_wrapper"] = None
        state["dual_last_role"] = ""
        state["status"] = "新对话"
        state.pop("dual_state_dir", None)
        self._chat_cwd = None
        self._chat_resume_id = None
        self._chat_session_id = None
        self._needs_reconfigure = True
        # 新对话默认回到普通聊天：关掉研读模式，避免下一条消息又被当成研读请求
        self._study_pending = False
        if self.btn_study_mode.isChecked():
            self.btn_study_mode.setChecked(False)
        self._cancel_chat_media()
        self._clear_quote()
        self._clear_chat_view()
        self._clear_attachments()
        # 代码开发会话跟随对话关闭：清空对话与待确认改动，收起独立窗口
        self._reset_dev_for_new_chat()
        self._show_empty_state("新对话已准备好\n在下方输入消息开始")
        self.chat_status.setText("新对话")
        self.tabs.setCurrentIndex(0)
        self.chat_input.setFocus()
        self._update_chat_buttons()

    def _continue_last_chat(self):
        if not self.current_project:
            self.statusBar().showMessage("请先选择一个项目", 3000)
            return
        if self._chat_busy:
            return
        sessions = load_project_sessions(self.current_project)
        if not sessions:
            self.statusBar().showMessage("当前项目还没有可恢复的 Claude 会话", 3000)
            return
        self._resume_in_chat(sessions[0].sid)

    def _resume_in_chat(self, sid):
        if not self.current_project or self._chat_busy:
            return
        session = next((s for s in self.sessions
                        if s.get("source") == "claude" and s.get("sid") == sid),
                       None)
        if session and session.get("cwd"):
            try:
                session_cwd = Path(session["cwd"]).resolve()
                current_cwd = Path(self.current_project).resolve()
            except OSError:
                session_cwd = current_cwd = None
            if session_cwd and current_cwd and session_cwd != current_cwd:
                destination = next(
                    (p for p in self.config["projects"]
                     if Path(p["path"]).resolve() == session_cwd), None)
                if destination is None:
                    QMessageBox.information(
                        self, "恢复会话",
                        f"该会话属于另一个目录：\n{session['cwd']}\n\n"
                        "请先在左侧添加并切换到该目录，再恢复会话。")
                    return
                self._select_project_path(destination["path"])
        state = self._chat_state()
        if state.get("worker") is not None:
            state["worker"].reset()
        state["blocks"] = []
        state["live"] = ""
        state["status"] = "已恢复会话"
        state.pop("dual_state_dir", None)
        self._chat_cwd = None
        self._chat_resume_id = sid
        self._chat_session_id = sid
        self._needs_reconfigure = True
        self._clear_chat_view()
        self._clear_attachments()

        title = session["title"] if session else sid[:8]
        self._append_chat_block(
            {"kind": "note", "text": f"已恢复会话：{title}（可继续对话）"})
        if session is not None:
            for role, text in load_session_messages(Path(session["path"])):
                self._append_chat_block(
                    {"kind": role, "text": text, "plain": role == "user"})
        self.chat_status.setText("已恢复会话")
        self.tabs.setCurrentIndex(0)
        self.chat_input.setFocus()
        self._update_chat_buttons()

    def _reset_chat_for_project(self):
        """切换项目：只换显示，不打断别的项目仍在运行的会话。"""
        self._cancel_chat_media()
        self._clear_quote()
        self._clear_attachments()
        self._clear_chat_view()
        if not self.current_project:
            self._show_empty_state("请先在左侧选择一个项目")
            self.chat_status.setText("未选择项目")
            self._update_chat_buttons()
            return
        state = self._chat_state()
        self._render_chat_state()
        thread = state.get("dual_thread")
        dual_busy = thread is not None and thread.isRunning()
        if dual_busy:
            self.chat_status.setText("双 AI 协作中…")
        else:
            self.chat_status.setText(
                "Claude 正在回复…" if state["busy"]
                else (state["status"] or "就绪"))
        self._update_chat_buttons()

    # ------------------------------------------------------------- actions --
    def _run_in_terminal(self, command, quiet=False):
        if not self.current_project:
            self.statusBar().showMessage("请先选择一个项目", 3000)
            return
        try:
            terminal = launch_terminal(self.current_project, command)
        except Exception as exc:  # noqa: BLE001 - 面向用户的错误提示
            QMessageBox.critical(self, "无法打开终端", str(exc))
            return
        if not quiet:
            self.statusBar().showMessage(
                f"已在 {terminal} 中运行：{command}", 6000)

    def _open_folder(self):
        if self.current_project:
            if not win_compat.open_path(self.current_project):
                self.statusBar().showMessage("无法打开该文件夹", 3000)

    def _open_editor(self):
        if self.current_project and not win_compat.open_editor(self.current_project):
            self.statusBar().showMessage(
                "未找到 VS Code 的 code 命令，无法打开编辑器", 4000)

    def _copy_path(self):
        if self.current_project:
            QApplication.clipboard().setText(self.current_project)
            self.statusBar().showMessage("已复制项目路径", 2500)

    # --------------------------------------------------------------- misc --
    def _probe_claude(self):
        executable = find_native_claude_exe() or find_claude()
        if not executable:
            self.version_label.setText("未检测到 claude 命令")
            if not SDK_AVAILABLE:
                self._add_chat_note(
                    "无法在界面内对话：找不到 claude 命令，且未安装 Claude Agent SDK。")
            return
        # Windows 上的 claude.cmd 必须经 cmd.exe 启动
        argv = win_compat.wrap_argv([executable, "--version"])
        self._claude_proc = QProcess(self)
        self._claude_proc.finished.connect(self._on_version_ready)
        self._claude_proc.start(argv[0], argv[1:])

    def _on_version_ready(self, _code, _status):
        output = bytes(self._claude_proc.readAllStandardOutput()).decode(
            "utf-8", "replace").strip()
        if not output:
            self.version_label.setText("Claude Code 已就绪")
        elif "Claude Code" in output:
            self.version_label.setText(output)
        else:
            self.version_label.setText(f"Claude Code {output}")
        if not SDK_AVAILABLE:
            self.version_label.setText(
                f"{self.version_label.text()}\n界面对话组件缺失")

    def _restore_geometry(self):
        try:
            geometry = self.config.get("geometry") or ""
            if geometry and self.restoreGeometry(QByteArray.fromBase64(
                    geometry.encode("ascii"))):
                return
        except Exception:  # noqa: BLE001 - 损坏的几何信息不影响启动
            pass
        self.resize(1180, 800)

    def event(self, event):
        if event.type() == QEvent.WindowActivate and self.current_project:
            QTimer.singleShot(250, self._refresh_sessions)
        return super().event(event)

    def _has_unfinished_work(self):
        """是否还有正在生成、尚未完成的回复。"""
        try:
            for state in getattr(self, "_chat_states", {}).values():
                worker = state.get("worker")
                dual = state.get("dual_thread")
                if (state.get("busy")
                        or (worker is not None and worker.busy)
                        or (dual is not None and dual.isRunning())):
                    return True
            if getattr(self, "_chat_qa_busy", False):
                return True
            for page in getattr(self, "_study_pages", {}).values():
                if getattr(page, "_qa_busy", False):
                    return True
            for page in getattr(self, "_dev_pages", {}).values():
                if (getattr(page, "_busy", False)
                        or getattr(page, "_qa_busy", False)):
                    return True
        except Exception:  # noqa: BLE001 - 关闭检查不应阻止退出
            pass
        return False

    def closeEvent(self, event):
        if getattr(self, "_closing", False):
            super().closeEvent(event)
            return
        pending = self._has_unfinished_work()
        if pending:
            message = ("还有回复正在生成，关闭会中断当前回复。\n\n"
                       "确定要退出 Claude 控制台吗？")
        else:
            message = "确定要退出 Claude 控制台吗？"
        answer = QMessageBox.question(
            self, "退出确认", message,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            self._closing = False
            event.ignore()
            return
        self._closing = True
        try:
            self.config["geometry"] = bytes(
                self.saveGeometry().toBase64()).decode("ascii")
            save_config(self.config)
            workers = [getattr(self, attr, None)
                       for attr in ("_skill_worker", "_plugin_worker",
                                    "_chat_media_worker", "_catalog_thread")]
            workers += [att.get("worker")
                        for att in getattr(self, "chat_attachments", [])
                        if att.get("worker") is not None]
            for worker in workers:
                if worker is not None and worker.isRunning():
                    if not worker.wait(5000):
                        # 用户坚持关闭时避免 QThread 在运行中被销毁导致崩溃
                        worker.terminate()
                        worker.wait(1000)
            proc = self._claude_proc
            if proc is not None and proc.state() != QProcess.NotRunning:
                proc.terminate()
                proc.waitForFinished(1000)
            dual_threads = []
            for state in getattr(self, "_chat_states", {}).values():
                dual = state.get("dual_thread")
                if dual is not None and dual.isRunning():
                    dual.cancel()
                    dual_threads.append(dual)
            for dual in dual_threads:
                if not dual.wait(6000):
                    dual.terminate()
                    dual.wait(1000)
            for state in getattr(self, "_chat_states", {}).values():
                worker = state.get("worker")
                if worker is not None:
                    worker.shutdown()
            chat_qa_worker = getattr(self, "_chat_qa_worker", None)
            if chat_qa_worker is not None:
                chat_qa_worker.shutdown()
            chat_qa_window = getattr(self, "chat_qa_window", None)
            if chat_qa_window is not None:
                chat_qa_window.close()
            for window in list(getattr(self, "_study_windows", {}).values()):
                window.blockSignals(True)
                window.close()
            getattr(self, "_study_windows", {}).clear()
            for page in list(getattr(self, "_study_pages", {}).values()):
                page.shutdown()
            for window in list(getattr(self, "_dev_windows", {}).values()):
                window.blockSignals(True)
                window.close()
            getattr(self, "_dev_windows", {}).clear()
            for page in list(getattr(self, "_dev_pages", {}).values()):
                page.shutdown()
        finally:
            super().closeEvent(event)


def install_exception_guard():
    """捕获槽函数里未处理的异常，避免 PyQt5 直接 abort 整个程序。

    PyQt5 从 5.5 起，信号槽回调中逃逸的异常会在打印 traceback 后调用
    qFatal() 结束进程；接管 sys.excepthook 后异常只会被记录并提示。
    """
    def hook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        try:
            QMessageBox.critical(
                QApplication.activeWindow(), "程序错误",
                f"{exc_type.__name__}: {exc}\n\n"
                "这次操作没有完成，但程序会继续运行。\n"
                "详细信息已输出到终端。")
        except Exception:  # noqa: BLE001 - 提示失败时不影响主流程
            pass

    sys.excepthook = hook


def main():
    install_exception_guard()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Claude Console")
    app.setApplicationVersion(APP_VERSION)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("claude-console")
    app.setStyleSheet(QSS)
    app.setWindowIcon(make_app_icon())
    if not ICON_PATH.exists():
        make_app_icon(256).pixmap(256, 256).save(str(ICON_PATH), "PNG")

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
