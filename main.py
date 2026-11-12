#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude 控制台 —— 在桌面窗口中与 Claude Code 对话。"""

import asyncio
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from PyQt5.QtCore import (QBuffer, QByteArray, QDir, QEvent, QIODevice, QObject,
                          QProcess, QRect, QSize, QSortFilterProxyModel,
                          QStringListModel, Qt, QThread, QTimer, QUrl,
                          pyqtSignal)
from PyQt5.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics,
                         QIcon, QImage, QKeySequence, QPainter, QPixmap,
                         QSyntaxHighlighter, QTextCharFormat, QTextCursor,
                         QTextFormat)
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                             QCompleter, QDialog, QFileDialog, QFormLayout,
                             QFrame, QFileSystemModel, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton,
                             QScrollArea, QShortcut, QSpinBox, QSplitter,
                             QStackedWidget, QTabWidget, QTextBrowser,
                             QTextEdit, QToolButton, QTreeView, QVBoxLayout,
                             QWidget)

import agent_shared as shared_agents
import image_ocr

# --------------------------------------------------------------------------- #
# 常量 & 依赖
# --------------------------------------------------------------------------- #
APP_NAME = "Claude 控制台"
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

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "claude-console"
CONFIG_PATH = CONFIG_DIR / "config.json"
STUDY_DIR = CONFIG_DIR / "studies"
ICON_PATH = BASE_DIR / "icon.png"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

