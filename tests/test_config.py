import unittest

from kelab.config import DomainConfig, ExperimentConfig, config_hash
from pathlib import Path


class HashDisciplineTest(unittest.TestCase):
    """None 值不进配置哈希：新增可选字段（默认 None）不改变既有配置的 run_id。"""

    def make_domain(self, **kw) -> DomainConfig:
        values = dict(name="d", grammar_path=Path("g.lark"), prompt_template=Path("p.md"))
        values.update(kw)
        return DomainConfig(**values)

    def make_exp(self, **kw) -> ExperimentConfig:
        values = dict(name="e", domain="d", kind="k", testset=Path("t.jsonl"), ks=[0])
        values.update(kw)
        return ExperimentConfig(**values)

    def test_none_fields_do_not_change_hash(self):
        d1 = self.make_domain()
        d2 = self.make_domain(judge_system=None, ontology_json=None)
        self.assertEqual(config_hash(d1), config_hash(d2))

    def test_set_optional_field_changes_hash(self):
        d1 = self.make_domain()
        d2 = self.make_domain(judge_system="自定义")
        self.assertNotEqual(config_hash(d1), config_hash(d2))

    def test_limit_items_none_vs_set(self):
        e1 = self.make_exp()
        e2 = self.make_exp(limit_items=5)
        self.assertNotEqual(config_hash(e1), config_hash(e2))  # 试跑 = 独立 run
        e3 = self.make_exp(limit_items=None)
        self.assertEqual(config_hash(e1), config_hash(e3))

    def test_existing_smoke_run_hash_unchanged(self):
        # 回归保护：当前配置算出的 run_id 必须能对应到磁盘上已迁移的 run（续跑不断线）
        from pathlib import Path as P
        from kelab.config import load_domain, load_experiment

        root = P(__file__).resolve().parent.parent
        domain = load_domain(root / "configs" / "domains" / "conic.toml")
        exp = load_experiment(root / "configs" / "experiments" / "smoke_k.toml")
        run_id = f"{exp.name}-{config_hash(domain, exp)}"
        self.assertTrue(
            (root / "runs" / run_id / "kelab.sqlite").exists(),
            f"run {run_id} 在磁盘上不存在——配置哈希与既有 run 断线，需迁移",
        )

    def test_onto_modes_validation(self):
        from kelab.config import ConfigError, load_experiment
        import tempfile
        bad = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                          encoding="utf-8")
        bad.write('[experiment]\nname="x"\ndomain="d"\nkind="k"\ntestset="t"\n'
                  'ks=[0]\nonto_modes=["linked","bogus"]\n')
        bad.close()
        with self.assertRaises(ConfigError):
            load_experiment(bad.name)


if __name__ == "__main__":
    unittest.main()
