#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 SiliconFlow 的视频接口（MiniMax-H3 / Wan2.2 等）生成视频。

只依赖 Python 标准库。Key 读取顺序：
1. 环境变量 SILICONFLOW_API_KEY
2. ~/.dsh/secrets/media-tools.env
3. ~/.codex/secrets/media-tools.env

接口：
  POST https://api.siliconflow.cn/v1/video/submit
  POST https://api.siliconflow.cn/v1/video/status
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "MiniMaxAI/MiniMax-H3"


def load_key():
    value = os.environ.get("SILICONFLOW_API_KEY")
    if value and value.strip():
        return value.strip()
    for env_file in (Path.home() / ".dsh/secrets/media-tools.env",
                     Path.home() / ".codex/secrets/media-tools.env"):
        try:
            if not env_file.is_file():
                continue
            for line in env_file.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("SILICONFLOW_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def post_json(url, payload, key, timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "User-Agent": "claude-console-siliconflow-video"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:600]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误：{exc.reason}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise RuntimeError(f"返回不是 JSON：{body[:600]}") from exc


def download(url, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "claude-console-siliconflow-video"})
    with urllib.request.urlopen(request, timeout=300) as response:
        data = response.read()
    if not data:
        raise RuntimeError("下载到的视频为空")
    tmp = output.with_suffix(output.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(output)
    return output


def main():
    parser = argparse.ArgumentParser(description="SiliconFlow 视频生成")
    parser.add_argument("prompt")
    parser.add_argument("out")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--resolution", default="768p")
    parser.add_argument("--ratio", default="16:9")
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll", type=float, default=3.0)
    args = parser.parse_args()

    key = load_key()
    if not key:
        sys.exit(
            "缺少 SILICONFLOW_API_KEY：请配置环境变量或 "
            "~/.dsh/secrets/media-tools.env")

    model = (args.model or os.environ.get("SILICONFLOW_VIDEO_MODEL")
             or DEFAULT_MODEL)
    payload = {
        "model": model,
        "prompt": args.prompt,
        "duration": max(5, min(15, int(args.seconds or 5))),
        "resolution": args.resolution if args.resolution in ("480p", "768p")
        else "768p",
        "ratio": args.ratio,
    }
    print(f"submit: model={model} duration={payload['duration']} "
          f"resolution={payload['resolution']} ratio={payload['ratio']}",
          flush=True)
    result = post_json(API_BASE + "/video/submit", payload, key)
    request_id = result.get("requestId") or result.get("request_id")
    if not request_id:
        raise RuntimeError(f"没有拿到 requestId：{result}")
    print(f"requestId: {request_id}", flush=True)

    deadline = time.time() + max(60, args.timeout)
    video_url = ""
    while time.time() < deadline:
        status = post_json(
            API_BASE + "/video/status", {"requestId": request_id}, key)
        state = status.get("status") or ""
        print(f"status: {state}", flush=True)
        if state == "Succeed":
            results = status.get("results") or {}
            videos = results.get("videos") or []
            if videos:
                video_url = videos[0].get("url") or ""
            if not video_url:
                raise RuntimeError(f"生成成功但没有视频地址：{status}")
            break
        if state == "Failed":
            reason = status.get("reason") or status.get("message") or "未知原因"
            raise RuntimeError(f"生成失败：{reason}")
        time.sleep(max(1.0, args.poll))

    if not video_url:
        raise RuntimeError("等待视频生成超时")

    print("downloading video…", flush=True)
    output = download(video_url, args.out)
    print(f"done: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - 作为命令行脚本输出给控制台
        sys.exit(f"{type(exc).__name__}: {exc}")
