"""预登记（协议 §7.5/7.6）：跑主实验前由决策人冻结 δ、质量门槛、抽检比例、判分口径。

防事后解释：预登记内容哈希 + 时间戳落盘；改动不覆盖旧版，历史在 prereg_history.jsonl。
报告引用 prereg_hash 以证明「先登记后看数」。
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
