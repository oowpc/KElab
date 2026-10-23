"""数据流水线（第一层内核，与领域无关）：split、题族、近重复、manifest、指纹。

datasets.py 提供底层函数（group_families/split_dataset3/dedupe_items），
本模块把它们组织成「prepare 一次跑完、产出可审计 manifest」的流水线：
- 每个分区（testset/dev/pool）的条数与内容指纹
- 题族划分结果（近重复/同模板题整族不跨分区，协议 §7.3）
- 逐条 item 指纹（冻结与变更追踪用）
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import datasets
from .adapters.base import DatasetAdapter, Item, default_fingerprint


def item_fingerprint(item: Item, adapter: DatasetAdapter | None = None) -> str:
    """单条内容指纹：优先用领域适配器算法（缺省 default_fingerprint）。"""
    return adapter.fingerprint(item) if adapter else default_fingerprint(item)


def partition_fingerprint(
    items: list[Item], adapter: DatasetAdapter | None = None
) -> str:
    """一个分区的内容指纹：逐条指纹排序后聚合（与顺序无关）。"""
    parts = sorted(item_fingerprint(it, adapter) for it in items)
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:12]


def assign_families(
    items: list[Item], threshold: float = 0.72
) -> dict[str, str]:
    """题族划分：近重复/同模板题归族。返回 item_id → family_id（f1/f2/…）。"""
    groups = datasets.group_families(items, threshold)
    mapping: dict[str, str] = {}
    for idx, group in enumerate(groups, 1):
        fid = f"f{idx}"
        for i in group:
            mapping[items[i].item_id] = fid
    return mapping


def build_manifest(
    partitions: dict[str, list[Item]],
    *,
    seed: int | None = None,
    family_threshold: float = 0.72,
    source: str | None = None,
    adapter: DatasetAdapter | None = None,
) -> dict:
    """汇总一次 prepare 的可审计清单。

    partitions 形如 {"testset": [...], "dev": [...], "pool": [...]}（没有的键省略）。
    题族在全体条目上划分后标注到各分区——跨分区同族意味着泄露风险，直接列出。
    指纹经领域适配器算法（adapter 缺省时用通用默认）。
    """
    all_items = [it for part in partitions.values() for it in part]
    family_of = assign_families(all_items, family_threshold) if all_items else {}

    section: dict[str, dict] = {}
    for name, part in partitions.items():
        fams: dict[str, list[str]] = {}
        for it in part:
            fams.setdefault(family_of[it.item_id], []).append(it.item_id)
        section[name] = {
            "count": len(part),
            "fingerprint": partition_fingerprint(part, adapter),
            "families": {fid: sorted(ids) for fid, ids in sorted(fams.items())},
            "items": {it.item_id: item_fingerprint(it, adapter)
                      for it in sorted(part, key=lambda x: x.item_id)},
        }

    # 跨分区题族：同族题落在不同分区 = 近重复泄露通道，必须显式列出
    fam_partitions: dict[str, set[str]] = {}
    for name, part in partitions.items():
        for it in part:
            fam_partitions.setdefault(family_of[it.item_id], set()).add(name)
    cross_family = sorted(fid for fid, parts in fam_partitions.items() if len(parts) > 1)

    return {
        "version": 1,
        "source": source,
        "seed": seed,
        "family_threshold": family_threshold,
        "total": len(all_items),
        "partitions": section,
        "family_count": len(set(family_of.values())),
        "cross_partition_families": cross_family,
    }


def write_manifest(manifest: dict, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
