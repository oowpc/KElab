import unittest
from pathlib import Path

from kelab.aggregate import ConditionMetrics, MetricStat
from kelab.config import ExperimentConfig
from kelab.interpret import interpret_run, top_finding


def cm(cid, kind, axis, retriever, semantic, onto="full", gen_model=None,
       gen_err=0, judge_err=0, alert=False):
    m = ConditionMetrics(condition_id=cid, kind=kind,
                         k=axis if kind == "k" else 3,
                         n=axis if kind == "n" else 19,
                         retriever=retriever, onto=onto, gen_model=gen_model)
    m.stats["semantic"] = MetricStat(mean=semantic, std=0.01, alert=alert)
    m.stats["lark"] = MetricStat(mean=1.0, std=0.0)
    m.stats["light"] = MetricStat(mean=1.0, std=0.0)
    m.n_gen_error = gen_err
    m.n_judge_error = judge_err
    return m


def exp(kind="k", repeats=3):
    return ExperimentConfig(name="t", domain="d", kind=kind,
                            testset=Path("t.jsonl"), repeats=repeats,
                            seeds=[1, 2, 3])


class InterpretTest(unittest.TestCase):
    def test_knee_gives_stop_advice(self):
        # 增益 +20,+5,+1 → k*=5（增益在 k=5 首次 <5pp），性价比停在前一档 3
        metrics = [cm(f"k-bm25-k{k}", "k", k, "bm25", s)
                   for k, s in ((0, 0.5), (1, 0.7), (3, 0.75), (5, 0.76))]
        findings = interpret_run(metrics, exp(), 0.11)
        texts = "\n".join(f.text for f in findings)
        self.assertIn("k* = 5", texts)
        self.assertIn("性价比最优停在 3", texts)

    def test_flat_curve_warns_under_mde(self):
        # 每档增益恰好 +5pp（不触发拐点），总跨度 10pp < MDE 11pp → 平坦警告
        metrics = [cm(f"k-bm25-k{k}", "k", k, "bm25", s)
                   for k, s in ((0, 0.70), (1, 0.75), (3, 0.80))]
        findings = interpret_run(metrics, exp(), 0.11)
        self.assertTrue(any(f.level == "warn" and "曲线平坦" in f.text
                            for f in findings))

    def test_no_knee_unsaturated(self):
        metrics = [cm(f"k-bm25-k{k}", "k", k, "bm25", s)
                   for k, s in ((0, 0.3), (1, 0.5), (3, 0.7), (5, 0.9))]
        findings = interpret_run(metrics, exp(), 0.11)
        self.assertTrue(any("未出现拐点" in f.text and "继续加档" in f.text
                            for f in findings))

    def test_oracle_gap_h4(self):
        metrics = [cm("n-bm25-n10", "n", 10, "bm25", 0.60),
                   cm("n-oracle-n10", "n", 10, "oracle", 0.75)]
        findings = interpret_run(metrics, exp(kind="n"), 0.11)
        self.assertTrue(any("瓶颈在检索器" in f.text for f in findings))
        metrics2 = [cm("n-bm25-n10", "n", 10, "bm25", 0.74),
                    cm("n-oracle-n10", "n", 10, "oracle", 0.75)]
        findings2 = interpret_run(metrics2, exp(kind="n"), 0.11)
        self.assertTrue(any("H4 不支持" in f.text for f in findings2))

    def test_judge_agreement_levels(self):
        metrics = [cm("k-bm25-k0", "k", 0, "bm25", 0.8)]
        findings = interpret_run(metrics, exp(), 0.11,
                                 review_agree=9, review_total=10)
        self.assertTrue(any("判分可信" in f.text for f in findings))
        self.assertFalse(any("保守下界" in f.text for f in findings))  # 一致率 ≥90% 不出交叉验证
        findings = interpret_run(metrics, exp(), 0.11,
                                 review_agree=5, review_total=10)
        self.assertTrue(any("先校准 judge" in f.text for f in findings))

    def test_light_as_conservative_floor_when_uncalibrated(self):
        metrics = [cm("k-bm25-k0", "k", 0, "bm25", 0.8),
                   cm("k-bm25-k1", "k", 1, "bm25", 0.85)]
        for m in metrics:
            m.stats["light"] = MetricStat(mean=0.75, std=0.0)
        findings = interpret_run(metrics, exp(), 0.11,
                                 review_agree=12, review_total=14)  # 86% < 90%
        self.assertTrue(any("保守下界" in f.text and "75.0%" in f.text
                            for f in findings))

    def test_error_and_repeats_notes(self):
        metrics = [cm("k-bm25-k0", "k", 0, "bm25", 0.8, gen_err=3)]
        findings = interpret_run(metrics, exp(repeats=1), 0.11)
        texts = "\n".join(f.text for f in findings)
        self.assertIn("生成失败 3 条", texts)
        self.assertIn("单次运行（repeats=1）", texts)

    def test_top_finding_prefers_non_info(self):
        metrics = [cm(f"k-bm25-k{k}", "k", k, "bm25", s)
                   for k, s in ((0, 0.5), (1, 0.7), (3, 0.75), (5, 0.76))]
        text = top_finding(metrics, exp(), 0.11)
        self.assertIn("k* = 5", text)


if __name__ == "__main__":
    unittest.main()
