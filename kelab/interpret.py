"""解读引擎：把 ConditionMetrics 翻译成「人话判定」。

设计纪律（防止平台替用户下错结论）：
- 只出确定性规则，每条 Finding 附依据数字，证据可点回明细
- 守 MDE：小于 MDE 的差异一律标「仅作参考」，不说「有效/无效」
- 三档表达：good（可以放心用）/ warn（要注意）/ bad（有问题）/ info（中性陈述）
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .aggregate import ConditionMetrics, detect_knee, marginal_gains
from .config import ExperimentConfig


@dataclass
class Finding:
    level: str  # good | warn | bad | info
    text: str
    evidence: str = ""


def _pct(v: float | None, digits: int = 1) -> str:
    return f"{v * 100:.{digits}f}%" if v is not None else "—"


def _axis(cm: ConditionMetrics) -> float:
    return float(cm.k if cm.kind == "k" else cm.n)


def _mean(cm: ConditionMetrics, metric: str) -> float | None:
    stat = cm.stats.get(metric)
    return stat.mean if stat and stat.mean is not None else None


def interpret_run(
    metrics: list[ConditionMetrics],
    exp: ExperimentConfig,
    mde: float | None,
    *,
    review_agree: int = 0,
    review_total: int = 0,
) -> list[Finding]:
    """run 级解读：输入聚合指标，输出按重要性排序的 Finding 列表。"""
    findings: list[Finding] = []
    full = [cm for cm in metrics if cm.onto == "full" and cm.kind == exp.kind]
    if not full:
        return [Finding("info", "还没有可解读的数据（条件均未出结果）")]
    axis_name = "k" if exp.kind == "k" else "N"
    axis_unit = "示例数" if exp.kind == "k" else "池规模"

    # ---- 曲线形态与拐点（按主线检索器 bm25 或第一个）
    retrievers = sorted({cm.retriever for cm in full},
                        key=lambda r: (r != "bm25", r))
    main = retrievers[0]
    cms = sorted([cm for cm in full if cm.retriever == main], key=_axis)
    means = [_mean(cm, "semantic") for cm in cms]
    if all(m is not None for m in means) and len(means) >= 2:
        xs = [_axis(cm) for cm in cms]
        knee = detect_knee(xs, means, exp.knee_threshold)
        gains = marginal_gains(xs, means)
        if knee is not None:
            prev = xs[xs.index(knee) - 1] if knee in xs and xs.index(knee) > 0 else None
            advice = f"性价比最优停在 {prev:g}" if prev is not None else f"{axis_unit} 起步即饱和"
            findings.append(Finding(
                "good",
                f"{axis_name}* = {knee:g}（{main}）：超过该档边际增益 < {exp.knee_threshold:.0%}，{advice}",
                f"边际增益序列 {', '.join(f'{g * 100:+.1f}pp' for _, g in gains)}",
            ))
        else:
            span = max(means) - min(means)
            if mde is not None and span < mde:
                findings.append(Finding(
                    "warn",
                    f"曲线平坦：全档最大差距仅 {_pct(span)}，低于 MDE（{_pct(mde)}）"
                    f"——{axis_unit}在该范围无明显收益",
                    f"min {_pct(min(means))} / max {_pct(max(means))}（{main}）",
                ))
            else:
                findings.append(Finding(
                    "info",
                    f"未出现拐点：各档边际增益均 ≥ {exp.knee_threshold:.0%}，"
                    f"曲线未饱和，可考虑继续加档",
                    f"边际增益序列 {', '.join(f'{g * 100:+.1f}pp' for _, g in gains)}",
                ))

    # ---- oracle 差距（H4：瓶颈在池还是在检索器）
    oracle = [_mean(cm, "semantic") for cm in full if cm.retriever == "oracle"]
    measured = [_mean(cm, "semantic") for cm in full if cm.retriever != "oracle"]
    oracle = [m for m in oracle if m is not None]
    measured = [m for m in measured if m is not None]
    if oracle and measured:
        gap = max(oracle) - max(measured)
        if gap > exp.oracle_gap_threshold:
            findings.append(Finding(
                "warn",
                f"oracle 上界比实测检索高 {_pct(gap)}——瓶颈在检索器而非池（H4 支持）；"
                "建议先改进检索（更好检索器/更大 k），扩池收益有限",
                f"oracle 最佳 {_pct(max(oracle))} vs 实测最佳 {_pct(max(measured))}",
            ))
        else:
            findings.append(Finding(
                "good",
                f"oracle 与实测检索差距仅 {_pct(gap)}——检索器判别力已够，"
                "质量提升主要靠扩池/扩示例（H4 不支持）",
                f"oracle 最佳 {_pct(max(oracle))} vs 实测最佳 {_pct(max(measured))}",
            ))

    # ---- 校验失败聚类（最常见的一类）
    lark_vals = [_mean(cm, "lark") for cm in full]
    light_vals = [_mean(cm, "light") for cm in full]
    lark_vals = [v for v in lark_vals if v is not None]
    light_vals = [v for v in light_vals if v is not None]
    if lark_vals and min(lark_vals) < 1.0:
        findings.append(Finding(
            "warn",
            f"语法解析未满 100%（最低 {_pct(min(lark_vals))}）——到失败分类看 lark 失败明细",
        ))
    if light_vals and min(light_vals) < 1.0:
        findings.append(Finding(
            "info",
            f"light 静态检查最低 {_pct(min(light_vals))}——以失败分类中的聚类为准，"
            "常见修法：prompt 强调「变量先声明再用 / 只用本体算子」",
        ))

    # ---- 稳定性与失败计数
    alerts = [cm.condition_id for cm in full if any(s.alert for s in cm.stats.values())]
    if alerts:
        findings.append(Finding(
            "warn",
            f"{len(alerts)} 个条件 std 超 3pp：{', '.join(alerts[:3])}"
            + ("…" if len(alerts) > 3 else "") + "——建议重跑或加重复",
        ))
    gen_errors = sum(cm.n_gen_error for cm in full)
    judge_errors = sum(cm.n_judge_error for cm in full)
    if gen_errors:
        findings.append(Finding(
            "bad" if gen_errors > len(full) else "warn",
            f"生成失败 {gen_errors} 条——用 run 页「重试失败」补上，token 只花在失败条",
        ))
    if judge_errors:
        findings.append(Finding(
            "warn",
            f"judge 失败 {judge_errors} 条——「重判 judge（仅失败）」可补齐",
        ))

    # ---- judge 可信度（人工抽检一致率）
    if review_total >= 5:
        rate = review_agree / review_total
        if rate >= 0.9:
            findings.append(Finding(
                "good",
                f"judge 一致率 {rate:.0%}（{review_agree}/{review_total}）——判分可信",
            ))
        elif rate < 0.8:
            findings.append(Finding(
                "warn",
                f"judge 一致率仅 {rate:.0%}（{review_agree}/{review_total}）——"
                "语义等价率绝对值可信度有限，先校准 judge（换模型/改 judge prompt）再读结论",
            ))
        else:
            findings.append(Finding(
                "info",
                f"judge 一致率 {rate:.0%}（{review_agree}/{review_total}）——基本可用，"
                "建议继续抽检到 30+ 条再定置信",
            ))
        # 交叉验证：校准后一致率未达 90% 时，light 通过率是保守下界
        if rate < 0.9:
            light_vals = [_mean(cm, "light") for cm in full]
            light_vals = [v for v in light_vals if v is not None]
            if light_vals:
                findings.append(Finding(
                    "info",
                    f"judge 一致率未达 90%：light 静态检查通过率"
                    f"（{_pct(statistics.fmean(light_vals))}）可作语义等价率的保守下界对照",
                    "类型误用/多余约束类分歧时，light 口径比 judge 更严",
                ))
    else:
        findings.append(Finding(
            "info",
            "人工抽检不足 5 条——judge 可信度未校准；建议先在 review 队列判一批",
        ))

    # ---- 单次运行提示
    if exp.repeats == 1:
        findings.append(Finding(
            "info",
            "单次运行（repeats=1）：无重复 std，稳定性/重采样一致性结论不可用",
        ))
    return findings


def review_agreement(store, run_id: str, reviews: dict) -> tuple[int, int]:
    """人工判定 vs judge 的一致数/总数（映射：correct≈equivalent、acceptable≈minor、
    wrong≈different；undecidable 不参与）。"""
    mapping = {"correct": "equivalent", "acceptable": "minor", "wrong": "different"}
    rows = store._conn.execute(
        "SELECT g.gen_id, j.llm_verdict FROM generations g"
        " JOIN judgments j ON j.gen_id=g.gen_id WHERE g.run_id=?", (run_id,),
    ).fetchall()
    verdict = {r["gen_id"]: r["llm_verdict"] for r in rows}
    agree = total = 0
    for gen_id, rev in reviews.items():
        mapped = mapping.get(rev["human_verdict"])
        if mapped and verdict.get(gen_id):
            total += 1
            agree += mapped == verdict[gen_id]
    return agree, total


def top_finding(metrics: list[ConditionMetrics], exp: ExperimentConfig,
                mde: float | None) -> str:
    """一句话解读（汇总表/运行列表用）：取第一条非 info 的 Finding，否则第一条。"""
    findings = interpret_run(metrics, exp, mde)
    for f in findings:
        if f.level != "info":
            return f.text
    return findings[0].text if findings else "—"
