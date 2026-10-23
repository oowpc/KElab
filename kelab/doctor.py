"""环境自检（doctor）：新手拿到平台后第一件事就是跑它——每项检查附修复方法。

CLI:  python -m kelab.cli doctor [--ping]
看板: 向导首页内嵌同样的自检（不含 API 实调）。
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import file_md5, load_domain


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""  # 失败时的修复指引


def _check_python() -> Check:
    version = sys.version_info
    ok = version >= (3, 11)
    return Check(
        "Python 版本",
        ok,
        f"{version.major}.{version.minor}.{version.micro}（要求 ≥3.11，tomllib 读取配置）",
        "" if ok else "安装 Python 3.11+（建议 3.13）：https://www.python.org/downloads/",
    )


def _check_deps() -> Check:
    missing = []
    for module, pip_name in (("lark", "lark"), ("matplotlib", "matplotlib"),
                             ("openpyxl", "openpyxl"), ("numpy", "numpy")):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(pip_name)
    return Check(
        "Python 依赖包",
        not missing,
        "lark / matplotlib / openpyxl / numpy 全部可导入" if not missing
        else f"缺少：{', '.join(missing)}",
        "" if not missing else "在项目目录执行：pip install -r requirements.txt",
    )


def _check_cwd(root: Path) -> Check:
    ok = (root / "configs" / "domains").is_dir() and (root / "kelab").is_dir()
    return Check(
        "运行目录",
        ok,
        "当前目录是项目根目录" if ok else f"当前目录 {root} 下找不到 kelab/ 和 configs/",
        "" if ok else "先 cd 到项目根目录（含有 kelab/、configs/ 的那个文件夹）再执行命令",
    )


def _check_domains(root: Path) -> list[Check]:
    checks: list[Check] = []
    domains_dir = root / "configs" / "domains"
    if not domains_dir.is_dir():
        return []
    for toml in sorted(domains_dir.glob("*.toml")):
        try:
            domain = load_domain(toml)
        except Exception as exc:
            checks.append(Check(f"领域配置 {toml.name}", False, f"配置解析失败: {exc}",
                                "到「配置」页检查该文件，或对照 configs/domains/conic.toml 修正"))
            continue
        grammar = domain.grammar_path
        if not grammar.is_absolute():
            grammar = root / grammar
        if not grammar.exists():
            checks.append(Check(
                f"领域 {domain.name}：grammar", False,
                f"grammar 文件不存在：{grammar}",
                "确认 grammar 路径；平台内嵌语法在 kelab/grammar/ke_syntax.lark",
            ))
            continue
        if domain.grammar_md5:
            actual = file_md5(grammar)
            if actual != domain.grammar_md5.lower():
                checks.append(Check(
                    f"领域 {domain.name}：grammar md5", False,
                    f"md5 不匹配：配置 {domain.grammar_md5[:8]}… 实际 {actual[:8]}…",
                    "到「配置」页把 grammar_md5 留空保存（自动重算），或确认语法文件版本",
                ))
                continue
        template = domain.prompt_template
        if not template.is_absolute():
            template = root / template
        if not template.exists():
            checks.append(Check(
                f"领域 {domain.name}：prompt 模板", False,
                f"模板文件不存在：{template}",
                "到「Prompt」页检查模板路径，或新建模板",
            ))
            continue
        checks.append(Check(f"领域 {domain.name}：语法与模板", True,
                            f"grammar md5 匹配、prompt 模板在位"))
    return checks


def _check_api_key(root: Path | None = None) -> Check:
    from .settings import load_api_settings, mask_key

    saved = load_api_settings((root / "configs" / "api.json") if root else None)
    found_env = [v for v in ("NL2KE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
                 if os.getenv(v)]
    if saved.get("api_key"):
        return Check("LLM API key", True,
                     f"configs/api.json（key {mask_key(saved['api_key'])}"
                     + (f"，base_url={saved['base_url']}" if saved.get("base_url") else "")
                     + "）")
    return Check(
        "LLM API key",
        bool(found_env),
        f"已配置环境变量：{', '.join(found_env)}" if found_env
        else "configs/api.json 不存在且三个环境变量都没设",
        "" if found_env else
        "看板「设置」页配置（推荐，落 configs/api.json）；"
        "或 PowerShell：$env:NL2KE_API_KEY=\"sk-你的key\"",
    )


def _check_writable(root: Path) -> Check:
    target = root / "data" / "derived"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check("数据目录可写", True, "data/derived 可写")
    except OSError as exc:
        return Check("数据目录可写", False, f"{target}: {exc}",
                     "检查目录权限；不要把项目放在需要管理员权限的目录")


def _ping_models(root: Path) -> list[Check]:
    """可选：真实调一次 API（花费约几十个 token），验证生成/judge 模型可用。"""
    from .llm import chat_with_usage, resolve_api

    checks: list[Check] = []
    domains_dir = root / "configs" / "domains"
    models: set[str] = set()
    for toml in sorted(domains_dir.glob("*.toml")):
        try:
            domain = load_domain(toml)
            models.add(domain.gen_model)
            models.add(domain.judge_model)
        except Exception:
            continue
    for model in sorted(models):
        try:
            cfg = resolve_api(model, timeout=30)
            chat_with_usage(cfg, [{"role": "user", "content": "只回复两个字：正常"}])
            checks.append(Check(f"API 实调 {model}", True, "响应正常"))
        except Exception as exc:
            checks.append(Check(f"API 实调 {model}", False, str(exc)[:200],
                                "检查 key 是否有效、模型名是否存在、网络/代理"))
    return checks


def run_checks(root: Path, ping: bool = False) -> list[Check]:
    root = Path(root).resolve()
    checks = [
        _check_python(),
        _check_deps(),
        _check_cwd(root),
        *_check_domains(root),
        _check_api_key(root),
        _check_writable(root),
    ]
    if ping:
        checks.extend(_ping_models(root))
    return checks


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "✓" if c.ok else "✗"
        lines.append(f"{mark} {c.name}：{c.detail}")
        if not c.ok and c.fix:
            lines.append(f"    修复：{c.fix}")
    failed = sum(1 for c in checks if not c.ok)
    lines.append("")
    lines.append("全部通过，可以开跑：python -m kelab.cli web --port 8765 --open"
                 if failed == 0 else f"{failed} 项未通过，按上面的「修复」指引处理后再试")
    return "\n".join(lines)
