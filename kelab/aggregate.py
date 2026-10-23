"""聚合：从 SQLite 出每条件 × 指标的均值±std（3 次重复），边际增益与拐点检测。

口径：
- 指标均值 = 先按重复求题均值（rep mean），再对 3 个 rep mean 求均值；std 同理按 rep mean。
  （方案"同档位 3 次重跑标准差 ≤3pp"指重复级 std，不是题目级。）
- status=error 的生成不计入指标分母，单独计数；judge_error 不计入 semantic 分母，单独计数。
- gen_coverage 解析失败记 None 的，聚合按 0 计（由 runner 落库时保证 None 只出现在解析失败）。
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .config import ExperimentConfig
from .store import Store

METRICS = [
    "semantic",
    "lark",
    "light",
    "exact",
    "retrieval_coverage",
    "gen_coverage",
    "prompt_tokens",
    "completion_tokens",
    "latency_ms",
]


@dataclass
class MetricStat:
    mean: float | None
    std: float
    alert: bool = False  # std 超过方案阈值（默认 3pp）
    n: int = 0           # 计入的 rep 数


@dataclass
class ConditionMetrics:
    condition_id: str
    kind: str
    k: int | None
    n: int | None
    retriever: str
    onto: str = "full"  # full=全量本体 / linked=相关概念裁剪
    gen_model: str | None = None  # None = 领域默认模型
    stats: dict[str, MetricStat] = field(default_factory=dict)
    n_expected: int = 0
    n_ok: int = 0
    n_gen_error: int = 0
    n_judge_error: int = 0
    # 语义等价率的 95% CI（逐题得分 t 区间，协议 §7.4）
    semantic_ci: tuple[float | None, float | None, float | None] = (None, None, None)


def _row_metric(row, metric: str) -> float | None:
    if metric == "semantic":
        return row["llm_score"]
    if metric == "lark":
        return _as_float(row["lark_ok"])
    if metric == "light":
        return _as_float(row["light_ok"])
    if metric == "exact":
        return row["exact_match"]
    if metric in {"retrieval_coverage", "gen_coverage"}:
        return row[metric]
    if metric in {"prompt_tokens", "completion_tokens", "latency_ms"}:
        return _as_float(row[metric])
    raise KeyError(metric)


def _as_float(value) -> float | None:
    return float(value) if value is not None else None


def aggregate_run(
    store: Store, run_id: str, exp: ExperimentConfig
) -> list[ConditionMetrics]:
    out: list[ConditionMetrics] = []
    for cond in store.condition_rows(run_id):
        rows = store.generation_join_rows(run_id, cond["condition_id"])
        cm = ConditionMetrics(
            condition_id=cond["condition_id"],
            kind=cond["kind"],
            k=cond["k"],
            n=cond["n"],
            retriever=cond["retriever"],
            onto=cond["onto"] if "onto" in cond.keys() and cond["onto"] else "full",
            gen_model=cond["gen_model"] if "gen_model" in cond.keys() else None,
        )
        ok_rows = [r for r in rows if r["status"] == "ok"]
        cm.n_expected = len(rows)
        cm.n_ok = len(ok_rows)
        cm.n_gen_error = sum(1 for r in rows if r["status"] == "error")
        cm.n_judge_error = sum(1 for r in ok_rows if r["judge_error"])

        for metric in METRICS:
            rep_means: list[float] = []
            for rep in range(exp.repeats):
                values = []
                for r in ok_rows:
                    if r["rep"] != rep:
                        continue
                    if metric == "gen_coverage":
                        # 解析失败 → None 按 0 计
                        values.append(r["gen_coverage"] if r["gen_coverage"] is not None else 0.0)
                        continue
                    value = _row_metric(r, metric)
                    if value is not None:
                        values.append(value)
                if values:
                    rep_means.append(statistics.fmean(values))
            if not rep_means:
                cm.stats[metric] = MetricStat(mean=None, std=0.0)
                continue
            std = statistics.stdev(rep_means) if len(rep_means) >= 2 else 0.0
            cm.stats[metric] = MetricStat(
                mean=statistics.fmean(rep_means),
                std=std,
                alert=std > exp.std_alert and metric not in {"prompt_tokens", "completion_tokens", "latency_ms"},
                n=len(rep_means),
            )
        # 语义等价率的逐题 95% CI（协议 §7.4「按题目计算」：
        # 先求每题跨重复均值，再对题目集合求 t 区间——不把「题×重复」池化当独立样本）
        from .stats import mean_ci
        by_item: dict[str, list[float]] = {}
        for r in ok_rows:
            if r["llm_score"] is not None:
                by_item.setdefault(r["item_id"], []).append(r["llm_score"])
        item_means = [statistics.fmean(v) for v in by_item.values()]
        # 比例指标：t 区间截断到 [0,1]（对称外推会越界，如 [78.5%, 101.5%]）
        cm.semantic_ci = mean_ci(item_means, clamp=(0.0, 1.0))
        out.append(cm)
    return out


def write_metrics_csv(metrics: list[ConditionMetrics], path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["condition_id", "kind", "retriever", "k", "n", "onto", "gen_model",
            "n_expected", "n_ok", "n_gen_error", "n_judge_error"]
    for m in METRICS:
        cols += [f"{m}_mean", f"{m}_std", f"{m}_alert"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(cols)
        for cm in metrics:
            row = [cm.condition_id, cm.kind, cm.retriever, cm.k, cm.n, cm.onto,
                   cm.gen_model or "",
                   cm.n_expected, cm.n_ok, cm.n_gen_error, cm.n_judge_error]
            for m in METRICS:
                stat = cm.stats.get(m, MetricStat(None, 0.0))
                row += [
                    f"{stat.mean:.4f}" if stat.mean is not None else "",
                    f"{stat.std:.4f}",
                    int(stat.alert),
                ]
            writer.writerow(row)


def marginal_gains(xs: list[float], means: list[float]) -> list[tuple[float, float]]:
    """相邻档位的边际增益 Δ(x) = y(x) − y(上一档)。xs 需已按档排序。"""
    return [(xs[i], means[i] - means[i - 1]) for i in range(1, len(xs))]


def detect_knee(
    xs: list[float], means: list[float], threshold: float = 0.05
) -> float | None:
    """拐点 = 边际增益首次低于 threshold 的档位（到达该档已不划算，性价比停在前一档）。

    返回 None 表示全程增益 ≥ threshold（未出现拐点）。
    """
    for x, gain in marginal_gains(xs, means):
        if gain < threshold:
            return x
    return None


def per_rep_condition_means(
    store: Store, run_id: str, retriever: str | None = None, metric: str = "semantic"
) -> dict[str, dict[int, float]]:
    """每条件 × 每次重复的题均值：{condition_id: {rep: mean}}。
    重采样一致性检验与重采样散点图共用。llm_score 为 None 的行不计入。"""
    out: dict[str, dict[int, float]] = {}
    for cond in store.condition_rows(run_id):
        if retriever is not None and cond["retriever"] != retriever:
            continue
        by_rep: dict[int, list[float]] = {}
        for r in store.generation_join_rows(run_id, cond["condition_id"]):
            if r["status"] != "ok" or r["rep"] is None:
                continue
            value = _row_metric(r, metric)
            if value is not None:
                by_rep.setdefault(r["rep"], []).append(value)
        out[cond["condition_id"]] = {
            rep: statistics.fmean(vals) for rep, vals in by_rep.items() if vals
        }
    return out


def per_seed_knee(
    store: Store, run_id: str, exp: ExperimentConfig, retriever: str
) -> dict[int, float | None]:
    """每个 seed（重复）单独算拐点——重采样 N*/k* 一致性检验（验收：相差 ≤1 档）。
    返回 {seed: knee or None（该 seed 无拐点 / 数据不足）}。"""
    from .stats import knee_two_gains
    by_condition = per_rep_condition_means(store, run_id, retriever=retriever)
    conds = {c["condition_id"]: c for c in store.condition_rows(run_id)
             if c["retriever"] == retriever}
    axis_of = (lambda cid: conds[cid]["k"]) if exp.kind == "k" else (lambda cid: conds[cid]["n"])
    out: dict[int, float | None] = {}
    for rep, seed in enumerate(exp.seeds[: exp.repeats]):
        xs = sorted(axis_of(cid) for cid in by_condition if rep in by_condition[cid])
        means = [by_condition[cid][rep] for cid in
                 sorted(by_condition, key=axis_of) if rep in by_condition[cid]]
        if len(xs) < 2 or len(xs) != len(means):
            out[seed] = None
            continue
        out[seed] = knee_two_gains([float(x) for x in xs], means, exp.knee_threshold)
    return out


def knee_consistency(
    knees: dict[int, float | None], axis_values: list[float]
) -> tuple[str, str]:
    """重采样一致性判定（方案验收：两次独立重采样 N* 相差 ≤1 档）。
    返回 (三态判定, 证据文字)。"""
    present = sorted(k for k in knees.values() if k is not None)
    if len(present) < 2:
        return "数据不足", f"仅 {len(present)} 个 seed 出现拐点（≥2 个才能比对）"
    order = {v: i for i, v in enumerate(sorted(set(axis_values)))}
    idxs = [order[k] for k in present]
    spread = max(idxs) - min(idxs)
    verdict = "一致" if spread <= 1 else "不一致"
    return verdict, (f"各 seed 拐点 {present}，档位最大相差 {spread} 档（验收线 ≤1 档）")


def mde_estimate(n_items: int, anchor_n: int = 100, anchor_mde: float = 0.05) -> float | None:
    """统计功效估算：以方案锚点（anchor_n 题 ≈ anchor_mde，方案 §5.3 待精确计算）
    按 1/√n 换算当前题量的最小可检出效应（MDE）。小于 MDE 的差异一律判「数据不足」。"""
    if n_items <= 0:
        return None
    return anchor_mde * (anchor_n / n_items) ** 0.5
