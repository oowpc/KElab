import unittest

from kelab import stats


class MeanCiTest(unittest.TestCase):
    def test_basic(self):
        import statistics

        vals = [0.5, 0.6, 0.7, 0.8, 0.9] * 4
        m, lo, hi = stats.mean_ci(vals)
        self.assertAlmostEqual(0.7, m)
        self.assertLess(lo, m)
        self.assertGreater(hi, m)
        expected_half = stats.t_crit_95(19) * statistics.stdev(vals) / (20 ** 0.5)
        self.assertAlmostEqual((hi - lo) / 2, expected_half, places=6)

    def test_single_value_no_ci(self):
        m, lo, hi = stats.mean_ci([0.8])
        self.assertEqual(0.8, m)
        self.assertIsNone(lo)

    def test_empty(self):
        self.assertEqual((None, None, None), stats.mean_ci([]))


class WilsonTest(unittest.TestCase):
    def test_bounds(self):
        p, lo, hi = stats.wilson_ci(90, 100)
        self.assertAlmostEqual(0.9, p)
        self.assertGreater(lo, 0.8)
        self.assertLess(hi, 0.96)

    def test_zero_n(self):
        self.assertEqual((None, None, None), stats.wilson_ci(0, 0))


class PairedTest(unittest.TestCase):
    def test_paired_diff(self):
        a = [0.5, 0.6, 0.7, 0.8, 0.9] * 4
        b = [0.6, 0.7, 0.8, 0.9, 1.0] * 4
        d, lo, hi, n = stats.paired_diff_ci(b, a)
        self.assertAlmostEqual(0.1, d)
        self.assertEqual(20, n)
        self.assertGreater(lo, 0)

    def test_mcnemar(self):
        self.assertIsNone(stats.mcnemar_chi2(3, 2))  # b+c<10 检验力不足
        self.assertAlmostEqual((abs(15 - 2) - 1) ** 2 / 17, stats.mcnemar_chi2(15, 2))


class KneeTwoGainsTest(unittest.TestCase):
    def test_two_consecutive_below_delta(self):
        xs = [0, 1, 2, 3, 5, 8]
        means = [0.5, 0.7, 0.78, 0.83, 0.84, 0.845]
        # 增益: +20,+8,+5,+1,+0.5pp —— 连续两档 <5% 首次出现在 k=3（3→5:+1, 5→8:+0.5）
        self.assertEqual(5, stats.knee_two_gains(xs, means, 0.05))

    def test_no_knee_when_gains_keep_large(self):
        xs = [0, 1, 2, 3]
        means = [0.5, 0.7, 0.9, 1.0]
        self.assertIsNone(stats.knee_two_gains(xs, means, 0.05))

    def test_single_gain_below_not_enough(self):
        xs = [0, 1, 2, 3, 5]
        means = [0.5, 0.7, 0.71, 0.8, 0.85]
        # 仅 k=2 一档 <5%，下一档又 +9pp → 协议定义下无拐点
        self.assertIsNone(stats.knee_two_gains(xs, means, 0.05))

    def test_sensitivity_table(self):
        xs = [0, 1, 2, 3, 5]
        means = [0.5, 0.7, 0.78, 0.83, 0.845]
        # 增益：+20pp,+8pp,+5pp(恰等于阈值不算),+1.5pp
        sens = stats.knee_sensitivity(xs, means, deltas=(0.02, 0.05, 0.10))
        self.assertEqual(3, len(sens))
        self.assertIsNone(sens[0][1])   # δ=2%：无连续两档 <2%
        self.assertIsNone(sens[1][1])   # δ=5%：仅末档 1.5pp 一档 <5%，不够「连续两档」
        self.assertEqual(2, sens[2][1])  # δ=10%：k=2 起 +8pp,+5pp 均 <10%


if __name__ == "__main__":
    unittest.main()
