import unittest

from kelab import retrieve
from kelab.datasets import Item


def doc(item_id: str, question: str, ops: list[str] | None = None) -> Item:
    return Item(item_id=item_id, question=question, gold_ke="ke",
                gold_operators=ops or [])


POOL = [
    doc("d1", "双曲线的焦距是多少", ["focal_length", "get_expression"]),
    doc("d2", "椭圆的离心率取值范围", ["eccentricity", "range"]),
    doc("d3", "抛物线上点到直线距离最短的坐标", ["locate", "point_line_distance", "when_min"]),
    doc("d4", "双曲线渐近线方程", ["asymptote", "get_expression"]),
]


class Bm25Test(unittest.TestCase):
    def test_relevant_doc_ranks_first(self):
        query = doc("q", "双曲线焦距")
        ranked = retrieve._retrieve_bm25(query, POOL)
        self.assertEqual("d1", ranked[0].item_id)
        self.assertEqual("d4", ranked[1].item_id)  # 共享"双曲线"

    def test_unrelated_query_still_returns_all(self):
        query = doc("q", "圆切线")
        ranked = retrieve._retrieve_bm25(query, POOL)
        self.assertEqual(len(POOL), len(ranked))

    def test_idf_prefers_rare_shared_term(self):
        # "渐近线"只在 d4 出现，应把 d4 排第一
        query = doc("q", "渐近线")
        ranked = retrieve._retrieve_bm25(query, POOL)
        self.assertEqual("d4", ranked[0].item_id)


class OracleTest(unittest.TestCase):
    def test_same_operator_set_ranks_first(self):
        query = doc("q", "任意题", ops=["eccentricity", "range"])
        ranked = retrieve._retrieve_oracle(query, POOL)
        self.assertEqual("d2", ranked[0].item_id)

    def test_partial_overlap_beats_none(self):
        query = doc("q", "任意题", ops=["focal_length", "when_min"])
        ranked = retrieve._retrieve_oracle(query, POOL)
        self.assertIn(ranked[0].item_id, {"d1", "d3"})
        self.assertNotEqual("d2", ranked[0].item_id)


class RandomTest(unittest.TestCase):
    def test_same_seed_same_result(self):
        query = doc("q", "题")
        a = retrieve._retrieve_random(query, POOL, seed=42)
        b = retrieve._retrieve_random(query, POOL, seed=42)
        self.assertEqual([i.item_id for i in a], [i.item_id for i in b])

    def test_different_seed_usually_differs(self):
        query = doc("q", "题")
        orders = {
            tuple(i.item_id for i in retrieve._retrieve_random(query, POOL, seed=s))
            for s in range(5)
        }
        self.assertGreater(len(orders), 1)


class RrfTest(unittest.TestCase):
    def test_top_in_both_wins(self):
        r1 = [doc("a", ""), doc("b", ""), doc("c", "")]
        r2 = [doc("b", ""), doc("a", ""), doc("d", "")]
        fused = retrieve.rrf_fuse([r1, r2])
        self.assertIn(fused[0].item_id, {"a", "b"})
        self.assertEqual("d", fused[-2].item_id if fused[-1].item_id == "c" else fused[-1].item_id)


class RetrieveEntryTest(unittest.TestCase):
    def test_k0_returns_empty(self):
        result = retrieve.retrieve("bm25", doc("q", "题"), POOL, k=0, seed=1)
        self.assertEqual([], result.items)
        self.assertIsNone(result.warning)

    def test_k_larger_than_pool_fallback_with_warning(self):
        result = retrieve.retrieve("bm25", doc("q", "题"), POOL, k=99, seed=1)
        self.assertEqual(len(POOL), len(result.items))
        self.assertIn("pool_smaller_than_k", result.warning)

    def test_top_k_size(self):
        result = retrieve.retrieve("bm25", doc("q", "双曲线焦距"), POOL, k=2, seed=1)
        self.assertEqual(2, len(result.items))
        self.assertEqual("d1", result.items[0].item_id)

    def test_unknown_retriever_raises(self):
        with self.assertRaises(ValueError):
            retrieve.retrieve("不存在", doc("q", "题"), POOL, k=1, seed=1)


if __name__ == "__main__":
    unittest.main()
