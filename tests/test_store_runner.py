import tempfile
import unittest
from pathlib import Path

from kelab import datasets, runner
from kelab.config import ExperimentConfig
from kelab.llm import ClientConfig
from kelab.store import Store

from tests.helpers import FakeChatHandler, MockServerMixin, load_conic

GOOD_KE = ('output = { focal_length(C) | C: Hyperbola, '
           'get_expression(C) = sympy_expression("Eq(x**2 - y**2, 1)") };')

ITEMS = [
    datasets.Item(item_id="t1", question="双曲线焦距题甲", gold_ke=GOOD_KE,
                  gold_operators=["focal_length", "get_expression", "sympy_expression"]),
    datasets.Item(item_id="t2", question="双曲线焦距题乙", gold_ke=GOOD_KE,
                  gold_operators=["focal_length", "get_expression", "sympy_expression"]),
]


def make_exp(**overrides) -> ExperimentConfig:
    values = dict(
        name="test-exp", domain="conic", kind="k",
        testset=Path("unused"), pool_strategy="loo",
        ks=[0, 1], retrievers=["bm25"], repeats=2, seeds=[11, 22],
    )
    values.update(overrides)
    return ExperimentConfig(**values)


class MiniExperimentTest(MockServerMixin):
    def make_tmpdir(self) -> str:
        # Windows 下 sqlite 文件句柄须先释放再删目录：
        # addCleanup 后进先出——先注册目录清理，store.close 会在它之前执行
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def build_resources(self, tmp: str, exp: ExperimentConfig) -> runner.RunResources:
        domain, template, ontology = load_conic()
        base_url = self.start_mock()
        gen = ClientConfig(base_url=base_url, api_key="k", model="gen-model",
                           temperature=0.0, timeout=10.0, max_retries=1)
        judge_client = ClientConfig(base_url=base_url, api_key="k", model="judge-model",
                                    temperature=0.0, timeout=10.0, max_retries=1)
        store = Store(Path(tmp) / "test.sqlite")
        self.addCleanup(store.close)
        return runner.RunResources(
            domain=domain, exp=exp, template=template, ontology=ontology,
            testset=ITEMS, pools=datasets.loo_pools(ITEMS),
            gen_clients={"default": gen}, judge_client=judge_client,
            store=store, run_id="test-run",
        )

    def test_full_run_rows_and_metrics(self):
        res = self.build_resources(self.make_tmpdir(), make_exp())
        stats = runner.run_experiment(res)
        # 2 条件 × 2 题 × 2 重复 = 8
        self.assertEqual(8, stats["ok"])
        self.assertEqual(0, stats["error"])
        rows = res.store.generation_join_rows("test-run", "k-bm25-k1")
        self.assertEqual(4, len(rows))
        for row in rows:
            self.assertEqual("ok", row["status"])
            self.assertEqual(1, row["lark_ok"])
            self.assertEqual(1, row["light_ok"])
            self.assertEqual(1.0, row["exact_match"])  # mock 返回与 gold 相同
            self.assertEqual("equivalent", row["llm_verdict"])
            self.assertEqual(1.0, row["llm_score"])
            self.assertEqual(100, row["prompt_tokens"])
        # k=1 条件检索了 1 条示例
        self.assertEqual(1, len(__import__("json").loads(rows[0]["retrieved_ids"])))
        # k=0 条件无示例、无覆盖度
        rows0 = res.store.generation_join_rows("test-run", "k-bm25-k0")
        self.assertEqual(4, len(rows0))
        for row in rows0:
            self.assertEqual("[]", row["retrieved_ids"])
            self.assertIsNone(row["retrieval_coverage"])
        # judge token 消耗落库（成本核算）
        jrows = res.store._conn.execute(
            "SELECT judge_prompt_tokens, judge_completion_tokens FROM judgments"
        ).fetchall()
        self.assertEqual(8, len(jrows))
        for r in jrows:
            self.assertEqual(100, r["judge_prompt_tokens"])
            self.assertEqual(42, r["judge_completion_tokens"])

    def test_resume_only_fills_gaps(self):
        exp = make_exp()
        res = self.build_resources(self.make_tmpdir(), exp)
        runner.run_experiment(res)
        n_requests = len(FakeChatHandler.requests)
        stats = runner.run_experiment(res)  # 二次运行：全部跳过
        self.assertEqual(8, stats["skipped"])
        self.assertEqual(0, stats["ok"])
        self.assertEqual(n_requests, len(FakeChatHandler.requests))

    def test_api_error_recorded_not_raised(self):
        res = self.build_resources(self.make_tmpdir(), make_exp())
        FakeChatHandler.fail_on_marker = "题乙"  # start_mock 会重置，必须在建资源之后设置
        stats = runner.run_experiment(res)
        # t2 自身 4 条（2 条件 × 2 重复）全部失败；
        # t1 的 k=1 prompt 因 LOO 示例就是 t2（题干含"题乙"）同样被注入失败 2 条——
        # 侧面验证 few-shot 示例确实进入了 prompt；
        # 仅 t1 的 k=0（无示例）2 条成功
        self.assertEqual(2, stats["ok"])
        self.assertEqual(6, stats["error"])
        rows1 = res.store.generation_join_rows("test-run", "k-bm25-k1")
        for row in rows1:
            self.assertEqual("error", row["status"])  # k=1 两题全失败
            self.assertIn("injected failure", row["error"])
        rows0 = res.store.generation_join_rows("test-run", "k-bm25-k0")
        for row in rows0:
            # k=0 无示例：t1 成功；t2 自身题干就含标记，依然失败
            self.assertEqual("ok" if row["item_id"] == "t1" else "error", row["status"])

    def test_n_experiment_nested_pools_and_conditions(self):
        exp = make_exp(kind="n", ns=[1], fixed_k=1, retrievers=["random", "oracle"])
        res = self.build_resources(self.make_tmpdir(), exp)
        stats = runner.run_experiment(res)
        self.assertEqual(8, stats["ok"])
        conds = res.store.condition_rows("test-run")
        self.assertEqual({"n-random-n1", "n-oracle-n1"}, {c["condition_id"] for c in conds})
        for c in conds:
            self.assertEqual(1, c["k"])
            self.assertEqual(1, c["n"])

    def test_limit_items_pilot(self):
        # 试跑模式：limit_items=1 → 只跑 t1（2 条件 × 1 题 × 2 重复 = 4）
        exp = make_exp(limit_items=1)
        res = self.build_resources(self.make_tmpdir(), exp)
        stats = runner.run_experiment(res)
        self.assertEqual(4, stats["ok"])
        rows = res.store.generation_join_rows("test-run", "k-bm25-k1")
        self.assertTrue(all(r["item_id"] == "t1" for r in rows))

    def test_onto_modes_full_and_linked(self):
        # 本体注入对照：full + linked 双模式
        exp = make_exp(onto_modes=["full", "linked"])
        res = self.build_resources(self.make_tmpdir(), exp)
        stats = runner.run_experiment(res)
        # 2 k 档 × 2 模式 × 2 题 × 2 重复 = 16
        self.assertEqual(16, stats["ok"])
        conds = {c["condition_id"]: c for c in res.store.condition_rows("test-run")}
        self.assertIn("k-bm25-k1-linked", conds)
        self.assertEqual("linked", conds["k-bm25-k1-linked"]["onto"])
        self.assertEqual("full", conds["k-bm25-k1"]["onto"])
        # linked prompt 只含检索示例的算子并集（LOO 示例的 ops），不含全量本体其他算子
        linked_rows = res.store.generation_join_rows("test-run", "k-bm25-k1-linked")
        full_rows = res.store.generation_join_rows("test-run", "k-bm25-k1")
        self.assertTrue(linked_rows)
        self.assertNotIn("op asymptote", linked_rows[0]["prompt_text"])
        self.assertIn("op asymptote", full_rows[0]["prompt_text"])
        self.assertIn("op focal_length", linked_rows[0]["prompt_text"])  # 示例算子并集包含

    def test_onto_linking_gold(self):
        exp = make_exp(onto_modes=["linked"], onto_linking="gold")
        res = self.build_resources(self.make_tmpdir(), exp)
        runner.run_experiment(res)
        rows = res.store.generation_join_rows("test-run", "k-bm25-k1-linked")
        # gold 模式：子集=本题 gold 算子（含 get_expression/sympy_expression/focal_length）
        self.assertIn("op get_expression", rows[0]["prompt_text"])
        self.assertNotIn("op asymptote", rows[0]["prompt_text"])

    def test_frozen_n_experiment_respects_subset(self):
        """frozen + N 实验：序列只在当前 N 子集内取（N 的控制语义不被穿透）。"""
        exp = make_exp(kind="n", ns=[1], fixed_k=1, retrievers=["frozen"])
        res = self.build_resources(self.make_tmpdir(), exp)
        stats = runner.run_experiment(res)
        self.assertEqual(4, stats["ok"])  # 1 档 × 2 题 × 2 重复
        rows = res.store.generation_join_rows("test-run", "n-frozen-n1")
        for row in rows:
            ids = __import__("json").loads(row["retrieved_ids"])
            self.assertEqual(1, len(ids))  # N=1 子集内恰 1 条

    def test_frozen_k_experiment_shared_sequence(self):
        """frozen + k 实验：所有题共用同一套序列（剔除自身），k 档嵌套。"""
        exp = make_exp(ks=[1, 2], retrievers=["frozen"])
        res = self.build_resources(self.make_tmpdir(), exp)
        runner.run_experiment(res)
        import json as _json
        for rep in (0, 1):
            a = _json.loads(res.store.generation_join_rows(
                "test-run", "k-frozen-k2")[rep]["retrieved_ids"])
            b = _json.loads(res.store.generation_join_rows(
                "test-run", "k-frozen-k2")[1 - rep]["retrieved_ids"])
            # 两题各自剔除自身后，交集非空（共享同一全局序列）
            self.assertTrue(set(a) & set(b))

    def test_validation_crash_marks_error_row(self):
        """校验阶段意外异常 → 该行标 error（不留「ok 但无判分」脏行），且批次不中断。"""
        from unittest.mock import patch

        exp = make_exp()
        res = self.build_resources(self.make_tmpdir(), exp)
        with patch.object(runner.ke_validate, "validate_ke",
                          side_effect=RuntimeError("校验器炸了")):
            stats = runner.run_experiment(res)
        self.assertEqual(8, stats["error"])
        rows = res.store.generation_join_rows("test-run", "k-bm25-k0")
        self.assertTrue(all(r["status"] == "error" for r in rows))
        self.assertTrue(all("校验/判分异常" in (r["error"] or "") for r in rows))
        # 标 error 的行可用重试路径补跑（pending 已算完成，走 retry_errors 删除重跑）
        ok_rows = [r for r in rows if r["status"] == "ok"]
        self.assertEqual([], ok_rows)

    def test_gen_models_dimension(self):
        # 模型对照：gen_models 两个模型 → 条件翻倍，各用各的客户端
        exp = make_exp(gen_models=["gen-model", "gen-model-2"])
        domain, template, ontology = load_conic()
        base_url = self.start_mock()
        clients = {
            m: ClientConfig(base_url=base_url, api_key="k", model=m,
                            temperature=0.0, timeout=10.0, max_retries=1)
            for m in ("gen-model", "gen-model-2")
        }
        judge_client = ClientConfig(base_url=base_url, api_key="k", model="judge-model",
                                    temperature=0.0, timeout=10.0, max_retries=1)
        store = Store(Path(self.make_tmpdir()) / "t.sqlite")
        self.addCleanup(store.close)
        res = runner.RunResources(
            domain=domain, exp=exp, template=template, ontology=ontology,
            testset=ITEMS, pools=datasets.loo_pools(ITEMS),
            gen_clients=clients, judge_client=judge_client,
            store=store, run_id="test-run",
        )
        stats = runner.run_experiment(res)
        self.assertEqual(16, stats["ok"])  # 2 k × 2 模型 × 2 题 × 2 重复
        conds = {c["condition_id"] for c in res.store.condition_rows("test-run")}
        self.assertIn("k-bm25-k1-m-gen-model", conds)
        self.assertIn("k-bm25-k1-m-gen-model-2", conds)
        # 两个模型都真实被调用过
        models_called = {r["model"] for r in FakeChatHandler.requests if r["model"].startswith("gen")}
        self.assertEqual({"gen-model", "gen-model-2"}, models_called)

    def test_judge_prompt_override_used(self):
        from kelab import judge as judge_mod
        from kelab.llm import ClientConfig

        domain, _, _ = load_conic()
        domain.judge_system = "自定义判分系统提示"
        base_url = self.start_mock()
        client = ClientConfig(base_url=base_url, api_key="k", model="judge-model",
                              temperature=0.0, timeout=10.0, max_retries=1)
        result = judge_mod.llm_judge(client, domain, "题", "gold", "gen")
        self.assertEqual("equivalent", result.verdict)
        self.assertEqual(100, result.prompt_tokens)  # judge token 追踪
        sent = FakeChatHandler.requests[-1]
        self.assertEqual("自定义判分系统提示", sent["messages"][0]["content"])
        self.assertIn("题", sent["messages"][1]["content"])


