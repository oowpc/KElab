import unittest

from kelab import datasets
from kelab.datasets import DataError, Item

from tests.helpers import TESTSET_XLSX, load_conic


def make_items(n: int) -> list[Item]:
    return [
        Item(item_id=f"q{i:03d}", question=f"题目{i}", gold_ke=f"ke{i}",
             gold_operators=[f"op{i}"])
        for i in range(n)
    ]


class ImportXlsxTest(unittest.TestCase):
    def test_import_real_xlsx(self):
        items = datasets.import_xlsx(TESTSET_XLSX)
        self.assertEqual(20, len(items))
        first = items[0]
        self.assertEqual("conic10K-005", first.item_id)
        self.assertIn("椭圆", first.question)
        # 空白归一化：多行 gold KE 变单行
        self.assertNotIn("\n", first.gold_ke)
        self.assertNotIn("  ", first.gold_ke)
        self.assertTrue(first.gold_ke.startswith("output = {"))
        self.assertEqual("train:686", first.source_id)

    def test_missing_file_raises(self):
        with self.assertRaises(DataError):
            datasets.import_xlsx("不存在的文件.xlsx")


class LooPoolTest(unittest.TestCase):
    def test_each_pool_excludes_self(self):
        items = make_items(20)
        pools = datasets.loo_pools(items)
        self.assertEqual(20, len(pools))
        for item in items:
            pool = pools[item.item_id]
            self.assertEqual(19, len(pool))
            self.assertNotIn(item.item_id, {p.item_id for p in pool})


class NestedPrefixTest(unittest.TestCase):
    def test_nested_inclusion(self):
        items = make_items(10)
        p5 = datasets.nested_prefix(items, 5, seed=1, item_id="q000")
        p8 = datasets.nested_prefix(items, 8, seed=1, item_id="q000")
        p10 = datasets.nested_prefix(items, 10, seed=1, item_id="q000")
        ids5 = [p.item_id for p in p5]
        ids8 = [p.item_id for p in p8]
        self.assertEqual(ids5, ids8[:5])  # 只增不减，严格前缀
        self.assertEqual(ids8, [p.item_id for p in p10][:8])

    def test_same_seed_same_order(self):
        items = make_items(10)
        a = datasets.nested_prefix(items, 8, seed=7, item_id="q000")
        b = datasets.nested_prefix(items, 8, seed=7, item_id="q000")
        self.assertEqual([p.item_id for p in a], [p.item_id for p in b])

    def test_pool_shortage_fails_fast(self):
        with self.assertRaises(DataError):
            datasets.nested_prefix(make_items(3), 5, seed=1, item_id="q000")


class PoolSubsetTest(unittest.TestCase):
    def test_nested_false_independent_resampling(self):
        items = make_items(10)
        # seed=2 已验证档间不互含（独立采样有约 22% 偶然互含概率，固定种子保证可复现）
        p5 = datasets.pool_subset(items, 5, seed=2, item_id="q000", nested=False)
        p8 = datasets.pool_subset(items, 8, seed=2, item_id="q000", nested=False)
        self.assertEqual(5, len(p5))
        self.assertEqual(8, len(p8))
        # 独立重采样：档间无包含关系（允许偶然包含，但大概率不全包含）
        self.assertFalse(set(i.item_id for i in p5) <= set(i.item_id for i in p8))
        # 同参数确定性
        again = datasets.pool_subset(items, 5, seed=2, item_id="q000", nested=False)
        self.assertEqual([i.item_id for i in p5], [i.item_id for i in again])

    def test_nested_true_delegates_to_prefix(self):
        items = make_items(10)
        a = datasets.pool_subset(items, 5, seed=1, item_id="q000", nested=True)
        b = datasets.nested_prefix(items, 5, seed=1, item_id="q000")
        self.assertEqual([i.item_id for i in a], [i.item_id for i in b])

    def test_nested_false_also_fails_fast(self):
        with self.assertRaises(DataError):
            datasets.pool_subset(make_items(3), 5, seed=1, item_id="q000", nested=False)


class LeakageTest(unittest.TestCase):
    def test_clean_loo_passes(self):
        items = make_items(5)
        report = datasets.check_leakage(items, datasets.loo_pools(items), "loo")
        self.assertTrue(report["passed"])
        self.assertEqual([], report["hits"])

    def test_self_in_pool_caught(self):
        items = make_items(5)
        pools = datasets.loo_pools(items)
        pools["q000"] = pools["q000"] + [items[0]]
        report = datasets.check_leakage(items, pools, "loo")
        self.assertFalse(report["passed"])
        self.assertEqual("self_in_pool", report["hits"][0]["kind"])

    def test_fixed_pool_id_overlap_caught(self):
        testset = make_items(3)
        pool = [Item(item_id="q000", question="另一道题", gold_ke="ke")]
        report = datasets.check_leakage(testset, pool, "fixed")
        self.assertFalse(report["passed"])
        self.assertEqual("id_overlap", report["hits"][0]["kind"])

    def test_fixed_pool_question_overlap_caught(self):
        testset = make_items(3)
        pool = [Item(item_id="p100", question="题 目 1", gold_ke="ke")]  # 空白差异应被归一化
        report = datasets.check_leakage(testset, pool, "fixed")
        self.assertFalse(report["passed"])
        self.assertEqual("question_overlap", report["hits"][0]["kind"])

    def test_fixed_pool_clean_passes(self):
        testset = make_items(3)
        pool = [Item(item_id=f"p{i}", question=f"完全不同的{i}", gold_ke="ke") for i in range(5)]
        report = datasets.check_leakage(testset, pool, "fixed")
        self.assertTrue(report["passed"])


class JsonlRoundTripTest(unittest.TestCase):
    def test_write_read_roundtrip(self):
        import tempfile
        from pathlib import Path

        items = make_items(3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.jsonl"
            datasets.write_jsonl(items, path)
            back = datasets.read_jsonl(path)
        self.assertEqual([i.item_id for i in items], [i.item_id for i in back])
        self.assertEqual(items[0].gold_operators, back[0].gold_operators)


class GoldOperatorsAttachTest(unittest.TestCase):
    def test_attach_fails_fast_on_bad_gold(self):
        domain, _, ontology = load_conic()
        bad = [Item(item_id="bad1", question="题", gold_ke="这不是 KE")]
        with self.assertRaises(DataError):
            datasets.attach_gold_operators(bad, domain.grammar_path, ontology)


if __name__ == "__main__":
    unittest.main()
