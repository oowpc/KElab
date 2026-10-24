"""API 设置页：base_url + key + 连接测试 + 模型列表——全程不碰环境变量和代码。

设置存 configs/api.json（明文 key，纯本地）；优先级高于环境变量（见 kelab/settings.py）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..settings import load_api_settings, mask_key, save_api_settings
from .app import WebApp
from .ui import ESC, layout

DEFAULT_BASE_URL = "https://api.llm.ustc.edu.cn/v1"


def _current(app: WebApp) -> dict:
    saved = load_api_settings(app.root / "configs" / "api.json")
    import os
    return {
        "base_url": saved.get("base_url") or os.getenv("NL2KE_BASE_URL") or DEFAULT_BASE_URL,
        "api_key": saved.get("api_key") or os.getenv("NL2KE_API_KEY") or "",
        "embed_base_url": saved.get("embed_base_url") or os.getenv("NL2KE_EMBED_BASE_URL") or "",
        "embed_api_key": saved.get("embed_api_key") or os.getenv("NL2KE_EMBED_API_KEY") or "",
        "embed_model": saved.get("embed_model") or os.getenv("NL2KE_EMBED_MODEL") or "",
        "from_file": bool(saved.get("api_key") or saved.get("base_url")),
    }


def test_connection(base_url: str, api_key: str, timeout: float = 15.0) -> tuple[bool, str]:
    """用 /models 验证连通与鉴权（零 token）：返回 (ok, 详情)。"""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = sorted(m.get("id", "") for m in data.get("data", []))
        return True, f"连接正常，{len(models)} 个可用模型：{', '.join(models[:12])}" + (
            "…" if len(models) > 12 else "")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}：{exc.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def page_settings(app: WebApp, message: str = "") -> bytes:
    cur = _current(app)
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    source = "configs/api.json" if cur["from_file"] else "环境变量/默认"
    body = f"""<h1>API 设置</h1>{flash}
<div class='card'>
<p class='muted'>当前生效来源：{ESC(source)}；key 以明文保存在项目 configs/api.json（请勿外传/提交）。
页面保存后 <b>CLI、看板、启动的实验子进程全部生效</b>，无需再设环境变量。</p>
<form method='post' action='/settings/save'>
<div style='margin:8px 0'><label>base_url
<input type='text' name='base_url' value='{ESC(cur["base_url"])}' style='width:45%' class='mono'>
</label></div>
<div style='margin:8px 0'><label>API key
<input type='password' name='api_key' value='' placeholder='{ESC(mask_key(cur["api_key"]) or "未配置")}'
 style='width:45%' class='mono' autocomplete='off'>
<span class='muted'>留空表示不修改（当前 {ESC(mask_key(cur["api_key"]) or "无")}）</span></label></div>
<h2 style='margin-top:14px'>embedding（dense/hybrid 检索用，可选）</h2>
<p class='muted'>留空则 dense/hybrid 检索器不可用；key 留空 = 复用上面的主 key，base_url 留空 = 同主 base_url。</p>
<div style='margin:8px 0'><label>embed base_url
<input type='text' name='embed_base_url' value='{ESC(cur["embed_base_url"])}' style='width:41%' class='mono'
 placeholder='默认同主 base_url'></label></div>
<div style='margin:8px 0'><label>embed key
<input type='password' name='embed_api_key' value='' placeholder='{ESC(mask_key(cur["embed_api_key"]) or "复用主 key")}'
 style='width:41%' class='mono' autocomplete='off'></label></div>
<div style='margin:8px 0'><label>embed 模型
<input type='text' name='embed_model' value='{ESC(cur["embed_model"])}' style='width:41%' class='mono'
 placeholder='如 text-embedding-v4'></label></div>
<button>保存</button>
<button formaction='/settings/test' formmethod='post' class='ghost'>测试连接（用上面填的值，零 token）</button>
<button formaction='/settings/clear' formmethod='post' class='ghost'
 onclick="return confirm('确定清除本地保存的 API 配置？（configs/api.json 将被删除）')"
 style='border-color:var(--crit);color:var(--crit)'>清除本地保存</button>
</form></div>
<div class='card'><b>可用模型</b>（配置页模型字段会从这里自动下拉）：
{app.models_html()}</div>"""
    return layout("API 设置", body, active="settings")


def post_settings_save(app: WebApp, form: dict) -> str:
    base_url = form.get("base_url", [""])[0].strip()
    api_key = form.get("api_key", [""])[0].strip()
    current = _current(app)
    settings = {
        "base_url": base_url or current["base_url"],
        "api_key": api_key or current["api_key"],
        "embed_base_url": form.get("embed_base_url", [""])[0].strip(),
        "embed_api_key": form.get("embed_api_key", [""])[0].strip() or current["embed_api_key"],
        "embed_model": form.get("embed_model", [""])[0].strip(),
    }
    if not settings["api_key"]:
        return "未保存：API key 为空"
    save_api_settings(settings, app.root / "configs" / "api.json")
    app.invalidate_models_cache()
    embed_note = f"；embed 模型 {settings['embed_model']}" if settings["embed_model"] else ""
    return (f"已保存到 configs/api.json（base_url={settings['base_url']}，"
            f"key {mask_key(settings['api_key'])}{embed_note}）")


def post_settings_test(app: WebApp, form: dict) -> str:
    base_url = form.get("base_url", [""])[0].strip() or _current(app)["base_url"]
    api_key = form.get("api_key", [""])[0].strip() or _current(app)["api_key"]
    if not api_key:
        return "请先填 API key 再测试"
    ok, detail = test_connection(base_url, api_key)
    return ("✓ " if ok else "✗ ") + detail


def post_settings_clear(app: WebApp) -> str:
    path = app.root / "configs" / "api.json"
    if path.exists():
        path.unlink()
    app.invalidate_models_cache()
    return "已删除 configs/api.json（回落到环境变量/默认）"
