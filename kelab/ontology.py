"""本体解析：从 prompt 模板内嵌的 op 块（或 JSON）解析 concepts 与 operators。

conic 域的本体权威来源是 NL2KE_Prompt.md 内嵌的两个代码块（**concepts** / **operators**），
而不是 conic_onto.json（该 JSON 是 CamelCase 命名体系，与 gold KE 的 snake_case 不符）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


class OntologyError(ValueError):
    pass


# op x_coordinate(p: Point) -> Number
# op between(p1: Point, p2: Point, p3: Point)        # 无返回类型（谓词）
_OP_LINE = re.compile(
    r"^op\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>[^)]*)\)"
    r"\s*(?:->\s*(?P<ret>.+?))?\s*$"
)

_ARG = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?P<type>.+)$")


@dataclass(frozen=True)
class OpSig:
    name: str
    arg_types: tuple[str, ...]
    ret: str | None  # None 表示谓词（无返回值）

    @property
    def arity(self) -> int:
        return len(self.arg_types)


@dataclass
class Ontology:
    concepts: set[str] = field(default_factory=set)
    operators: dict[str, OpSig] = field(default_factory=dict)
    # 保序与原文（本体注入对照的裁剪渲染需要按原顺序/原文重建代码块）
    concepts_order: list[str] = field(default_factory=list)
    op_raw_lines: dict[str, str] = field(default_factory=dict)

    def is_concept(self, name: str) -> bool:
        return name in self.concepts

    def is_operator(self, name: str) -> bool:
        return name in self.operators

    def ordered_concepts(self) -> list[str]:
        if self.concepts_order:
            return [c for c in self.concepts_order if c in self.concepts]
        return sorted(self.concepts)

    def raw_line(self, name: str) -> str:
        """算子的原始定义行（无原文时按签名重建）。"""
        if name in self.op_raw_lines:
            return self.op_raw_lines[name]
        sig = self.operators[name]
        args = ", ".join(f"arg{i}: {t}" for i, t in enumerate(sig.arg_types, 1))
        line = f"op {name}({args})"
        return line + (f" -> {sig.ret}" if sig.ret else "")


def _parse_op_line(line: str) -> OpSig | None:
    match = _OP_LINE.match(line.strip())
    if not match:
        return None
    arg_types: list[str] = []
    args = match.group("args").strip()
    if args:
        for raw in args.split(","):
            raw = raw.strip()
            arg_match = _ARG.match(raw)
            if arg_match:
                arg_types.append(arg_match.group("type").strip())
            else:
                # 无类型标注的参数，原样保留（light check 不做类型推导）
                arg_types.append(raw)
    ret = match.group("ret")
    return OpSig(
        name=match.group("name"),
        arg_types=tuple(arg_types),
        ret=ret.strip() if ret else None,
    )


def _fenced_block_after(text: str, header: str) -> str:
    """提取 **header** 之后的第一个 ``` 代码块内容。"""
    pattern = re.compile(
        re.escape(header) + r"\s*```[^\n]*\n(?P<body>.*?)\n?```",
        flags=re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise OntologyError(f"prompt 模板中找不到 {header} 代码块")
    return match.group("body")


def parse_ontology_from_prompt(text: str) -> Ontology:
    concepts_block = _fenced_block_after(text, "**concepts**")
    concepts_order = [
        token.strip()
        for token in re.split(r"[,\s]+", concepts_block)
        if token.strip()
    ]
    concepts = set(concepts_order)
    operators_block = _fenced_block_after(text, "**operators**")
    operators: dict[str, OpSig] = {}
    raw_lines: dict[str, str] = {}
    for line in operators_block.splitlines():
        line = line.strip()
        if not line:
            continue
        sig = _parse_op_line(line)
        if sig is None:
            raise OntologyError(f"无法解析 operator 行: {line!r}")
        operators[sig.name] = sig
        raw_lines[sig.name] = line
    if not concepts or not operators:
        raise OntologyError(
            f"本体解析结果为空: concepts={len(concepts)}, operators={len(operators)}"
        )
    return Ontology(concepts=concepts, operators=operators,
                    concepts_order=concepts_order, op_raw_lines=raw_lines)


def parse_ontology_from_json(path: Path | str) -> Ontology:
    """解析 conic_onto.json 风格：{"concepts": [...], "operators": {"Name": "Sig(...) -> Ret：描述"}}。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    concepts_raw = raw.get("concepts", [])
    if concepts_raw and isinstance(concepts_raw[0], dict):
        concepts = {c.get("name", "") for c in concepts_raw}
    else:
        concepts = {str(c) for c in concepts_raw}
    concepts.discard("")
    operators: dict[str, OpSig] = {}
    for name, sig_text in raw.get("operators", {}).items():
        if isinstance(sig_text, dict):
            sig_text = sig_text.get("signature", "")
        # 复用 op 行解析：拼成 op 行格式，去掉中文描述部分
        line = f"op {name}{sig_text}".split("：")[0]
        sig = _parse_op_line(line)
        if sig is None:
            raise OntologyError(f"无法解析 JSON operator {name!r}: {sig_text!r}")
        operators[sig.name] = sig
    return Ontology(concepts=concepts, operators=operators)


def load_ontology(source: str, *, prompt_text: str = "", json_path: Path | None = None) -> Ontology:
    if source == "prompt":
        return parse_ontology_from_prompt(prompt_text)
    if source == "json":
        if not json_path:
            raise OntologyError("ontology_source=json 但缺少 json_path")
        return parse_ontology_from_json(json_path)
    raise OntologyError(f"未知 ontology_source: {source!r}")
