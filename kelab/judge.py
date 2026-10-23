"""judgment：完全匹配、算子覆盖度、LLM judge、人工抽检导出、ke2sql 执行比对接口。

- exact_match：归一化后字符串相等（不做大小写折叠，算子名大小写敏感）
- retrieval_coverage（N 实验 P0 检索覆盖度）：top-k 示例算子集 vs gold 算子集，Jaccard
- gen_coverage：生成 KE 算子集 vs gold 算子集，Jaccard（解析失败记 None，聚合按 0 并计数）
- llm_judge：严格 JSON 输出，坏 JSON 重试 1 次，再失败记 judge_error（不静默记 0）
- execute_compare：SQL 域 ke2sql 执行比对，接口预留（execution_enabled=false 时返回 None）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from nl2ke import clean_output

from .config import DomainConfig
from .llm import ClientConfig, chat_with_usage, LLMError


# ---------------------------------------------------------------- 归一化与确定性指标


def normalize_ke(text: str) -> str:
    """完全匹配的归一化口径：去 markdown 围栏 + 空白折叠为单空格。"""
    return " ".join(clean_output(text).split())


def exact_match(gen_ke: str, gold_ke: str) -> float:
    return 1.0 if normalize_ke(gen_ke) == normalize_ke(gold_ke) else 0.0


def jaccard(a: set[str] | list[str], b: set[str] | list[str]) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


# ---------------------------------------------------------------- LLM judge

JUDGE_SYSTEM = """你是 KE（Knowledge Equation，知识方程）语义等价性判分专家。

给定：一道自然语言题目、一条 gold KE（人工标注的金标准）、一条模型生成的 KE。
任务：判断生成 KE 与 gold KE 的语义等价程度。

判分要点：
1. 结构等价优先于字面一致：变量改名、约束书写顺序、等价写法不影响判定
   （例如 output 目标写 d 加 when_min(d) 约束 等价于 直接 min(d)；
   用 point_on_curve 组合表达 intersection 语义等价）。
2. 下列情况必须降级：目标表达式语义不同；缺失实质性约束（如漏掉关键的几何关系）；
   类型混用（如把 Expression 当 Number 参与相等）；多出改变语义的约束。
3. 仅缺辅助性声明（如 a > 0, b > 0 这类题面已给的辅助范围）算 minor，不算 different。
4. 生成 KE 无法解析语法时，先按内容尝试判断；完全无法理解才判 different。

verdict 三档：
- equivalent：语义完全等价（含等价写法）
- minor：语义基本正确，有轻微缺省或多余
- different：语义不同、错误或无法理解

只输出一行 JSON，不要输出任何其它内容：
{"verdict": "equivalent|minor|different", "rationale": "一句话理由"}"""

JUDGE_USER = """题目：
{question}

gold KE：
{gold_ke}

生成 KE：
{gen_ke}"""


@dataclass
class JudgeResult:
    verdict: str | None
    score: float | None
    rationale: str | None
    error: str | None = None
    prompt_tokens: int | None = None      # judge 调用自身的 token 消耗（成本核算）
    completion_tokens: int | None = None


def _parse_verdict(text: str, score_map: dict[str, float]) -> JudgeResult:
    cleaned = clean_output(text)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return JudgeResult(None, None, None, error=f"judge 输出非 JSON: {cleaned[:200]}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return JudgeResult(None, None, None, error=f"judge JSON 解析失败: {exc}")
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in score_map:
        return JudgeResult(None, None, None, error=f"未知 verdict: {verdict!r}")
    rationale = str(data.get("rationale", "")).strip()[:500]
    return JudgeResult(verdict, score_map[verdict], rationale)


def llm_judge(
    client: ClientConfig,
    domain: DomainConfig,
    question: str,
    gold_ke: str,
    gen_ke: str,
) -> JudgeResult:
    """LLM 判分；坏 JSON 重试 1 次，再失败返回 error（聚合时剔除并计数）。
    多次尝试的 token 消耗累加，供成本核算。
    judge prompt 优先用领域配置的 judge_system / judge_user_template，缺省用内置模板。"""
    system = domain.judge_system or JUDGE_SYSTEM
    user_template = domain.judge_user_template or JUDGE_USER
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": user_template.format(
                question=question, gold_ke=gold_ke, gen_ke=gen_ke
            ),
        },
    ]
    last: JudgeResult | None = None
    total_prompt = 0
    total_completion = 0
    for _ in range(2):
        try:
            result = chat_with_usage(client, messages)
        except LLMError as exc:
            err = JudgeResult(None, None, None, error=f"judge 调用失败: {exc}")
            err.prompt_tokens = total_prompt or None
            err.completion_tokens = total_completion or None
            return err
        total_prompt += result.prompt_tokens or 0
        total_completion += result.completion_tokens or 0
        parsed = _parse_verdict(result.content, domain.judge_score_map)
        parsed.prompt_tokens = total_prompt or None
        parsed.completion_tokens = total_completion or None
        if parsed.error is None:
            return parsed
        last = parsed
    return last or JudgeResult(None, None, None, error="judge 未知失败")


# ---------------------------------------------------------------- ke2sql 执行比对（接口预留）


def execute_compare(gen_ke: str, gold_ke: str, domain: DomainConfig) -> bool | None:
    """SQL 域执行比对。execution.enabled=false 时返回 None；具体接入属阶段 3。"""
    if not domain.execution_enabled:
        return None
    raise NotImplementedError("ke2sql 执行比对属阶段 3（SQL 域接入时实现）")
