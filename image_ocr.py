"""本地图片文字识别（OCR）。

使用 RapidOCR + ONNX Runtime，模型随 pip 包一起安装，完全离线运行，
不依赖任何在线接口；适合把截图里的报错、代码、表格文字识别成文本
后交给对话模型。

安装（已在控制台中执行过）：

    python3 -m pip install --user rapidocr-onnxruntime

其余依赖（numpy / opencv）本机已有，不需要额外安装。
"""

from __future__ import annotations

import os
import tempfile
import threading

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_RECOGNIZE_LOCK = threading.Lock()  # RapidOCR 引擎不保证并发安全，串行识别


def available() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - 缺依赖时把原因交给界面
        return False, str(exc)
    return True, ""


def _get_engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR

            _ENGINE = RapidOCR()
    return _ENGINE


def recognize_bytes(data: bytes) -> str:
    """识别 PNG/JPEG 等图片字节中的文字，返回按行拼接的文本。"""
    engine = _get_engine()
    handle = tempfile.NamedTemporaryFile(
        prefix="claude-console-ocr-", suffix=".png", delete=False)
    path = handle.name
    try:
        handle.write(data)
        handle.close()
        with _RECOGNIZE_LOCK:
            result, _elapsed = engine(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    lines = []
    for item in result or []:
        # RapidOCR 每项形如 [坐标框, 文字, 置信度]
        if len(item) < 2:
            continue
        text = str(item[1]).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)
