"""API 设置：base_url + key 的本地持久化（看板「设置」页维护）。

存 configs/api.json（纯本地文件，含明文 key——勿外传、勿提交版本库）。
优先级：显式参数 > 设置文件 > NL2KE_* 环境变量 > DEEPSEEK/OPENAI 环境变量 > 内置默认。
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path("configs") / "api.json"


def load_api_settings(path: Path | None = None) -> dict:
    """读设置文件；不存在返回 {}。"""
    path = path or DEFAULT_PATH
    if not Path(path).exists():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v}


ALLOWED_KEYS = {"base_url", "api_key", "embed_base_url", "embed_api_key", "embed_model"}


def save_api_settings(settings: dict, path: Path | None = None) -> Path:
    """写设置文件（只保留 ALLOWED_KEYS 中的非空字符串键）。"""
    path = Path(path or DEFAULT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        k: v for k, v in settings.items()
        if k in ALLOWED_KEYS and isinstance(v, str) and v
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def mask_key(key: str) -> str:
    """显示用脱敏：sk-…尾号。"""
    if not key:
        return ""
    if len(key) <= 7:
        return "…"
    return f"{key[:3]}…{key[-4:]}"
