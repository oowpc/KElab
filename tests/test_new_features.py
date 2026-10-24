"""新增功能测试：split 切分、状态自愈、备注、网格页、rejudge、批量队列、prepare 页。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import openpyxl

from kelab import datasets
from kelab.datasets import DataError, Item
from kelab.llm import ClientConfig
from kelab.store import Store
from kelab.web import WebApp, make_handler

from tests.helpers import FakeChatHandler, MockServerMixin
from tests.test_web import seed_root, seed_second_run


class SplitDatasetTest(unittest.TestCase):
    def make_items(self, n: int) -> list[Item]:
        return [Item(item_id=f"q{i:03d}", question=f"题{i}", gold_ke=f"ke{i}")
                for i in range(n)]

    def test_split_sizes_and_disjoint(self):
        testset, pool = datasets.split_dataset(self.make_items(200), 100, seed=42)
        self.assertEqual(100, len(testset))
        self.assertEqual(100, len(pool))
        self.assertFalse({it.item_id for it in testset} & {it.item_id for it in pool})

    def test_same_seed_same_split(self):
        a = datasets.split_dataset(self.make_items(50), 20, seed=7)
        b = datasets.split_dataset(self.make_items(50), 20, seed=7)
        self.assertEqual([i.item_id for i in a[0]], [i.item_id for i in b[0]])

    def test_split_leakage_check_passes(self):
        testset, pool = datasets.split_dataset(self.make_items(30), 10, seed=1)
        report = datasets.check_leakage(testset, pool, "fixed")
        self.assertTrue(report["passed"])

    def test_invalid_size(self):
        with self.assertRaises(DataError):
            datasets.split_dataset(self.make_items(5), 5)
        with self.assertRaises(DataError):
            datasets.split_dataset(self.make_items(5), 0)


class NewFeaturesTest(MockServerMixin, unittest.TestCase):
    """独立 fixture（不与 WebTest 共享现场，避免测试间状态交叉）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.run_id = seed_root(cls.root)
        cls.app = WebApp(cls.root)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        # mock 网关（独立于看板服务）：模型列表/设置页都走它，不打真实网络
        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatHandler)
        cls.gthread = threading.Thread(target=cls.gateway.serve_forever, daemon=True)
        cls.gthread.start()
        gateway_base = f"http://127.0.0.1:{cls.gateway.server_port}"
        (cls.root / "configs" / "api.json").write_text(
            json.dumps({"base_url": gateway_base + "/v1", "api_key": "sk-test"}),
            encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(1)
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.gthread.join(1)
        cls._tmp.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def post(self, path: str, data: dict) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    # ---- 状态自愈 ----

    def test_mark_interrupted(self):
        db = self.root / "runs" / self.run_id / "kelab.sqlite"
        store = Store(db)
        # 新鲜 run（刚写入）不算僵死
        self.assertEqual(0, store.mark_interrupted_stale_runs(stale_seconds=300))
        # 把 generation 时间戳改到 1 小时前 → 标记 interrupted
        store._conn.execute(
            "UPDATE generations SET created_at='2020-01-01T00:00:00+00:00'")
        store._conn.commit()
        self.assertEqual(1, store.mark_interrupted_stale_runs(stale_seconds=300))
        info = store.run_info(self.run_id)
        self.assertEqual("interrupted", info["status"])
        store.close()

    # ---- 备注 ----

    def test_no_nested_forms_anywhere(self):
        """回归：浏览器会丢弃嵌套的内层 form 标签（启动页曾因此错乱）。"""
        import re

        for path in ("/", "/runs", "/launch", "/compare", "/domains", "/grid",
                     "/prepare", "/prompts", "/configs", "/settings",
                     f"/run/{self.run_id}", f"/run/{self.run_id}/gens",
                     f"/run/{self.run_id}/failures", f"/run/{self.run_id}/review"):
            status, body = self.get(path)
            self.assertEqual(200, status, path)
            # 逐个 <form> 检查其闭合范围内是否还有 <form 开头
            for m in re.finditer(r"<form\b", body):
                close = body.find("</form>", m.end())
                self.assertFalse(
                    close != -1 and "<form" in body[m.end():close],
                    f"{path} 存在嵌套表单",
                )

    def test_run_note_roundtrip(self):
        self.post("/note", {"run_id": self.run_id, "note": "改了 judge prompt v2"})
        runs = {r["run_id"]: r for r in self.app.list_runs()}
        self.assertEqual("改了 judge prompt v2", runs[self.run_id]["note"])

    def test_reviews_trigger_chart_rerender(self):
        # 先出图到稳定态，再新增一条人工判定 → 版本号变 → 重新触发渲染
        self.app.maybe_render_charts(self.run_id, sync=True)
        self.app.maybe_render_charts(self.run_id, sync=True)
        self.app.save_review(self.run_id, 1, "correct", "校准", reviewer="A")
        self.assertEqual("rendering", self.app.maybe_render_charts(self.run_id, sync=True))

    # ---- 日志页 ----

    def test_log_page_no_file(self):
        status, body = self.get(f"/run/{self.run_id}/log")
        self.assertEqual(200, status)
        self.assertIn("没有日志文件", body)

    # ---- (k, N) 网格 ----

    def test_grid_page(self):
        run_b = seed_second_run(self.root)  # 也是 k 实验（k=0, N=19）
        status, body = self.get(f"/grid?k={self.run_id}&n={run_b}")
        self.assertEqual(200, status)
        self.assertIn("N=19", body)
        self.assertIn("k=0", body)
        # a 的语义均值 (1.0+0.0)/2=0.5 → 50.0%（setdefault 保留先到 run）
        self.assertIn("50.0%", body)

    # ---- rejudge ----

    def test_rejudge_errors_scope_with_mock(self):
        # 先把 g1 的 judge_error 置为有错，再重判 errors 范围（mock judge）
        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite")
        store._conn.execute(
            "UPDATE judgments SET judge_error='坏 JSON', llm_verdict=NULL, llm_score=NULL"
            " WHERE gen_id=1")
        store._conn.commit()
        store.close()
        self.assertEqual(1, self.app.rejudge_count(self.run_id, "errors"))

        base_url = self.start_mock()
        client = ClientConfig(base_url=base_url, api_key="k", model="judge-model",
                              temperature=0.0, timeout=10.0, max_retries=1)
        msg = self.app.rejudge_run(self.run_id, "errors", judge_client=client)
        self.assertIn("1 条", msg)
        # 等后台线程完成
        for _ in range(300):  # 后台线程等待预算 15s（防慢机器时序抖动）
            if not self.app._rejudge_state[self.run_id]["running"]:
                break
            time.sleep(0.05)
        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite", readonly=True)
        row = store._conn.execute(
            "SELECT llm_verdict, judge_error FROM judgments WHERE gen_id=1").fetchone()
        self.assertEqual("equivalent", row["llm_verdict"])
        self.assertIsNone(row["judge_error"])
        store.close()

    # ---- 批量启动队列 ----

    def test_batch_queue_drains(self):
        with patch.object(self.app, "launch", return_value=("fake-run", "ok")) as mocked:
            msg = self.app.enqueue_launch([("a.toml", None), ("b.toml", 5)])
            self.assertIn("2 个实验", msg)
            for _ in range(300):  # 后台线程等待预算 15s（防慢机器时序抖动）
                if not self.app._launch_queue and not self.app._queue_worker_alive:
                    break
                time.sleep(0.05)
            self.assertEqual(2, mocked.call_count)
            self.assertEqual([], self.app._launch_queue)

    # ---- prepare 页 ----

    def make_xlsx(self, root: Path) -> Path:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["题目", "题干", "李天音-KE", "原始ID"])
        ws.append(["t1", "双曲线焦距",
                   'output = { focal_length(C) | C: Hyperbola };', "x:1"])
        ws.append(["t2", "椭圆离心率",
                   'output = { eccentricity(C) | C: Ellipse };', "x:2"])
        path = root / "annotated.xlsx"
        wb.save(path)
        return path

    def test_prepare_preview_and_commit(self):
        xlsx = self.make_xlsx(self.root)
        form = {"domain": "conic", "xlsx": str(xlsx), "action": "preview",
                "split": "", "split_seed": "42"}
        status, body = self.post("/prepare", form)
        self.assertEqual(200, status)
        self.assertIn("导入 2 题", body)
        self.assertIn("gold 全部通过适配器规则校验", body)
        self.assertIn("未写任何文件", body)
        self.assertFalse((self.root / "data" / "derived" / "annotated.jsonl").exists())

        form["action"] = "commit"
        form["split"] = "1"
        form["save_colmap"] = "on"
        status, body = self.post("/prepare", form)
        self.assertEqual(200, status)
        self.assertIn("泄露检查", body)
        testset_path = self.root / "data" / "derived" / "annotated.jsonl"
        pool_path = self.root / "data" / "derived" / "annotated_pool.jsonl"
        self.assertTrue(testset_path.exists())
        self.assertTrue(pool_path.exists())
        testset = datasets.read_jsonl(testset_path)
        pool = datasets.read_jsonl(pool_path)
        self.assertEqual(1, len(testset))
        self.assertEqual(1, len(pool))
        self.assertTrue(testset[0].gold_operators, "gold 算子应已预提取")
        # save_colmap：列映射已写入领域配置 [xlsx] 节
        toml_text = (self.root / "configs" / "domains" / "conic.toml").read_text(encoding="utf-8")
        self.assertIn("[xlsx]", toml_text)
        self.assertIn("题目", toml_text)


    # ---- 新建配置 ----

    def test_create_experiment_config(self):
        status, body = self.get("/configs/new?type=experiment")
        self.assertEqual(200, status)
        self.assertIn("新建实验配置", body)
        path = self.root / "configs" / "experiments" / "my-exp.toml"
        self.assertFalse(path.exists())
        status, _ = self.post("/configs/save", {
            "new": "experiment", "name": "my-exp", "domain": "conic", "kind": "k",
            "testset": "data/derived/mini.jsonl", "pool_strategy": "loo",
            "ks": "0, 3", "ns": "", "fixed_k": "3", "ret_bm25": "on",
            "nested": "on", "repeats": "2", "seeds": "1, 2",
            "knee_threshold": "0.05", "std_alert": "0.03", "oracle_gap_threshold": "0.05",
        })
        self.assertIn(status, (200, 303))
        self.assertTrue(path.exists())
        from kelab.config import load_experiment
        exp = load_experiment(path)
        self.assertEqual("my-exp", exp.name)
        self.assertEqual([0, 3], exp.ks)
        # 重名拒绝
        status, body = self.post("/configs/save", {
            "new": "experiment", "name": "my-exp", "domain": "conic", "kind": "k",
            "testset": "data/derived/mini.jsonl", "ks": "0",
        })
        self.assertEqual(500, status)
        self.assertIn("同名", body)

    def test_create_domain_config(self):
        status, _ = self.post("/configs/save", {
            "new": "domain", "name": "newdom", "grammar": "g.lark",
            "prompt_template": "p.md", "ontology_source": "prompt",
            "gen_model": "m1", "gen_temperature": "0",
            "gen_max_concurrency": "4", "gen_max_retries": "3", "gen_timeout": "120",
            "judge_model": "m2", "review_sample_rate": "0.1",
        })
        self.assertIn(status, (200, 303))
        path = self.root / "configs" / "domains" / "newdom.toml"
        self.assertTrue(path.exists())
        from kelab.config import load_domain
        self.assertEqual("newdom", load_domain(path).name)

    def test_configs_page_has_create_buttons(self):
        status, body = self.get("/configs")
        self.assertIn("新建实验配置", body)
        self.assertIn("新建领域配置", body)

    # ---- 协议 §7.x：frozen 序列 / 预登记 / 盲评双人裁决 / 实验矩阵 ----

    def test_frozen_retriever_shared_sequence(self):
        from kelab import retrieve
        from kelab.datasets import Item

        pool = [Item(item_id=f"p{i}", question=f"题{i}", gold_ke=f"ke{i}")
                for i in range(10)]
        probe1 = Item(item_id="t1", question="查题一", gold_ke="")
        probe2 = Item(item_id="t2", question="查题二", gold_ke="")
        a = retrieve.retrieve("frozen", probe1, pool, 3, seed=101)
        b = retrieve.retrieve("frozen", probe2, pool, 3, seed=101)
        # 同一套序列，与题目无关
        self.assertEqual([i.item_id for i in a.items],
                         [i.item_id for i in b.items])
        # 嵌套：k=1 是 k=3 的前缀
        c = retrieve.retrieve("frozen", probe1, pool, 1, seed=101)
        self.assertEqual([i.item_id for i in c.items],
                         [i.item_id for i in a.items][:1])
        # 确定性
        d = retrieve.retrieve("frozen", probe1, pool, 3, seed=101)
        self.assertEqual([i.item_id for i in a.items], [i.item_id for i in d.items])

    def test_prereg_create_and_lock(self):
        from kelab.prereg import create_prereg, load_prereg
        from kelab.config import load_experiment

        exp = load_experiment(self.root / "configs" / "experiments" / "mini.toml")
        payload = create_prereg(
            self.root, exp, dataset_md5="abc123", delta=0.05, quality_gate=0.85,
            review_rate=0.1,
            scoring_protocol="人工盲评为主，LLM judge 辅助", actor="测试导师",
        )
        self.assertEqual(1, payload["version"])
        self.assertTrue(payload["prereg_hash"])
        again = create_prereg(
            self.root, exp, dataset_md5="abc123", delta=0.03, quality_gate=0.85,
            review_rate=0.1, scoring_protocol="同上", actor="测试导师", reason="调 δ",
        )
        self.assertEqual(2, again["version"])
        loaded = load_prereg(self.root, exp.name)
        self.assertEqual(0.03, loaded["delta"])  # 新版生效
        history = (self.root / "configs" / "prereg" / "prereg_history.jsonl").read_text(
            encoding="utf-8")
        self.assertEqual(2, len(history.strip().splitlines()))  # 旧版留痕

    def test_prereg_pages(self):
        status, body = self.get("/prereg")
        self.assertEqual(200, status)
        self.assertIn("预登记", body)
        status, body = self.get("/prereg/edit?exp=mini")
        self.assertEqual(200, status)
        self.assertIn("拐点阈值", body)
        status, _ = self.post("/prereg/save", {
            "exp": "mini", "actor": "测试导师", "delta": "0.05",
            "quality_gate": "", "review_rate": "0.1",
            "scoring_protocol": "人工盲评为主", "reason": "",
        })
        self.assertIn(status, (200, 303))

    def test_blind_dual_review_and_adjudication(self):
        # A 判 correct、B 判 wrong → 分歧进 C 裁决队列；C 裁后最终判定为 C 的
        self.app.save_review(self.run_id, 1, "correct", "", reviewer="A")
        self.app.save_review(self.run_id, 1, "wrong", "", reviewer="B")
        self.assertEqual([1], self.app.adjudication_pending(self.run_id))
        final = self.app.get_reviews(self.run_id)
        self.assertNotIn(1, final)  # 分歧未裁 → 不进最终集
        self.app.save_review(self.run_id, 1, "wrong", "第三人裁决", reviewer="C")
        self.assertEqual([], self.app.adjudication_pending(self.run_id))
        final = self.app.get_reviews(self.run_id)
        self.assertEqual("wrong", final[1]["human_verdict"])

    def test_review_page_blind_hides_judge(self):
        status, body = self.get(f"/run/{self.run_id}/review?reviewer=A&blind=1")
        self.assertEqual(200, status)
        self.assertIn("盲评：不显示条件与 judge 结论", body)
        self.assertNotIn("judge：equivalent", body)  # judge 结论被隐藏
        self.assertNotIn("k-bm25-k0", body)          # 条件（k）也被隐藏
        status, body = self.get(f"/run/{self.run_id}/review?reviewer=A&blind=0")
        self.assertIn("judge：", body)               # 非盲评可见

    def test_matrix_page(self):
        status, body = self.get("/matrix")
        self.assertEqual(200, status)
        self.assertIn("实验矩阵", body)
        self.assertIn(self.run_id, body)
        status, body = self.get("/matrix.csv")
        self.assertEqual(200, status)
        self.assertIn("run_id", body)

    # ---- 第二轮评审修复 ----

    def test_prompt_template_guard(self):
        """prompt 模板内容变更后拒绝续跑（与数据集守卫同口径）。"""
        import os

        from kelab.cli import CliError, _build_resources
        from kelab.config import config_hash, load_domain, load_experiment

        exp = load_experiment(self.root / "configs" / "experiments" / "mini.toml")
        domain = load_domain(self.root / "configs" / "domains" / "conic.toml")
        run_id = f"{exp.name}-{config_hash(domain, exp)}"
        run_dir = self.root / "runs" / run_id
        store = Store(run_dir / "kelab.sqlite")
        store.create_run(run_id, config_json="{}", config_hash="x",
                         prompt_template_md5=None, grammar_md5=None)
        store.close()
        self.addCleanup(lambda: __import__("shutil").rmtree(run_dir, ignore_errors=True))

        tpl = self.root / "prompt_mini.md"
        original = tpl.read_text(encoding="utf-8")
        self.addCleanup(lambda: tpl.write_text(original, encoding="utf-8"))
        cwd = os.getcwd()
        try:
            os.chdir(self.root)
            # 第一次：旧库回填模板指纹，通过
            res, _ = _build_resources(exp, self.root / "configs" / "domains" / "conic.toml",
                                      need_api=False)
            res.store.close()
            # 改模板 → 拒绝续跑
            tpl.write_text(original + "\n改动\n", encoding="utf-8")
            with self.assertRaises(CliError) as caught:
                _build_resources(exp, self.root / "configs" / "domains" / "conic.toml",
                                 need_api=False)
            self.assertIn("prompt 模板内容已变更", str(caught.exception))
        finally:
            os.chdir(cwd)

    def test_estimate_calls_counts_all_dimensions(self):
        from kelab.config import ExperimentConfig
        from pathlib import Path as P

        exp = ExperimentConfig(
            name="x", domain="conic", kind="k",
            testset=P("data/derived/mini.jsonl"),
            ks=[0, 1], retrievers=["bm25"],
            onto_modes=["full", "linked"], gen_models=["m1", "m2"],
            repeats=1, seeds=[1],
        )
        n_conds, gens, judges = self.app.estimate_calls(exp)
        self.assertEqual(8, n_conds)  # 2 k × 1 检索 × 2 onto × 2 模型
        self.assertEqual(8, gens)     # 8 条件 × 1 题 × 1 重复

    def test_split_shortfall_warns_loudly(self):
        import contextlib
        import io

        from kelab import datasets
        from kelab.datasets import Item

        # 大族（全部同模板）导致分区装不满 → stderr 大声警告
        items = [Item(item_id=f"q{i:03d}", question="同一模板题", gold_ke=f"ke{i}")
                 for i in range(12)]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            t, d, p = datasets.split_dataset3(items, 8, dev_size=3, seed=1)
        self.assertIn("装不满", buf.getvalue())
        self.assertEqual(0, len(t))  # 全进池（协议不允许拆族）


    # ---- dry-run 不建幽灵 run / 重试失败 / 抽检口径 / grammar 报错 ----

    def test_dry_run_creates_no_run(self):
        import os

        from kelab.cli import main
        from kelab.config import config_hash, load_domain, load_experiment

        exp_path = self.root / "configs" / "experiments" / "mini.toml"
        exp = load_experiment(exp_path)
        domain = load_domain(self.root / "configs" / "domains" / "conic.toml")
        run_id = f"{exp.name}-{config_hash(domain, exp)}"
        cwd = os.getcwd()
        try:
            os.chdir(self.root)  # cli 用相对路径
            code = main(["run", "--exp", str(exp_path), "--dry-run"])
        finally:
            os.chdir(cwd)
        self.assertEqual(0, code)
        self.assertFalse((self.root / "runs" / run_id).exists(),
                         "dry-run 不应创建 run 目录")

    def test_retry_errors(self):
        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite", readonly=True)
        before = store._conn.execute(
            "SELECT COUNT(*) c FROM generations WHERE status='error'").fetchone()["c"]
        store.close()
        self.assertEqual(1, before)  # 种子里 t2 是 error
        with patch.object(self.app, "launch", return_value=(self.run_id, "ok")) as mocked:
            msg = self.app.retry_errors(self.run_id)
        self.assertIn("1 条失败记录", msg)
        mocked.assert_called_once()
        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite", readonly=True)
        after = store._conn.execute(
            "SELECT COUNT(*) c FROM generations WHERE status='error'").fetchone()["c"]
        total = store._conn.execute("SELECT COUNT(*) c FROM generations").fetchone()["c"]
        store.close()
        self.assertEqual(0, after)
        self.assertEqual(2, total)  # 只剩 2 条 ok

    def test_retry_button_visible_when_errors(self):
        status, body = self.get(f"/run/{self.run_id}")
        self.assertEqual(200, status)
        self.assertIn("重试失败（", body)

    def test_run_page_interpretation_card(self):
        status, body = self.get(f"/run/{self.run_id}")
        self.assertEqual(200, status)
        # 反 cherry-pick：结论框架（资格章 + 样本量 + MDE），不报单点最佳数
        self.assertIn("试跑 pilot", body)
        self.assertIn("MDE", body)
        self.assertNotIn("最佳语义等价率", body)
        self.assertIn("人工抽检不足 5 条", body)  # 种子无人工判定 → 校准提示

    def test_review_rate_from_domain_config(self):
        # 把领域抽检比例改成 0.5，页面应显示约 50%（此前写死 10%）
        toml_path = self.root / "configs" / "domains" / "conic.toml"
        text = toml_path.read_text(encoding="utf-8")
        toml_path.write_text(text.replace("[judge]", "[judge]\nreview_sample_rate = 0.5"),
                             encoding="utf-8")
        status, body = self.get(f"/run/{self.run_id}/review")
        self.assertEqual(200, status)
        self.assertIn("约 50%", body)

    # ---- 标注工作台 ----

    def test_annotate_page(self):
        status, body = self.get("/annotate")
        self.assertEqual(200, status)
        self.assertIn("标注工作台", body)
        self.assertIn("生成草稿", body)

    # ---- 分享版报告 ----

    def test_share_export(self):
        status, _ = self.post("/share", {"run_id": self.run_id})
        self.assertIn(status, (200, 303))
        share = self.root / "results" / self.run_id / "share.html"
        fragment = self.root / "results" / self.run_id / "share.fragment.html"
        self.assertTrue(share.exists())
        self.assertTrue(fragment.exists())
        html_text = share.read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", html_text)     # 独立文档
        self.assertIn("结论速读", html_text)
        self.assertIn("口径与复现", html_text)
        self.assertIn("data:image/png;base64,", html_text)  # 图表内嵌
        frag_text = fragment.read_text(encoding="utf-8")
        self.assertNotIn("<!doctype html>", frag_text)  # 片段版无骨架（发布用）
        self.assertIn("<title>", frag_text)

    def test_annotate_draft_shows_ke_and_issues(self):
        status, body = self.post("/annotate/draft",
                                 {"domain": "conic", "question": "双曲线的焦距是多少"})
        self.assertEqual(200, status)
        self.assertIn("output = {", body)          # mock 生成的 KE 草稿
        self.assertIn("error", body)               # mini 本体缺 get_expression → 校验提示
        self.assertIn("保存为 gold", body)

    def test_annotate_save_valid_and_reject_invalid(self):
        good = "output = { focal_length(C) | C: Hyperbola };"
        status, _ = self.post("/annotate/save",
                              {"domain": "conic", "question": "双曲线焦距题", "ke": good})
        self.assertIn(status, (200, 303))
        from kelab import datasets
        staged = datasets.read_jsonl(self.root / "data" / "derived" / "annotation_conic.jsonl")
        self.assertEqual(1, len(staged))
        self.assertEqual(["focal_length"], staged[0].gold_operators)  # 算子已预提取
        # 坏 KE（未知算子）拒绝入库
        bad = "output = { bad_op(C) | C: Hyperbola };"
        status, _ = self.post("/annotate/save",
                              {"domain": "conic", "question": "另一题", "ke": bad})
        staged = datasets.read_jsonl(self.root / "data" / "derived" / "annotation_conic.jsonl")
        self.assertEqual(1, len(staged))  # 没多
        # 同题重复也拒绝
        self.post("/annotate/save", {"domain": "conic", "question": "双曲线焦距题", "ke": good})
        staged = datasets.read_jsonl(self.root / "data" / "derived" / "annotation_conic.jsonl")
        self.assertEqual(1, len(staged))
        # 删除
        self.post("/annotate/delete", {"domain": "conic",
                                       "item_id": staged[0].item_id})
        text = (self.root / "data" / "derived" / "annotation_conic.jsonl").read_text(
            encoding="utf-8")
        self.assertNotIn("ann-conic", text)

    def test_verify_grammar_md5_missing_file(self):
        from kelab.config import ConfigError, DomainConfig, verify_grammar_md5

        domain = DomainConfig(name="x", grammar_path=Path("不存在.lark"),
                              prompt_template=Path("p.md"))
        with self.assertRaises(ConfigError) as caught:
            verify_grammar_md5(domain)
        self.assertIn("grammar 文件不存在", str(caught.exception))

    def test_export_review_sample_strided(self):
        from kelab.report import export_review_sample

        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite")
        out = self.root / "review_out.csv"
        exp = self.app.find_experiment(self.run_id)
        n = export_review_sample(store, self.run_id, exp, {}, 0.5, out)
        store.close()
        # 种子 ok 条 = 2（g1、g3），stride=2 → 取第 1 条
        self.assertEqual(1, n)
        content = out.read_text(encoding="utf-8-sig")
        self.assertIn("t1", content)
        self.assertNotIn("t3", content)

    def test_domains_page(self):
        run_b = seed_second_run(self.root)
        status, body = self.get(f"/domains?runs={self.run_id}&runs={run_b}")
        self.assertEqual(200, status)
        self.assertIn("汇总对比", body)
        self.assertIn("总览（按最佳语义降序）", body)
        self.assertIn("各域拐点", body)
        pngs = list((self.root / "results" / "_domains").glob("domains_*.png"))
        self.assertTrue(pngs, "应生成分域小多图 PNG")
        # 同实验同检索器的两个 run → 同轴叠加图
        overlays = list((self.root / "results" / "_domains").glob("overlay_*.png"))
        self.assertTrue(overlays, "同 kind×retriever 的 run 应生成同轴叠加图")

    def test_dataset_version_guard(self):
        """数据集变更拒绝续跑；旧库（无指纹）一次性回填后可继续。"""
        import os
        import shutil

        from kelab.cli import CliError, _build_resources, _dataset_fingerprint
        from kelab.config import config_hash, load_domain, load_experiment

        exp = load_experiment(self.root / "configs" / "experiments" / "mini.toml")
        domain = load_domain(self.root / "configs" / "domains" / "conic.toml")
        run_id = f"{exp.name}-{config_hash(domain, exp)}"
        run_dir = self.root / "runs" / run_id
        # 预建一个「旧版」run：有 runs 行但 dataset_md5 为 NULL
        store = Store(run_dir / "kelab.sqlite")
        store.create_run(run_id, config_json="{}", config_hash="x",
                         prompt_template_md5=None, grammar_md5=None)
        store.close()
        self.addCleanup(lambda: shutil.rmtree(run_dir, ignore_errors=True))

        cwd = os.getcwd()
        try:
            os.chdir(self.root)  # cli 用相对路径
            # ① 旧库回填：NULL 指纹 → 补记并通过
            res, _ = _build_resources(exp, self.root / "configs" / "domains" / "conic.toml",
                                      need_api=False)
            res.store.close()
            store = Store(run_dir / "kelab.sqlite", readonly=True)
            self.assertEqual(_dataset_fingerprint(exp),
                             store.run_info(run_id)["dataset_md5"])
            store.close()
            # ② 改测试集内容 → 拒绝续跑
            jsonl = self.root / "data" / "derived" / "mini.jsonl"
            jsonl.write_text(jsonl.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaises(CliError) as caught:
                _build_resources(exp, self.root / "configs" / "domains" / "conic.toml",
                                 need_api=False)
            self.assertIn("数据集已变更", str(caught.exception))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
