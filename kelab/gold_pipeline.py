"""gold 流水线（协议前置：200 条/域真数据标注的入库通道）。

候选生成 → 规则校验 → 人工复核 → 冻结 manifest。

通用化的是流水线本身，不是每个域的判定逻辑：
- 规则校验由领域适配器提供（能自动化的部分：语法/闭词表/arity/绑定）
- 语义正确性无法自动化通用——人工复核环节是强制关卡，不是可选装饰
- 冻结产出 frozen.jsonl + _gold_manifest.json（逐条指纹 + 版本坐标），
  之后的 prepare 只认冻结产物，保证 gold 版本可追踪
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import datasets
from .adapters.base import DatasetAdapter, DomainPack, Item, default_fingerprint

REVIEW_FILE = "_review.jsonl"
FROZEN_FILE = "frozen.jsonl"
MANIFEST_FILE = "_gold_manifest.json"
QUEUE_META_FILE = "_queue.json"  # 队列元信息（domain 等），web 冻结时自动带上


@dataclass
class GoldCandidate:
    item: Item
    auto_ok: bool
    errors: list[str]
    warnings: list[str]


def import_candidates(
    source: Path | str, adapter: DatasetAdapter, col_map: dict | None = None
) -> list[Item]:
    """候选生成：从领域原始源导入（xlsx/jsonl，格式由适配器解释）。"""
    return adapter.load(Path(source), col_map=col_map)


def validate_candidates(items: list[Item], pack: DomainPack) -> list[GoldCandidate]:
    """规则校验：每条 gold 过领域适配器（语法层失败的一律不得冻结）。"""
    out: list[GoldCandidate] = []
    for item in items:
        result = pack.adapter.validate_gold(item, pack)
        out.append(GoldCandidate(item, result.ok, result.errors, result.warnings))
    return out


def export_review(candidates: list[GoldCandidate], out_dir: Path | str,
                  domain: str | None = None) -> Path:
    """导出人工复核队列：复核人在 human_verdict 填 pass/fail，可直接改 gold_ke。

    每行保留完整 Item（item 键）——source_id/dataset/note/metadata 全程不丢，
    冻结后的 gold 能追回源数据；顶层 gold_ke 是可编辑镜像（复核人改这里）。
    domain 写入 _queue.json sidecar（web 冻结时自动选领域包）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if domain:
        (out_dir / QUEUE_META_FILE).write_text(
            json.dumps({"domain": domain}, ensure_ascii=False) + "\n", encoding="utf-8")
    path = out_dir / REVIEW_FILE
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for cand in candidates:
            fh.write(json.dumps({
                "item": cand.item.to_dict(),
                "item_id": cand.item.item_id,
                "question": cand.item.question,
                "gold_ke": cand.item.gold_ke,   # 可编辑镜像：复核/标注改这里
                "auto_ok": cand.auto_ok,
                "auto_errors": cand.errors,
                "auto_warnings": cand.warnings,
                "human_verdict": "",   # 复核人填 pass / fail
                "human_note": "",
            }, ensure_ascii=False) + "\n")
    return path


def row_to_item(row: dict) -> Item:
    """复核行 → Item：完整 item 键优先（溯源保留），旧版平铺行兜底兼容；
    顶层 gold_ke（复核人可编辑）始终覆盖 item 内值。"""
    if isinstance(row.get("item"), dict):
        item = Item.from_dict(row["item"])
    else:  # 旧格式：只有 item_id/question/gold_ke 三个字段
        item = Item(item_id=row["item_id"], question=row["question"], gold_ke="")
    edited = row.get("gold_ke")
    if edited is not None and str(edited).strip():
        item.gold_ke = datasets.normalize_ke_text(str(edited))
    return item


def queue_meta(out_dir: Path | str) -> dict:
    """读队列 sidecar（_queue.json）；没有返回 {}。"""
    path = Path(out_dir) / QUEUE_META_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_review(path: Path | str) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise datasets.DataError(f"{path} 第 {line_no} 行解析失败: {exc}") from exc
    if not rows:
        raise datasets.DataError(f"复核文件 {path} 为空")
    return rows


def freeze(
    review_path: Path | str,
    pack: DomainPack,
    out_dir: Path | str,
    *,
    actor: str,
    allow_unreviewed: bool = False,
) -> Path:
    """冻结：复核通过 + 规则校验通过的条目写入 frozen.jsonl + 冻结 manifest。

    硬规则（不满足则拒绝冻结，exit 由调用方处理）：
    - 规则校验（适配器）失败的条目永远不得冻结——人工也无法覆盖语法层错误，
      复核人可以直接在复核文件里改 gold_ke 文本再标 pass
    - 默认要求每条都有明确人工结论（pass/fail）；--allow-unreviewed 只放过
      规则校验已通过且未填结论的条目（会在 manifest 里标记）
    """
    rows = load_review(review_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frozen: list[Item] = []
    blockers: list[str] = []
    pending: list[str] = []
    rejected = 0
    for row in rows:
        item = row_to_item(row)  # 完整溯源（source_id/metadata 随 item 键保留）
        result = pack.adapter.validate_gold(item, pack)
        verdict = str(row.get("human_verdict") or "").strip().lower()
        if not result.ok:
            blockers.append(f"{item.item_id}: {'; '.join(result.errors)}")
            continue
        if verdict == "fail":
            rejected += 1
            continue
        if verdict == "" and not allow_unreviewed:
            pending.append(item.item_id)
            continue
        # 冻结：补提算子（task 适配器口径），复核信息入 metadata
        task_val = pack.task.validate_output(item.gold_ke, pack.domain, pack.ontology)
        item.gold_operators = task_val.operators
        item.metadata["gold_review"] = {
            "verdict": verdict or "unreviewed",
            "note": str(row.get("human_note") or ""),
        }
        frozen.append(item)

    problems: list[str] = []
    if blockers:
        problems.append(
            f"{len(blockers)} 条规则校验未过（改正 gold 或标 fail 后再冻结）：\n  "
            + "\n  ".join(blockers[:20])
            + ("\n  …" if len(blockers) > 20 else "")
        )
    if pending:
        problems.append(
            f"{len(pending)} 条缺人工结论（如确需跳过用 --allow-unreviewed）："
            + ", ".join(pending[:20])
            + (", …" if len(pending) > 20 else "")
        )
    if problems:
        raise datasets.DataError("冻结被拒绝：\n" + "\n".join(problems))
    if not frozen:
        raise datasets.DataError("没有任何条目通过冻结（frozen 为空）")

    frozen.sort(key=lambda it: it.item_id)
    datasets.write_jsonl(frozen, out_dir / FROZEN_FILE)
    manifest = {
        "version": 1,
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor,
        "allow_unreviewed": allow_unreviewed,
        "counts": {"in": len(rows), "frozen": len(frozen), "rejected": rejected},
        "fingerprints": {it.item_id: default_fingerprint(it) for it in frozen},
        "grammar_md5": pack.grammar_md5,
        "adapter": pack.adapter.name,
        "task": pack.task.name,
    }
    manifest_path = out_dir / MANIFEST_FILE
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_dir / FROZEN_FILE
