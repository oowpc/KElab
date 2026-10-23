"""配置文件写出与备份：Web 看板在线编辑配置的落盘层。

tomllib 只读，这里实现与 config.py schema 匹配的 TOML 序列化：
- 保存前先把原文件备份到 archive/（时间戳 .bak）
- 写出后立即回读校验（load_domain/load_experiment round-trip），失败即报错不写坏配置
- 保留 TOML 中表单未覆盖的键（在原始 dict 上更新，不重建）
"""
from __future__ import annotations

import json
import shutil
import tomllib
from datetime import datetime
from pathlib import Path

from .config import DomainConfig, ExperimentConfig, load_domain, load_experiment


class SaveError(ValueError):
    pass


def toml_value(value) -> str:
    """Python 标量/列表 → TOML 字面量。字符串走 JSON 转义（与 TOML 基本串兼容）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, Path):
        value = str(value).replace("\\", "/")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise SaveError(f"不支持的 TOML 值类型: {type(value).__name__} ({value!r})")


def dump_toml(raw: dict, header: str = "") -> str:
    """嵌套 dict → TOML 文本（只支持 [section] 一层 + 标量/数组/内联表，与平台 schema 一致）。"""
    lines: list[str] = []
    if header:
        lines += [f"# {line}" for line in header.splitlines()] + [""]
    scalars = {k: v for k, v in raw.items() if not isinstance(v, dict)}
    sections = {k: v for k, v in raw.items() if isinstance(v, dict)}
    for key, value in scalars.items():
        lines.append(f"{key} = {toml_value(value)}")
    for name, table in sections.items():
        lines += ["", f"[{name}]"]
        for key, value in table.items():
            lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def backup_file(path: Path, archive_dir: Path) -> Path | None:
    """把现有文件复制到 archive_dir/<原名>.<时间戳（含微秒防同秒覆盖）>.bak。"""
    path = Path(path)
    if not path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = archive_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup)
    return backup


def save_config_toml(
    path: Path,
    updates: dict,
    archive_dir: Path,
    *,
    kind: str,
    header: str,
) -> None:
    """在现有 TOML 上应用 updates（{section: {key: value}} 或顶层 {key: value}），
    备份原文件后写出，并回读校验。kind: "domain" | "experiment"。"""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except tomllib.TOMLDecodeError as exc:
        raise SaveError(f"原配置 {path} 解析失败，拒绝覆盖: {exc}") from exc

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key].update(value)
        elif isinstance(value, dict):
            raw[key] = dict(value)
        else:
            raw[key] = value

    text = dump_toml(raw, header=header)
    backup_file(path, archive_dir)
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        if kind == "domain":
            load_domain(path)
        else:
            load_experiment(path)
    except Exception as exc:
        raise SaveError(f"配置已写出但回读校验失败: {exc}（原文件备份在 {archive_dir}）") from exc


# ---------------------------------------------------------------- 表单 ↔ 配置


def _parse_int_list(text: str, field: str) -> list[int]:
    try:
        return [int(x.strip()) for x in text.replace("，", ",").split(",") if x.strip()]
    except ValueError as exc:
        raise SaveError(f"字段 {field} 需要逗号分隔的整数，得到 {text!r}") from exc


def _parse_float(text: str, field: str) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise SaveError(f"字段 {field} 需要数字，得到 {text!r}") from exc


def domain_updates_from_form(form: dict) -> dict:
    """Web 表单 → domain TOML updates。form 值均为字符串（checkbox 缺席=False）。"""
    updates = {
        "domain": {
            "name": form["name"],
            "grammar": form["grammar"],
            "prompt_template": form["prompt_template"],
            "ontology_source": form.get("ontology_source", "prompt"),
            "question_format": form.get("question_format", ""),
        },
        "generation": {
            "model": form.get("gen_model", ""),
            "temperature": _parse_float(form.get("gen_temperature", "0"), "gen_temperature"),
            "max_concurrency": int(_parse_float(form.get("gen_max_concurrency", "4"), "gen_max_concurrency")),
            "max_retries": int(_parse_float(form.get("gen_max_retries", "3"), "gen_max_retries")),
            "timeout": _parse_float(form.get("gen_timeout", "120"), "gen_timeout"),
        },
        "judge": {
            "model": form.get("judge_model", ""),
            "review_sample_rate": _parse_float(form.get("review_sample_rate", "0.1"), "review_sample_rate"),
        },        "execution": {
            "enabled": form.get("execution_enabled") == "on",
            "ke2sql_cmd": form.get("ke2sql_cmd", ""),
        },
    }
    if form.get("judge_system", "").strip():
        updates["judge"]["system"] = form["judge_system"]
    if form.get("judge_user_template", "").strip():
        updates["judge"]["user_template"] = form["judge_user_template"]
    if form.get("onto_always_include", "").strip():
        updates["generation"]["onto_always_include"] = [
            x.strip() for x in form["onto_always_include"].replace("，", ",").split(",")
            if x.strip()
        ]
    if form.get("grammar_md5"):
        updates["domain"]["grammar_md5"] = form["grammar_md5"]
    if form.get("ontology_json"):
        updates["domain"]["ontology_json"] = form["ontology_json"]
    if not updates["domain"]["question_format"]:
        del updates["domain"]["question_format"]  # 留空 = 用默认
    # 价格（可选，[pricing] 节，供看板换算 ¥；不进 DomainConfig 以免影响配置哈希）
    pricing: dict[str, float] = {}
    for key in ("gen_price_per_mtok", "judge_price_per_mtok", "annotation_cost_per_item"):
        text = (form.get(key) or "").strip()
        if text:
            pricing[key] = _parse_float(text, key)
    if pricing:
        updates["pricing"] = pricing
    return updates


def experiment_updates_from_form(form: dict) -> dict:
    retrievers = form.get("retrievers", [])
    if isinstance(retrievers, str):
        retrievers = [retrievers]
    onto_modes = form.get("onto_modes", [])
    if isinstance(onto_modes, str):
        onto_modes = [onto_modes]
    updates = {
        "experiment": {
            "name": form["name"],
            "domain": form["domain"],
            "kind": form["kind"],
            "testset": form["testset"],
            "pool_strategy": form.get("pool_strategy", "loo"),
            "ks": _parse_int_list(form.get("ks", ""), "ks"),
            "ns": _parse_int_list(form.get("ns", ""), "ns"),
            "fixed_k": int(_parse_float(form.get("fixed_k", "3"), "fixed_k")),
            "retrievers": retrievers or ["bm25"],
            "nested": form.get("nested") == "on",
            "repeats": int(_parse_float(form.get("repeats", "3"), "repeats")),
            "seeds": _parse_int_list(form.get("seeds", "101,202,303"), "seeds"),
            "knee_threshold": _parse_float(form.get("knee_threshold", "0.05"), "knee_threshold"),
            "std_alert": _parse_float(form.get("std_alert", "0.03"), "std_alert"),
            "oracle_gap_threshold": _parse_float(
                form.get("oracle_gap_threshold", "0.05"), "oracle_gap_threshold"
            ),
        }
    }
    if onto_modes:
        updates["experiment"]["onto_modes"] = onto_modes
    if form.get("onto_linking", "").strip():
        updates["experiment"]["onto_linking"] = form["onto_linking"].strip()
    if form.get("gen_models", "").strip():
        updates["experiment"]["gen_models"] = [
            x.strip() for x in form["gen_models"].replace("，", ",").split(",") if x.strip()
        ]
    return updates


def load_raw_toml(path: Path) -> dict:
    if not Path(path).exists():
        raise SaveError(f"配置文件不存在: {path}")
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def config_for_edit(path: Path) -> tuple[dict, DomainConfig | ExperimentConfig]:
    """读原始 TOML + 解析后的配置对象，供表单回填。"""
    raw = load_raw_toml(path)
    if "experiment" in raw:
        return raw, load_experiment(path)
    return raw, load_domain(path)
