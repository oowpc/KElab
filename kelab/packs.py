"""领域包加载：configs/domains/<name>/ 目录布局，兼容旧单文件 <name>.toml。

目录布局（新领域的推荐形态——新增领域 = 新目录 + 配置，不改 runner/store/report）：

    configs/domains/<name>/
        domain.toml      # [domain]/[generation]/[judge]/[pack] 节
        prompt.md        # prompt 模板（相对路径相对包目录解析）
        grammar.lark     # KE 语法
        ontology.json    # 可选（ontology_source="json" 时）

旧单文件布局（configs/domains/<name>.toml，内部路径相对工作目录）继续可用，
两种布局产出的 DomainConfig 字段值不因加载路径不同而改变（config_hash 稳定）。
"""
from __future__ import annotations

from pathlib import Path

from .adapters import get_adapter
from .adapters.base import DomainPack
from .config import ConfigError, DomainConfig, load_domain, verify_grammar_md5
from .ontology import load_ontology
from .tasks import get_task


def resolve_domain_path(ref: str | Path, root: Path | str = ".") -> Path:
    """把领域引用解析为实际路径：已存在的路径直接用；否则在 configs/domains/
    下先找 <name>/ 目录（含 domain.toml），再退回 <name>.toml。"""
    p = Path(ref)
    if p.exists():
        return p
    base = Path(root) / "configs" / "domains" / str(ref)
    if base.is_dir() and (base / "domain.toml").exists():
        return base
    toml = base.with_suffix(".toml")
    if toml.exists():
        return toml
    raise ConfigError(
        f"找不到领域 {ref!r}：既非路径，configs/domains/ 下也没有 "
        f"{ref}/domain.toml 或 {ref}.toml"
    )


def _rebase_paths(domain: DomainConfig, pack_dir: Path) -> None:
    """目录布局：相对路径一律按包目录解析（包自包含，可整体移动）。

    保持相对形式（不 resolve 成绝对路径），保证不同机器上 config_hash 一致。
    只在目录布局下调用；单文件布局维持工作目录相对，不动。
    """
    def rebased(value: Path | None) -> Path | None:
        if value is None or value.is_absolute():
            return value
        return pack_dir / value

    domain.grammar_path = rebased(domain.grammar_path)
    domain.prompt_template = rebased(domain.prompt_template)
    domain.ontology_json = rebased(domain.ontology_json)


def load_pack(ref: str | Path, root: Path | str = ".") -> DomainPack:
    """加载领域包：DomainConfig + prompt 模板 + 本体 + 数据适配器 + 任务适配器。"""
    path = resolve_domain_path(ref, root)
    pack_dir: Path | None = None
    if path.is_dir():
        pack_dir = path
        path = path / "domain.toml"
    domain = load_domain(path)
    if pack_dir is not None:
        _rebase_paths(domain, pack_dir)
    grammar_md5 = verify_grammar_md5(domain)
    try:
        template = Path(domain.prompt_template).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取 prompt 模板 {domain.prompt_template}: {exc}") from exc
    ontology = load_ontology(
        domain.ontology_source, prompt_text=template, json_path=domain.ontology_json
    )
    return DomainPack(
        domain=domain,
        template=template,
        ontology=ontology,
        grammar_md5=grammar_md5,
        adapter=get_adapter(domain.adapter),
        task=get_task(domain.task),
        pack_dir=pack_dir,
    )
