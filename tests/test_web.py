import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from kelab.store import Store
from kelab.web import WebApp, make_handler

from tests.helpers import FakeChatHandler, load_conic


def seed_root(root: Path) -> str:
    """造一个最小运行现场：1 个 run、1 个条件、2 条 generation（1 成功带判分、1 失败）。"""
    (root / "configs" / "experiments").mkdir(parents=True)
    (root / "configs" / "experiments" / "mini.toml").write_text(
        '[experiment]\nname = "mini"\ndomain = "conic"\nkind = "k"\n'
        'testset = "data/derived/mini.jsonl"\nks = [0]\nrepeats = 1\nseeds = [7]\n',
        encoding="utf-8",
    )
    (root / "configs" / "domains").mkdir(parents=True)
    (root / "configs" / "domains" / "conic.toml").write_text(
        '[domain]\nname = "conic"\n'
        f'grammar = "{Path(load_conic()[0].grammar_path).resolve().as_posix()}"\n'
        'prompt_template = "prompt_mini.md"\n'
        'ontology_source = "prompt"\n[generation]\nmodel = "gen-model"\ntemperature = 0.0\n'
        '[judge]\nmodel = "judge-model"\n[execution]\nenabled = false\n',
        encoding="utf-8",
    )
    (root / "prompt_mini.md").write_text(
        "# 角色\n\n你是标注专家。\n\n**concepts**\n\n```\nCurve, Point, Hyperbola, Ellipse\n```\n\n"
        "**operators**\n\n```\nop focal_length(c: Curve) -> Number\n"
        "op eccentricity(c: Curve) -> Number\n```\n\n"
        "## 示例\n\n### 示例 1\n\n输入：题甲\n\n输出：\n\n```\nKE\n```\n\n"
        "## 待转换题目\n\n输入：{{题目}}\n\n输出：\n",
        encoding="utf-8",
    )
    (root / "data" / "derived").mkdir(parents=True)
    (root / "data" / "derived" / "mini.jsonl").write_text(
        json.dumps({"item_id": "t1", "question": "双曲线焦距",
                    "gold_ke": "output = { focal_length(C) | C: Hyperbola };",
                    "gold_operators": ["focal_length"]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    run_id = "mini-deadbeef1234"
    store = Store(root / "runs" / run_id / "kelab.sqlite")
    store.create_run(run_id, config_json="{}", config_hash="deadbeef",
                     prompt_template_md5="abc", grammar_md5="def")
    store.add_condition(run_id, "k-bm25-k0", "k", 0, 19, "bm25", "hash")
    g1 = store.insert_generation({
        "run_id": run_id, "condition_id": "k-bm25-k0", "item_id": "t1", "rep": 0,
        "seed": 7, "status": "ok", "ke_text": "output = { focal_length(C) | C: Hyperbola };",
        "retrieved_ids": "[]", "latency_ms": 100, "prompt_tokens": 50, "completion_tokens": 20,
    })
    store.insert_validation(g1, {"lark_ok": 1, "lark_error": None, "light_ok": 1,
                                 "issues_json": "[]", "gen_operators": '["focal_length"]'})
    store.insert_judgment(g1, {"exact_match": 1.0, "retrieval_coverage": None,
                               "gen_coverage": 1.0, "llm_verdict": "equivalent",
                               "llm_score": 1.0, "llm_rationale": "一致",
                               "judge_model": "judge-model", "judge_error": None})
    store.insert_generation({
        "run_id": run_id, "condition_id": "k-bm25-k0", "item_id": "t2", "rep": 0,
        "seed": 7, "status": "error", "error": "api boom", "retrieved_ids": "[]",
    })
    # 第三条：light 检查失败（覆盖失败分类的 issues 聚类分支）
    g3 = store.insert_generation({
        "run_id": run_id, "condition_id": "k-bm25-k0", "item_id": "t3", "rep": 0,
        "seed": 7, "status": "ok", "ke_text": "output = { bad_op(C) | C: Hyperbola };",
        "retrieved_ids": "[]", "latency_ms": 90, "prompt_tokens": 40, "completion_tokens": 15,
    })
    store.insert_validation(g3, {
        "lark_ok": 1, "lark_error": None, "light_ok": 0,
        "issues_json": '[{"level":"error","kind":"unknown_operator","message":"未知算子 bad_op"}]',
        "gen_operators": '["bad_op"]',
    })
    store.insert_judgment(g3, {"exact_match": 0.0, "retrieval_coverage": None,
                               "gen_coverage": 0.0, "llm_verdict": "different",
                               "llm_score": 0.0, "llm_rationale": "用了不存在的算子",
                               "judge_model": "judge-model", "judge_error": None})
    store.close()
    return run_id


def seed_second_run(root: Path, run_id: str = "mini-c0ffee567890") -> str:
    """第二个 run：与 seed_root 同实验配置（mini），判分 0.75（a 的语义均值是 (1.0+0.0)/2=0.5）。
    幂等：已存在直接返回（同一 fixture 可能被多个测试调用）。"""
    if (root / "runs" / run_id / "kelab.sqlite").exists():
        return run_id
    store = Store(root / "runs" / run_id / "kelab.sqlite")
    store.create_run(run_id, config_json="{}", config_hash="c0ffee",
                     prompt_template_md5="abc", grammar_md5="def")
    store.add_condition(run_id, "k-bm25-k0", "k", 0, 19, "bm25", "hash")
    g1 = store.insert_generation({
        "run_id": run_id, "condition_id": "k-bm25-k0", "item_id": "t1", "rep": 0,
        "seed": 7, "status": "ok", "ke_text": "output = { focal_length(C) | C: Hyperbola };",
        "retrieved_ids": "[]", "latency_ms": 200, "prompt_tokens": 80, "completion_tokens": 20,
    })
    store.insert_validation(g1, {"lark_ok": 1, "lark_error": None, "light_ok": 1,
                                 "issues_json": "[]", "gen_operators": '["focal_length"]'})
    store.insert_judgment(g1, {"exact_match": 1.0, "retrieval_coverage": None,
                               "gen_coverage": 1.0, "llm_verdict": "minor",
                               "llm_score": 0.75, "llm_rationale": "轻微缺省",
                               "judge_model": "judge-model", "judge_error": None})
    store.close()
    return run_id


class WebTest(unittest.TestCase):
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

    def test_runs_list(self):
        status, body = self.get("/runs")
        self.assertEqual(200, status)
        self.assertIn(self.run_id, body)
        self.assertIn("运行列表", body)

    def test_home_guide(self):
        status, body = self.get("/")
        self.assertEqual(200, status)
        self.assertIn("工作流向导", body)
        self.assertIn("数据流", body)
        self.assertIn("核心概念", body)
        self.assertIn("嵌套池", body)

    def test_run_detail(self):
        status, body = self.get(f"/run/{self.run_id}")
        self.assertEqual(200, status)
        self.assertIn("k-bm25-k0", body)
        self.assertIn("条件 × 指标", body)

    def test_gens_filter_verdict(self):
        status, body = self.get(f"/run/{self.run_id}/gens?verdict=equivalent")
        self.assertEqual(200, status)
        self.assertIn("focal_length", body)
        self.assertIn("等价", body)

    def test_gens_filter_error_status(self):
        status, body = self.get(f"/run/{self.run_id}/gens?status=error")
        self.assertIn("api boom", body)

    def test_progress_api(self):
        status, body = self.get(f"/api/run/{self.run_id}/progress")
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertIn("conditions", data)
        self.assertIn("k-bm25-k0", data["conditions"])
        self.assertEqual(3, data["conditions"]["k-bm25-k0"]["done"])

    def test_review_save_and_export(self):
        form = urllib.parse.urlencode({
            "run_id": self.run_id, "gen_id": "1",
            "human_verdict": "correct", "human_note": "没问题",
        }).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/review", data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (303, 200))
        reviews = self.app.get_reviews(self.run_id)
        self.assertEqual(1, len(reviews))
        self.assertEqual("correct", reviews[1]["human_verdict"])
        status, body = self.get(f"/run/{self.run_id}/review.csv")
        self.assertEqual(200, status)
        self.assertIn("correct", body)
        self.assertIn("没问题", body)

    def test_review_page_lists_sample(self):
        status, body = self.get(f"/run/{self.run_id}/review")
        self.assertEqual(200, status)
        self.assertIn("人工抽检队列", body)

    def test_auto_chart_rendering(self):
        # 首次：触发渲染（同步模式），应出图 + report.md
        status = self.app.maybe_render_charts(self.run_id, sync=True)
        self.assertEqual("rendering", status)
        figs = list((self.root / "results" / self.run_id).glob("*.png"))
        self.assertTrue(figs, "应自动生成 PNG 图表")
        self.assertTrue((self.root / "results" / self.run_id / "report.md").exists())
        # 数据没变：再次调用短路为 fresh，不重画
        self.assertEqual("fresh", self.app.maybe_render_charts(self.run_id, sync=True))

    def test_run_page_shows_charts_after_render(self):
        self.app.maybe_render_charts(self.run_id, sync=True)
        status, body = self.get(f"/run/{self.run_id}")
        self.assertEqual(200, status)
        self.assertIn("/files/results/", body)
        self.assertIn("?v=", body)  # 缓存破坏参数

    def test_files_traversal_guard(self):
        status, _ = self.get("/files/results/..%2F..%2Fsecret.txt")
        self.assertEqual(404, status)

    # ---- Prompt 编辑与预览 ----

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

    def test_prompts_list_and_edit_page(self):
        status, body = self.get("/prompts")
        self.assertEqual(200, status)
        self.assertIn("conic", body)
        status, body = self.get("/prompts/edit?domain=conic")
        self.assertEqual(200, status)
        self.assertIn("标注专家", body)  # 可能被保存测试改过（资深/不资深），只验内容载入

    def test_prompt_save_valid_creates_backup(self):
        tpl = self.root / "prompt_mini.md"
        original = tpl.read_text(encoding="utf-8")
        before = list((self.root / "archive" / "prompts").glob("prompt_mini.md.*.bak"))
        new_content = original.replace("标注专家", "资深标注专家")
        status, _ = self.post("/prompts/save", {"domain": "conic", "content": new_content})
        self.assertIn(status, (200, 303))
        self.assertIn("资深标注专家", tpl.read_text(encoding="utf-8"))
        after = list((self.root / "archive" / "prompts").glob("prompt_mini.md.*.bak"))
        self.assertEqual(len(before) + 1, len(after))  # 恰好多一个备份（共享现场顺序无关）
        self.assertTrue(
            any(b.read_text(encoding="utf-8") == original for b in after),
            "至少一个备份应是改动前内容",
        )

    def test_prompt_save_invalid_rejected(self):
        tpl = self.root / "prompt_mini.md"
        original = tpl.read_text(encoding="utf-8")
        self.post("/prompts/save", {"domain": "conic", "content": "没有标记的模板"})
        self.assertEqual(original, tpl.read_text(encoding="utf-8"))  # 未被写坏

    def test_prompt_preview(self):
        status, body = self.get("/prompts/preview?domain=conic&k=1&retriever=random")
        self.assertEqual(200, status)
        self.assertIn("system 消息", body)
        self.assertIn("你是标注专家", body)
        self.assertIn("user 消息", body)

    # ---- 配置编辑 ----

    def test_configs_list_and_edit_pages(self):
        status, body = self.get("/configs")
        self.assertEqual(200, status)
        self.assertIn("mini.toml", body)
        status, body = self.get(f"/configs/edit?path={self.root}/configs/experiments/mini.toml")
        self.assertIn("k 档位", body)
        status, body = self.get(f"/configs/edit?path={self.root}/configs/domains/conic.toml")
        self.assertIn("生成模型", body)

    def test_experiment_config_save_roundtrip(self):
        path = self.root / "configs" / "experiments" / "mini.toml"
        status, _ = self.post("/configs/save", {
            "path": str(path), "name": "mini", "domain": "conic", "kind": "k",
            "testset": "data/derived/mini.jsonl", "pool_strategy": "loo",
            "ks": "0, 1, 3", "ns": "", "fixed_k": "3",
            "ret_bm25": "on", "ret_random": "on",
            "nested": "on", "repeats": "2", "seeds": "7, 8",
            "knee_threshold": "0.05", "std_alert": "0.03", "oracle_gap_threshold": "0.05",
        })
        self.assertIn(status, (200, 303))
        import tomllib
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([0, 1, 3], raw["experiment"]["ks"])
        self.assertEqual(["random", "bm25"], raw["experiment"]["retrievers"])  # 按规范顺序
        self.assertEqual(2, raw["experiment"]["repeats"])
        from kelab.config import load_experiment
        exp = load_experiment(path)  # 回读校验（seeds≥repeats 等）
        self.assertEqual([7, 8], exp.seeds)
        self.assertTrue(list((self.root / "archive" / "configs").glob("mini.toml.*.bak")))

    def test_experiment_config_save_invalid_rejected_with_message(self):
        path = self.root / "configs" / "experiments" / "mini.toml"
        status, body = self.post("/configs/save", {
            "path": str(path), "name": "mini", "domain": "conic", "kind": "k",
            "testset": "data/derived/mini.jsonl", "pool_strategy": "loo",
            "ks": "abc", "ns": "", "fixed_k": "3",
            "nested": "on", "repeats": "1", "seeds": "7",
        })
        self.assertEqual(500, status)  # 非法整数 → SaveError → 错误页，不写文件
        import tomllib
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([0], raw["experiment"]["ks"])  # 未被写坏

    def test_domain_config_save_roundtrip(self):
        path = self.root / "configs" / "domains" / "conic.toml"
        grammar = Path(load_conic()[0].grammar_path).resolve().as_posix()  # 真实语法，避免污染后续测试
        status, _ = self.post("/configs/save", {
            "path": str(path), "name": "conic", "grammar": grammar,
            "prompt_template": "prompt_mini.md", "ontology_source": "prompt",
            "question_format": "输入：{question}\n\n输出：",
            "gen_model": "gen-model", "gen_temperature": "0.1",
            "gen_max_concurrency": "2", "gen_max_retries": "3", "gen_timeout": "60",
            "judge_model": "judge-model", "review_sample_rate": "0.2",
            "annotation_cost_per_item": "3.5",
        })
        self.assertIn(status, (200, 303))
        from kelab.config import load_domain
        domain = load_domain(path)
        self.assertEqual(0.1, domain.gen_temperature)
        self.assertEqual(2, domain.gen_max_concurrency)
        self.assertEqual(0.2, domain.review_sample_rate)
        self.assertEqual(3.5, domain.annotation_cost_per_item)
        self.assertFalse(domain.execution_enabled)  # checkbox 缺席 = False

    # ---- 成本可见性 / 失败分类 / 一致率 / 停止 ----

    def test_launch_page_shows_estimate(self):
        status, body = self.get("/launch")
        self.assertEqual(200, status)
        self.assertIn("调用量估算", body)
        # mini.toml 可能被配置保存测试改过，只验证估算格式存在，不绑死数字
        self.assertRegex(body, r"\d+ 条件 → 约 \d+ 生成 \+ \d+ judge ≈ \d+ 次调用")

    def test_run_page_cost_card(self):
        status, body = self.get(f"/run/{self.run_id}")
        self.assertEqual(200, status)
        self.assertIn("消耗", body)
        self.assertIn("生成 2 次 / 125 tokens", body.replace(",", ""))  # (50+20)+(40+15)

    def test_failures_page(self):
        status, body = self.get(f"/run/{self.run_id}/failures")
        self.assertEqual(200, status)
        self.assertIn("失败案例分类", body)
        self.assertIn("生成失败（API/网络/超时）", body)
        self.assertIn("api boom", body)
        self.assertIn("light 检查失败：未知算子（闭词表外）", body)
        self.assertIn("judge 判不同（语义错误）", body)

    def test_review_agreement_shown(self):
        self.post("/review", {"run_id": self.run_id, "gen_id": "1",
                              "human_verdict": "correct", "human_note": ""})
        status, body = self.get(f"/run/{self.run_id}/review")
        self.assertEqual(200, status)
        self.assertIn("一致率", body)
        self.assertIn("1/1（100%）", body)  # correct≈equivalent，judge 也是 equivalent

    def test_stop_endpoint(self):
        status, _ = self.post("/stop", {"run_id": self.run_id})
        self.assertIn(status, (200, 303))  # 非本会话启动 → 提示消息，不报错

    def test_gens_question_text_search(self):
        q = urllib.parse.quote("双曲线")
        status, body = self.get(f"/run/{self.run_id}/gens?q={q}")
        self.assertEqual(200, status)
        self.assertIn("共 1 条", body)
        q = urllib.parse.quote("不存在的题干")
        status, body = self.get(f"/run/{self.run_id}/gens?q={q}")
        self.assertIn("共 0 条", body)

    # ---- 跨 run 对比 ----

    def test_compare_pickers(self):
        status, body = self.get("/compare")
        self.assertEqual(200, status)
        self.assertIn("基准 a", body)
        self.assertIn(self.run_id, body)

    def test_compare_two_runs(self):
        run_b = seed_second_run(self.root)
        status, body = self.get(f"/compare?a={self.run_id}&b={run_b}")
        self.assertEqual(200, status)
        self.assertIn("主指标（语义等价率）平均 Δ", body)
        self.assertIn("+25.0pp", body)  # 0.75 - 0.5（a 的语义均值 = (1.0+0.0)/2）
        self.assertIn("改善 1", body)
        self.assertIn("k-bm25-k0", body)
        # 叠加曲线已生成
        pngs = list((self.root / "results" / "_compare").glob(f"*{run_b}*.png"))
        self.assertTrue(pngs, "应生成对比叠加曲线 PNG")

    def test_compare_same_run_rejected(self):
        status, body = self.get(f"/compare?a={self.run_id}&b={self.run_id}")
        self.assertEqual(200, status)
        self.assertIn("同一个 run", body)

    # ---- 重新校验 / ETA / 失败CSV / prompt 历史 ----

    def test_revalidate_flips_stale_validation(self):
        # 人为制造"过期"校验结果（light_ok=0），重新校验应修回 1
        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite")
        store._conn.execute("UPDATE validations SET light_ok=0 WHERE gen_id=1")
        store._conn.commit()
        store.close()
        status, _ = self.post("/revalidate", {"run_id": self.run_id})
        self.assertIn(status, (200, 303))
        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite", readonly=True)
        row = store._conn.execute(
            "SELECT light_ok FROM validations WHERE gen_id=1").fetchone()
        self.assertEqual(1, row["light_ok"])
        # g3 的 unknown_operator 是真失败，重校后仍失败
        row = store._conn.execute(
            "SELECT light_ok FROM validations WHERE gen_id=3").fetchone()
        self.assertEqual(0, row["light_ok"])
        store.close()

    def test_eta_text(self):
        from kelab.web import eta_text
        from datetime import datetime, timedelta, timezone

        store = Store(self.root / "runs" / self.run_id / "kelab.sqlite")
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE runs SET started_at=? WHERE run_id=?",
                            (past, self.run_id))
        store._conn.commit()
        text = eta_text(store, self.run_id, expected=6, done=3)
        self.assertIn("约", text)  # 60s 跑 3 条 → 剩 3 条约 60 秒
        self.assertEqual("", eta_text(store, self.run_id, expected=3, done=3))
        store.close()

    def test_failures_csv(self):
        status, body = self.get(f"/run/{self.run_id}/failures.csv")
        self.assertEqual(200, status)
        self.assertIn("category", body)
        self.assertIn("bad_op", body)

    def test_prompt_history_diff_restore(self):
        tpl = self.root / "prompt_mini.md"
        original = tpl.read_text(encoding="utf-8")
        # 制造一个历史版本
        self.post("/prompts/save", {"domain": "conic",
                                    "content": original.replace("标注专家", "改版专家")})
        status, body = self.get("/prompts/history?domain=conic")
        self.assertEqual(200, status)
        self.assertIn("历史", body)
        self.assertIn("与当前 diff", body)
        bak = sorted((self.root / "archive" / "prompts").glob("prompt_mini.md.*.bak"))[-1]
        status, body = self.get(f"/prompts/diff?domain=conic&file={bak.name}")
        self.assertEqual(200, status)
        self.assertIn("改版专家", body)
        # 恢复历史版
        status, _ = self.post("/prompts/restore", {"domain": "conic", "file": bak.name})
        self.assertIn(status, (200, 303))
        self.assertEqual(original, tpl.read_text(encoding="utf-8"))


class RoundFiveUiTest(unittest.TestCase):
    """第五轮 UX：导航/守卫/时间/启动门槛/gold 复核队列。"""

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

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def post(self, path: str, data: dict) -> tuple[int, str]:
        payload = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=payload)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_layout_nav_groups_and_active(self):
        status, body = self.get("/runs")
        self.assertEqual(200, status)
        self.assertIn("aria-current", body)          # 无障碍：当前页标注
        self.assertIn("active", body)
        self.assertIn("＋ 启动实验", body)            # 主 CTA
        self.assertIn("@media (max-width: 720px)", body)  # 移动端断点
        self.assertIn("overflow-x: auto", body)      # 表格横向滚动

    def test_guard_js_present(self):
        _, body = self.get("/runs")
        self.assertIn("data-confirm", body)          # 确认守卫已注入
        self.assertIn("处理中", body)                # 防重复提交

    def test_fmt_time_local_relative(self):
        from kelab.web.ui import fmt_time

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = fmt_time(now)
        self.assertIn("刚刚", text)
        self.assertIn("（", text)  # 本地时间（相对）
        self.assertEqual("—", fmt_time(None))
        self.assertEqual("乱写的", fmt_time("乱写的"))  # 解析失败原样返回

    def test_launch_gate_and_estimate(self):
        status, body = self.get("/launch")
        self.assertEqual(200, status)
        self.assertIn("未登记", body)                # mini 实验无登记 → 徽标
        self.assertIn("data-needs-prereg", body)     # 启动按钮需勾选
        self.assertIn("万 tokens", body)             # token 粗估
        self.assertIn("data-confirm", body)          # 启动确认

    def test_prereg_page_validity_status(self):
        status, body = self.get("/prereg")
        self.assertEqual(200, status)
        self.assertIn("未登记", body)
        # 登记后再看：有效（配置与数据都对上）
        from kelab.prereg import create_prereg
        from kelab.config import dataset_fingerprint, load_experiment

        exp = load_experiment(self.root / "configs" / "experiments" / "mini.toml")
        create_prereg(self.root, exp, dataset_md5=dataset_fingerprint(exp, root=self.root),
                      delta=0.05, quality_gate=None, review_rate=0.1,
                      scoring_protocol="人工盲评", actor="测试")
        status, body = self.get("/prereg")
        self.assertIn("有效", body)

    def test_gold_review_queue_flow(self):
        """gold 复核队列 Web 入口：打开 → 保存编辑 → 冻结。"""
        # 造一个复核队列（用 conic 包校验：合法/非法各一）
        from kelab import gold_pipeline
        from kelab.adapters.base import Item
        from kelab.packs import load_pack

        pack = load_pack("conic", root=self.root)
        queue_dir = self.root / "data" / "gold" / "q1"
        items = [
            Item("g1", "双曲线焦距题", "output = { focal_length(C) | C: Hyperbola };"),
            Item("g2", "坏题", "不是 KE"),
        ]
        cands = gold_pipeline.validate_candidates(items, pack)
        gold_pipeline.export_review(cands, queue_dir)

        status, body = self.get("/annotate")
        self.assertEqual(200, status)
        self.assertIn("q1", body)                    # 队列出现在标注页
        status, body = self.get("/annotate/review?dir=q1")
        self.assertEqual(200, status)
        self.assertIn("g1", body)
        self.assertIn("goldke__g1", body)            # 可编辑 gold 文本框
        self.assertIn("规则 ✗", body)                # g2 规则未过

        # 保存：g1 pass；g2 改好再 pass
        status, _ = self.post("/annotate/review_save", {
            "dir": "q1", "page": "1",
            "verdict__g1": "pass",
            "goldke__g1": "output = { focal_length(C) | C: Hyperbola };",
            "verdict__g2": "pass",
            "goldke__g2": "output = { eccentricity(C) | C: Ellipse };",
        })
        self.assertIn(status, (200, 303))

        # 冻结
        status, _ = self.post("/annotate/review_freeze", {
            "dir": "q1", "domain": "conic", "actor": "测试员",
        })
        self.assertIn(status, (200, 303))
        frozen = queue_dir / "frozen.jsonl"
        self.assertTrue(frozen.exists())
        from kelab.datasets import read_jsonl
        items = read_jsonl(frozen)
        self.assertEqual(2, len(items))
        self.assertIn("focal_length", items[0].gold_operators)

    def test_freeze_requires_actor(self):
        from kelab import gold_pipeline
        from kelab.adapters.base import Item
        from kelab.packs import load_pack

        pack = load_pack("conic", root=self.root)
        queue_dir = self.root / "data" / "gold" / "q2"
        cands = gold_pipeline.validate_candidates(
            [Item("g1", "题", "output = { focal_length(C) | C: Hyperbola };")], pack)
        gold_pipeline.export_review(cands, queue_dir)
        status, body = self.post("/annotate/review_freeze",
                                 {"dir": "q2", "domain": "conic", "actor": ""})
        self.assertIn(status, (200, 303))
        self.assertFalse((queue_dir / "frozen.jsonl").exists())  # 缺复核人 → 拒绝


class RoundSixTest(unittest.TestCase):
    """第六轮：服务端启动守卫、目录包支持、compare 可比性。"""

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

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def post(self, path: str, data: dict) -> tuple[int, str]:
        payload = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=payload)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    # ---- 启动守卫（服务端兜底） ----

    def test_gate_refuses_unregistered_without_override(self):
        from kelab.config import load_experiment

        exp = load_experiment(self.root / "configs" / "experiments" / "mini.toml")
        limit, msg = self.app.launch_gate(exp, None, prereg_ok=False)
        self.assertIsNone(limit)
        self.assertIn("未预登记", msg)

    def test_gate_forces_limit5_with_override(self):
        from kelab.config import load_experiment

        exp = load_experiment(self.root / "configs" / "experiments" / "mini.toml")
        limit, msg = self.app.launch_gate(exp, None, prereg_ok=True)
        self.assertEqual(5, limit)          # 留空全量也被强制压到 5
        self.assertIn("试跑口径", msg)
        limit2, _ = self.app.launch_gate(exp, 3, prereg_ok=True)
        self.assertEqual(3, limit2)         # 更小的 limit 不动

    def test_launch_writes_note_and_limit_via_cli_args(self):
        """override 启动：子进程命令带 --limit 5 --note（不真启动，mock Popen）。"""
        from unittest.mock import patch

        with patch("kelab.web.app.subprocess.Popen") as popen:
            popen.return_value.poll.return_value = None
            run_id, msg = self.app.launch(
                str(self.root / "configs" / "experiments" / "mini.toml"),
                limit=None, prereg_ok=True)
        self.assertIsNotNone(run_id)
        cmd = popen.call_args[0][0]
        self.assertIn("--limit", cmd)
        self.assertIn("5", cmd)
        self.assertIn("--note", cmd)
        self.assertIn("未预登记", msg)
        # mock 的 call_args 扣着日志句柄（stdout=log）——显式释放，Windows 才能清理临时目录
        popen.call_args[1]["stdout"].close()
        self.app.launched.clear()

    def test_gate_passes_when_prereg_valid(self):
        from kelab.config import dataset_fingerprint, load_experiment
        from kelab.prereg import create_prereg

        # 用独立实验名（mini 留给未登记用例；本类共享 fixture，不能互相污染）
        mini2 = self.root / "configs" / "experiments" / "mini2.toml"
        mini2.write_text(
            (self.root / "configs" / "experiments" / "mini.toml").read_text(encoding="utf-8")
            .replace('name = "mini"', 'name = "mini2"'), encoding="utf-8")
        exp = load_experiment(mini2)
        create_prereg(self.root, exp, dataset_md5=dataset_fingerprint(exp, root=self.root),
                      delta=0.05, quality_gate=None, review_rate=0.1,
                      scoring_protocol="人工盲评", actor="测试")
        limit, msg = self.app.launch_gate(exp, None, prereg_ok=False)
        self.assertIsNone(limit)            # 有效登记：不限 limit、无覆盖消息
        self.assertIsNone(msg)

    # ---- 目录包支持 ----

    def _make_dir_pack(self) -> Path:
        from tests.helpers import load_conic

        grammar = Path(load_conic()[0].grammar_path).resolve().as_posix()
        pack_dir = self.root / "configs" / "domains" / "pkd"
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "prompt.md").write_text(
            "# 角色\n\n**concepts**\n\n```\nCurve, Hyperbola\n```\n\n"
            "**operators**\n\n```\nop focal_length(c: Curve) -> Number\n```\n\n"
            "## 示例\n\n## 待转换题目\n\n输入：{{题目}}\n\n输出：\n", encoding="utf-8")
        (pack_dir / "domain.toml").write_text(
            "[domain]\nname = \"pkd\"\n"
            f'grammar = "{grammar}"\n'
            'prompt_template = "prompt.md"\n'
            'ontology_source = "prompt"\n'
            "[pack]\nadapter = \"conic\"\ntask = \"nl2ke\"\n",
            encoding="utf-8")
        return pack_dir

    def test_dir_pack_recognized_everywhere(self):
        """目录包：资源加载 + Prompt 页列出 + 配置页列出 + 模板读取。"""
        pack_dir = self._make_dir_pack()
        domain, path = self.app._load_domain_resources("pkd")
        self.assertEqual("pkd", domain.name)
        self.assertEqual(pack_dir / "prompt.md", domain.prompt_template)  # 相对包目录解析
        template = self.app._read_template(domain)
        self.assertIn("## 示例", template)
        status, body = self.get("/prompts")
        self.assertIn("pkd", body)
        status, body = self.get("/configs")
        self.assertIn("pkd", body)

    def test_dir_pack_run_id_matches_cli(self):
        """目录包 run_id 与 CLI 一致：两者都走 load_domain_config(ref, root='.')
        （CLI 子进程 cwd=项目根）。这里验证同 root 重复加载哈希恒定 +
        web 预测路径用的正是同一函数。"""
        from kelab.config import config_hash, load_experiment
        from kelab.packs import load_domain_config

        self._make_dir_pack()
        exp_toml = self.root / "configs" / "experiments" / "pkd.toml"
        exp_toml.write_text(
            '[experiment]\nname = "pkd-exp"\ndomain = "pkd"\nkind = "k"\n'
            'testset = "data/derived/mini.jsonl"\nks = [0]\nrepeats = 1\nseeds = [7]\n',
            encoding="utf-8")
        exp = load_experiment(exp_toml)
        d1, _ = load_domain_config("pkd", root=self.root)
        d2, _ = load_domain_config("pkd", root=self.root)
        self.assertEqual(config_hash(d1), config_hash(d2))
        run_id = f"{exp.name}-{config_hash(d1)}"
        self.assertTrue(run_id.startswith("pkd-exp-"))

    # ---- compare 可比性 ----

    def test_compare_incomparable_pair_empty_state(self):
        """条件无交集的一对：空状态、不出图。"""
        from kelab.store import Store

        other = "mini-ffffffffffff"
        store = Store(self.root / "runs" / other / "kelab.sqlite")
        store.create_run(other, config_json="{}", config_hash="ffff",
                         prompt_template_md5="abc", grammar_md5="def")
        store.add_condition(other, "n-bm25-n5", "n", 3, 5, "bm25", "hash")
        store.finish_run(other)  # done
        store.close()
        status, body = self.get(f"/compare?a={self.run_id}&b={other}")
        self.assertEqual(200, status)
        self.assertIn("没有可比的条件", body)
        self.assertNotIn("<img", body)

    def test_compare_default_prefers_comparable(self):
        """默认预选跳过不可比/未完成的 run。"""
        from kelab.web.pages_experiments import _default_compare_pair

        runs = self.app.list_runs()
        a, b = _default_compare_pair(self.app, runs)
        if a and b:
            ids_a = {c["condition_id"] for c in self.app.open_run(a).condition_rows(a)}
            ids_b = {c["condition_id"] for c in self.app.open_run(b).condition_rows(b)}
            self.assertTrue(ids_a & ids_b)


