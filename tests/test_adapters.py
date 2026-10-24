"""适配器/任务/领域包测试：第一层内核接口与 conic 实现。"""
import json
import tempfile
import unittest
from pathlib import Path

from kelab import datasets, ke_validate
from kelab.adapters import get_adapter
from kelab.adapters.base import DomainPack, Item
from kelab.adapters.conic import Conic10KAdapter, ConicAdapter
from kelab.config import ConfigError
from kelab.packs import load_pack, resolve_domain_path
from kelab.tasks import get_task
from kelab.tasks.nl2ke import NL2KETask

from tests.helpers import DOMAIN_TOML, PROMPT_MD, ROOT, TESTSET_XLSX, MockServerMixin, load_conic


def make_pack() -> DomainPack:
    domain, template, ontology = load_conic()
    return DomainPack(domain=domain, template=template, ontology=ontology,
                      grammar_md5="x", adapter=ConicAdapter(), task=NL2KETask())


GOOD_KE = ('output = { focal_length(C) | C: Hyperbola, '
           'get_expression(C) = sympy_expression("Eq(x**2 - y**2, 1)") };')


class ItemMetadataTest(unittest.TestCase):
    """metadata 随 jsonl 往返，核心 schema 不被领域字段污染。"""

    def test_metadata_roundtrip(self):
        item = Item(item_id="a", question="题", gold_ke="ke",
                    metadata={"family_id": "f3", "answer": "2c"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.jsonl"
            datasets.write_jsonl([item], path)
            back = datasets.read_jsonl(path)[0]
        self.assertEqual({"family_id": "f3", "answer": "2c"}, back.metadata)

    def test_metadata_default_empty(self):
        back = Item.from_dict({"item_id": "a", "question": "q", "gold_ke": "k"})
        self.assertEqual({}, back.metadata)


class ConicAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = ConicAdapter()
        self.pack = make_pack()

    def test_load_xlsx(self):
        items = self.adapter.load(TESTSET_XLSX)
        self.assertEqual(20, len(items))
        self.assertTrue(all(it.question and it.gold_ke for it in items))

    def test_load_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            datasets.write_jsonl([Item("a", "题", GOOD_KE)], path)
            items = self.adapter.load(path)
        self.assertEqual("a", items[0].item_id)

    def test_load_rejects_unknown_suffix(self):
        with self.assertRaises(datasets.DataError):
            self.adapter.load(Path("data.csv"))

    def test_validate_gold_ok(self):
        result = self.adapter.validate_gold(Item("a", "题", GOOD_KE), self.pack)
        self.assertTrue(result.ok)
        self.assertEqual([], result.errors)

    def test_validate_gold_unknown_operator(self):
        bad = 'output = { not_an_operator(C) | C: Hyperbola };'
        result = self.adapter.validate_gold(Item("a", "题", bad), self.pack)
        self.assertFalse(result.ok)
        self.assertTrue(any("not_an_operator" in e for e in result.errors))

    def test_validate_gold_unparseable(self):
        result = self.adapter.validate_gold(Item("a", "题", "这不是 KE"), self.pack)
        self.assertFalse(result.ok)

    def test_fingerprint_stable_and_sensitive(self):
        a = Item("a", "双曲线 焦距", GOOD_KE)
        b = Item("b", "双曲线焦距", GOOD_KE)  # 归一化后同题干同 KE → 同指纹
        c = Item("c", "椭圆离心率", GOOD_KE)
        self.assertEqual(self.adapter.fingerprint(a), self.adapter.fingerprint(b))
        self.assertNotEqual(self.adapter.fingerprint(a), self.adapter.fingerprint(c))


class RegistryTest(unittest.TestCase):
    def test_default_adapter_is_conic(self):
        self.assertEqual("conic", get_adapter(None).name)

    def test_unknown_adapter_raises(self):
        with self.assertRaises(ConfigError):
            get_adapter("不存在")

    def test_default_task_is_nl2ke(self):
        self.assertEqual("nl2ke", get_task(None).name)

    def test_unknown_task_raises(self):
        with self.assertRaises(ConfigError):
            get_task("不存在")


class NL2KETaskTest(MockServerMixin):
    """task 适配器口径与底层模块逐字节一致。"""

    def test_validate_output_matches_ke_validate(self):
        domain, _template, ontology = load_conic()
        task = NL2KETask()
        direct = ke_validate.validate_ke(GOOD_KE, domain.grammar_path, ontology)
        via_task = task.validate_output(GOOD_KE, domain, ontology)
        self.assertEqual(direct.lark_ok, via_task.parse_ok)
        self.assertEqual(direct.light_ok, via_task.checks_ok)
        self.assertEqual(direct.operators, via_task.operators)

    def test_judge_fields(self):
        from kelab.llm import ClientConfig

        domain, _template, ontology = load_conic()
        task = NL2KETask()
        client = ClientConfig(base_url=self.start_mock(), api_key="k",
                              model="judge-model", temperature=0.0,
                              timeout=10.0, max_retries=1)
        item = Item("q", "题", GOOD_KE,
                    gold_operators=["focal_length", "get_expression", "sympy_expression"])
        validation = task.validate_output(GOOD_KE, domain, ontology)
        fields = task.judge(client=client, domain=domain, item=item, gen_text=GOOD_KE,
                            retrieved_items=[item], k=3, validation=validation)
        # 键集即 store.insert_judgment 的字段
        self.assertEqual(
            {"exact_match", "retrieval_coverage", "gen_coverage", "llm_verdict",
             "llm_score", "llm_rationale", "judge_model", "judge_error",
             "judge_prompt_tokens", "judge_completion_tokens"},
            set(fields))
        self.assertEqual(1.0, fields["exact_match"])
        self.assertEqual(1.0, fields["llm_score"])  # mock 判 equivalent
        self.assertEqual(1.0, fields["retrieval_coverage"])  # 同一 item 算子全覆盖
        self.assertIsNone(fields["judge_error"])

    def test_k0_retrieval_coverage_none(self):
        from kelab.llm import ClientConfig

        domain, _template, ontology = load_conic()
        task = NL2KETask()
        client = ClientConfig(base_url=self.start_mock(), api_key="k",
                              model="judge-model", temperature=0.0,
                              timeout=10.0, max_retries=1)
        item = Item("q", "题", GOOD_KE, gold_operators=["focal_length"])
        validation = task.validate_output(GOOD_KE, domain, ontology)
        fields = task.judge(client=client, domain=domain, item=item, gen_text=GOOD_KE,
                            retrieved_items=[], k=0, validation=validation)
        self.assertIsNone(fields["retrieval_coverage"])  # k=0 无检索覆盖度口径


class PackLoaderTest(unittest.TestCase):
    def test_legacy_toml_pack(self):
        pack = load_pack(DOMAIN_TOML, root=ROOT)
        self.assertIsNone(pack.pack_dir)
        self.assertEqual("conic", pack.adapter.name)
        self.assertEqual("nl2ke", pack.task.name)
        self.assertIn("## 示例", pack.template)
        self.assertTrue(pack.grammar_md5)

    def test_resolve_by_name(self):
        path = resolve_domain_path("conic", root=ROOT)
        self.assertEqual(DOMAIN_TOML, path)

    def test_resolve_missing_raises(self):
        with self.assertRaises(ConfigError):
            resolve_domain_path("不存在的领域", root=ROOT)

    def test_directory_pack_rebases_paths(self):
        """目录布局：相对路径按包目录解析；[pack] 声明进 DomainConfig。"""
        grammar_src = ROOT / "kelab" / "grammar" / "ke_syntax.lark"
        with tempfile.TemporaryDirectory() as tmp:
            pack_dir = Path(tmp) / "mydom"
            pack_dir.mkdir()
            (pack_dir / "grammar.lark").write_bytes(grammar_src.read_bytes())
            (pack_dir / "prompt.md").write_bytes(PROMPT_MD.read_bytes())
            (pack_dir / "domain.toml").write_text(
                "[domain]\n"
                'name = "mydom"\n'
                'grammar = "grammar.lark"\n'
                'prompt_template = "prompt.md"\n'
                'ontology_source = "prompt"\n'
                "[pack]\n"
                'adapter = "conic"\n'
                'task = "nl2ke"\n',
                encoding="utf-8")
            pack = load_pack(pack_dir)
        self.assertEqual(pack_dir, pack.pack_dir)
        self.assertEqual(pack_dir / "grammar.lark", pack.domain.grammar_path)
        self.assertEqual(pack_dir / "prompt.md", pack.domain.prompt_template)
        self.assertEqual("conic", pack.domain.adapter)
        self.assertEqual("nl2ke", pack.domain.task)
        self.assertIn("## 示例", pack.template)

    def test_pack_fields_none_not_in_hash(self):
        """[pack] 缺省（旧配置）时 adapter/task 为 None，不进 config_hash。"""
        from kelab.config import config_hash, load_domain

        domain = load_domain(DOMAIN_TOML)
        self.assertIsNone(domain.adapter)
        self.assertIsNone(domain.task)
        # 与加字段前的哈希口径一致：None 键被排除
        self.assertEqual(12, len(config_hash(domain)))


class Conic10KAdapterTest(unittest.TestCase):
    """Conic10K 全量数据入口：审校记录目录 / train.json / jsonl 三种形态。"""

    AUDIT_RECORD = {
        "uuid": "0045c466-4cb2-4d3b-84cb-f11eca54d116",
        "source": "conic_10k",
        "query": {"question": "如果椭圆 x²/(10-m)+y²/(m+4)=1 的焦距为 4，则 m 的值为?",
                  "lang": "zh"},
        "gold_ke": {"content": "output = { m | G: Ellipse, FocalLength(G) = 4 };\n"},
        "answer": {"type": "gold_expr", "gold_expr": {"content": "{1,5}"}},
        "verification": {"answer_verified": True},
        "tags": ["#analytic_geometry"],
        "notes": "本体缺口…",
    }

    def setUp(self):
        self.adapter = Conic10KAdapter()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_audit_record_dir(self):
        rec_dir = Path(self.tmp.name) / "records"
        rec_dir.mkdir()
        (rec_dir / "0045.json").write_text(
            json.dumps(self.AUDIT_RECORD, ensure_ascii=False), encoding="utf-8")
        items = self.adapter.load(rec_dir)
        self.assertEqual(1, len(items))
        it = items[0]
        self.assertEqual("0045c466-4cb2-4d3b-84cb-f11eca54d116", it.item_id)
        self.assertIn("焦距", it.question)
        self.assertIn("FocalLength", it.gold_ke)      # 审校记录自带 gold_ke
        self.assertEqual("{1,5}", it.metadata["answer"])
        self.assertEqual(["#analytic_geometry"], it.metadata["tags"])

    def test_raw_train_json_list(self):
        src = Path(self.tmp.name) / "train.json"
        src.write_text(json.dumps([
            {"id": "r1", "text": "求椭圆离心率", "expressions": "x^2/4+y^2=1"},
            {"id": "r2", "problem": "求抛物线焦点"},
        ], ensure_ascii=False), encoding="utf-8")
        items = self.adapter.load(src)
        # item_id 一律 split 前缀格式（官方记录无全局唯一 id）；原始 id 进 source_id
        self.assertEqual(["conic10k-train-00001", "conic10k-train-00002"],
                         [it.item_id for it in items])
        self.assertEqual(["r1", "r2"], [it.source_id for it in items])
        self.assertEqual("", items[0].gold_ke)        # 原始数据无 KE → 待标注
        self.assertEqual("x^2/4+y^2=1", items[0].metadata["answer"])

    def test_official_schema_full_metadata(self):
        """官方记录字段（text/fact_expressions/query_expressions/answer_expressions/
        fact_spans/query_spans/process）一个不丢，全进 metadata。"""
        record = {
            "text": "已知椭圆 x²/25+y²/9=1，求其焦距",
            "fact_expressions": "Eq(x**2/25 + y**2/9, 1)",
            "query_expressions": "focal_length",
            "answer_expressions": "8",
            "fact_spans": ["x²/25+y²/9=1"],
            "query_spans": ["焦距"],
            "process": "c²=25-9=16, 2c=8",
        }
        src = Path(self.tmp.name) / "dev.json"
        src.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
        it = self.adapter.load(src)[0]
        self.assertEqual("conic10k-dev-00001", it.item_id)
        self.assertIn("焦距", it.question)
        self.assertEqual("", it.gold_ke)
        md = it.metadata
        self.assertEqual("8", md["answer"])  # answer 主键 = answer_expressions
        for key in ("fact_expressions", "query_expressions", "answer_expressions",
                    "fact_spans", "query_spans", "process"):
            self.assertIn(key, md, f"metadata 缺 {key}")
        self.assertEqual("c²=25-9=16, 2c=8", md["process"])
        self.assertEqual("dev", md["split"])
        self.assertEqual(1, md["source_index"])

    def test_cross_split_ids_unique(self):
        """train/dev/test 各自从 1 编号也不撞 id（官方数据实测撞过）。"""
        rec = {"text": "同一题干"}
        paths = []
        for split in ("train", "dev", "test"):
            p = Path(self.tmp.name) / f"{split}.json"
            p.write_text(json.dumps([rec, rec], ensure_ascii=False), encoding="utf-8")
            paths.extend(self.adapter.load(p))
        ids = [it.item_id for it in paths]
        self.assertEqual(6, len(set(ids)))  # 全唯一
        self.assertIn("conic10k-test-00001", ids)
        self.assertIn("conic10k-dev-00002", ids)

    def test_missing_question_fails_loud(self):
        src = Path(self.tmp.name) / "bad.json"
        src.write_text(json.dumps([{"id": "x", "foo": 1}]), encoding="utf-8")
        with self.assertRaises(datasets.DataError):
            self.adapter.load(src)

    def test_registered(self):
        self.assertEqual("conic10k", get_adapter("conic10k").name)


class ConfigHashPinTest(unittest.TestCase):
    """run_id 稳定性钉死：哈希口径一旦漂移，既有 run 全部失联（断点续跑/报告寻址）。"""

    def test_conic_domain_hash_pinned(self):
        from kelab.config import config_hash, load_domain

        self.assertEqual("2a2a60c6495b", config_hash(load_domain(DOMAIN_TOML)))

    def test_main_k_frozen_run_id_pinned(self):
        from kelab.config import config_hash, load_domain, load_experiment

        domain = load_domain(DOMAIN_TOML)
        exp = load_experiment(ROOT / "configs" / "experiments" / "main-k-frozen.toml")
        self.assertEqual("main-k-frozen-e64b4761b25c", f"{exp.name}-{config_hash(domain, exp)}")


if __name__ == "__main__":
    unittest.main()
