"""数据集与候选池：xlsx 导入、JSONL 读写、LOO 池、嵌套池、泄露检查。

泄露零容忍是方案硬性要求：prepare 阶段 fail-fast，命中即视为测试失败。
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import openpyxl

from . import ke_validate
from .ontology import Ontology


class DataError(ValueError):
    pass


@dataclass
class Item:
    item_id: str
    question: str
    gold_ke: str
    gold_operators: list[str] = field(default_factory=list)
    source_id: str | None = None
    dataset: str | None = None
    note: str | None = None

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
        )


# testset20.xlsx 默认列映射（可通过参数覆盖以适配其它表）
DEFAULT_COL_MAP = {
    "item_id": "题目",
    "question": "题干",
    "gold_ke": "李天音-KE",
    "source_id": "原始ID",
    "dataset": "所属数据集",
    "note": "备注",
}


def count_lines(path: Path | str) -> int:
    """统计 jsonl 非空行数（with 块确保句柄关闭）。"""
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def normalize_ke_text(text: str) -> str:
    """gold KE 空白归一化（多行带缩进 → 单行单空格）。"""
    return " ".join(str(text).split())


def import_xlsx(
    xlsx_path: Path | str,
    col_map: dict[str, str] | None = None,
    sheet: str | None = None,
) -> list[Item]:
    """从标注 xlsx 导入题目与 gold KE。"""
    col_map = col_map or DEFAULT_COL_MAP
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    except OSError as exc:
        raise DataError(f"无法打开 xlsx: {xlsx_path}: {exc}") from exc
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        raise DataError(f"{xlsx_path} 首个工作表为空")

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    index = {name: i for i, name in enumerate(header)}
    # 只有必需列缺失才报错；可选列（source_id/dataset/note）缺失按 None 处理
    required = ["item_id", "question", "gold_ke"]
    missing = [col_map[f] for f in required if col_map.get(f) and col_map[f] not in index]
    if missing:
        raise DataError(
            f"xlsx 缺少必需列: {missing}；实际列: {header}。"
            "可用 --col 字段=列名 或领域配置 [xlsx] 节调整列映射"
        )

    items: list[Item] = []
    for row in rows[1:]:
        def cell(field: str) -> str | None:
            col = col_map.get(field)
            if not col or col not in index:
                return None
            value = row[index[col]]
            return str(value).strip() if value is not None else None

        item_id = cell("item_id")
        question = cell("question")
        gold_ke = cell("gold_ke")
        if not any([item_id, question, gold_ke]):
            continue  # 整行空白
        if not item_id or not question or not gold_ke:
            raise DataError(f"行数据不完整（item_id/question/gold_ke 必填）: {row!r}")
        items.append(
            Item(
                item_id=item_id,
                question=question,
                gold_ke=normalize_ke_text(gold_ke),
                source_id=cell("source_id"),
                dataset=cell("dataset"),
                note=cell("note"),
            )
        )
    if not items:
        raise DataError(f"{xlsx_path} 未导入任何题目")
    return items


def attach_gold_operators(
    items: list[Item], grammar_path: Path | str, ontology: Ontology
) -> None:
    """prepare 阶段为每条 gold KE 预提取算子；gold 必须合法，解析失败 fail-fast。"""
    failures: list[str] = []
    for item in items:
        result = ke_validate.validate_ke(item.gold_ke, grammar_path, ontology)
        if not result.lark_ok:
            failures.append(f"{item.item_id}: {result.lark_error}")
            continue
        item.gold_operators = result.operators
        errors = [i.message for i in result.issues if i.level == "error"]
        if errors:
            failures.append(f"{item.item_id}: gold KE light 检查未过: {errors}")
    if failures:
        raise DataError(
            "gold KE 校验失败（gold 必须全部合法，否则实验口径不成立）：\n"
            + "\n".join(failures)
        )


def write_jsonl(items: list[Item], path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for item in items:
            fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")


def read_jsonl(path: Path | str) -> list[Item]:
    items: list[Item] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(Item.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise DataError(f"{path} 第 {line_no} 行解析失败: {exc}") from exc
    if not items:
        raise DataError(f"{path} 为空")
    return items


def loo_pools(items: list[Item]) -> dict[str, list[Item]]:
    """leave-one-out：每题的池 = 其余全部题目（冒烟期用，真实数据用 fixed 池）。"""
    return {it.item_id: [x for x in items if x.item_id != it.item_id] for it in items}


def dedupe_items(items: list[Item]) -> tuple[list[Item], dict]:
    """去重（方案 §5.2：题干 + KE 结构双查）。

    - 题干归一化完全相同 → 去重（保留首条，记录被去条目）
    - KE 归一化相同但题干不同 → 保留但列入报告（同构题，供人工决定是否剔除）
    返回 (保留条目, 报告)。
    """
    kept: list[Item] = []
    seen_q: dict[str, str] = {}
    dropped: list[dict] = []
    for it in items:
        nq = _norm_question(it.question)
        if nq in seen_q:
            dropped.append({"kept": seen_q[nq], "dropped": it.item_id})
            continue
        seen_q[nq] = it.item_id
        kept.append(it)
    seen_ke: dict[str, list[str]] = {}
    for it in kept:
        nke = normalize_ke_text(it.gold_ke)
        seen_ke.setdefault(nke, []).append(it.item_id)
    same_ke = [ids for ids in seen_ke.values() if len(ids) > 1]
    return kept, {
        "in": len(items),
        "out": len(kept),
        "dropped_question_dups": dropped,
        "same_ke_groups": same_ke,
    }


def group_families(items: list[Item], threshold: float = 0.72) -> list[list[int]]:
    """按题干归一化相似度把近重复/同模板题聚成族（并查集）。
    协议 §7.3：同族题必须落在同一分区（池/开发集/测试集不跨族泄露）。"""
    import difflib

    norms = [_norm_question(it.question) for it in items]
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if abs(len(norms[i]) - len(norms[j])) / max(len(norms[i]), len(norms[j]), 1) > 0.5:
                continue  # 长度差太大直接跳过（省 O(n²) 里的明显负对）
            if difflib.SequenceMatcher(None, norms[i], norms[j]).quick_ratio() >= threshold \
                    and difflib.SequenceMatcher(None, norms[i], norms[j]).ratio() >= threshold:
                union(i, j)
    families: dict[int, list[int]] = {}
    for i in range(len(items)):
        families.setdefault(find(i), []).append(i)
    return list(families.values())


def split_dataset3(
    items: list[Item],
    test_size: int,
    dev_size: int = 0,
    seed: int = 42,
    group_threshold: float | None = 0.72,
) -> tuple[list[Item], list[Item], list[Item]]:
    """三分数据集（协议 §7.3）：(测试集, 开发集, 候选池)。
    开发集用于流程调试与判分器校准，不计入主结果。
    group_threshold 非 None 时按题族整族切分（同族不跨分区）。

    返回 (testset, dev, pool)，各自按 item_id 排序；同 seed 同结果。"""
    n = len(items)
    if test_size < 1:
        raise DataError(f"test_size={test_size} 非法（至少 1）")
    if test_size + dev_size >= n:
        raise DataError(
            f"test_size+dev_size={test_size + dev_size} 必须小于总量 {n}（池不能为空）")
    if group_threshold is None:
        order = list(items)
        random.Random(seed).shuffle(order)
        testset = order[:test_size]
        dev = order[test_size:test_size + dev_size]
        pool = order[test_size + dev_size:]
    else:
        families = [[items[i] for i in fam]
                    for fam in group_families(items, group_threshold)]
        random.Random(seed).shuffle(families)
        testset, dev, pool = [], [], []
        for fam in families:  # 整族依次填入：先测试集满 → 开发集满 → 其余进池
            if len(testset) + len(fam) <= test_size:
                testset.extend(fam)
            elif len(dev) + len(fam) <= dev_size:
                dev.extend(fam)
            else:
                pool.extend(fam)
        if len(testset) < test_size or len(dev) < dev_size:
            # 族粒度导致某分区装不满：协议不允许拆族——大声警告（CI/用户必须看到），
            # 由人决定调整规模或关掉分组
            import sys as _sys
            print(
                f"⚠️ 题族整族切分装不满：请求 test={test_size}/dev={dev_size}，"
                f"实际 test={len(testset)}/dev={len(dev)}。"
                "近重复大族占据了分区名额；请调整规模、换种子或 group_threshold=None 关闭分组。",
                file=_sys.stderr,
            )
    key = lambda it: it.item_id
    return (sorted(testset, key=key), sorted(dev, key=key), sorted(pool, key=key))


def split_dataset(
    items: list[Item], test_size: int, seed: int = 42
) -> tuple[list[Item], list[Item]]:
    """二分兼容接口（旧调用）→ 委托 split_dataset3，dev 为空。"""
    testset, _dev, pool = split_dataset3(items, test_size, dev_size=0, seed=seed,
                                         group_threshold=None)
    return testset, pool


def nested_prefix(
    pool: list[Item], n: int, seed: int, item_id: str
) -> list[Item]:
    """嵌套池前缀：同一 (seed, item_id) 下 shuffle 固定，N 增大只加不换（5⊂10⊂19）。"""
    if n > len(pool):
        raise DataError(f"池不足：N={n} 超过池规模 {len(pool)}（item={item_id}）")
    order = list(pool)
    random.Random(f"{seed}:{item_id}").shuffle(order)
    return order[:n]


def pool_subset(
    pool: list[Item], n: int, seed: int, item_id: str, nested: bool = True
) -> list[Item]:
    """N 档池截取。nested=True：嵌套前缀（只增不减，A 因子主实验）；
    nested=False：每档独立随机重采样（B 对照：区分「量」的效果与「具体样本」的运气，
    档间无包含关系）。"""
    if nested:
        return nested_prefix(pool, n, seed, item_id)
    if n > len(pool):
        raise DataError(f"池不足：N={n} 超过池规模 {len(pool)}（item={item_id}）")
    return random.Random(f"{seed}:{item_id}:n{n}").sample(pool, n)


def _norm_question(question: str) -> str:
    return re.sub(r"\s+", "", question)


def check_leakage(
    testset: list[Item],
    pools: dict[str, list[Item]] | list[Item],
    strategy: str,
) -> dict:
    """泄露检查。返回报告 dict；命中时 report["hits"] 非空，调用方必须 fail-fast。"""
    hits: list[dict] = []
    test_ids = {it.item_id for it in testset}
    if len(test_ids) != len(testset):
        hits.append({"kind": "duplicate_test_id", "detail": "测试集 item_id 有重复"})
    test_questions = {_norm_question(it.question): it.item_id for it in testset}

    if strategy == "loo":
        assert isinstance(pools, dict)
        for item_id, pool in pools.items():
            for entry in pool:
                if entry.item_id == item_id:
                    hits.append(
                        {"kind": "self_in_pool", "detail": f"LOO 池 {item_id} 含题目自身"}
                    )
    else:
        assert isinstance(pools, list)
        pool_ids = {p.item_id for p in pools}
        if len(pool_ids) != len(pools):
            hits.append({"kind": "duplicate_pool_id", "detail": "池 item_id 有重复"})
        for overlap in sorted(test_ids & pool_ids):
            hits.append({"kind": "id_overlap", "detail": f"item_id 同时出现在测试集与池: {overlap}"})
        for entry in pools:
            norm = _norm_question(entry.question)
            if norm in test_questions:
                hits.append(
                    {
                        "kind": "question_overlap",
                        "detail": f"池条目 {entry.item_id} 题干与测试题 {test_questions[norm]} 完全相同",
                    }
                )
    return {
        "strategy": strategy,
        "testset_size": len(testset),
        "hits": hits,
        "passed": not hits,
    }


def save_leakage_report(report: dict, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_pools(
    strategy: str,
    testset: list[Item],
    pool_path: Path | str | None = None,
) -> dict[str, list[Item]] | list[Item]:
    """按策略加载池：loo 从测试集派生；fixed 读独立 pool.jsonl。"""
    if strategy == "loo":
        return loo_pools(testset)
    if not pool_path:
        raise DataError("pool_strategy=fixed 但未提供 pool 路径")
    return read_jsonl(pool_path)
