"""统计层（协议 §7.4/7.5）：95% CI、配对比较、k* 操作性定义、δ 敏感性。

口径：
- 比例类指标（语法通过率等）→ Wilson 95% CI
- 分数类指标（语义等价率 0/0.5/1）→ 逐题得分的 t 95% CI
- 跨 k 比较 → 同题配对差值 CI + McNemar（卡方近似）
"""
from __future__ import annotations

import math
import statistics

# 双侧 95% 的 t 临界值表（自由度 → t）
_T_CRIT = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 14: 2.145, 16: 2.120,
    18: 2.101, 20: 2.086, 24: 2.064, 28: 2.048, 30: 2.042,
}


def t_crit_95(df: int) -> float:
    if df <= 0:
        return float("nan")
    if df in _T_CRIT:
        return _T_CRIT[df]
    if df > 30:
        return 1.96
    return _T_CRIT[min(_T_CRIT, key=lambda k: abs(k - df))]


def mean_ci(
    values: list[float | None],
    clamp: tuple[float, float] | None = None,
) -> tuple[float | None, float | None, float | None]:
    """逐题得分序列的均值与 t 95% CI。返回 (mean, lo, hi)；n<2 → CI 为 None。

    clamp：比例指标传 (0.0, 1.0)——t 区间对称外推会越界（如 [78.5%, 101.5%]），
    截断到合法值域；配对差值等可为负的指标不要传（差值域 [-1,1]）。
    """
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return None, None, None
    m = statistics.fmean(vals)
    if n < 2:
        return m, None, None
    half = t_crit_95(n - 1) * statistics.stdev(vals) / math.sqrt(n)
    lo, hi = m - half, m + half
    if clamp:
        lo = max(clamp[0], lo)
        hi = min(clamp[1], hi)
    return m, lo, hi


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None, float | None]:
    """二分类比例的 Wilson 95% CI。返回 (p, lo, hi)。"""
    if n == 0:
        return None, None, None
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, center - half), min(1.0, center + half)


def paired_diff_ci(
    a: list[float | None], b: list[float | None]
) -> tuple[float | None, float | None, float | None, int]:
    """同题配对差值（a − b）的均值 + 95% CI。返回 (mean_diff, lo, hi, n_pairs)。"""
    diffs = [x - y for x, y in zip(a, b) if x is not None and y is not None]
    if not diffs:
        return None, None, None, 0
    m, lo, hi = mean_ci(diffs)
    return m, lo, hi, len(diffs)


def mcnemar_chi2(b: int, c: int) -> float | None:
    """McNemar 卡方（带连续性校正；df=1，>3.84 即 p<0.05）。
    b=仅 A 条件等价的题数，c=仅 B 条件等价的题数。b+c<10 返回 None（检验力不足）。"""
    if b + c < 10:
        return None
    return (abs(b - c) - 1) ** 2 / (b + c)


def knee_two_gains(xs: list[float], means: list[float], delta: float) -> float | None:
    """协议 §7.5 拐点第一步：从某 k 起，后续【两个相邻档位】的边际提升均 < δ。
    返回该 k（即拐点前最后一档的下一档=进入平台期的档位）；不满足返回 None。"""
    eps = 1e-9  # 浮点误差容忍：恰等于 δ 不算低于
    gains = [(xs[i], means[i] - means[i - 1]) for i in range(1, len(xs))]
    for i in range(len(gains) - 1):
        if gains[i][1] < delta - eps and gains[i + 1][1] < delta - eps:
            return gains[i][0]
    return None


def knee_sensitivity(
    xs: list[float], means: list[float], deltas=(0.02, 0.03, 0.05, 0.08, 0.10)
) -> list[tuple[float, float | None]]:
    """δ 敏感性分析：同一曲线在不同 δ 下的 k*（协议：未预登记 δ 时只报此表）。"""
    return [(d, knee_two_gains(xs, means, d)) for d in deltas]