class NExperimentReportTest(MockServerMixin):
    """N 实验 + 报告：统计功效/重采样一致性/散点图/池成本换算的产物检查。"""

    ITEMS4 = ITEMS + [
        datasets.Item(item_id="t3", question="双曲线焦距题丙", gold_ke=GOOD_KE,
                      gold_operators=["focal_length", "get_expression", "sympy_expression"]),
        datasets.Item(item_id="t4", question="双曲线焦距题丁", gold_ke=GOOD_KE,
                      gold_operators=["focal_length", "get_expression", "sympy_expression"]),
    ]

    def test_report_contains_methodology_sections(self):
        from kelab.report import render_report

        exp = make_exp(kind="n", ns=[1, 3], fixed_k=1, retrievers=["bm25"], name="n-rep")
        domain, template, ontology = load_conic()
        base_url = self.start_mock()
        gen = ClientConfig(base_url=base_url, api_key="k", model="gen-model",
                           temperature=0.0, timeout=10.0, max_retries=1)
        judge_client = ClientConfig(base_url=base_url, api_key="k", model="judge-model",
                                    temperature=0.0, timeout=10.0, max_retries=1)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "kelab.sqlite")
        self.addCleanup(store.close)
        res = runner.RunResources(
            domain=domain, exp=exp, template=template, ontology=ontology,
            testset=self.ITEMS4, pools=datasets.loo_pools(self.ITEMS4),
            gen_clients={"default": gen}, judge_client=judge_client,
            store=store, run_id="n-rep-run",
        )
        stats = runner.run_experiment(res)
        self.assertEqual(16, stats["ok"])  # 2 档 × 4 题 × 2 重复
        report_path = render_report(store, "n-rep-run", exp, domain, self.ITEMS4,
                                    Path(tmp.name) / "results", provisional=True)
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("解读摘要", text)
        self.assertIn("统计功效", text)
        self.assertIn("MDE", text)
        self.assertIn("重采样拐点一致性", text)
        self.assertIn("池构建成本换算", text)
        self.assertTrue((Path(tmp.name) / "results" / "resample_scatter.png").exists())
        self.assertTrue((Path(tmp.name) / "results" / "retrieval_coverage.png").exists())


