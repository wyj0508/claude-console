#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude / Codex 共享技能库、统一历史与插件管理。

设计说明
--------
* 技能：两份工具都读取带有 ``SKILL.md`` 的目录，因此把技能实体放在
  ``~/.agents/skills``，再分别软链接到 ``~/.claude/skills`` 与
  ``~/.codex/skills``。Codex 的 ``.system`` 目录保持原样。
* 历史：两者格式不同，这里提供 Codex rollout 的解析；界面把 Claude 与
  Codex 的会话合并展示，不修改任何原始记录。
* 插件：Claude 使用 ``.claude-plugin``，Codex 使用 ``.codex-plugin``，
  格式不兼容，因此只做并排管理，不强行互链。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
SHARED_ROOT = HOME / ".agents"
SHARED_SKILLS = SHARED_ROOT / "skills"
SHARED_HISTORY = SHARED_ROOT / "history"

CLAUDE_DIR = HOME / ".claude"
CLAUDE_SKILLS = CLAUDE_DIR / "skills"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"

CODEX_DIR = HOME / ".codex"
CODEX_SKILLS = CODEX_DIR / "skills"
CODEX_SESSIONS = CODEX_DIR / "sessions"
CODEX_CONFIG = CODEX_DIR / "config.toml"

SKILL_LINK_TARGETS = (
    ("Claude", CLAUDE_SKILLS),
    ("Codex", CODEX_SKILLS),
)

GITHUB_WEB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?/?$"
)
GITHUB_SHORT_RE = re.compile(r"^([\w.-]+)/([\w.-]+)(?:/(.+))?$")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _read_text(path: Path, limit: int | None = None) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter（只覆盖技能常用字段）。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    data: dict = {}
    key = None
    buffer: list[str] = []
    multiline = False

    def flush():
        nonlocal buffer, multiline
        if key is None:
            return
        if multiline:
            data[key] = " ".join(part for part in buffer if part).strip()
        buffer = []
        multiline = False

    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        match = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", line.strip())
        if indent == 0 and match:
            flush()
            key = match.group(1)
            value = match.group(2).strip()
            if value in (">", "|", ">-", "|-", ">+", "|+"):
                multiline = True
                buffer = []
            else:
                data[key] = _unquote(value)
                key = None
            continue
        if key is None:
            continue
        stripped = line.strip()
        if multiline:
            buffer.append(stripped)
            continue
        if indent > 0:
            # metadata 之类的一层嵌套，取其中的 version / license
            nested_match = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", stripped)
            if nested_match:
                sub_key, sub_value = nested_match.groups()
                if sub_key in ("version", "license", "author", "official_repository"):
                    data.setdefault(sub_key, _unquote(sub_value))
    flush()
    if not data.get("version"):
        match = re.search(r"(?m)^\s+version:\s*([\"']?)([^\"'\n]+)\1", block)
        if match:
            data["version"] = _unquote(match.group(2))
    return data


def read_skill_meta(skill_dir: Path) -> dict:
    meta = parse_frontmatter(_read_text(skill_dir / "SKILL.md"))
    meta.setdefault("name", skill_dir.name)
    if not meta.get("description"):
        meta["description"] = ""
    return meta


def _link_ok(target: Path, real: Path) -> bool:
    if not os.path.lexists(target):
        return False
    try:
        return Path(os.path.realpath(target)) == Path(os.path.realpath(real))
    except OSError:
        return False


def _ensure_link(target: Path, real: Path, replace: bool = False) -> str:
    """让 target 指向 real；返回 created / kept / replaced / conflict。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if _link_ok(target, real):
        return "kept"
    if os.path.lexists(target):
        if not replace:
            return "conflict"
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        state = "replaced"
    else:
        state = "created"
    os.symlink(real, target, target_is_directory=True)
    return state


# --------------------------------------------------------------------------- #
# 技能库
# --------------------------------------------------------------------------- #
def _skill_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    result = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or entry.name == ".system":
            continue
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            result.append(entry)
    return result


def list_skills() -> list[dict]:
    ensure_shared_skills()
    skills = []
    for entry in _skill_dirs(SHARED_SKILLS):
        real = Path(os.path.realpath(entry))
        meta = read_skill_meta(real)
        skills.append({
            "name": meta.get("name") or entry.name,
            "dir_name": entry.name,
            "path": real,
            "description": _one_line(meta.get("description", "")),
            "version": meta.get("version", ""),
            "links": {
                label: _link_ok(root / entry.name, real)
                for label, root in SKILL_LINK_TARGETS
            },
        })
    return skills


def _one_line(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def ensure_shared_skills() -> list[str]:
    """创建共享目录，并补齐到 Claude/Codex 的软链接。"""
    actions = []
    SHARED_SKILLS.mkdir(parents=True, exist_ok=True)
    for _label, root in SKILL_LINK_TARGETS:
        root.mkdir(parents=True, exist_ok=True)
    for entry in _skill_dirs(SHARED_SKILLS):
        real = Path(os.path.realpath(entry))
        for label, root in SKILL_LINK_TARGETS:
            state = _ensure_link(root / entry.name, real)
            if state == "created":
                actions.append(f"{label}：已链接 {entry.name}")
            elif state == "conflict":
                actions.append(f"{label}：{entry.name} 已存在且未被覆盖")
    return actions


README_TEXT = """# Claude / Codex 共享目录

