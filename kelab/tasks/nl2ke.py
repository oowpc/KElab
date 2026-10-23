"""NL→KE 任务适配器：KE grammar 校验 + 语义等价判分（现平台全部判分口径）。

实现是对 ke_validate/judge 模块的委托——行为与硬编码路径逐字节一致，
只是把编排从 runner 移到这里，让 runner 只认 TaskAdapter 接口。
"""
from __future__ import annotations

from .. import judge, ke_validate
from ..adapters.base import Item
from ..config import DomainConfig
from ..llm import ClientConfig
from ..ontology import Ontology
from .base import TaskAdapter, TaskValidation


class NL2KETask(TaskAdapter):
    name = "nl2ke"
    primary_metric = "语义等价率"

    def validate_output(
        self, text: str, domain: DomainConfig, ontology: Ontology
    ) -> TaskValidation:
        result = ke_validate.validate_ke(text, domain.grammar_path, ontology)
        return TaskValidation(
            parse_ok=result.lark_ok,
            parse_error=result.lark_error,
            checks_ok=result.light_ok,
            issues=[i.to_dict() for i in result.issues],
            operators=result.operators,
        )

    def judge(
        self,
        *,
        client: ClientConfig,
        domain: DomainConfig,
        item: Item,
        gen_text: str,
        retrieved_items: list[Item],
        k: int,
        validation: TaskValidation,
    ) -> dict:
        gold_ops = set(item.gold_operators)
        retrieved_ops: set[str] = set()
        for ex in retrieved_items:
            retrieved_ops |= set(ex.gold_operators)
        gen_ops = set(validation.operators) if validation.parse_ok else None

        judged = judge.llm_judge(client, domain, item.question, item.gold_ke, gen_text)
        return {
            "exact_match": judge.exact_match(gen_text, item.gold_ke),
            "retrieval_coverage": judge.jaccard(retrieved_ops, gold_ops) if k > 0 else None,
            "gen_coverage": judge.jaccard(gen_ops, gold_ops) if gen_ops is not None else None,
            "llm_verdict": judged.verdict,
            "llm_score": judged.score,
            "llm_rationale": judged.rationale,
            "judge_model": domain.judge_model,
            "judge_error": judged.error,
            "judge_prompt_tokens": judged.prompt_tokens,
            "judge_completion_tokens": judged.completion_tokens,
        }