class MethodologyUnitsTest(unittest.TestCase):
    def test_mde_estimate(self):
        from kelab.aggregate import mde_estimate

        self.assertAlmostEqual(0.05, mde_estimate(100))
        self.assertAlmostEqual(0.10, mde_estimate(25))
        self.assertIsNone(mde_estimate(0))

    def test_knee_consistency(self):
        from kelab.aggregate import knee_consistency

        axis = [5, 10, 20, 50]
        verdict, _ = knee_consistency({101: 10, 202: 10, 303: 20}, axis)
        self.assertEqual("一致", verdict)  # 档位 index 差 1
        verdict, _ = knee_consistency({101: 5, 202: 50}, axis)
        self.assertEqual("不一致", verdict)  # 差 3 档
        verdict, _ = knee_consistency({101: 10, 202: None, 303: None}, axis)
        self.assertEqual("数据不足", verdict)

    def test_lift_pool_cost(self):
        from kelab.aggregate import ConditionMetrics, MetricStat
        from kelab.report import _lift_and_cost

        def cm(cid, n, mean):
            m = ConditionMetrics(condition_id=cid, kind="n", k=3, n=n, retriever="bm25")
            m.stats["semantic"] = MetricStat(mean=mean, std=0.01)
            return m

        exp = make_exp(kind="n", ns=[5, 105])
        domain, _, _ = load_conic()
        domain.annotation_cost_per_item = 2.0
        lines = _lift_and_cost([cm("n-bm25-n5", 5, 0.50), cm("n-bm25-n105", 105, 0.70)],
                               exp, domain)
        text = "\n".join(lines)
        self.assertIn("多标注 5 条", text)  # (105-5) / (0.2*100) = 5 条/pp
        self.assertIn("¥10/pp", text)  # 5 × ¥2

    def test_dedupe_items(self):
        items = [
            datasets.Item(item_id="a", question="题干 相同", gold_ke="ke1"),
            datasets.Item(item_id="b", question="题干相同", gold_ke="ke2"),  # 题干重复 → 去
            datasets.Item(item_id="c", question="题干丙", gold_ke=" ke1 "),  # 与 a 同 KE → 标记
        ]
        kept, report = datasets.dedupe_items(items)
        self.assertEqual(["a", "c"], [i.item_id for i in kept])
        self.assertEqual([{"kept": "a", "dropped": "b"}], report["dropped_question_dups"])
        self.assertEqual([["a", "c"]], report["same_ke_groups"])

    def test_model_ablation_verdict(self):
        from kelab.aggregate import ConditionMetrics, MetricStat
        from kelab.report import _model_ablation

        def cm(cid, model, mean):
            m = ConditionMetrics(condition_id=cid, kind="k", k=int(cid.rsplit("k", 1)[1].split("-")[0]),
                                 n=19, retriever="bm25", gen_model=model)
            m.stats["semantic"] = MetricStat(mean=mean, std=0.01)
            return m

        exp = make_exp(gen_models=["small-m", "big-m"])
        # small-m: 0.50→0.70（收益 +20pp）；big-m: 0.70→0.72（收益 +2pp）→ 收益随模型变化
        metrics = [
            cm("k-bm25-k0-m-small-m", "small-m", 0.50),
            cm("k-bm25-k3-m-small-m", "small-m", 0.70),
            cm("k-bm25-k0-m-big-m", "big-m", 0.70),
            cm("k-bm25-k3-m-big-m", "big-m", 0.72),
        ]
        lines = _model_ablation(metrics, exp, 0.03)
        text = "\n".join(lines)
        self.assertIn("收益随模型能力变化", text)
        self.assertIn("+20.0pp", text)
        self.assertIn("small-m", text)


class MigrationTest(unittest.TestCase):
    def test_old_judgments_table_migrated(self):
        import sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "old.sqlite"
        # 先造一个旧版 judgments 表（无 judge token 列）
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE judgments (gen_id INTEGER UNIQUE, exact_match REAL,"
            " retrieval_coverage REAL, gen_coverage REAL, llm_verdict TEXT, llm_score REAL,"
            " llm_rationale TEXT, judge_model TEXT, judge_error TEXT)"
        )
        conn.close()
        store = Store(db)  # 写模式打开应自动迁移
        self.addCleanup(store.close)
        store.insert_judgment(1, {"llm_verdict": "equivalent", "llm_score": 1.0,
                                  "judge_prompt_tokens": 7, "judge_completion_tokens": 3})
        row = store._conn.execute(
            "SELECT judge_prompt_tokens, judge_completion_tokens FROM judgments WHERE gen_id=1"
        ).fetchone()
        self.assertEqual(7, row["judge_prompt_tokens"])
        self.assertEqual(3, row["judge_completion_tokens"])


if __name__ == "__main__":
    unittest.main()
