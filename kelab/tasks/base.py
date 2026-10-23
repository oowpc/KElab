"""任务适配器接口：不同任务类型（NL→KE / finance QA / …）的校验与判分缝。

任务类型要分开：NL→KE 用 KE grammar/等价判分，finance QA 用 EM/F1/LLM judge；
runner/store 共用，judge/metric 走这个接口，不硬塞进 KE 逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..adapters.base import Item
    from ..config import DomainConfig
    from ..llm import ClientConfig
    from ..ontology import Ontology


@dataclass
class TaskValidation:
    """一条生成输出的任务级校验结果（runner 落 validations 表的来源）。"""
    parse_ok: bool
    parse_error: str | None = None
    checks_ok: bool = True
    issues: list[dict] = field(default_factory=list)  # [{"level","kind","message"}]
    operators: list[str] = field(default_factory=list)


class TaskAdapter(Protocol):
    """任务级校验 + 判分接口。judge 返回 dict，键即 store.insert_judgment 的字段。"""
    name: str
    primary_metric: str  # 报告主指标名（如「语义等价率」）

    def validate_output(
        self, text: str, domain: "DomainConfig", ontology: "Ontology"
    ) -> TaskValidation:
        ...

    def judge(
        self,
        *,
        client: "ClientConfig",
        domain: "DomainConfig",
        item: "Item",
        gen_text: str,
        retrieved_items: list["Item"],
        k: int,
        validation: TaskValidation,
    ) -> dict:
        ...