这个目录由「Claude 控制台」维护，用来让 Claude Code 与 Codex 共用同一套资源。

## skills/
技能实体存放处。每个技能是一个含 `SKILL.md` 的目录。
`~/.claude/skills/<name>` 和 `~/.codex/skills/<name>` 都软链接到这里。

## history/
- `claude-projects` -> `~/.claude/projects`
- `codex-sessions` -> `~/.codex/sessions`

两边原始会话格式不同，这里只做只读统一入口；控制台的「会话历史」
会把两边的会话合并展示。

## 插件
Claude 使用 `.claude-plugin`，Codex 使用 `.codex-plugin`，格式不兼容，
因此插件不做目录互链，由控制台的「技能与插件 → 插件」分别管理。

## 全局说明
`~/.claude/CLAUDE.md` 与 `~/.codex/AGENTS.md` 已写入同样的共享入口说明，
两边的 AI 新会话都能知道去哪里查找对方的历史。
"""


def ensure_shared_layout() -> list[str]:
    """建立共享技能库、历史只读入口和说明文件。"""
    SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    SHARED_HISTORY.mkdir(parents=True, exist_ok=True)
    for name, target in (("claude-projects", CLAUDE_PROJECTS),
                         ("codex-sessions", CODEX_SESSIONS)):
        if target.is_dir():
            _ensure_link(SHARED_HISTORY / name, target)
    readme = SHARED_ROOT / "README.md"
    if not readme.exists():
        try:
            readme.write_text(README_TEXT, encoding="utf-8")
        except OSError:
            pass
    return ensure_shared_skills()


def adopt_existing_skills() -> list[str]:
    """把直接装在 Claude/Codex 技能目录里的实体技能收进共享库。"""
    actions = []
    SHARED_SKILLS.mkdir(parents=True, exist_ok=True)
    for label, root in SKILL_LINK_TARGETS:
        for entry in _skill_dirs(root):
            if entry.is_symlink():
                continue
            target = SHARED_SKILLS / entry.name
            if os.path.lexists(target):
                actions.append(f"跳过 {label}/{entry.name}：共享库中已有同名技能")
                continue
            shutil.move(str(entry), str(target))
            actions.append(f"已从 {label} 收编 {entry.name}")
            ensure_shared_skills()
    return actions


def install_skill_dir(source: Path) -> str:
    source = Path(source)
    if not (source / "SKILL.md").is_file():
        raise ValueError("所选目录里没有 SKILL.md")
    meta = read_skill_meta(source)
    name = re.sub(r"[^\w.-]+", "-", meta.get("name") or source.name)
    name = name.strip(".-")
    if not name or name in (".", ".."):
        raise ValueError("无法确定安全的技能名称")
    target = SHARED_SKILLS / name
    if target.exists() or os.path.lexists(target):
        raise ValueError(f"共享库中已存在技能「{name}」")
    SHARED_SKILLS.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)
    ensure_shared_skills()
    return name


def remove_skill(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("非法的技能名称")
    target = SHARED_SKILLS / name
    for _label, root in SKILL_LINK_TARGETS:
        link = root / name
        if os.path.lexists(link):
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link)
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


# --------------------------------------------------------------------------- #
# 从 GitHub 安装技能
# --------------------------------------------------------------------------- #
def parse_github_source(text: str) -> dict:
    text = text.strip()
    match = GITHUB_WEB_RE.match(text)
    if match:
        owner, repo, ref, subpath = match.groups()
        return {"owner": owner, "repo": repo, "ref": ref or "",
                "subpath": (subpath or "").strip("/")}
    match = re.match(
        r"^([\w.-]+)/([\w.-]+)/(?:tree|blob)/([^/]+)(?:/(.*))?$", text)
    if match:
        owner, repo, ref, subpath = match.groups()
        return {"owner": owner, "repo": repo, "ref": ref,
                "subpath": (subpath or "").strip("/")}
    match = GITHUB_SHORT_RE.match(text)
    if match and " " not in text and not text.startswith(("http://", "https://")):
        owner, repo, subpath = match.groups()
        return {"owner": owner, "repo": repo, "ref": "",
                "subpath": (subpath or "").strip("/")}
    raise ValueError("无法识别 GitHub 地址，示例：owner/repo 或 https://github.com/owner/repo")


def _download(url: str, dest: Path, progress=None,
              should_cancel=None) -> None:
    """下载文件；progress(done, total, stage) 用于界面显示进度。

    total 为 0 表示服务器没给 Content-Length，界面应显示为不确定进度。
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": "claude-console-skill-installer",
        "Accept": "application/x-gzip,application/octet-stream,*/*",
    })
    with urllib.request.urlopen(request, timeout=90) as response, dest.open("wb") as fh:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        done = 0
        if progress:
            progress(0, total, "正在下载")
        while True:
            if should_cancel and should_cancel():
                raise RuntimeError("操作已取消")
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total, "正在下载")
        if progress:
            progress(done, total or done, "下载完成")


