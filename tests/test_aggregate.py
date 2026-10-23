import unittest

from kelab.aggregate import detect_knee, marginal_gains


class MarginalGainsTest(unittest.TestCase):
    def test_basic(self):
        # 用二进制可精确表示的数，避免浮点误差
        xs = [0, 1, 2, 3]
        means = [0.5, 0.75, 0.875, 0.9375]
        self.assertEqual(
            [(1, 0.25), (2, 0.125), (3, 0.0625)],
            marginal_gains(xs, means),
        )

    def test_empty_for_single_point(self):
        self.assertEqual([], marginal_gains([1], [0.5]))


class DetectKneeTest(unittest.TestCase):
    def test_knee_is_first_gain_below_threshold(self):
        xs = [0, 1, 2, 3, 5]
        means = [0.5, 0.75, 0.875, 0.9375, 0.96875]
        # 增益: +25pp, +12.5pp, +6.25pp(≥5pp), +3.125pp(<5pp) → 拐点在 5
        self.assertEqual(5, detect_knee(xs, means, threshold=0.05))

    def test_no_knee_returns_none(self):
        xs = [0, 1, 2]
        means = [0.50, 0.70, 0.90]
        self.assertIsNone(detect_knee(xs, means, threshold=0.05))

    def test_first_step_below_threshold(self):
        xs = [0, 1, 2]
        means = [0.50, 0.51, 0.52]
        self.assertEqual(1, detect_knee(xs, means, threshold=0.05))

    def test_negative_gain_is_knee(self):
        xs = [0, 1, 2]
        means = [0.50, 0.40, 0.45]
        self.assertEqual(1, detect_knee(xs, means, threshold=0.05))


if __name__ == "__main__":
    unittest.main()
