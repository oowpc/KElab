"""conic（圆锥曲线）域适配器：Conic10K/标注 xlsx → Item，gold KE 规则校验。"""
from __future__ import annotations

from pathlib import Path

from .. import datasets, ke_validate
from .base import DatasetAdapter, DomainPack, Item, ValidationResult, default_fingerprint


class ConicAdapter(DatasetAdapter):
    name = "conic"

    def load(self, source: Path, col_map: dict[str, str] | None = None) -> list[Item]:
        source = Path(source)
        if source.suffix == ".jsonl":
            return datasets.read_jsonl(source)
        if source.suffix in (".xlsx", ".xlsm"):
            return datasets.import_xlsx(source, col_map=col_map)
        raise datasets.DataError(
            f"conic 适配器不支持的数据源格式：{source}（支持 .xlsx/.jsonl）"
        )

    def validate_gold(self, item: Item, pack: DomainPack) -> ValidationResult:
        """gold KE 规则校验：Lark 解析 + light 检查（闭词表/arity/绑定/单 output）。"""
        result = ke_validate.validate_ke(item.gold_ke, pack.domain.grammar_path, pack.ontology)
        errors: list[str] = []
        warnings: list[str] = []
        if not result.lark_ok:
            errors.append(f"lark 解析失败: {result.lark_error}")
        for issue in result.issues:
            target = errors if issue.level == "error" else warnings
            target.append(f"{issue.kind}: {issue.message}")
        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)

    def fingerprint(self, item: Item) -> str:
        return default_fingerprint(item)
