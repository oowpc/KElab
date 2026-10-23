"""报告：matplotlib 图 + markdown 结论（含拐点、稳定性、H1–H4 三态、成本换算、抽检导出）。

视觉规范（dataviz skill 参考实例，已通过调色板校验）：
- 浅色图表面 #fcfcfb，网格发线 #e1e0d9，基线 #c3c2b7，主墨色 #0b0b0b，次级 #52514e，弱化 #898781
- 分类色固定顺序：random=#2a78d6 / bm25=#eb6834 / oracle=#1baf7a / dense=#eda100 / hybrid=#e87ba4
  （前三个槽位在 all-pairs 下通过 CVD 与正常视力下限；oracle 额外用虚线做二级编码）
- 线宽 2、标记 ≥6pt、≥2 系列必有图例、阈值线灰色虚线、文本一律墨色不用系列色
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .aggregate import (
    ConditionMetrics,
    aggregate_run,
    detect_knee,
    marginal_gains,
    mde_estimate,
    write_metrics_csv,
)
from .stats import knee_two_gains
from .config import DomainConfig, ExperimentConfig
from .datasets import Item
from .store import Store

SURFACE = "#fcfcfb"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"

SERIES_ORDER = ["random", "bm25", "frozen", "oracle", "dense", "hybrid"]
SERIES_COLOR = {
    "random": "#2a78d6",
    "bm25": "#eb6834",
    "frozen": "#6250d6",
    "oracle": "#1baf7a",
    "dense": "#eda100",
    "hybrid": "#e87ba4",
}
SERIES_LABEL = {
    "random": "random",
    "bm25": "BM25",
    "frozen": "frozen（冻结序列）",
    "oracle": "oracle（上界）",
    "dense": "dense",
    "hybrid": "hybrid",
}


def _setup_fonts() -> str:
    """中文字体优先 Microsoft YaHei；找不到时回退英文标签。返回实际可用的语言。"""
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return "zh"
    plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    return "en"


def _style_ax(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=12, pad=10)
    ax.set_xlabel(xlabel, color=INK2)
    ax.set_ylabel(ylabel, color=INK2)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
    ax.tick_params(colors=MUTED)


def _categorical_x(values: list[float | int]) -> tuple[list[int], list[str]]:
    """档位等距排布（N 跨数量级时优于线性轴），返回位置与刻度标签。"""
    return list(range(len(values))), [str(v) for v in values]


def _series_for(metrics: list[ConditionMetrics], kind: str) -> dict[str, list[ConditionMetrics]]:
    series: dict[str, list[ConditionMetrics]] = {}
    for cm in metrics:
        if cm.kind != kind or cm.onto != "full":
            continue  # 曲线只画全量本体；linked 在「本体注入对照」段单列
        series.setdefault(cm.retriever, []).append(cm)
    key = (lambda cm: cm.k) if kind == "k" else (lambda cm: cm.n)
    for cms in series.values():
        cms.sort(key=key)  # type: ignore[arg-type]
    return series


def _mean_std(cm: ConditionMetrics, metric: str) -> tuple[float | None, float]:
    stat = cm.stats.get(metric)
    if stat is None or stat.mean is None:
        return None, 0.0
    return stat.mean, stat.std


# ---------------------------------------------------------------- 图表


def _line_with_optional_errors(ax, xs, ys, errs, color, label, linestyle="-"):
    """std 全 0（如 repeats=1）时画干净折线；有 std 才用误差棒（零长度帽子太丑）。"""
    if any(e is not None and e > 1e-9 for e in errs):
        ax.errorbar(xs, ys, yerr=errs, fmt=linestyle + "o", color=color,
                    linewidth=2, markersize=6, capsize=4, elinewidth=1, label=label)
    else:
        ax.plot(xs, ys, linestyle + "o", color=color, linewidth=2.2, markersize=7,
                label=label)


def chart_quality(
    metrics: list[ConditionMetrics], kind: str, path: Path, knee: float | None
) -> bool:
    """主图（结论折线图）：语义等价率实线 + 完全匹配率虚线，按检索器分系列。
    repeats=1 时为纯折线（无退化误差棒）；y 轴百分比；拐点画竖参考线；
    首末点选择性标注（绝不每点都标）。"""
    from matplotlib.ticker import FuncFormatter

    series = _series_for(metrics, kind)
    if not series:
        return False
    fig, ax = plt.subplots(figsize=(7.2, 4.6), facecolor=SURFACE)
    xlabel = "few-shot 示例数 k" if kind == "k" else "候选池规模 N"
    key = (lambda cm: cm.k) if kind == "k" else (lambda cm: cm.n)
    multi = len(series) > 1
    for retriever in SERIES_ORDER:
        cms = series.get(retriever)
        if not cms:
            continue
        xs_raw = [key(cm) for cm in cms]
        xs, labels = _categorical_x(xs_raw)
        ys, errs = [], []
        for cm in cms:
            mean, std = _mean_std(cm, "semantic")
            ys.append(mean if mean is not None else float("nan"))
            errs.append(std)
        color = SERIES_COLOR[retriever]
        linestyle = "--" if retriever == "oracle" else "-"
        label = (f"{SERIES_LABEL[retriever]} 语义等价率" if multi else "语义等价率")
        _line_with_optional_errors(ax, xs, ys, errs, color, label, linestyle)
        exact_ys = []
        for cm in cms:
            mean, _ = _mean_std(cm, "exact")
            exact_ys.append(mean if mean is not None else float("nan"))
        ax.plot(xs, exact_ys, linestyle=":", color=color, linewidth=1.4,
                marker=".", markersize=5,
                label=f"{SERIES_LABEL[retriever]} 完全匹配" if multi else "完全匹配率")
        ax.set_xticks(xs, labels)
        # 选择性直接标注：首点、末点
        for i in (0, len(xs) - 1):
            if ys[i] == ys[i]:  # 非 NaN
                ax.annotate(f"{ys[i]:.0%}", (xs[i], ys[i]), textcoords="offset points",
                            xytext=(0, 9), ha="center", fontsize=9, color=INK2)
    if knee is not None:
        xs_all = sorted({key(cm) for cms in series.values() for cm in cms})
        if knee in xs_all:
            ki = xs_all.index(knee)
            ax.axvline(ki, color=MUTED, linestyle="--", linewidth=1)
            ax.annotate(f"{'k' if kind == 'k' else 'N'}* = {knee:g}",
                        xy=(ki, 1.04), xytext=(0, 0), textcoords="offset points",
                        ha="center", color=INK2, fontsize=10)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_ylim(-0.05, 1.12)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    _style_ax(ax,
              ("k" if kind == "k" else "N") + "–质量曲线：语义等价率与完全匹配率",
              xlabel, "比例")
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def chart_marginal(metrics: list[ConditionMetrics], kind: str, threshold: float, path: Path) -> bool:
    """边际增益柱状图：Δ(x)=y(x)−y(上一档)，阈值参考线。"""
    series = _series_for(metrics, kind)
    if not series:
        return False
    fig, ax = plt.subplots(figsize=(7.2, 4.0), facecolor=SURFACE)
    key = (lambda cm: cm.k) if kind == "k" else (lambda cm: cm.n)
    xs_all = sorted({key(cm) for cms in series.values() for cm in cms})
    n_series = len(series)
    width = 0.8 / max(n_series, 1)
    xlabel = "k" if kind == "k" else "N"
    for si, retriever in enumerate(SERIES_ORDER):
        cms = series.get(retriever)
        if not cms:
            continue
        xs_raw = [key(cm) for cm in cms]
        means = []
        for cm in cms:
            mean, _ = _mean_std(cm, "semantic")
            means.append(mean if mean is not None else float("nan"))
        gains = dict(marginal_gains(xs_raw, means))
        positions = [xs_all.index(x) + (si - (n_series - 1) / 2) * width for x in xs_all[1:]]
        heights = [gains.get(x, float("nan")) for x in xs_all[1:]]
        ax.bar(positions, heights, width=width * 0.92, color=SERIES_COLOR[retriever],
               label=SERIES_LABEL[retriever] if n_series > 1 else None)
    ax.axhline(threshold, color=MUTED, linestyle="--", linewidth=1)
    ax.text(0.05, threshold + 0.006, f"拐点阈值 {threshold:.0%}", color=INK2,
            fontsize=9, va="bottom", ha="left")
    ax.axhline(0, color=BASELINE, linewidth=1)
    ax.set_xticks(range(len(xs_all)), [str(x) for x in xs_all])
    _style_ax(ax, "边际增益 Δ = y(本档) − y(上一档)", xlabel, "Δ 语义等价率")
    if n_series > 1:
        ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def chart_cost_effect(metrics: list[ConditionMetrics], path: Path) -> bool:
    """成本–效果散点：x=单条平均 prompt tokens，y=语义等价率，点标条件名。"""
    points = []
    for cm in metrics:
        sem, _ = _mean_std(cm, "semantic")
        tok, _ = _mean_std(cm, "prompt_tokens")
        if sem is not None and tok is not None:
            points.append((tok, sem, cm))
    if not points:
        return False
    fig, ax = plt.subplots(figsize=(7.2, 4.4), facecolor=SURFACE)
    for tok, sem, cm in points:
        color = SERIES_COLOR.get(cm.retriever, MUTED)
        ax.scatter(tok, sem, s=64, color=color, zorder=3)
        label = f"k{cm.k}" if cm.kind == "k" else f"N{cm.n}"
        if len({p[2].retriever for p in points}) > 1:
            label = f"{cm.retriever}-{label}"
        ax.annotate(label, (tok, sem), textcoords="offset points", xytext=(6, 4),
                    fontsize=8, color=INK2)
    _style_ax(ax, "成本–效果（每点一个条件）", "平均 prompt tokens / 条", "语义等价率")
    ax.set_ylim(-0.05, 1.1)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def chart_model_comparison(
    metrics: list[ConditionMetrics], exp: ExperimentConfig, path: Path
) -> bool:
    """模型对照图（k 方案 C / n 方案 I 配套图）：每个检索器一个小图，
    小模型/大模型双线（误差棒=3 重复 std）。看 k/N 收益形态是否随模型能力变化。"""
    models = list(exp.gen_models or [])
    if len(models) < 2:
        return False
    _setup_fonts()
    cms_all = [cm for cm in metrics if cm.kind == exp.kind and cm.onto == "full"]
    retrievers = [r for r in SERIES_ORDER if r in {cm.retriever for cm in cms_all}]
    if not retrievers:
        return False
    # 模型色：前两个模型用分类槽位 1/2（蓝/橙），更多模型顺延（对照一般就两个）
    extra = ["#1baf7a", "#eda100"]
    model_colors = {m: c for m, c in zip(models, ["#2a78d6", "#eb6834"] + extra)}

    fig, axes = plt.subplots(1, len(retrievers), figsize=(4.4 * len(retrievers), 3.9),
                             facecolor=SURFACE, sharey=True, squeeze=False)
    key = (lambda cm: cm.k) if exp.kind == "k" else (lambda cm: cm.n)
    for ax, retriever in zip(axes.flat, retrievers):
        xs_all = sorted({key(cm) for cm in cms_all if cm.retriever == retriever})
        xs, labels = _categorical_x(xs_all)
        for model in models:
            cms = sorted([cm for cm in cms_all
                          if cm.retriever == retriever and cm.gen_model == model], key=key)
            if not cms:
                continue
            means = {key(cm): _mean_std(cm, "semantic") for cm in cms}
            pts = [(xs[xs_all.index(x)], means[x][0], means[x][1])
                   for x in [key(cm) for cm in cms] if means[x][0] is not None]
            if not pts:
                continue
            _line_with_optional_errors(ax, [p[0] for p in pts], [p[1] for p in pts],
                                       [p[2] for p in pts], model_colors[model], model)
        ax.set_xticks(xs, labels, fontsize=8)
        _style_ax(ax, SERIES_LABEL.get(retriever, retriever),
                  "k" if exp.kind == "k" else "N", "")
        ax.set_ylim(-0.05, 1.1)
    for ax in list(axes.flat)[len(retrievers):]:
        ax.axis("off")
    handles, legend_labels = axes.flat[0].get_legend_handles_labels()
    if legend_labels:
        fig.legend(handles, legend_labels, frameon=False, fontsize=9, loc="upper right")
    fig.suptitle("模型对照（judge 固定）", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def chart_runs_overlay(groups: list[dict], path: Path) -> bool:
    """汇总对比叠加图：同 (kind, retriever) 的多个 run 把语义曲线叠在一张图。

    groups: [{"title": str, "kind": "k"|"n",
              "lines": [{"label": str, "xs": [...], "ys": [...], "errs": [...]}]}]
    每个 group 一个小图（≤3 条/run 线保证可读；分类槽位 1-3 固定顺序）。
    """
    _setup_fonts()
    groups = [g for g in groups if g["lines"]]
    if not groups:
        return False
    colors = ["#2a78d6", "#eb6834", "#1baf7a"]
    fig, axes = plt.subplots(1, len(groups), figsize=(4.4 * len(groups), 3.9),
                             facecolor=SURFACE, sharey=True, squeeze=False)
    for ax, group in zip(axes.flat, groups):
        xs_all = sorted({x for line in group["lines"] for x in line["xs"]})
        xs, labels = _categorical_x(xs_all)
        for i, line in enumerate(group["lines"][:3]):  # 每图至多 3 条
            means = dict(zip(line["xs"], zip(line["ys"], line["errs"])))
            pts = [(xs[xs_all.index(x)], means[x][0], means[x][1])
                   for x in line["xs"] if x in means and means[x][0] is not None
                   and means[x][0] == means[x][0]]  # 去 NaN
            if not pts:
                continue
            _line_with_optional_errors(ax, [p[0] for p in pts], [p[1] for p in pts],
                                       [p[2] for p in pts], colors[i], line["label"])
        ax.set_xticks(xs, labels, fontsize=8)
        _style_ax(ax, group["title"], "k" if group["kind"] == "k" else "N", "")
        ax.set_ylim(-0.05, 1.1)
    fig.suptitle("汇总对比：语义等价率", color=INK, fontsize=12)
    # 图例放首个子图内（frameon=False），避免与 suptitle 在图顶相撞
    if axes.flat[0].get_legend_handles_labels()[1]:
        axes.flat[0].legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def chart_domain_small_multiples(panels: list[dict], path: Path) -> bool:
    """分领域小多图（方案配套图：看 N*/k* 是否跨域一致）。

    panels: [{"label": str, "kind": "k"|"n",
              "series": {retriever: (xs, ys, errs)}}]，每个 run（域）一个子图，共享 y 轴。
    """
    _setup_fonts()
    if not panels:
        return False
    n = len(panels)
    cols = min(3, n)
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 3.4 * rows),
                             facecolor=SURFACE, sharey=True, squeeze=False)
    legend_handles = None
    for ax, panel in zip(axes.flat, panels):
        for retriever, (xs_raw, ys, errs) in panel["series"].items():
            color = SERIES_COLOR.get(retriever, MUTED)
            xs, labels = _categorical_x(xs_raw)
            linestyle = "--" if retriever == "oracle" else "-"
            _line_with_optional_errors(ax, xs, ys, errs, color,
                                       SERIES_LABEL.get(retriever, retriever), linestyle)
            ax.set_xticks(xs, labels, fontsize=8)
        _style_ax(ax, panel["label"], "k" if panel["kind"] == "k" else "N", "")
        ax.set_ylim(-0.05, 1.1)
        if legend_handles is None:
            legend_handles = ax.get_legend_handles_labels()
    for ax in list(axes.flat)[len(panels):]:
        ax.axis("off")
    if legend_handles and legend_handles[1]:
        fig.legend(*legend_handles, frameon=False, fontsize=9,
                   loc="lower center", ncol=min(4, len(legend_handles[1])))
    fig.suptitle("分领域语义等价率小多图", color=INK)
    fig.tight_layout(rect=(0, 0.03, 1, 0.96))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def chart_resample_scatter(    store: Store, run_id: str, exp: ExperimentConfig, path: Path
) -> bool:
    """倒 U 检查（H2 证据）：同一 N 的多次独立重采样散点（每 seed 一点）+ 均值线。"""
    from .aggregate import per_rep_condition_means

    _setup_fonts()

    fig, ax = plt.subplots(figsize=(7.2, 4.2), facecolor=SURFACE)
    drew = False
    retriever_set = {c["retriever"] for c in store.condition_rows(run_id)}
    for retriever in SERIES_ORDER:
        if retriever not in retriever_set:
            continue
        by_condition = per_rep_condition_means(store, run_id, retriever=retriever)
        conds = {c["condition_id"]: c for c in store.condition_rows(run_id)
                 if c["retriever"] == retriever}
        ns = sorted({c["n"] for c in conds.values() if c["n"] is not None})
        if not ns:
            continue
        color = SERIES_COLOR[retriever]
        xs_all, labels = _categorical_x(ns)
        for idx, n in enumerate(ns):
            cid = next(c for c, v in conds.items() if v["n"] == n)
            points = [(idx + (i - 1) * 0.08, mean)
                      for i, (_rep, mean) in enumerate(sorted(by_condition[cid].items()))]
            if points:
                ax.scatter([p[0] for p in points], [p[1] for p in points],
                           s=26, color=color, alpha=0.55, zorder=3)
                drew = True
        means_line = []
        for idx, n in enumerate(ns):
            cid = next(c for c, v in conds.items() if v["n"] == n)
            vals = list(by_condition[cid].values())
            means_line.append(statistics.fmean(vals) if vals else float("nan"))
        ax.plot(xs_all, means_line, "-o", color=color, linewidth=2, markersize=6,
                label=SERIES_LABEL[retriever])
        ax.set_xticks(xs_all, labels)
    if not drew:
        plt.close(fig)
        return False
    _style_ax(ax, "同一 N 的多次重采样散点（每 seed 一点；散点发散=样本运气大）",
              "候选池规模 N", "语义等价率")
    ax.set_ylim(-0.05, 1.1)
    if len(retriever_set) > 1:
        ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def chart_coverage(metrics: list[ConditionMetrics], path: Path) -> bool:
    """检索覆盖度 vs N（机制图，解释质量为何变化）。"""
    series = _series_for(metrics, "n")
    if not series:
        return False
    fig, ax = plt.subplots(figsize=(7.2, 4.0), facecolor=SURFACE)
    multi = len(series) > 1
    for retriever in SERIES_ORDER:
        cms = series.get(retriever)
        if not cms:
            continue
        xs_raw = [cm.n for cm in cms]
        xs, labels = _categorical_x(xs_raw)
        ys = []
        for cm in cms:
            mean, _ = _mean_std(cm, "retrieval_coverage")
            ys.append(mean if mean is not None else float("nan"))
        ax.plot(xs, ys, ("--" if retriever == "oracle" else "-") + "o",
                color=SERIES_COLOR[retriever], linewidth=2, markersize=6,
                label=SERIES_LABEL[retriever] if multi else None)
        ax.set_xticks(xs, labels)
    _style_ax(ax, "检索覆盖度 vs 池规模（top-k 示例算子集与 gold 算子集 Jaccard）",
              "候选池规模 N", "检索覆盖度")
    ax.set_ylim(-0.05, 1.1)
    if multi:
        ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


# ---------------------------------------------------------------- 人工抽检导出


def export_review_sample(
    store: Store,
    run_id: str,
    exp: ExperimentConfig,
    items_by_id: dict[str, Item],
    rate: float,
    path: Path,
    seed: int = 42,  # 保留参数以兼容旧调用（等距抽样是确定性的，不需要种子）
) -> int:
    """按条件分层等距抽样导出人工抽检表（与看板 review 队列同一套样本：
    每条件按 gen_id 排序后等距抽取 stride=max(1, round(1/rate))）。返回抽样行数。"""
    rows_out: list[dict] = []
    stride = max(1, round(1 / rate))
    for cond in store.condition_rows(run_id):
        rows = [r for r in store.generation_join_rows(run_id, cond["condition_id"])
                if r["status"] == "ok"]
        rows.sort(key=lambda r: r["gen_id"])  # 与看板 review 队列同一套样本
        sampled = rows[::stride] or rows[:1]
        for r in sampled:
            item = items_by_id.get(r["item_id"])
            rows_out.append({
                "condition_id": r["condition_id"],
                "item_id": r["item_id"],
                "rep": r["rep"],
                "question": item.question if item else "",
                "gold_ke": item.gold_ke if item else "",
                "gen_ke": r["ke_text"] or "",
                "llm_verdict": r["llm_verdict"] or "",
                "llm_rationale": r["llm_rationale"] or "",
                "human_verdict": "",
                "human_note": "",
            })
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()) if rows_out else
                                ["condition_id", "item_id", "rep", "question", "gold_ke",
                                 "gen_ke", "llm_verdict", "llm_rationale",
                                 "human_verdict", "human_note"])
        writer.writeheader()
        writer.writerows(rows_out)
    return len(rows_out)


# ---------------------------------------------------------------- markdown 报告

PROVISIONAL_NOTE = (
    "> ⚠️ **冒烟声明**：本次为 20 题管线验证（leave-one-out 池，每题池=其余 19 条），"
    "样本量不足，所有曲线/拐点/假设判定仅用于检查管线，**不做统计推断**；"
    "H1–H4 一律记「数据不足」。LOO 下 N=19 档检索区分度小、曲线平坦属预期。"
    "P1「类型检查通过率」本轮口径 = light 静态检查（闭词表/arity/变量绑定/单 output），"
    "完整类型检查属后续阶段。"
)


def _pct(value: float | None, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%" if value is not None else "—"


def _mean_std_text(cm: ConditionMetrics, metric: str) -> str:
    stat = cm.stats.get(metric)
    if stat is None or stat.mean is None:
        return "—"
    text = f"{stat.mean * 100:.1f}±{stat.std * 100:.1f}"
    return text + (" ⚠️" if stat.alert else "")


def _axis_value(cm: ConditionMetrics) -> float:
    return float(cm.k if cm.kind == "k" else cm.n)


def render_report(
    store: Store,
    run_id: str,
    exp: ExperimentConfig,
    domain: DomainConfig,
    testset: list[Item],
    out_dir: Path,
    provisional: bool = False,
    prereg: dict | None = None,
    prereg_history: list[dict] | None = None,
) -> Path:
    """生成 results/<run_id>/ 下的 metrics.csv、图与 report.md。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lang = _setup_fonts()
    metrics = aggregate_run(store, run_id, exp)
    write_metrics_csv(metrics, out_dir / "metrics.csv")
    items_by_id = {it.item_id: it for it in testset}
    n_review = export_review_sample(
        store, run_id, exp, items_by_id, domain.review_sample_rate,
        out_dir / "review_sample.csv",
    )
    # 试跑/中断：按实际评估题数（distinct item_id），MDE 与题数不虚高
    evaluated_items = store.n_distinct_items(run_id)
    if evaluated_items:
        provisional = provisional or evaluated_items < 50
        if exp.limit_items and evaluated_items <= exp.limit_items:
            provisional = True

    axis = "k" if exp.kind == "k" else "n"
    primary_model = (exp.gen_models or [None])[0]  # 曲线只画主模型；模型对照单列
    delta_eff = (prereg or {}).get("delta", exp.knee_threshold)  # 预登记优先（§7.5）
    axis_metrics = [
        cm for cm in metrics
        if cm.kind == exp.kind and cm.gen_model in (None, primary_model)
    ]
    xs = sorted({_axis_value(cm) for cm in axis_metrics})

    # 主指标曲线与拐点（多检索器时按主线检索器 bm25 判拐点，其余在表中呈现）
    knee_by_retriever: dict[str, float | None] = {}
    for retriever in SERIES_ORDER:
        cms = [cm for cm in axis_metrics if cm.retriever == retriever]
        cms.sort(key=_axis_value)
        means = [_mean_std(cm, "semantic")[0] for cm in cms]
        if all(m is not None for m in means) and len(means) >= 2:
            knee_by_retriever[retriever] = knee_two_gains(
                [float(_axis_value(cm)) for cm in cms],
                [float(m) for m in means],
                delta_eff,
            )
    main_retriever = "bm25" if "bm25" in knee_by_retriever else next(iter(knee_by_retriever), None)
    knee = knee_by_retriever.get(main_retriever) if main_retriever else None

    fig_quality = out_dir / f"{axis}_quality.png"
    fig_marginal = out_dir / "marginal_gain.png"
    fig_cost = out_dir / "cost_effect.png"
    fig_cov = out_dir / "retrieval_coverage.png"
    fig_scatter = out_dir / "resample_scatter.png"
    fig_model = out_dir / "model_comparison.png"
    have_quality = chart_quality(axis_metrics, exp.kind, fig_quality, knee)
    have_marginal = chart_marginal(axis_metrics, exp.kind, exp.knee_threshold, fig_marginal)
    have_cost = chart_cost_effect(metrics, fig_cost)
    have_cov = exp.kind == "n" and chart_coverage(metrics, fig_cov)
    have_scatter = exp.kind == "n" and chart_resample_scatter(
        store, run_id, exp, fig_scatter
    )
    have_model = chart_model_comparison(metrics, exp, fig_model)

    md = _build_markdown(
        store=store, run_id=run_id, exp=exp, domain=domain, testset=testset,
        metrics=metrics, xs=xs, knee_by_retriever=knee_by_retriever,
        main_retriever=main_retriever, provisional=provisional,
        figures={
            "quality": (fig_quality.name, have_quality),
            "marginal": (fig_marginal.name, have_marginal),
            "cost": (fig_cost.name, have_cost),
            "coverage": (fig_cov.name, have_cov),
            "scatter": (fig_scatter.name, have_scatter),
            "model": (fig_model.name, have_model),
        },
        n_review=n_review,
        prereg=prereg,
        prereg_history=prereg_history,
        primary_model=primary_model,
        evaluated_items=evaluated_items or None,
    )
    report_path = out_dir / "report.md"
    report_path.write_text(md, encoding="utf-8")
    return report_path


