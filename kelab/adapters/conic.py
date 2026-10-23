"""conic（圆锥曲线）域适配器：Conic10K/标注 xlsx → Item，gold KE 规则校验。"""
from __future__ import annotations

import json
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


class Conic10KAdapter(ConicAdapter):
    """Conic10K 全量原始/审校数据 → 候选 Item（120/600 扩题的入口）。

    支持三种源形态：
    - 审校记录目录（每文件一条：uuid/query.question/gold_ke.content/answer.gold_expr）
    - train/dev/test.json（list 或 {"data": [...]}，键名宽松探测 problem/question/text）
    - .jsonl（每行一条记录，同上探测）
    gold_ke 缺失 → 空串（待标注）；原始表达式/答案/标签进 metadata，不污染核心 schema。
    """
    name = "conic10k"

    def load(self, source: Path, col_map: dict[str, str] | None = None) -> list[Item]:
        source = Path(source)
        records: list[dict] = []
        if source.is_dir():
            for path in sorted(source.glob("*.json")):
                records.append(self._read_record(path))
        elif source.suffix == ".jsonl":
            with source.open("r", encoding="utf-8") as fh:
                records = [json.loads(line) for line in fh if line.strip()]
        elif source.suffix == ".json":
            raw = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw = raw.get("data") or raw.get("records") or [raw]
            records = list(raw)
        else:
            raise datasets.DataError(
                f"conic10k 适配器不支持的源：{source}（支持 记录目录/.json/.jsonl）"
            )
        if not records:
            raise datasets.DataError(f"{source} 未导入任何记录")
        return [self._record_to_item(rec, i) for i, rec in enumerate(records, 1)]

    def _read_record(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise datasets.DataError(f"记录文件 {path} 读取失败: {exc}") from exc

    def _record_to_item(self, raw: dict, idx: int) -> Item:
        query = raw.get("query") if isinstance(raw.get("query"), dict) else {}
        question = (query.get("question") or raw.get("question") or raw.get("problem")
                    or raw.get("text") or raw.get("problem_zh"))
        if not question:
            raise datasets.DataError(f"第 {idx} 条记录没有题干字段: {sorted(raw)[:10]}")
        gold = raw.get("gold_ke")
        if isinstance(gold, dict):
            gold = gold.get("content") or ""
        gold = gold or raw.get("ke") or raw.get("gold") or ""
        answer = raw.get("answer")
        if isinstance(answer, dict) and "gold_expr" in answer:
            expr = answer["gold_expr"]
            answer = expr.get("content") if isinstance(expr, dict) else str(expr)
        item_id = str(raw.get("uuid") or raw.get("id") or raw.get("upstream_id")
                      or f"conic10k-{idx:05d}")
        metadata = {
            "answer": answer or raw.get("expressions"),
            "tags": raw.get("tags") or [],
            "notes": raw.get("notes") or "",
            "verification": raw.get("verification") or {},
            "lang": query.get("lang") or raw.get("lang"),
        }
        return Item(
            item_id=item_id,
            question=str(question).strip(),
            gold_ke=datasets.normalize_ke_text(gold) if gold else "",
            source_id=raw.get("upstream_id") or raw.get("uuid"),
            dataset=raw.get("source") or "conic_10k",
            metadata={k: v for k, v in metadata.items() if v not in (None, "", [], {})},
        )
