"""通用数据/实验内核的第一层：与领域无关的核心数据结构与适配器接口。

设计约束（架构评审结论）：
- 接口保持很小，不做大而全的插件框架
- Item 核心字段只有 item_id/question/gold_ke/metadata；领域特有字段
  （family_id、source、answer 等）一律进 metadata，不污染核心 schema
- gold_operators 是 NL→KE 任务级字段（平台当前唯一任务），保留为一等字段；
  非 KE 任务通过 task 适配器自行解释 metadata
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # 类型注解专用，避免循环 import
    from ..config import DomainConfig
    from ..ontology import Ontology
    from ..tasks.base import TaskAdapter


@dataclass
class Item:
    """一条题目 + gold 标注。metadata 承载领域特有字段，序列化随 jsonl 往返。"""
    item_id: str
    question: str
    gold_ke: str
    gold_operators: list[str] = field(default_factory=list)
    source_id: str | None = None
    dataset: str | None = None
    note: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Item":
        return cls(
            item_id=raw["item_id"],
            question=raw["question"],
            gold_ke=raw["gold_ke"],
            gold_operators=list(raw.get("gold_operators") or []),
            source_id=raw.get("source_id"),
            dataset=raw.get("dataset"),
            note=raw.get("note"),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass
class ValidationResult:
    """gold 校验结果（适配器口径：人读的错误/警告列表）。

    与 ke_validate.ValidationResult 不同：那个是 Lark 解析层的内部结构，
    这个是适配器对外的「这条 gold 能不能用」结论。
    """
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DatasetAdapter(Protocol):
    """领域数据适配器：把领域原始数据变成 Item，并校验 gold 可用性。

    新增领域 = 实现这个接口 + 一个 configs/domains/<name>/ 目录，
    不改 runner/store/report。
    """
    name: str

    def load(self, source: Path, col_map: dict[str, str] | None = None) -> list[Item]:
        """从领域原始源（xlsx/jsonl/…）加载 Item 列表。"""
        ...

    def validate_gold(self, item: Item, pack: "DomainPack") -> ValidationResult:
        """规则校验一条 gold（能自动化的部分；语义正确性仍靠人工复核）。"""
        ...

    def fingerprint(self, item: Item) -> str:
        """单条内容指纹（题面+标注归一化后的哈希），manifest/冻结用。"""
        ...


def default_fingerprint(item: Item) -> str:
    """适配器缺省指纹算法：归一化题干 + 归一化 gold。"""
    norm_q = "".join(item.question.split())
    norm_ke = " ".join(item.gold_ke.split())
    return hashlib.sha1(f"{norm_q}␟{norm_ke}".encode("utf-8")).hexdigest()[:12]


@dataclass
class DomainPack:
    """领域包：一个领域跑实验所需的全部资源，一次加载、随处传递。

    第一层内核（runner/store/report）只认 DomainPack，不关心领域细节。
    """
    domain: "DomainConfig"
    template: str
    ontology: "Ontology"
    grammar_md5: str
    adapter: DatasetAdapter
    task: "TaskAdapter"
    pack_dir: Path | None = None  # 目录布局时为 configs/domains/<name>/；单文件布局为 None
