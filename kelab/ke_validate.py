"""KE validation 三层：lark 语法解析、算子提取、light 静态检查。

light 检查口径（冒烟期 P1"类型检查通过率"的替代，报告需注明）：
  a) 算子闭词表：所有 op 调用名 ∈ ontology.operators
  b) arity：调用实参数 == 签名形参数
  c) 变量绑定：裸名字引用必须 ∈ 绑定变量 ∪ concepts 常量 ∪ 算子名
  d) 有且仅有一个 output
完整类型检查（vendored kelang query_type_checker 改造）属阶段 3。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from lark import Lark, Tree
from lark.exceptions import LarkError

from .ontology import Ontology


@dataclass
class Issue:
    level: str  # "error" | "warning"
    kind: str
    message: str

    def to_dict(self) -> dict:
        return {"level": self.level, "kind": self.kind, "message": self.message}


@dataclass
class ValidationResult:
    lark_ok: bool
    lark_error: str | None = None
    operators: list[str] = field(default_factory=list)  # 排序后的算子名
    issues: list[Issue] = field(default_factory=list)

    @property
    def light_ok(self) -> bool:
        return self.lark_ok and not any(i.level == "error" for i in self.issues)


@lru_cache(maxsize=4)
def _parser(grammar_path: str, mtime: float) -> Lark:
    # mtime 仅作缓存 key，文件变更后重建 parser
    text = Path(grammar_path).read_text(encoding="utf-8")
    return Lark(text, parser="lalr", start="start")


_parse_lock = threading.Lock()


def get_parser(grammar_path: Path | str) -> Lark:
    path = Path(grammar_path)
    return _parser(str(path.resolve()), path.stat().st_mtime)


def parse_ke(text: str, grammar_path: Path | str) -> tuple[Tree | None, str | None]:
    """解析 KE 文本，返回 (tree, error)。任何解析失败都不抛异常。"""
    parser = get_parser(grammar_path)
    try:
        with _parse_lock:  # lark 解析在部分版本非线程安全，加锁保险
            tree = parser.parse(text)
        return tree, None
    except (LarkError, ValueError) as exc:
        return None, str(exc).splitlines()[0][:500]


def _is_name_ref(node) -> bool:
    return isinstance(node, Tree) and node.data == "name_ref"


def _name_of(name_ref: Tree) -> str:
    return str(name_ref.children[0])


def _suffix_nodes(postfix: Tree) -> list[Tree]:
    """postfix 的后缀节点列表。grammar 中 postfix_suffix 非内联规则，需剥一层。"""
    out: list[Tree] = []
    for child in postfix.children[1:]:
        if isinstance(child, Tree) and child.data == "postfix_suffix" and child.children:
            out.append(child.children[0])
        elif isinstance(child, Tree):
            out.append(child)
    return out


def _op_calls(tree: Tree) -> list[tuple[str, int]]:
    """收集所有算子调用：postfix 主名 + 实参个数（只统计 op_call_suffix，不含 Set[...] 这类 replacement）。

    postfix: primary postfix_suffix*，? 内联规则使 postfix 节点只在有后缀时出现。
    """
    calls: list[tuple[str, int]] = []
    for postfix in tree.find_data("postfix"):
        primary = postfix.children[0]
        if not _is_name_ref(primary):
            continue
        for suffix in _suffix_nodes(postfix):
            if suffix.data == "op_call_suffix":
                argc = 0
                if suffix.children:
                    tuple_body = suffix.children[0]
                    argc = len(tuple_body.children)
                calls.append((_name_of(primary), argc))
    return calls


def extract_operators(tree: Tree) -> list[str]:
    """从解析树提取算子名集合（排序）。sympy_expression 的字符串参数是 literal，不会被误提取。"""
    return sorted({name for name, _ in _op_calls(tree)})


def _bound_names(tree: Tree) -> set[str]:
    """收集绑定的名字（全局作用域近似，冒烟口径）：
    a) membership_individual（X: Concept）左侧裸名字；
    b) assertion_individual（X = 表达式）左侧裸名字——赋值/定义式绑定，
       是 KE 的自然写法（如 F = get_a_focus(Right, C)、E = (1/2)*length(...)）。
    """
    bound: set[str] = set()
    for node in tree.find_data("membership_individual"):
        left = node.children[0]
        if _is_name_ref(left):
            bound.add(_name_of(left))
    for node in tree.find_data("assertion_individual"):
        left = node.children[0]
        if _is_name_ref(left):
            bound.add(_name_of(left))
    return bound


def _operator_position_names(tree: Tree) -> set[int]:
    """处于 postfix 主名位置（算子/构造子位置）的 name_ref 节点 id 集合。"""
    ids: set[int] = set()
    for postfix in tree.find_data("postfix"):
        if postfix.children and _is_name_ref(postfix.children[0]):
            ids.add(id(postfix.children[0]))
    return ids


def light_check(tree: Tree, ontology: Ontology) -> list[Issue]:
    issues: list[Issue] = []

    # (a)(b) 算子闭词表 + arity
    for name, argc in _op_calls(tree):
        sig = ontology.operators.get(name)
        if sig is None:
            issues.append(Issue("error", "unknown_operator", f"未知算子 {name!r}（本体闭词表外）"))
        elif argc != sig.arity:
            issues.append(
                Issue("error", "arity_mismatch", f"算子 {name!r} 实参 {argc} 个，签名要求 {sig.arity} 个")
            )

    # (c) 变量绑定
    bound = _bound_names(tree)
    op_position = _operator_position_names(tree)
    allowed = bound | ontology.concepts | set(ontology.operators)
    for name_ref in tree.find_data("name_ref"):
        if id(name_ref) in op_position:
            continue  # 算子位置已在 (a) 检查
        name = _name_of(name_ref)
        if name not in allowed:
            issues.append(Issue("error", "unbound_name", f"未绑定的名字 {name!r}"))

    # replacement 构造子（Set[Point] 等）位置的名字：concepts/算子表外的给 warning
    for postfix in tree.find_data("postfix"):
        primary = postfix.children[0]
        if not _is_name_ref(primary):
            continue
        suffixes = _suffix_nodes(postfix)
        has_replacement = any(s.data == "replacement_suffix" for s in suffixes)
        has_call = any(s.data == "op_call_suffix" for s in suffixes)
        if has_replacement and not has_call:
            name = _name_of(primary)
            if name not in ontology.concepts and name not in ontology.operators:
                issues.append(Issue("warning", "unknown_constructor", f"未知类型构造子 {name!r}[...]"))

    # (d) 恰好一个 output
    n_outputs = sum(1 for _ in tree.find_data("output_individual"))
    if n_outputs != 1:
        issues.append(Issue("error", "output_count", f"output 个数为 {n_outputs}，要求恰好 1 个"))

    return issues


def validate_ke(text: str, grammar_path: Path | str, ontology: Ontology) -> ValidationResult:
    """一条 KE 的完整 validation：解析 → 算子提取 → light 检查。"""
    tree, error = parse_ke(text, grammar_path)
    if tree is None:
        return ValidationResult(lark_ok=False, lark_error=error)
    operators = extract_operators(tree)
    issues = light_check(tree, ontology)
    return ValidationResult(lark_ok=True, operators=operators, issues=issues)