def _build_markdown(
    *,
    store: Store,
    run_id: str,
    exp: ExperimentConfig,
    domain: DomainConfig,
    testset: list[Item],
    metrics: list[ConditionMetrics],
    xs: list[float],
    knee_by_retriever: dict[str, float | None],
    main_retriever: str | None,
    provisional: bool,
    figures: dict[str, tuple[str, bool]],
    n_review: int,
    prereg: dict | None = None,
    prereg_history: list[dict] | None = None,
    primary_model: str | None = None,
    evaluated_items: int | None = None,
) -> str:
    run_info = store.run_info(run_id)
    axis_name = "k" if exp.kind == "k" else "N"
    n_items = evaluated_items or len(testset)  # 试跑/中断按实际评估题数（MDE 不虚高）
    models_text = "、".join(exp.gen_models) if exp.gen_models else domain.gen_model
    # 预登记一致性/时序校验（consistency：配置漂移；timing：先看数后登记）
    prereg_warnings: list[str] = []
    if prereg:
        from .config import dataset_fingerprint
        from .prereg import check_consistency

        fp_now = dataset_fingerprint(exp) if Path(exp.testset).exists() else None
        prereg_warnings = check_consistency(
            prereg, exp, dataset_md5_now=fp_now,
            run_started_at=run_info["started_at"] if run_info else None,
            history=prereg_history,
        )
    lines: list[str] = [
        f"# 实验报告：{exp.name}（{axis_name} 扫描）",
        "",
        f"- run_id：`{run_id}`",
        f"- 领域：{domain.name}　实验类型：{exp.kind}　生成模型：{models_text}（温度 {domain.gen_temperature}）",
        f"- judge 模型：{domain.judge_model}　重复：{exp.repeats}（种子 {exp.seeds[:exp.repeats]}）",
        f"- 测试集：{n_items} 题（实际评估题数）　池策略：{exp.pool_strategy}",
        f"- prompt 模板 md5：`{run_info['prompt_template_md5'] if run_info else '—'}`　"
        f"grammar md5：`{run_info['grammar_md5'] if run_info else '—'}`",
        (f"- 预登记：`v{prereg['version']}` 哈希 `{prereg['prereg_hash']}`"
         f"（δ={prereg['delta']}，质量门槛={prereg.get('quality_gate') or '未设'}，"
         f"抽检={prereg.get('review_rate')}，决策人={prereg['actor']}）"
         if prereg else "- 预登记：未登记（报告阈值取实验配置；协议要求先登记再看数）"),
        "",
    ]
    for warning in prereg_warnings:
        icon = "ℹ️" if warning.startswith("时序说明") else "⚠️"
        lines += [f"> {icon} 预登记校验：{warning}", ""]
    if provisional:
        lines += [PROVISIONAL_NOTE, ""]

    # ---- 解读摘要（规则引擎产出；每条附依据，详见 run 页解读卡）
    from .interpret import final_verdicts, interpret_run, review_agreement

    mde = mde_estimate(n_items)
    reviews: dict = {}
    try:
        rows = store._conn.execute("SELECT * FROM reviews_v2").fetchall()
        reviews = final_verdicts(rows)  # 协议 §7.4：C 裁决 > A/B 一致 > 单人
    except Exception:
        try:
            reviews = {r["gen_id"]: r for r in
                       store._conn.execute("SELECT * FROM reviews").fetchall()}
        except Exception:
            pass  # 无 reviews 表（看板外生成的 run）
    agree, total_reviewed = review_agreement(store, run_id, reviews)
    findings = interpret_run(metrics, exp, mde,
                             review_agree=agree, review_total=total_reviewed)
    marks = {"good": "✅", "warn": "⚠️", "bad": "❌", "info": "ℹ️"}
    lines += ["## 0. 解读摘要", ""]
    for f in findings:
        lines.append(f"- {marks[f.level]} {f.text}"
                     + (f"（依据：{f.evidence}）" if f.evidence else ""))
    lines.append("")

    # ---- 主结果表
    lines += [
        "## 1. 主结果（均值与 95% 置信区间（逐题 t 区间）；⚠️ = 重复间 std 超 3pp）",
        "",
        "| 条件 | 语义等价率 | 95% CI | 完全匹配 | 语法可解析 | light 检查 | 检索覆盖度 | 生成覆盖度 | tokens(prompt) | 耗时(ms) | 生成失败 | judge 失败 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cm in metrics:
        tok, _ = _mean_std(cm, "prompt_tokens")
        lat, _ = _mean_std(cm, "latency_ms")
        cov_r, _ = _mean_std(cm, "retrieval_coverage")
        cov_g, _ = _mean_std(cm, "gen_coverage")
        exact, _ = _mean_std(cm, "exact")
        lark, _ = _mean_std(cm, "lark")
        light, _ = _mean_std(cm, "light")
        lo, hi = cm.semantic_ci[1], cm.semantic_ci[2]
        ci_text = f"[{_pct(lo)}, {_pct(hi)}]" if lo is not None else "—"
        lines.append(
            f"| {cm.condition_id} | {_mean_std_text(cm, 'semantic')} | {ci_text} | {_pct(exact)} "
            f"| {_pct(lark)} | {_pct(light)} | {_pct(cov_r)} | {_pct(cov_g)} "
            f"| {tok:.0f} | {lat:.0f} | {cm.n_gen_error} | {cm.n_judge_error} |"
            if tok is not None and lat is not None else
            f"| {cm.condition_id} | {_mean_std_text(cm, 'semantic')} | {ci_text} | {_pct(exact)} "
            f"| {_pct(lark)} | {_pct(light)} | {_pct(cov_r)} | {_pct(cov_g)} | — | — "
            f"| {cm.n_gen_error} | {cm.n_judge_error} |"
        )
    lines.append("")

    # ---- 曲线与拐点（协议 §7.5：连续两档增益<δ + 配对 CI 未达最小实际差异 + δ 敏感性）
    delta_eff = (prereg or {}).get("delta", exp.knee_threshold)  # 预登记优先（§7.5）
    lines += [f"## 2. {axis_name}–质量曲线与拐点", ""]
    for key, caption in (("quality", "主图"), ("marginal", "边际增益")):
        fname, ok = figures[key]
        if ok:
            lines += [f"![{caption}]({fname})", ""]
    lines += _knee_section(store, run_id, exp, metrics, mde,
                           primary_model=primary_model, delta=delta_eff)
    lines.append("")

    # ---- 选择规则与停止线（协议 §7.6）
    lines += ["### 选择规则与停止线", ""]
    lines += _selection_rule(metrics, exp, primary_model=primary_model,
                             quality_gate=(prereg or {}).get("quality_gate"))
    lines.append("")

    # ---- 统计功效（方案 §5.3：主结论只对 ≥MDE 的差异下判断）
    if mde is not None:
        lines += [
            "## 2.1 统计功效（MDE）",
            "",
            f"- 当前每条件 {n_items} 题；按方案锚点（100 题 ≈ 5pp，待精确计算）"
            f"以 1/√n 换算：**MDE ≈ {mde * 100:.1f}pp**",
            f"- 判读规则：小于 MDE 的差异一律判「数据不足」，不下「有效/无效」结论"
            + ("；本轮为冒烟小样本，绝大多数差异在 MDE 之下" if n_items < 100 else ""),
            "",
        ]

    # 相对提升与成本换算（k 实验：vs k=0；n 实验：vs 最小池）
    lines += ["### 相对提升与成本换算", ""]
    lines += _lift_and_cost(metrics, exp, domain)
    lines.append("")

    # ---- 本体注入对照（k 方案 B 对照；只在同时跑了 full+linked 时有内容）
    ablation = _onto_ablation(metrics, exp, mde)
    if ablation:
        lines += ablation

    # ---- 模型对照（k 方案 C / n 方案 I；只在 gen_models ≥2 时有内容）
    model_ablation = _model_ablation(metrics, exp, mde)
    if model_ablation:
        fname, ok = figures.get("model", ("", False))
        if ok:
            lines += [f"![模型对照]({fname})", ""]
        lines += model_ablation

    # ---- 重采样一致性（方案验收：两次独立重采样 N*/k* 相差 ≤1 档）
    lines += ["### 重采样拐点一致性", ""]
    lines += _resample_consistency(store, run_id, exp)
    lines.append("")

    # ---- H1–H4（n 实验）或曲线形态（k 实验）
    if exp.kind == "n":
        lines += ["## 3. 假设判定（H1–H4 三态）", ""]
        lines += _hypotheses(metrics, exp, provisional)
    else:
        lines += ["## 3. 曲线形态判定", ""]
        lines += _curve_shape(metrics, exp)
    lines.append("")

    # ---- 检索覆盖度机制与重采样散点（n 实验）
    if exp.kind == "n":
        fname, ok = figures["coverage"]
        if ok:
            lines += ["## 4. 检索覆盖度机制", "", f"![检索覆盖度]({fname})", ""]
        fname, ok = figures.get("scatter", ("", False))
        if ok:
            lines += ["### 倒 U 检查：同 N 重采样散点（H2 证据）", "",
                      f"![重采样散点]({fname})", ""]

    # ---- 成本–效果
    fname, ok = figures["cost"]
    if ok:
        lines += ["## 5. 成本–效果", "", f"![成本效果]({fname})", ""]

    # ---- 异常与失败
    lines += ["## 6. 异常与失败清单", ""]
    lines += _failure_section(store, run_id, metrics)
    lines.append("")

    # ---- 数据备注
    noted = [it for it in testset if it.note]
    if noted:
        lines += ["## 7. 数据备注", ""]
        for it in noted:
            lines.append(f"- {it.item_id}：{it.note}")
        lines.append("")

    lines += [
        "## 8. 人工抽检与复现",
        "",
        f"- 人工抽检：按条件分层随机抽 {n_review} 条（比例 {domain.review_sample_rate:.0%}），"
        "见 `review_sample.csv`（human_verdict/human_note 列留白待人工填写）",
        "- **判分口径（协议 §7.4）**：语义等价以人工盲评为准（双人独立 + 分歧第三人裁决）；"
        "LLM judge 只作辅助信号，不单独决定 gold 等价",
        "- judge 与生成同源的偏差风险靠该抽检对冲；judge 模型可配置更换",
        f"- 复现：`python -m kelab.cli run --exp <实验配置>`（同配置重跑即自动断点续跑），"
        f"明细见 `runs/{run_id}/kelab.sqlite`（prompt 全文逐字落库）",
        "",
    ]
    return "\n".join(lines)


def _item_scores(store: Store, run_id: str, condition_id: str) -> dict[str, float]:
    """同题得分表：rep=0（预登记种子）+ 确定性排序——避免乱序重复覆盖造成的漂移。"""
    rows = store._conn.execute(
        "SELECT g.item_id, j.llm_score FROM generations g"
        " JOIN judgments j ON j.gen_id=g.gen_id"
        " WHERE g.run_id=? AND g.condition_id=? AND g.status='ok' AND g.rep=0"
        " AND j.llm_score IS NOT NULL ORDER BY g.gen_id", (run_id, condition_id),
    ).fetchall()
    return {r["item_id"]: r["llm_score"] for r in rows}


def _knee_section(
    store: Store, run_id: str, exp: ExperimentConfig,
    metrics: list[ConditionMetrics], mde: float | None,
    primary_model: str | None = None,
    delta: float | None = None,
) -> list[str]:
    """协议 §7.5 拐点段：k* = 从某档起连续两个相邻档位边际增益均 < δ，
    且相邻档配对 CI 未达最小实际差异（MDE）。附 δ 敏感性分析。
    δ 优先取预登记值（防事后解释），无登记用实验配置。"""
    from .stats import knee_sensitivity, knee_two_gains, paired_diff_ci

    axis_name = "k*" if exp.kind == "k" else "N*"
    delta = delta if delta is not None else exp.knee_threshold
    key = (lambda cm: cm.k) if exp.kind == "k" else (lambda cm: cm.n)
    out: list[str] = []
    seen_retrievers = []
    for cm in metrics:
        if (cm.kind == exp.kind and cm.onto == "full"
                and cm.gen_model in (None, primary_model)
                and cm.retriever not in seen_retrievers):
            seen_retrievers.append(cm.retriever)
    for retriever in SERIES_ORDER:
        if retriever not in seen_retrievers:
            continue
        cms = sorted([cm for cm in metrics
                      if cm.kind == exp.kind and cm.onto == "full"
                      and cm.gen_model in (None, primary_model)
                      and cm.retriever == retriever], key=key)
        xs = [float(key(cm)) for cm in cms]
        means = [_mean_std(cm, "semantic")[0] for cm in cms]
        if len(xs) < 3 or any(m is None for m in means):
            out.append(f"- {SERIES_LABEL[retriever]}：数据不足（拐点需 ≥3 档完整数据）")
            continue
        means_f = [float(m) for m in means]
        knee = knee_two_gains(xs, means_f, delta)
        sens = knee_sensitivity(xs, means_f)
        sens_text = "，".join(
            f"δ={d:.0%}→{axis_name}={f'{k:g}' if k is not None else '无'}"
            for d, k in sens
        )
        if knee is None:
            out.append(
                f"- {SERIES_LABEL[retriever]}：按协议定义**未出现拐点**"
                f"（无连续两档增益均 < {delta:.0%}）。δ 敏感性：{sens_text}"
            )
            continue
        # 配对 CI 确认：拐点档与其前一档、后一档的同题配对差值
        ki = xs.index(knee)
        confirms = []
        for j in (ki, ki + 1):
            if j <= 0 or j >= len(xs):
                continue
            a = _item_scores(store, run_id, cms[j - 1].condition_id)
            b = _item_scores(store, run_id, cms[j].condition_id)
            common = sorted(set(a) & set(b))
            diff, lo, hi, n = paired_diff_ci([b[i] for i in common], [a[i] for i in common])
            if n:
                under = mde is not None and hi is not None and hi < mde
                confirms.append(
                    f"{xs[j - 1]:g}→{xs[j]:g} 差 {diff * 100:+.1f}pp "
                    f"CI[{_pct(lo)}, {_pct(hi)}] n={n}"
                    + ("（未达 MDE ✓）" if under else "（CI 超 MDE，拐点存疑）")
                )
        out.append(
            f"- {SERIES_LABEL[retriever]}：**{axis_name} = {knee:g}**"
            f"（连续两档增益 < {delta:.0%}）。配对确认：{'；'.join(confirms) or '无共同题'}"
        )
        out.append(f"  - δ 敏感性：{sens_text}")
    return out


def _selection_rule(
    metrics: list[ConditionMetrics], exp: ExperimentConfig,
    primary_model: str | None = None, quality_gate: float | None = None,
) -> list[str]:
    """协议 §7.6 选择规则：先满足质量门槛与门禁指标，再选达标且成本最低的 k；
    未设门槛只报曲线，不宣称「最优 k」。门槛优先取预登记值。"""
    gate = quality_gate if quality_gate is not None else exp.quality_gate
    if gate is None:
        return [
            "- **选择规则**：未设质量门槛——按协议只报告曲线，不宣称最优 k；"
            "要出选择结论须先在预登记里由决策人设定门槛"
        ]
    full = [cm for cm in metrics if cm.onto == "full" and cm.kind == exp.kind
            and cm.gen_model in (None, primary_model)]
    passing = []
    for cm in full:
        sem, _ = _mean_std(cm, "semantic")
        lark, _ = _mean_std(cm, "lark")
        light, _ = _mean_std(cm, "light")
        tok, _ = _mean_std(cm, "prompt_tokens")
        if (sem is not None and sem >= gate
                and (lark is None or lark >= 1.0) and (light is None or light >= 1.0)):
            passing.append((cm, tok))
    if not passing:
        return [
            f"- **选择规则**：质量门槛 {gate:.0%} 无档位通过——只报告曲线；"
            "是否降门槛属决策人事项（协议禁止降标准关闭）"
        ]
    passing.sort(key=lambda t: (t[1] if t[1] is not None else float("inf")))
    cm, tok = passing[0]
    tok_text = f"{tok:.0f}" if tok is not None else "未知"
    return [
        f"- **选择规则**（质量门槛 {gate:.0%}，门禁全过）：推荐 "
        f"**{cm.condition_id}**——语义 {_pct(_mean_std(cm, 'semantic')[0])}，"
        f"prompt tokens {tok_text}，达标档中成本最低"
    ]


def _lift_and_cost(
    metrics: list[ConditionMetrics], exp: ExperimentConfig, domain=None
) -> list[str]:
    """相对提升（vs 最小档、到上限的追加增益）与成本换算。
    k 实验换算 token；N 实验另加「池构建成本」（方案验收：每 +1pp 需多少标注投入）。"""
    out: list[str] = []
    per_retriever: dict[str, list[ConditionMetrics]] = {}
    for cm in metrics:
        per_retriever.setdefault(cm.retriever, []).append(cm)
    for retriever, cms in per_retriever.items():
        cms.sort(key=_axis_value)
        means = {cm.condition_id: _mean_std(cm, "semantic")[0] for cm in cms}
        if any(v is None for v in means.values()) or len(cms) < 2:
            continue
        first, last = cms[0], cms[-1]
        best = max(cms, key=lambda cm: means[cm.condition_id])
        lift = means[best.condition_id] - means[first.condition_id]
        tail = means[last.condition_id] - means[best.condition_id]
        out.append(
            f"- {SERIES_LABEL[retriever]}：最佳档（{axis_of(best)}={_axis_value(best):g}）"
            f"相对最小档（{_axis_value(first):g}）提升 {_pct(lift)}；"
            f"到上限档（{_axis_value(last):g}）追加增益 {_pct(tail)}"
        )
        tok_first, _ = _mean_std(first, "prompt_tokens")
        tok_best, _ = _mean_std(best, "prompt_tokens")
        if tok_first is not None and tok_best is not None:
            if lift > 1e-9:
                cost = (tok_best - tok_first) / (lift * 100)
                out.append(
                    f"  - token 成本换算：语义等价率每 +1pp 约多花 {cost:.0f} prompt tokens"
                    f"（{tok_first:.0f} → {tok_best:.0f}）"
                )
            else:
                out.append("  - token 成本换算：语义等价率无提升，无法换算（token 增量无收益）")
        if exp.kind == "n":
            n_first, n_best = _axis_value(first), _axis_value(best)
            if lift > 1e-9 and n_best > n_first:
                per_pp = (n_best - n_first) / (lift * 100)
                line = (f"  - 池构建成本换算：语义等价率每 +1pp ≈ 多标注 {per_pp:.0f} 条 gold KE"
                        f"（N {n_first:g} → {n_best:g}）")
                if domain is not None and domain.annotation_cost_per_item:
                    yuan = per_pp * domain.annotation_cost_per_item
                    line += f"，按 ¥{domain.annotation_cost_per_item:g}/条 ≈ ¥{yuan:.0f}/pp"
                out.append(line)
            else:
                out.append("  - 池构建成本换算：增益不足或无增量档位，无法换算")
    return out or ["- 数据不足，无法计算相对提升"]


def _model_ablation(
    metrics: list[ConditionMetrics], exp: ExperimentConfig, mde: float | None
) -> list[str]:
    """模型对照（k 方案 C / n 方案 I）：逐档双模型语义对比 + 收益形态交互判定
    （k/N 的收益是否随模型能力变化——比水平差更重要的是曲线形状差）。"""
    models = list(exp.gen_models or [])
    if len(models) < 2:
        return []
    a, b = models[0], models[1]
    cms = [cm for cm in metrics if cm.kind == exp.kind and cm.onto == "full"]
    by: dict[tuple, ConditionMetrics] = {}
    for cm in cms:
        if cm.gen_model in (a, b):
            by[(cm.retriever, cm.k if exp.kind == "k" else cm.n, cm.gen_model)] = cm
    rows = [f"| 检索器 | 档位 | {a} | {b} | Δ（{b}−{a}） |", "|---|---|---|---|---|"]
    lifts: dict[str, list[float]] = {a: [], b: []}
    seen: set[tuple] = set()
    for (retriever, x, _m) in sorted(by, key=lambda t: (t[0], t[1])):
        if (retriever, x) in seen:
            continue
        seen.add((retriever, x))
        ca, cb = by.get((retriever, x, a)), by.get((retriever, x, b))
        if ca is None or cb is None:
            continue
        sa, _ = _mean_std(ca, "semantic")
        sb, _ = _mean_std(cb, "semantic")
        if sa is None or sb is None:
            continue
        rows.append(f"| {retriever} | {x:g} | {_pct(sa)} | {_pct(sb)} | {(sb - sa) * 100:+.1f}pp |")
    # 收益形态：每个 (检索器, 模型) 的「最佳档 − 最小档」
    for retriever in {cm.retriever for cm in cms}:
        for model in (a, b):
            xs_sem = sorted(
                ((cm.k if exp.kind == "k" else cm.n, _mean_std(cm, "semantic")[0])
                 for cm in cms if cm.retriever == retriever and cm.gen_model == model),
                key=lambda t: t[0],
            )
            vals = [s for _, s in xs_sem if s is not None]
            if len(vals) >= 2:
                lifts[model].append(max(vals) - vals[0])
    if not rows or not lifts[a] or not lifts[b]:
        return []
    lift_a = statistics.fmean(lifts[a])
    lift_b = statistics.fmean(lifts[b])
    tol = mde or 0.03
    if abs(lift_a - lift_b) > tol:
        verdict = (f"**k/N 收益随模型能力变化**：{a} 平均收益 {lift_a * 100:+.1f}pp vs "
                   f"{b} {lift_b * 100:+.1f}pp，差 {abs(lift_a - lift_b) * 100:.1f}pp > MDE"
                   f"（{tol * 100:.0f}pp）——建议分模型给 k*/N*")
    else:
        verdict = (f"**收益形态跨模型一致**：两模型平均收益差 "
                   f"{abs(lift_a - lift_b) * 100:.1f}pp ≤ MDE（{tol * 100:.0f}pp）——结论可共用")
    return [
        "### 模型对照（k 方案 C / n 方案 I：收益是否随模型能力变化）",
        "",
        *rows,
        "",
        f"**判定**：{verdict}",
        "",
        f"口径：judge 固定；收益=「最佳档语义 − 最小档语义」按检索器平均。模型：{a} vs {b}。",
        "",
    ]


def _onto_ablation(
    metrics: list[ConditionMetrics], exp: ExperimentConfig, mde: float | None
) -> list[str]:
    """本体注入对照（k 方案 B 对照）：full vs linked 逐档语义 Δ + prompt token 节省。"""
    fulls = {((cm.retriever), (cm.k if exp.kind == "k" else cm.n)): cm
             for cm in metrics if cm.onto == "full"}
    linkeds = {((cm.retriever), (cm.k if exp.kind == "k" else cm.n)): cm
               for cm in metrics if cm.onto == "linked"}
    if not fulls or not linkeds:
        return []
    rows = [
        "| 检索器 | 档位 | full 语义 | linked 语义 | Δ语义 | prompt tokens（full→linked，节省） |",
        "|---|---|---|---|---|---|",
    ]
    verdicts: list[tuple[float, float | None]] = []
    for key in sorted(set(fulls) & set(linkeds), key=lambda k: (k[0], k[1])):
        f, l = fulls[key], linkeds[key]
        fs, _ = _mean_std(f, "semantic")
        ls, _ = _mean_std(l, "semantic")
        if fs is None or ls is None:
            continue
        ft, _ = _mean_std(f, "prompt_tokens")
        lt, _ = _mean_std(l, "prompt_tokens")
        delta = ls - fs
        saved_pct = (ft - lt) / ft * 100 if ft and lt is not None else None
        rows.append(
            f"| {key[0]} | {key[1]:g} | {_pct(fs)} | {_pct(ls)} | {delta * 100:+.1f}pp "
            f"| {f'{ft:.0f}→{lt:.0f}（-{saved_pct:.0f}%）' if saved_pct is not None else '—'} |"
        )
        verdicts.append((delta, saved_pct))
    if not verdicts:
        return []
    tol = mde or 0.03
    worst = min(v[0] for v in verdicts)
    saves = [v[1] for v in verdicts if v[1] is not None]
    avg_saved = statistics.fmean(saves) if saves else None
    if worst >= -tol and avg_saved is not None and avg_saved > 5:
        verdict = (f"**支持裁剪**：各档语义 Δ ≥ {-tol * 100:.0f}pp（MDE 内），"
                   f"平均省 {avg_saved:.0f}% prompt tokens——只给相关概念就够")
    elif worst < -tol:
        verdict = (f"**不支持裁剪**：最差档语义 Δ {worst * 100:+.1f}pp 超过 MDE"
                   f"（{tol * 100:.0f}pp）——全量本体有实质贡献")
    else:
        verdict = "**数据不足**：token 节省不显著或档数太少，暂不下结论"
    return [
        "### 本体注入对照（k 方案 B：全量 vs 每题相关概念）",
        "",
        *rows,
        "",
        f"**判定**：{verdict}",
        "",
        "口径：linked 的相关概念取自同一检索结果（或 gold 诊断），"
        "Δ 在同一批题、同一 seed 下配对比较。",
        "",
    ]


def _resample_consistency(store: Store, run_id: str, exp: ExperimentConfig) -> list[str]:
    """重采样拐点一致性（方案验收：两次独立重采样 N*/k* 相差 ≤1 档）。"""
    from .aggregate import knee_consistency, per_seed_knee

    out: list[str] = []
    retrievers = [r for r in SERIES_ORDER
                  if r in {c["retriever"] for c in store.condition_rows(run_id)}]
    axis_name = "N*" if exp.kind == "n" else "k*"
    for retriever in retrievers:
        knees = per_seed_knee(store, run_id, exp, retriever)
        xs = [float(c["k"] if exp.kind == "k" else c["n"])
              for c in store.condition_rows(run_id) if c["retriever"] == retriever]
        verdict, evidence = knee_consistency(knees, xs)
        seed_text = "，".join(
            f"seed {s}: {axis_name}={k:g}" if k is not None else f"seed {s}: 无拐点"
            for s, k in knees.items()
        )
        out.append(f"- {SERIES_LABEL[retriever]}：**{verdict}**（{evidence}）。{seed_text}")
    return out or ["- 数据不足（无条件数据）"]


def axis_of(cm: ConditionMetrics) -> str:
    return "k" if cm.kind == "k" else "N"


def _curve_shape(metrics: list[ConditionMetrics], exp: ExperimentConfig) -> list[str]:
    """k 实验的曲线形态判定：单调 / 倒 U / 混合（按边际增益符号）。"""
    out: list[str] = []
    per_retriever: dict[str, list[ConditionMetrics]] = {}
    for cm in metrics:
        per_retriever.setdefault(cm.retriever, []).append(cm)
    for retriever, cms in per_retriever.items():
        cms.sort(key=_axis_value)
        means = [_mean_std(cm, "semantic")[0] for cm in cms]
        if any(m is None for m in means) or len(means) < 2:
            out.append(f"- {SERIES_LABEL[retriever]}：数据不足，无法判定曲线形态")
            continue
        gains = [g for _, g in marginal_gains(
            [float(_axis_value(cm)) for cm in cms], [float(m) for m in means])]
        tol = 0.005
        pos = sum(1 for g in gains if g > tol)
        neg = sum(1 for g in gains if g < -tol)
        if neg == 0 and pos > 0:
            shape = "单调不降"
        elif pos == 0 and neg == 0:
            shape = "全程平坦（增益在 ±0.5pp 内）"
        elif pos > 0 and neg > 0 and gains.index(max(gains)) < len(gains) - 1:
            shape = "先升后降/倒 U 迹象"
        else:
            shape = "混合波动"
        out.append(
            f"- {SERIES_LABEL[retriever]}：{shape}（边际增益序列 "
            + ", ".join(f"{g * 100:+.1f}pp" for g in gains) + "）"
        )
    return out


def _hypotheses(
    metrics: list[ConditionMetrics], exp: ExperimentConfig, provisional: bool
) -> list[str]:
    """N 实验 H1–H4 三态判定。冒烟期一律「数据不足」，但给出机械读数作证据。"""
    def wrap(verdict: str) -> str:
        return "数据不足（冒烟期）" if provisional else verdict

    out: list[str] = []
    non_oracle = [cm for cm in metrics if cm.retriever != "oracle"]
    oracle = [cm for cm in metrics if cm.retriever == "oracle"]

    # H1：池增大 → 质量升、边际递减 → 存在 N*
    per_retriever: dict[str, list[ConditionMetrics]] = {}
    for cm in non_oracle:
        per_retriever.setdefault(cm.retriever, []).append(cm)
    h1_evidence: list[str] = []
    h1_support = False
    for retriever, cms in per_retriever.items():
        cms.sort(key=_axis_value)
        means = [_mean_std(cm, "semantic")[0] for cm in cms]
        if any(m is None for m in means) or len(means) < 2:
            continue
        gains = [g for _, g in marginal_gains([float(cm.n) for cm in cms], [float(m) for m in means])]
        knee = detect_knee([float(cm.n) for cm in cms], [float(m) for m in means], exp.knee_threshold)
        h1_evidence.append(
            f"{SERIES_LABEL[retriever]} 增益序列 "
            + ", ".join(f"{g * 100:+.1f}pp" for g in gains)
            + (f"，N*={knee:g}" if knee else "，无拐点")
        )
        if knee is not None and any(g > exp.knee_threshold for g in gains):
            h1_support = True
    verdict = "支持" if h1_support else "不支持"
    out.append(f"- **H1（池增大→质量升、边际递减，存在 N\\*）**：{wrap(verdict)}。"
               + ("；".join(h1_evidence) if h1_evidence else "无可用序列"))

    # H2：倒 U（同 N 重采样散点）——冒烟无独立重采样设计，看增益符号
    h2_signs = any(
        (lambda means: any(
            g < -exp.knee_threshold
            for _, g in marginal_gains([float(cm.n) for cm in cms], means)
        ))([_mean_std(cm, "semantic")[0] for cm in cms])
        for cms in per_retriever.values()
        if len(cms) >= 2 and all(_mean_std(cm, "semantic")[0] is not None for cm in cms)
    )
    out.append(
        f"- **H2（池增大→质量先升后降，倒 U）**：{wrap('支持' if h2_signs else '不支持')}。"
        + ("观测到显著为负的边际增益" if h2_signs else "未观测到显著下降段")
        + "；正式的同 N 独立重采样散点待真实池数据补充"
    )

    # H3：长尾域收益大于结构化域 —— 单域无法判定
    out.append("- **H3（长尾术语密集领域收益更大）**：数据不足（本轮单领域，需 5 域对照）")

    # H4：oracle 显著高于实测 → 瓶颈在检索器
    if oracle and non_oracle:
        oracle_best = max(_mean_std(cm, "semantic")[0] or 0.0 for cm in oracle)
        measured_best = max(_mean_std(cm, "semantic")[0] or 0.0 for cm in non_oracle)
        gap = oracle_best - measured_best
        verdict = "支持" if gap > exp.oracle_gap_threshold else "不支持"
        out.append(
            f"- **H4（oracle 上界显著高于实测→瓶颈在检索器）**：{wrap(verdict)}。"
            f"oracle 最佳 {_pct(oracle_best)}，实测最佳 {_pct(measured_best)}，"
            f"差距 {_pct(gap)}（阈值 {_pct(exp.oracle_gap_threshold)}）"
        )
    else:
        out.append("- **H4（oracle 上界显著高于实测）**：数据不足（未跑 oracle 组）")
    return out


def _failure_section(store: Store, run_id: str, metrics: list[ConditionMetrics]) -> list[str]:
    out: list[str] = []
    total_gen_error = sum(cm.n_gen_error for cm in metrics)
    total_judge_error = sum(cm.n_judge_error for cm in metrics)
    if total_gen_error == 0 and total_judge_error == 0:
        out.append("- 生成失败 0 条，judge 失败 0 条 ✓")
    else:
        out.append(f"- 生成失败 {total_gen_error} 条，judge 失败 {total_judge_error} 条"
                   "（明细见 runs 库 generations.error / judgments.judge_error）")
    # 校验非正常值提示：lark/light 未满分的条件
    bad = [
        cm for cm in metrics
        if (_mean_std(cm, "lark")[0] or 1.0) < 1.0 or (_mean_std(cm, "light")[0] or 1.0) < 1.0
    ]
    if bad:
        out.append("- 校验指标未达 100% 的条件（需人工查阅 issues_json 归类）："
                   + ", ".join(cm.condition_id for cm in bad))
    alerts = [cm.condition_id for cm in metrics if any(s.alert for s in cm.stats.values())]
    if alerts:
        out.append("- std 超过 3pp 稳定性门槛的条件（按方案须说明或重跑）："
                   + ", ".join(alerts))
    return out