def _safe_extract(archive: Path, dest: Path) -> None:
    dest = dest.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(dest, filter="data")
            return
        except TypeError:
            pass
        for member in tar.getmembers():
            member_path = (dest / member.name).resolve()
            if not str(member_path).startswith(str(dest) + os.sep):
                raise ValueError("压缩包包含越界路径，已中止")
        tar.extractall(dest)


def install_skill_from_github(source: str, progress=None,
                              should_cancel=None) -> list[str]:
    """从 GitHub 下载并安装技能。

    progress(done, total, stage)：下载阶段给出字节进度；
    解压 / 安装阶段 total 为 1，表示进入下一个步骤。
    """
    info = parse_github_source(source)
    refs = [info["ref"]] if info["ref"] else ["main", "master"]
    tmp = Path(tempfile.mkdtemp(prefix="claude-console-skill-"))
    try:
        archive = tmp / "repo.tar.gz"
        last_error = None
        for ref in refs:
            if not ref:
                continue
            for namespace in ("heads", "tags", ""):
                suffix = f"refs/{namespace}/{ref}" if namespace else ref
                url = (f"https://codeload.github.com/{info['owner']}/"
                       f"{info['repo']}/tar.gz/{suffix}")
                try:
                    _download(url, archive, progress=progress,
                              should_cancel=should_cancel)
                    break
                except Exception as exc:  # noqa: BLE001 - 逐个分支/标签尝试
                    if should_cancel and should_cancel():
                        raise RuntimeError("操作已取消") from exc
                    last_error = exc
            else:
                continue
            break
        else:
            raise RuntimeError(f"下载失败：{last_error or '未知错误'}")

        if progress:
            progress(1, 1, "正在解压")
        extract_dir = tmp / "src"
        extract_dir.mkdir()
        _safe_extract(archive, extract_dir)
        tops = [p for p in extract_dir.iterdir() if p.is_dir()]
        if not tops:
            raise ValueError("压缩包内容为空")
        root = tops[0]
        if info["subpath"]:
            root = root / info["subpath"]
            if not root.is_dir():
                raise ValueError(f"仓库中找不到路径：{info['subpath']}")

        found = [candidate.parent for candidate in sorted(root.rglob("SKILL.md"))]
        if not found:
            raise ValueError("仓库中没有找到 SKILL.md")
        # 只保留最外层技能目录，避免嵌套重复安装
        found = [path for path in found
                 if not any(other != path and other in path.parents
                            for other in found)]
        installed = []
        for index, skill_dir in enumerate(found, start=1):
            if should_cancel and should_cancel():
                raise RuntimeError("操作已取消")
            if progress:
                progress(index, len(found), "正在安装技能")
            installed.append(install_skill_dir(skill_dir))
        if progress:
            progress(len(found), len(found), "安装完成")
        return installed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Codex 会话历史
# --------------------------------------------------------------------------- #
def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return None