class RoundSevenTest(unittest.TestCase):
    """第七轮：移动端卡片、上传、冻结领域默认链。"""

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

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_runs_and_launch_have_mobile_cards(self):
        _, runs = self.get("/runs")
        self.assertIn("rcards", runs)
        self.assertIn("rcard", runs)
        _, launch = self.get("/launch")
        self.assertIn("rcard", launch)
        _, home = self.get("/")
        self.assertIn("table.rtable", home)  # 移动端表格隐藏规则在全局 CSS

    def test_prepare_upload_flow(self):
        """multipart 上传 → 落 data/uploads → 重定向带回填路径。"""
        boundary = "----kelabtest"
        content = "题目内容\n".encode("utf-8")
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="我的标注.xlsx"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            self.base + "/upload", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                # 跟随重定向到 /prepare
                resp.read()
                self.assertEqual(200, resp.status)
        except urllib.error.HTTPError as exc:
            self.fail(f"upload 失败: {exc.code}")
        uploaded = self.root / "data" / "uploads" / "我的标注.xlsx"
        self.assertTrue(uploaded.exists())
        self.assertEqual(content.strip(), uploaded.read_bytes())
        # 回填：picked 参数预填数据源输入框（非 ASCII 要 quote）
        status, body = self.get("/prepare?picked=" + urllib.parse.quote("data/uploads/我的标注.xlsx"))
        self.assertIn("data/uploads/我的标注.xlsx", body)
        self.assertIn("multipart/form-data", body)  # 上传表单在页

    def test_freeze_domain_remembered(self):
        """冻结后记住领域选择（data/gold/_last_domain），下个队列默认它。"""
        from kelab import gold_pipeline
        from kelab.adapters.base import Item
        from kelab.packs import load_pack

        pack = load_pack("conic", root=self.root)
        queue_dir = self.root / "data" / "gold" / "q7"
        cands = gold_pipeline.validate_candidates(
            [Item("g1", "题", "output = { focal_length(C) | C: Hyperbola };")], pack)
        gold_pipeline.export_review(cands, queue_dir)
        # 人工 pass 后冻结
        path = queue_dir / "_review.jsonl"
        rows = gold_pipeline.load_review(path)
        rows[0]["human_verdict"] = "pass"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        payload = urllib.parse.urlencode(
            {"dir": "q7", "domain": "conic", "actor": "测试员"}).encode("utf-8")
        try:
            urllib.request.urlopen(
                urllib.request.Request(self.base + "/annotate/review_freeze", data=payload),
                timeout=10)
        except urllib.error.HTTPError as exc:
            self.fail(f"freeze 失败: {exc.code}")
        last = self.root / "data" / "gold" / "_last_domain"
        self.assertTrue(last.exists())
        self.assertEqual("conic", last.read_text(encoding="utf-8").strip())

    # ---- 系统资源监控 ----

    def test_sys_api_returns_html_and_metrics(self):
        status, body = self.get("/api/sys")
        self.assertEqual(200, status)
        data = json.loads(body)
        self.assertIn("html", data)
        self.assertIn("backend", data)
        self.assertIn("ts", data)
        self.assertIn("GPU", data["html"])  # 无 GPU 时如实说明而非省略

    def test_home_has_syscard(self):
        _, body = self.get("/")
        self.assertIn("data-sys-host", body)
        self.assertIn("/api/sys", body)

    def test_sysmon_sample_and_fallback(self):
        from kelab.web import sysmon

        data = sysmon.sample(".")
        for key in ("cpu_percent", "mem_percent", "backend", "gpu", "ts"):
            self.assertIn(key, data)
        if sysmon.psutil is not None:
            self.assertEqual("psutil", data["backend"])
            self.assertIsNotNone(data["cpu_percent"])
        # stdlib 兜底路径直接可跑（不依赖 psutil）
        fallback = sysmon._sample_stdlib(".")
        self.assertIn("cpu_percent", fallback)  # 系统级 CPU 无零依赖来源 → None 也合法
        card = sysmon.render_card(data)
        self.assertIn("CPU", card)
        self.assertIn("内存", card)

    def test_sysmon_gpu_card_with_fake_gpu(self):
        from kelab.web import sysmon

        card = sysmon.render_card({
            "cpu_percent": 12.0, "mem_percent": 55.0, "backend": "psutil",
            "gpu": {"name": "RTX 4060", "util": 88, "mem_used_mb": 3000,
                    "mem_total_mb": 8188}, "ts": "t"})
        self.assertIn("RTX 4060", card)
        self.assertIn("88%", card)
        self.assertIn("3000/8188", card)


if __name__ == "__main__":
    unittest.main()
