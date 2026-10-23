"""数据流水线 / gold 流水线 / 预登记一致性测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from kelab import data_pipeline, datasets, gold_pipeline, prereg
from kelab.adapters.base import DomainPack, Item
from kelab.adapters.conic import ConicAdapter
from kelab.config import ExperimentConfig
from kelab.tasks.nl2ke import NL2KETask

from tests.helpers import load_conic

GOOD_KE = ('output = { focal_length(C) | C: Hyperbola, '
           'get_expression(C) = sympy_expression("Eq(x**2 - y**2, 1)") };')
GOOD_KE_2 = ('output = { eccentricity(C) | C: Ellipse, '
             'get_expression(C) = sympy_expression("Eq(x**2/4 + y**2, 1)") };')


def item(i: str, q: str, ke: str = GOOD_KE) -> Item:
    return Item(item_id=i, question=q, gold_ke=ke)


def make_pack() -> DomainPack:
    domain, template, ontology = load_conic()
    return DomainPack(domain=domain, template=template, ontology=ontology,
                      grammar_md5="x", adapter=ConicAdapter(), task=NL2KETask())


def make_exp(**overrides) -> ExperimentConfig:
    values = dict(
        name="main-k-frozen", domain="conic", kind="k",
        testset=Path("data/derived/testset.jsonl"), pool_strategy="loo",
        ks=[0, 1, 3], retrievers=["frozen"], repeats=3, seeds=[11, 22, 33],
    )
    values.update(overrides)
    return ExperimentConfig(**values)


class DataPipelineTest(unittest.TestCase):
    def test_item_fingerprint_normalizes_whitespace(self):
        a = item("a", "双曲线 焦距")
        b = item("b", "双曲线焦距")
        self.assertEqual(data_pipeline.item_fingerprint(a),
                         data_pipeline.item_fingerprint(b))

    def test_partition_fingerprint_order_independent(self):
        a, b = item("a", "题甲"), item("b", "题乙")
        self.assertEqual(data_pipeline.partition_fingerprint([a, b]),
                         data_pipeline.partition_fingerprint([b, a]))

    def test_assign_families_groups_near_duplicates(self):
        items = [
            item("a", "已知双曲线 x²-y²=1，求焦距"),
            item("b", "已知双曲线 x²-y²=1，求其焦距"),   # 近重复 → 同族
            item("c", "求椭圆 x²/4+y²=1 的离心率取值范围"),
        ]
        fam = data_pipeline.assign_families(items)
        self.assertEqual(fam["a"], fam["b"])
        self.assertNotEqual(fam["a"], fam["c"])

    def test_manifest_partitions_and_cross_family(self):
        # 人为构造跨分区同族：a/b 近重复却被切到不同分区
        testset = [item("a", "已知双曲线 x²-y²=1，求焦距")]
        pool = [item("b", "已知双曲线 x²-y²=1，求其焦距"),
                item("c", "求椭圆离心率", GOOD_KE_2)]
        manifest = data_pipeline.build_manifest(
            {"testset": testset, "pool": pool}, seed=42)
        self.assertEqual(1, manifest["partitions"]["testset"]["count"])
        self.assertEqual(2, manifest["partitions"]["pool"]["count"])
        self.assertTrue(manifest["cross_partition_families"])  # 跨分区泄露通道被抓

    def test_manifest_clean_split_no_cross(self):
        items = [item("a", "题甲"), item("b", "完全不同的题乙", GOOD_KE_2)]
        manifest = data_pipeline.build_manifest(
            {"testset": items[:1], "pool": items[1:]}, seed=42)
        self.assertEqual([], manifest["cross_partition_families"])

    def test_manifest_uses_adapter_fingerprint(self):
        """适配器指纹不是死代码：manifest 走 adapter.fingerprint。"""

        class StubAdapter:
            name = "stub"

            def fingerprint(self, it):
                return f"fp-{it.item_id}"

        manifest = data_pipeline.build_manifest(
            {"testset": [item("a", "题")]}, adapter=StubAdapter())
        self.assertEqual({"a": "fp-a"}, manifest["partitions"]["testset"]["items"])
        import hashlib

        expected = hashlib.sha256("fp-a".encode("utf-8")).hexdigest()[:12]
        self.assertEqual(expected, manifest["partitions"]["testset"]["fingerprint"])

    def test_write_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = data_pipeline.write_manifest(
                {"version": 1, "total": 2}, Path(tmp) / "m.json")
            back = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(2, back["total"])


class GoldPipelineTest(unittest.TestCase):
    def setUp(self):
        self.pack = make_pack()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name)

    def _candidates(self, kess: dict[str, str] | None = None) -> list[gold_pipeline.GoldCandidate]:
        rows = kess or {"g1": GOOD_KE, "g2": GOOD_KE_2}
        items = [item(i, f"题 {i}", ke) for i, ke in rows.items()]
        return gold_pipeline.validate_candidates(items, self.pack)

    def _write_review(self, verdicts: dict[str, str],
                      kess: dict[str, str] | None = None) -> Path:
        cands = self._candidates(kess)
        path = gold_pipeline.export_review(cands, self.out_dir)
        # 模拟人工复核：按 verdicts 填结论
        lines = []
        for row in gold_pipeline.load_review(path):
            row["human_verdict"] = verdicts.get(row["item_id"], "")
            lines.append(row)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
                        encoding="utf-8")
        return path

    def test_validate_candidates_marks_bad(self):
        cands = self._candidates({"ok": GOOD_KE, "bad": "不是 KE"})
        by_id = {c.item.item_id: c for c in cands}
        self.assertTrue(by_id["ok"].auto_ok)
        self.assertFalse(by_id["bad"].auto_ok)

    def test_freeze_happy_path(self):
        review = self._write_review({"g1": "pass", "g2": "pass"})
        frozen_path = gold_pipeline.freeze(review, self.pack, self.out_dir, actor="王")
        frozen = datasets.read_jsonl(frozen_path)
        self.assertEqual(["g1", "g2"], [it.item_id for it in frozen])
        # 冻结条目已带算子与复核 metadata
        self.assertIn("focal_length", frozen[0].gold_operators)
        self.assertEqual("pass", frozen[0].metadata["gold_review"]["verdict"])
        manifest = json.loads(
            (self.out_dir / gold_pipeline.MANIFEST_FILE).read_text(encoding="utf-8"))
        self.assertEqual({"in": 2, "frozen": 2, "rejected": 0}, manifest["counts"])
        self.assertEqual("王", manifest["actor"])

    def test_freeze_refuses_auto_failure(self):
        review = self._write_review({"ok": "pass", "bad": "pass"},
                                    kess={"ok": GOOD_KE, "bad": "不是 KE"})
        with self.assertRaises(datasets.DataError) as caught:
            gold_pipeline.freeze(review, self.pack, self.out_dir, actor="王")
        self.assertIn("规则校验未过", str(caught.exception))
        self.assertIn("bad", str(caught.exception))

    def test_freeze_refuses_pending(self):
        review = self._write_review({"g1": "pass"})  # g2 未填
        with self.assertRaises(datasets.DataError) as caught:
            gold_pipeline.freeze(review, self.pack, self.out_dir, actor="王")
        self.assertIn("缺人工结论", str(caught.exception))

    def test_freeze_allow_unreviewed(self):
        review = self._write_review({"g1": "pass"})
        frozen_path = gold_pipeline.freeze(review, self.pack, self.out_dir,
                                           actor="王", allow_unreviewed=True)
        frozen = datasets.read_jsonl(frozen_path)
        self.assertEqual(2, len(frozen))
        self.assertEqual("unreviewed", frozen[1].metadata["gold_review"]["verdict"])

    def test_freeze_applies_human_ke_edit(self):
        """复核人改 gold_ke（修正标注错误）后按新文本冻结。"""
        cands = self._candidates({"g1": "不是 KE"})
        path = gold_pipeline.export_review(cands, self.out_dir)
        row = gold_pipeline.load_review(path)[0]
        row["gold_ke"] = GOOD_KE          # 人工改正
        row["human_verdict"] = "pass"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        frozen_path = gold_pipeline.freeze(path, self.pack, self.out_dir, actor="王")
        frozen = datasets.read_jsonl(frozen_path)
        self.assertEqual(GOOD_KE, frozen[0].gold_ke)

    def test_freeze_reject_reduces_count(self):
        review = self._write_review({"g1": "pass", "g2": "fail"})
        gold_pipeline.freeze(review, self.pack, self.out_dir, actor="王")
        manifest = json.loads(
            (self.out_dir / gold_pipeline.MANIFEST_FILE).read_text(encoding="utf-8"))
        self.assertEqual({"in": 2, "frozen": 1, "rejected": 1}, manifest["counts"])

    def test_freeze_preserves_provenance(self):
        """冻结不丢溯源：source_id/dataset/note/metadata 随完整 item 键保留。"""
        items = [Item(item_id="g1", question="题", gold_ke=GOOD_KE,
                      source_id="conic10K-005", dataset="conic_10k", note="备注甲",
                      metadata={"answer": "{1,5}", "tags": ["#syntax"]})]
        cands = gold_pipeline.validate_candidates(items, self.pack)
        path = gold_pipeline.export_review(cands, self.out_dir)
        row = gold_pipeline.load_review(path)[0]
        row["human_verdict"] = "pass"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        frozen = datasets.read_jsonl(
            gold_pipeline.freeze(path, self.pack, self.out_dir, actor="王"))
        it = frozen[0]
        self.assertEqual("conic10K-005", it.source_id)
        self.assertEqual("conic_10k", it.dataset)
        self.assertEqual("备注甲", it.note)
        self.assertEqual("{1,5}", it.metadata["answer"])
        self.assertEqual(["#syntax"], it.metadata["tags"])
        self.assertEqual("pass", it.metadata["gold_review"]["verdict"])

    def test_row_to_item_legacy_flat_row(self):
        """旧版平铺复核行（无 item 键）仍可加载——向后兼容。"""
        row = {"item_id": "g1", "question": "题", "gold_ke": GOOD_KE,
               "human_verdict": "pass"}
        it = gold_pipeline.row_to_item(row)
        self.assertEqual("g1", it.item_id)
        self.assertEqual(GOOD_KE, it.gold_ke)

    def test_empty_gold_pending_annotation(self):
        """conic10k 扩题场景：gold_ke 为空 = 待标注，复核人填入后冻结通过。"""
        cands = gold_pipeline.validate_candidates(
            [item("g1", "题", "")], self.pack)
        self.assertFalse(cands[0].auto_ok)  # 空 gold 自动校验不过
        path = gold_pipeline.export_review(cands, self.out_dir)
        row = gold_pipeline.load_review(path)[0]
        row["gold_ke"] = GOOD_KE            # 标注人写入 gold KE
        row["human_verdict"] = "pass"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        frozen = datasets.read_jsonl(
            gold_pipeline.freeze(path, self.pack, self.out_dir, actor="王"))
        self.assertEqual(GOOD_KE, frozen[0].gold_ke)
        self.assertIn("focal_length", frozen[0].gold_operators)


class PreregConsistencyTest(unittest.TestCase):
    def _prereg(self, exp: ExperimentConfig, created: str = "2026-01-01T00:00:00+00:00") -> dict:
        return {
            "experiment": exp.name, "version": 1, "created_at": created,
            "config": {
                "ks": exp.ks, "ns": exp.ns, "fixed_k": exp.fixed_k,
                "retrievers": exp.retrievers, "repeats": exp.repeats,
                "seeds": exp.seeds[: exp.repeats], "pool_strategy": exp.pool_strategy,
                "testset": str(exp.testset), "dataset_md5": "abc123",
            },
        }

    def test_consistent_no_warnings(self):
        exp = make_exp()
        warnings = prereg.check_consistency(
            self._prereg(exp), exp, dataset_md5_now="abc123",
            run_started_at="2026-06-01T00:00:00+00:00")
        self.assertEqual([], warnings)

    def test_config_drift_warns(self):
        exp = make_exp()
        reg = self._prereg(exp)
        exp2 = make_exp(ks=[0, 1, 3, 5])  # 登记后改了 k 列表
        warnings = prereg.check_consistency(reg, exp2, dataset_md5_now="abc123")
        self.assertTrue(any("ks" in w for w in warnings))

    def test_dataset_drift_warns(self):
        exp = make_exp()
        warnings = prereg.check_consistency(
            self._prereg(exp), exp, dataset_md5_now="变了")
        self.assertTrue(any("dataset_md5" in w for w in warnings))

    def test_dataset_md5_skipped_when_unknown(self):
        exp = make_exp()
        warnings = prereg.check_consistency(
            self._prereg(exp), exp, dataset_md5_now=None)
        self.assertEqual([], warnings)

    def test_timing_violation_warns(self):
        exp = make_exp()
        # run 开始早于预登记 → 先看数后登记
        warnings = prereg.check_consistency(
            self._prereg(exp, created="2026-06-01T00:00:00+00:00"), exp,
            dataset_md5_now="abc123",
            run_started_at="2026-01-01T00:00:00+00:00")
        self.assertTrue(any("时序违规" in w for w in warnings))

    def test_timing_downgraded_when_earlier_version_predates_run(self):
        """v2 是 run 开始后的修订版，但 v1 核心参数一致且登记早于 run → 时序有效。"""
        exp = make_exp()
        v1 = self._prereg(exp, created="2026-01-01T00:00:00+00:00")
        v1["version"] = 1
        v2 = self._prereg(exp, created="2026-06-01T00:00:00+00:00")
        v2["version"] = 2
        warnings = prereg.check_consistency(
            v2, exp, dataset_md5_now="abc123",
            run_started_at="2026-02-01T00:00:00+00:00",
            history=[v1, v2])
        self.assertFalse(any("时序违规" in w for w in warnings))
        self.assertTrue(any("时序说明" in w and "v1" in w for w in warnings))

    def test_timing_violation_stands_when_core_changed(self):
        """旧版虽早但核心参数被改过 → 不适用降级，时序违规成立。"""
        exp = make_exp()
        v1 = self._prereg(exp, created="2026-01-01T00:00:00+00:00")
        v1["version"] = 1
        v1["delta"] = 0.10  # 核心参数不同
        v2 = self._prereg(exp, created="2026-06-01T00:00:00+00:00")
        v2["version"] = 2
        warnings = prereg.check_consistency(
            v2, exp, dataset_md5_now="abc123",
            run_started_at="2026-02-01T00:00:00+00:00",
            history=[v1, v2])
        self.assertTrue(any("时序违规" in w for w in warnings))

    def test_timing_violation_stands_when_dataset_differs(self):
        """旧版参数一致但数据版本对不上（且无法对质）→ 时序违规成立。"""
        exp = make_exp()
        v1 = self._prereg(exp, created="2026-01-01T00:00:00+00:00")
        v1["version"] = 1
        v1["config"]["dataset_md5"] = "别的数据指纹"
        v2 = self._prereg(exp, created="2026-06-01T00:00:00+00:00")
        v2["version"] = 2
        warnings = prereg.check_consistency(
            v2, exp, dataset_md5_now="abc123", dataset_file_md5_now="raw999",
            run_started_at="2026-02-01T00:00:00+00:00",
            history=[v1, v2])
        self.assertTrue(any("时序违规" in w for w in warnings))

    def test_timing_downgraded_via_legacy_raw_md5(self):
        """旧版用 32 位原始文件 md5（指纹统一前的算法）：与当前文件 md5 对质一致即可降级。"""
        exp = make_exp()
        v1 = self._prereg(exp, created="2026-01-01T00:00:00+00:00")
        v1["version"] = 1
        v1["config"]["dataset_md5"] = "5c24b69a42c6f0ca13950e890da48b1c"  # 旧算法
        v2 = self._prereg(exp, created="2026-06-01T00:00:00+00:00")
        v2["version"] = 2
        warnings = prereg.check_consistency(
            v2, exp, dataset_md5_now="abc123",
            dataset_file_md5_now="5c24b69a42c6f0ca13950e890da48b1c",
            run_started_at="2026-02-01T00:00:00+00:00",
            history=[v1, v2])
        self.assertFalse(any("时序违规" in w for w in warnings))
        self.assertTrue(any("时序说明" in w and "v1" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