DEFAULT_CONFIG = {
    "projects": [],
    "history": [],
    "favorites": [],
    "geometry": "",
    "permission_mode": "default",
    "model": "",
    "effort": "high",
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

    if not found:
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


def load_config():
    if not CONFIG_PATH.exists():
        cfg = dict(DEFAULT_CONFIG)
        cfg["projects"] = default_projects()
        return cfg
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, default in DEFAULT_CONFIG.items():
            value = data.get(key)
            if isinstance(value, type(default)):
                cfg[key] = value
    except (OSError, ValueError):
        pass
    cfg["projects"] = [
        p for p in cfg["projects"]
        if isinstance(p, dict) and p.get("path") and p.get("name")
    ]
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
                self.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if generation == self._generation:
                self._busy = False
                self.busy_changed.emit(False)

    async def _ensure_client(self, generation):
        if self._client is not None:
            return self._client
        if not self._cwd:
            raise RuntimeError("请先选择一个项目目录")
        executable = find_claude()
        if not executable:
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
            cli_path=executable,
            env=env,
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
            self.assistant.emit(text, tools)
            return

        if isinstance(message, ResultMessage):
            self.result.emit({
                "session_id": message.session_id,
                "is_error": bool(message.is_error),
                "result": message.result or "",
                "cost": message.total_cost_usd,
                "duration": message.duration_ms,
                "subtype": message.subtype,
            })

    # ------------------------------------------------------------- permission --
    async def _can_use_tool(self, tool_name, input_data, _context):
        if self._permission_mode == "bypassPermissions":
            return PermissionResultAllow(updated_input=input_data)

        self._perm_seq += 1
        request_id = self._perm_seq
        holder = {"event": threading.Event(), "allowed": None}
        with self._pending_lock:
            self._pending[request_id] = holder
        pretty = json.dumps(input_data, ensure_ascii=False, indent=2, default=str)
        self.permission_asked.emit(request_id, tool_name, pretty)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, holder["event"].wait)
        with self._pending_lock:
            self._pending.pop(request_id, None)
        if holder["allowed"]:
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
        self.setObjectName("UserBubble" if role == "user" else "AssistantBubble")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 9, 13, 11)
        layout.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        role_label = QLabel("你" if role == "user" else "Claude")
        role_label.setObjectName("ChatRole")
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
        self.setMinimumWidth(340 if role == "assistant" else 280)

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
        return (f"[📄 {label}](code://{quote(path, safe='/._-')}"
                f"{query}{fragment})")

    return STUDY_REF_RE.sub(replace, text)


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
        while True:
            if self._cancelled:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
                self.failed.emit("已取消生成")
                return
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
        layout.setContentsMargins(12, 10, 12, 10)
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
        self.goal_label.setText(f"正在研读：{goal}")
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
        if self._study_goal:
            self.goal_label.setText(f"正在研读：{self._study_goal}")
        elif self._saved_study_name:
            self.goal_label.setText(f"研读记录：{self._saved_study_name}")
        else:
            self.goal_label.setText(self.HELP_TEXT)
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
        self.goal_label.setText(
            f"研读记录：{self._study_goal or self._saved_study_name}"
            if (self._study_goal or self._saved_study_name)
            else "已载入研读记录")
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
        subprocess.Popen(["xdg-open", str(STUDY_DIR)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _load_study_path(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "载入研读", f"无法读取记录：{exc}")
            return
        root = str(data.get("root") or "")
        if root and root != self.root:
            # 记录属于别的项目：交给主窗口切到那个项目自己的研读页再应用，
            # 不要污染当前项目（以及它可能已弹出的独立窗口）。
            self.load_record_requested.emit(data)
            return
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
        self._study_media = []
        self.goal_label.setText(self.HELP_TEXT)
        self._render_media_results()
        self.qa_quote_hint.setText("未选中文字")
        self.explain_status.setText("等待研读请求")
        self._update_study_buttons()

    # ------------------------------------------------------------ media --
    def _render_media_results(self):
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
        answer = text or self._qa_live
        if not answer:
            return
        message = self._qa_message(self._qa_answer_id)
        if message is not None:
            message["text"] = answer
            message["streaming"] = False
        self._last_answer = answer
        self._qa_live = ""
        self._render_qa()

    def _on_qa_result(self, info):
        self._qa_timer.stop()
        message = self._qa_message(self._qa_answer_id)
        if not self._last_answer and self._qa_live:
            self._last_answer = self._qa_live
            if message is not None:
                message["text"] = self._last_answer
                message["streaming"] = False
            self._render_qa()
        elif message is not None:
            message["streaming"] = False
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
        if not self._qa_messages:
            self.qa_view.clear()
            return
        blocks = []
        for message in self._qa_messages:
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
        if not shutil.which("code"):
            self.status_note("未找到 VS Code 的 code 命令")
            return
        subprocess.Popen(
            ["code", "-g", f"{entry['path']}:{entry['line']}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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


class PermissionDialog(QDialog):
    def __init__(self, tool_name, input_text, parent=None):
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
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

        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(60)
        self._stream_timer.timeout.connect(self._flush_stream)
        self._chat_qa_timer = QTimer(self)
        self._chat_qa_timer.setInterval(80)
        self._chat_qa_timer.timeout.connect(self._flush_chat_qa_live)

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
        self.btn_open_editor.setVisible(bool(shutil.which("code")))
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

        return content

    def _build_chat_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
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

        model_label = QLabel("模型")
        model_label.setObjectName("Muted")
        bar.addWidget(model_label)
        self.model_combo = QComboBox()
        self.model_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.model_combo.setMinimumWidth(190)
        self.model_combo.setToolTip(
            "模型列表从当前服务商自动获取（cc-switch 的 settings.json）。\n"
            "切换后会重建会话，并保留当前对话的上下文。")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        bar.addWidget(self.model_combo)

        self.btn_refresh_models = QToolButton()
        self.btn_refresh_models.setText("↻")
        self.btn_refresh_models.setObjectName("Small")
        self.btn_refresh_models.setToolTip("重新从服务商获取模型列表")
        self.btn_refresh_models.clicked.connect(self._refresh_model_catalog)
        bar.addWidget(self.btn_refresh_models)

        effort_label = QLabel("思考")
        effort_label.setObjectName("Muted")
        bar.addWidget(effort_label)
        self.effort_combo = QComboBox()
        self.effort_combo.setMinimumWidth(80)
        self.effort_combo.setToolTip("思考档位，随所选模型支持的档位变化")
        self.effort_combo.currentIndexChanged.connect(self._on_effort_changed)
        bar.addWidget(self.effort_combo)

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
        layout.addLayout(bar)
        self._populate_model_combo()

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

        bottom = QHBoxLayout()
        hint = QLabel(
            "Enter 发送 · Shift+Enter 换行 · 粘贴图片自动识别 · "
            "选中消息文字可右键提问 · Esc 停止")
        hint.setObjectName("Muted")
        bottom.addWidget(hint)
        self.btn_chat_qa = QPushButton("问答窗口")
        self.btn_chat_qa.setObjectName("Flat")
        self.btn_chat_qa.setToolTip(
            "直接打开对话问答窗口，不需要先选中内容（Ctrl+Shift+W 可随时召回）")
        self.btn_chat_qa.clicked.connect(self._show_chat_qa_window)
        bottom.addWidget(self.btn_chat_qa)
        self.btn_ask_selection = QPushButton("针对选中内容提问")
        self.btn_ask_selection.setObjectName("Flat")
        self.btn_ask_selection.setToolTip(
            "先在对话气泡里选中一段内容，再点这里打开悬浮问答窗（Ctrl+Shift+W 召回）")
        self.btn_ask_selection.clicked.connect(self._ask_chat_selection)
        bottom.addWidget(self.btn_ask_selection)
        bottom.addStretch(1)
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
        else:
            widget = getattr(self, f"{key}_tab", None)
        if widget is None:
            widget = getattr(self, "chat_tab", None)
        index = self.tabs.indexOf(widget) if widget is not None else -1
        if index < 0:
            return
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(index)
        self.tabs.blockSignals(False)

    def _on_main_tab_changed(self, index):
        self._remember_active_tab()
        if self.tabs.widget(index) is self.assets_tab:
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
        subprocess.Popen(["xdg-open", str(target)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
            subprocess.Popen(["xdg-open", entry["cwd"]],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

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
        command = f"{shlex.quote(executable)} resume {shlex.quote(entry['sid'])}"
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
        if effort and effort != self._effort:
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
        self.btn_refresh_models.setEnabled(True)
        self.btn_refresh_models.setText("↻")

    def _on_catalog_loaded(self, models, error):
        if self._chat_busy:
            # 正在回复时不要切换模型/思考强度，等空闲后再应用
            self._pending_catalog = (models, error)
            return
        if error or not models:
            self.statusBar().showMessage(
                f"模型列表获取失败，继续使用内置列表：{error}"[:180], 6000)
            return
        old_model, old_effort = self._model, self._effort
        self._model_catalog = {m["id"]: m for m in models}
        self._catalog_loaded = True
        self._populate_model_combo()
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
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow()
                and provider_settings_mtime() != self._settings_mtime):
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
            has_project and not self._chat_busy and SDK_AVAILABLE)
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
        self._scroll_to_bottom()
        return wrapper, bubble

    def _append_chat_block(self, block):
        """把一条对话内容记到当前项目并显示出来。"""
        self._chat_state()["blocks"].append(block)
        self._render_chat_block(block)

    def _render_chat_block(self, block):
        kind = block.get("kind")
        if kind in ("user", "assistant"):
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
        for block in state["blocks"]:
            self._render_chat_block(block)
        if state["live"]:
            self._live_wrapper, self._live_bubble = self._add_bubble(
                "assistant", "", plain=True)
            self._live_bubble.set_plain(state["live"])
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
        self._scroll_to_bottom()

    def _add_tool_chip(self, name, data):
        self._append_chat_block(
            {"kind": "tool", "name": name, "data": data})

    def _scroll_to_bottom(self):
        bar = self.chat_scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

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
        if not SDK_AVAILABLE:
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
            self._send_study_learning(prompt, display)
            self._clear_attachments()
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
        who = "你" if target.get("role") == "user" else "Claude"
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
        layout.setContentsMargins(12, 10, 12, 10)
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

        self.chat_qa_input = ChatInput()
        self.chat_qa_input.setObjectName("ChatInput")
        self.chat_qa_input.setFixedHeight(84)
        self.chat_qa_input.setPlaceholderText(
            "直接输入问题，Enter 发送，Shift+Enter 换行（可选：先在对话里选中内容）")
        self.chat_qa_input.submitted.connect(self._send_chat_qa)
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
        self.chat_qa_send_btn.setEnabled(ready and not self._chat_qa_busy)
        self.chat_qa_status.setText(
            "Claude 正在回答…" if self._chat_qa_busy else "")

    def _send_chat_qa(self):
        text = self.chat_qa_input.toPlainText().strip()
        if not text:
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
        self._ensure_chat_qa_worker()
        quote = self._chat_qa_pending_quote
        self._chat_qa_pending_quote = ""
        self.chat_qa_quote_hint.setText("未选中文字")
        self.chat_qa_input.clear()

        user_id = self._chat_qa_next_id
        self._chat_qa_next_id += 1
        answer_id = self._chat_qa_next_id
        self._chat_qa_next_id += 1
        self._chat_qa_messages.append({
            "id": user_id, "role": "user", "text": text,
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
            text,
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
        for message in self._chat_qa_messages:
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

    def _show_current_qa_window(self):
        page = self.study_page
        if page is not None:
            page._show_qa_window()

    def _attach_study_tab(self, page):
        """把某项目的研读页挂进主窗口页签（如果它已经在页签里就不动）。"""
        if page is None or self.tabs.indexOf(page) >= 0:
            return
        index = min(max(1, self._study_tab_index), self.tabs.count())
        self.tabs.insertTab(index, page, "代码研读")
        self._study_tab_index = self.tabs.indexOf(page)
        self.tabs.setTabToolTip(index, f"代码研读 · {page.root or '未选择项目'}")
        page.show()

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
        page.setParent(window)
        window.setCentralWidget(page)
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
        page = window.takeCentralWidget()
        if page is not None:
            page.setParent(self.tabs)
            page.set_floating(False)
            if key == (self.current_project or ""):
                self._attach_study_tab(page)
                self.tabs.setCurrentWidget(page)
            else:
                # 后台项目的窗口收起来了：页签等切到该项目时再挂
                page.hide()
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
        if self._chat_busy:
            self.worker.interrupt()
            self.chat_status.setText("正在停止…")

    def _new_chat(self):
        if not self.current_project:
            self.statusBar().showMessage("请先选择一个项目", 3000)
            return
        state = self._chat_state()
        if state.get("worker") is not None:
            state["worker"].reset()
        state["blocks"] = []
        state["live"] = ""
        state["status"] = "新对话"
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
            subprocess.Popen(["xdg-open", self.current_project],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _open_editor(self):
        if self.current_project and shutil.which("code"):
            subprocess.Popen(["code", self.current_project],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _copy_path(self):
        if self.current_project:
            QApplication.clipboard().setText(self.current_project)
            self.statusBar().showMessage("已复制项目路径", 2500)

    # --------------------------------------------------------------- misc --
    def _probe_claude(self):
        executable = find_claude()
        if not executable:
            self.version_label.setText("未检测到 claude 命令")
            if not SDK_AVAILABLE:
                self._add_chat_note(
                    "无法在界面内对话：找不到 claude 命令，且未安装 Claude Agent SDK。")
            return
        self._claude_proc = QProcess(self)
        self._claude_proc.finished.connect(self._on_version_ready)
        self._claude_proc.start(executable, ["--version"])

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

    def closeEvent(self, event):
        self._closing = True
        try:
            self.config["geometry"] = bytes(
                self.saveGeometry().toBase64()).decode("ascii")
            save_config(self.config)
            workers = [getattr(self, attr, None)
                       for attr in ("_skill_worker", "_plugin_worker",
                                    "_chat_media_worker")]
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
        finally:
            super().closeEvent(event)


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Claude Console")
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