def iter_codex_session_files():
    if not CODEX_SESSIONS.is_dir():
        return []
    return sorted(CODEX_SESSIONS.rglob("*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _codex_entries(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def parse_codex_session(path: Path) -> dict | None:
    path = Path(path)
    sid = path.stem
    cwd = ""
    title = ""
    rounds = 0
    last_ts = None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    fallback_rounds = 0
    is_subagent = False
    for entry in _codex_entries(path):
        ts = parse_timestamp(entry.get("timestamp"))
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
        kind = entry.get("type")
        payload = entry.get("payload") or {}
        if kind == "session_meta":
            sid = payload.get("session_id") or payload.get("id") or sid
            cwd = payload.get("cwd") or cwd
            source = payload.get("source")
            if isinstance(source, dict) and "subagent" in source:
                is_subagent = True
            if payload.get("thread_source") not in (None, "", "user"):
                is_subagent = True
        elif kind == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item") or {}
            if item.get("type") == "UserMessage":
                rounds += 1
                if not title:
                    title = _item_text(item)[:160]
        elif kind == "response_item" and payload.get("type") == "message":
            if payload.get("role") == "user":
                text = _item_text(payload)
                if text and not text.lstrip().startswith("<"):
                    fallback_rounds += 1
                    if not title:
                        title = text[:160]
    rounds = max(rounds, fallback_rounds)
    updated = last_ts or datetime.fromtimestamp(mtime).astimezone()
    return {
        "source": "codex",
        "sid": sid,
        "path": path,
        "title": " ".join(title.split()) or "未命名 Codex 会话",
        "rounds": rounds,
        "updated": updated,
        "cwd": cwd,
        "subagent": is_subagent,
    }


def _item_text(item: dict) -> str:
    parts = []
    for block in item.get("content") or []:
        if isinstance(block, dict):
            text = block.get("text") or block.get("input_text") or block.get("output_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def load_codex_messages(path: Path, limit: int = 40) -> list[tuple[str, str]]:
    """按时间顺序读取 Codex 会话中的用户/助手文本。

    新版 rollout 用 event_msg/item_completed 记录，
    旧版或续接会话则可能只有 response_item/message，两者都兼容。
    """
    candidates = []
    seen_ids = set()
    seen_texts = set()

    def add(ts, role, text, item_id):
        text = (text or "").strip()
        if not text:
            return
        if role == "user" and text.lstrip().startswith("<"):
            return
        if item_id and item_id in seen_ids:
            return
        normalized = " ".join(text.split())[:500]
        key = (role, normalized)
        if key in seen_texts:
            return
        if item_id:
            seen_ids.add(item_id)
        seen_texts.add(key)
        sort_key = (0, ts.timestamp()) if ts else (1, 0.0)
        candidates.append((sort_key, role, text))

    for entry in _codex_entries(Path(path)):
        kind = entry.get("type")
        payload = entry.get("payload") or {}
        ts = parse_timestamp(entry.get("timestamp"))
        if kind == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item") or {}
            item_type = item.get("type")
            if item_type == "UserMessage":
                add(ts, "user", _item_text(item), item.get("id"))
            elif item_type == "AgentMessage":
                add(ts, "assistant", _item_text(item), item.get("id"))
        elif kind == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                add(ts, role, _item_text(payload), payload.get("id"))

    candidates.sort(key=lambda item: item[0])
    messages = [(role, text) for _key, role, text in candidates]
    return messages[-limit:]


def codex_sessions_for_cwd(cwd: str, limit: int = 50,
                           include_subagents: bool = False) -> list[dict]:
    target = os.path.realpath(os.path.expanduser(cwd)) if cwd else ""
    sessions = []
    for path in iter_codex_session_files():
        info = parse_codex_session(path)
        if info is None:
            continue
        if info.get("subagent") and not include_subagents:
            continue
        if target and os.path.realpath(info["cwd"] or "") != target:
            continue
        sessions.append(info)
        if len(sessions) >= limit:
            break
    return sessions


def all_codex_sessions(limit: int = 200,
                       include_subagents: bool = False) -> list[dict]:
    sessions = []
    for path in iter_codex_session_files():
        info = parse_codex_session(path)
        if info is None:
            continue
        if info.get("subagent") and not include_subagents:
            continue
        sessions.append(info)
        if len(sessions) >= limit:
            break
    return sessions


# --------------------------------------------------------------------------- #
# 插件管理
# --------------------------------------------------------------------------- #
def _candidate_paths():
    yield shutil.which("claude")
    for pattern in (".nvm/versions/node/*/bin/claude",
                    ".local/share/fnm/node-versions/*/installation/bin/claude",
                    ".nodenv/versions/*/bin/claude",
                    ".asdf/installs/nodejs/*/bin/claude",
                    ".volta/bin/claude"):
        for path in HOME.glob(pattern):
            yield str(path)
    for fixed in (".local/bin/claude", ".npm-global/bin/claude",
                  ".bun/bin/claude", "/usr/local/bin/claude",
                  "/usr/bin/claude"):
        yield str(HOME / fixed) if not fixed.startswith("/") else fixed


def find_claude_cli() -> str | None:
    for candidate in _candidate_paths():
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_codex_cli() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    candidates = [
        Path("/usr/lib/chatgpt/resources/codex"),
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
        HOME / ".local/bin/codex",
        HOME / ".bun/bin/codex",
        HOME / ".npm-global/bin/codex",
    ]
    candidates.extend(HOME.glob(".nvm/versions/node/*/bin/codex"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run(argv: list[str], timeout: int = 120):
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, env=os.environ.copy())
    return proc.returncode, proc.stdout, proc.stderr


def _cli_env() -> dict:
    env = os.environ.copy()
    claude = find_claude_cli()
    if claude:
        bin_dir = str(Path(claude).parent)
        if bin_dir not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def _run_env(argv: list[str], timeout: int = 120):
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, env=_cli_env())
    return proc.returncode, proc.stdout, proc.stderr


def claude_plugins(include_available: bool = True) -> dict:
    cli = find_claude_cli()
    if not cli:
        raise RuntimeError("找不到 claude 命令")
    argv = [cli, "plugin", "list", "--json"]
    if include_available:
        argv.append("--available")
    code, out, err = _run_env(argv)
    if code != 0:
        raise RuntimeError((err or out or "claude plugin list 失败").strip())
    data = json.loads(out)
    # 不带 --available 时，Claude CLI 直接返回已安装插件数组
    if isinstance(data, list):
        data = {"installed": data}
    return data


def codex_plugins(include_available: bool = True) -> dict:
    cli = find_codex_cli()
    if not cli:
        raise RuntimeError("找不到 codex 命令")
    argv = [cli, "plugin", "list", "--json"]
    if include_available:
        argv.append("--available")
    code, out, err = _run(argv)
    if code != 0:
        raise RuntimeError((err or out or "codex plugin list 失败").strip())
    data = json.loads(out)
    if isinstance(data, list):
        data = {"installed": data}
    return data


def claude_plugin_action(action: str, plugin_id: str) -> tuple[bool, str]:
    cli = find_claude_cli()
    if not cli:
        return False, "找不到 claude 命令"
    argv = [cli, "plugin", action, plugin_id]
    if action == "install":
        argv = [cli, "plugin", "install", "-y", plugin_id]
    code, out, err = _run_env(argv, timeout=180)
    if code != 0 and action == "install" and ("-y" in (err + out).lower()
                                              or "unknown option" in (err + out).lower()):
        code, out, err = _run_env([cli, "plugin", "install", plugin_id], timeout=180)
    message = (out or err or "").strip()
    return code == 0, message


def codex_plugin_action(action: str, plugin_id: str) -> tuple[bool, str]:
    cli = find_codex_cli()
    if not cli:
        return False, "找不到 codex 命令"
    command = "add" if action == "install" else "remove"
    code, out, err = _run([cli, "plugin", command, "--json", plugin_id],
                          timeout=180)
    message = (out or err or "").strip()
    return code == 0, message


def codex_set_enabled(plugin_id: str, enabled: bool) -> tuple[bool, str]:
    """通过编辑 config.toml 切换 Codex 插件启用状态。"""
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"无法读取 Codex 配置：{exc}"
    header = f'[plugins."{plugin_id}"]'
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == header:
            start = index
            break
    if start is None:
        if not enabled:
            return False, "该插件未在配置中启用"
        lines.extend(["", header, "enabled = true"])
    else:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].lstrip().startswith("["):
                end = index
                break
        replaced = False
        for index in range(start + 1, end):
            if re.match(r"^\s*enabled\s*=", lines[index]):
                lines[index] = f"enabled = {'true' if enabled else 'false'}"
                replaced = True
                break
        if not replaced:
            lines.insert(start + 1, f"enabled = {'true' if enabled else 'false'}")
    tmp = CODEX_CONFIG.with_suffix(".tmp")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(CODEX_CONFIG)
    except OSError as exc:
        return False, f"写入 Codex 配置失败：{exc}"
    return True, "已更新 Codex 插件状态"
