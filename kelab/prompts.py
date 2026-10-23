"""prompt 组装：模板三段切分 + few-shot 示例动态渲染 + 哈希冻结。

模板结构（以 NL2KE_Prompt.md 为准）：
    [角色 / 本体 / 输出要求]  ← head，恒定
    ## 示例                    ← 示例区，按 k 动态渲染，k=0 时整区去掉
    ## 待转换题目              ← 其后内容不进 system（user 消息由 question_format 生成）

冻结口径：同条件内 prompt 逐字一致；渲染全文落 generations 表，静态部分哈希入 conditions。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .datasets import Item

EXAMPLES_MARKER = "## 示例"
QUESTION_MARKER = "## 待转换题目"

RENDER_VERSION = "v1"  # 示例渲染版式版本，改动版式时必须升级（哈希随之变化）


class PromptError(ValueError):
    pass


@dataclass
class AssembledPrompt:
    system: str
    user: str
    prompt_hash: str  # 完整 prompt（system+user）的 sha256 前 12 位
    static_hash: str  # head + question_format 的哈希（条件级，标识模板版本）


def split_template(template: str) -> str:
    """返回模板的 head 部分（示例区之前的恒定内容）。"""
    idx = template.find(EXAMPLES_MARKER)
    if idx == -1:
        # 模板没有示例区：退化为 QUESTION_MARKER 之前
        idx = template.find(QUESTION_MARKER)
    if idx == -1:
        raise PromptError(
            f"prompt 模板缺少 {EXAMPLES_MARKER!r} 或 {QUESTION_MARKER!r} 标记，无法切分"
        )
    return template[:idx].rstrip()


def render_example(index: int, item: Item) -> str:
    """逐字复刻原写死示例的版式：### 示例 i / 输入：... / 输出：``` KE ```"""
    return (
        f"### 示例 {index}\n\n"
        f"输入：{item.question}\n\n"
        f"输出：\n\n"
        f"```\n{item.gold_ke}\n```"
    )


def render_examples_block(examples: list[Item]) -> str:
    return "\n\n".join(render_example(i, item) for i, item in enumerate(examples, 1))


def assemble(
    template: str,
    question_format: str,
    examples: list[Item],
    question: str,
) -> AssembledPrompt:
    head = split_template(template)
    if examples:
        system = f"{head}\n\n{EXAMPLES_MARKER}\n\n{render_examples_block(examples)}"
    else:
        system = head
    user = question_format.format(question=question.strip())
    prompt_hash = hashlib.sha256(
        (RENDER_VERSION + "\x00" + system + "\x00" + user).encode("utf-8")
    ).hexdigest()[:12]
    static_hash = hashlib.sha256(
        (RENDER_VERSION + "\x00" + head + "\x00" + question_format).encode("utf-8")
    ).hexdigest()[:12]
    return AssembledPrompt(
        system=system, user=user, prompt_hash=prompt_hash, static_hash=static_hash
    )


def to_messages(prompt: AssembledPrompt) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt.system},
        {"role": "user", "content": prompt.user},
    ]


# ---------------------------------------------------------------- 本体注入对照：相关概念裁剪

_TYPE_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def concepts_closure(operators: list[str], ontology) -> list[str]:
    """选中算子的签名里出现的概念集合（类型闭包），按本体原顺序输出。"""
    used: set[str] = set()
    for name in operators:
        sig = ontology.operators.get(name)
        if sig is None:
            continue
        for type_text in list(sig.arg_types) + ([sig.ret] if sig.ret else []):
            used |= set(_TYPE_TOKEN.findall(type_text))
    return [c for c in ontology.ordered_concepts() if c in used]


def build_linked_template(template: str, ontology, operators_subset: list[str]) -> str:
    """本体注入对照的裁剪模板：**concepts** / **operators** 代码块只保留
    operators_subset（+ 其签名类型闭包），模板的其余部分逐字不动。

    渲染版式与原块一致（逗号分隔概念表、每行一条 op 原文），
    保证「全量 vs 裁剪」之间唯一差异是本体内容。"""
    subset = [name for name in ontology.operators if name in set(operators_subset)]
    # 保持原顺序：按 op_raw_lines 的插入顺序（operators dict 保序）
    known = [name for name in ontology.op_raw_lines if name in set(operators_subset)] or subset
    concepts = concepts_closure(known, ontology)

    concepts_text = ", ".join(concepts)
    operators_text = "\n".join(ontology.raw_line(name) for name in known)

    def replace_block(text: str, header: str, new_body: str) -> str:
        pattern = re.compile(
            "(" + re.escape(header) + r"\s*```[^\n]*\n).*?(\n?```)",
            flags=re.DOTALL,
        )
        new_text, count = pattern.subn(rf"\g<1>{new_body}\g<2>", text, count=1)
        if count != 1:
            raise PromptError(f"模板中 {header} 代码块替换失败（找不到或不止一个）")
        return new_text

    out = replace_block(template, "**concepts**", concepts_text)
    out = replace_block(out, "**operators**", operators_text)
    return out
