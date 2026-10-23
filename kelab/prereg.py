"""预登记（协议 §7.5/7.6）：跑主实验前由决策人冻结 δ、质量门槛、抽检比例、判分口径。

防事后解释：预登记内容哈希 + 时间戳落盘；改动不覆盖旧版，历史在 prereg_history.jsonl。
报告引用 prereg_hash 以证明「先登记后看数」。
check_consistency 做两层校验：
- consistency：预登记的实验配置 vs 当前实验配置/数据指纹（改配置 = 该重登记）
- timing：run 开始时间 vs 预登记时间（先看数后登记 = 预登记失效）
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def prereg_dir(root: Path) -> Path:
    return Path(root) / "configs" / "prereg"


def prereg_path(root: Path, exp_name: str) -> Path:
    return prereg_dir(root) / f"{exp_name}.json"


def load_prereg(root: Path, exp_name: str) -> dict | None:
    path = prereg_path(root, exp_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def create_prereg(
    root: Path,
    exp,
    *,
    dataset_md5: str,
    delta: float,
    quality_gate: float | None,
    review_rate: float,
    scoring_protocol: str,
    actor: str,
    reason: str = "",
) -> dict:
    """创建/更新预登记。改动保留历史（version 自增 + history jsonl）。"""
    existing = load_prereg(root, exp.name)
    payload = {
        "experiment": exp.name,
        "kind": exp.kind,
        "config": {
            "ks": exp.ks, "ns": exp.ns, "fixed_k": exp.fixed_k,
            "retrievers": exp.retrievers, "repeats": exp.repeats,
            "seeds": exp.seeds[: exp.repeats], "pool_strategy": exp.pool_strategy,
            "testset": str(exp.testset), "dataset_md5": dataset_md5,
        },
        "delta": delta,
        "quality_gate": quality_gate,
        "review_rate": review_rate,
        "scoring_protocol": scoring_protocol,
        "actor": actor,
        "reason": reason,
        "version": (existing["version"] + 1) if existing else 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["prereg_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    prereg_dir(root).mkdir(parents=True, exist_ok=True)
    prereg_path(root, exp.name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history = prereg_dir(root) / "prereg_history.jsonl"
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def load_history(root: Path, exp_name: str) -> list[dict]:
    """同一实验的预登记历史（含当前版），按版本升序。"""
    path = prereg_dir(root) / "prereg_history.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("experiment") == exp_name:
                rows.append(row)
    rows.sort(key=lambda r: r.get("version", 0))
    return rows


# 预登记与实验配置必须一致的字段（不一致 = 配置漂移，应重新登记）
_CONSISTENCY_FIELDS = (
    "ks", "ns", "fixed_k", "retrievers", "repeats", "seeds",
    "pool_strategy", "testset", "dataset_md5",
)

# 时序判定看「核心冻结参数」：这些在旧版里与当前版一致，旧版的登记时间即有效。
_CORE_FIELDS = ("delta", "quality_gate", "review_rate", "scoring_protocol")
_CORE_CONFIG_FIELDS = ("ks", "ns", "fixed_k", "retrievers", "repeats", "seeds",
                       "pool_strategy", "testset")


def _same_core(a: dict, b: dict) -> bool:
    if any(a.get(f) != b.get(f) for f in _CORE_FIELDS):
        return False
    ca, cb = a.get("config", {}), b.get("config", {})
    return all(ca.get(f) == cb.get(f) for f in _CORE_CONFIG_FIELDS)


def _dataset_vouched(
    old_md5: str | None, dataset_md5_now: str | None, dataset_file_md5_now: str | None
) -> bool:
    """旧版登记的数据指纹 vs 当前数据，两种算法都可对质：
    - 新算法（12 位 sha256 指纹）：与当前指纹相等
    - 旧算法（32 位文件 md5）：与当前测试集原始 md5 相等
    对不上（或当前证据缺失）→ 不能为旧版的数据版本背书。
    """
    if not old_md5:
        return False
    return old_md5 in {x for x in (dataset_md5_now, dataset_file_md5_now) if x}


def check_consistency(
    prereg: dict,
    exp,
    *,
    dataset_md5_now: str | None = None,
    dataset_file_md5_now: str | None = None,
    run_started_at: str | None = None,
    history: list[dict] | None = None,
) -> list[str]:
    """预登记一致性 + 时序校验。返回警告列表（空 = 全部一致）。

    consistency：预登记冻结的实验配置 vs 当前实验配置——改了 k 列表/检索器/
    重复数/数据集而没重登记，结论就不能引用预登记。
    timing：run 开始时间早于预登记时间 = 先看数后登记，预登记证明力失效。
      例外：history 里存在核心冻结参数一致、且数据版本可对质（新指纹或旧
      文件 md5 算法均认可）的更早版本，登记早于 run 开始——时序仍有效
      （当前版只是修订/修复），降级为「时序说明」并引用该版哈希与理由。
    """
    warnings: list[str] = []
    frozen = prereg.get("config", {})
    current = {
        "ks": exp.ks, "ns": exp.ns, "fixed_k": exp.fixed_k,
        "retrievers": exp.retrievers, "repeats": exp.repeats,
        "seeds": exp.seeds[: exp.repeats], "pool_strategy": exp.pool_strategy,
        "testset": str(exp.testset),
        "dataset_md5": dataset_md5_now,
    }
    for field_name in _CONSISTENCY_FIELDS:
        if field_name == "dataset_md5" and dataset_md5_now is None:
            continue  # 数据文件不在手边时跳过（调用方说明原因）
        old, new = frozen.get(field_name), current.get(field_name)
        if old != new:
            warnings.append(
                f"预登记与当前配置不一致：{field_name} 登记值 {old!r} ≠ 当前 {new!r}"
                "（改配置后应重新登记，否则结论不能引用预登记）"
            )
    started = _parse_ts(run_started_at)
    created = _parse_ts(prereg.get("created_at"))
    if started and created and started < created:
        earlier_valid = [
            row for row in (history or [])
            if row.get("version") != prereg.get("version")
            and _same_core(row, prereg)
            and _dataset_vouched(
                row.get("config", {}).get("dataset_md5"),
                dataset_md5_now, dataset_file_md5_now)
            and (_parse_ts(row.get("created_at")) or started) <= started
        ]
        if earlier_valid:
            first = earlier_valid[0]
            reason = f"；修订理由：{prereg.get('reason')}" if prereg.get("reason") else ""
            warnings.append(
                f"时序说明：当前版 v{prereg.get('version')} 登记于 run 开始之后，"
                f"但 v{first.get('version')}（{first.get('created_at')}，哈希 "
                f"{first.get('prereg_hash', '—')}）登记早于 run 开始（{run_started_at}），"
                f"核心冻结参数与数据指纹均可对质一致——时序有效，当前版为后续修订{reason}"
            )
        else:
            warnings.append(
                f"时序违规：run 开始于 {run_started_at}，早于预登记时间 "
                f"{prereg.get('created_at')}——先看数后登记，预登记证明力失效"
            )
    return warnings


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
