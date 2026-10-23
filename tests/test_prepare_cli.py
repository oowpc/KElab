"""prepare 主数据路径走适配器的测试：fake adapter + jsonl 证明新领域不需改 runner。"""
import argparse
import json
import tempfile
import unittest
from pathlib import Path

from kelab import cli, datasets
from kelab.adapters import register_adapter
from kelab.adapters.base import Item, ValidationResult
from kelab.tasks import register_task
from kelab.tasks.base import TaskValidation

from tests.helpers import PROMPT_MD, ROOT

GRAMMAR = (ROOT / "kelab" / "grammar" / "ke_syntax.lark").as_posix()
PROMPT = PROMPT_MD.as_posix()


class FakeAdapter:
    """最简适配器：jsonl 行 → Item；gold_ke == "ok" 才算合法（规则由领域自定）。"""
    name = "fake"

    def load(self, source, col_map=None):
        items = []
        with Path(source).open("r", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                items.append(Item(item_id=row["id"], question=row["q"],
                                  gold_ke=row.get("ke", "ok")))
        return items

    def validate_gold(self, item, pack):
        if item.gold_ke == "ok":
            return ValidationResult(ok=True)
        return ValidationResult(ok=False, errors=["fake 规则：gold_ke 不是 ok"])

    def fingerprint(self, item):
        return f"fp-{item.item_id}"


register_adapter(FakeAdapter)  # 注册即生效：这就是新增领域的全部平台侧动作


class FakeTask:
    """最简任务适配器：「解析」就是检查非空，算子为空。"""
    name = "fake"
    primary_metric = "准确率"

    def validate_output(self, text, domain, ontology):
        return TaskValidation(parse_ok=bool(text.strip()),
                              parse_error=None if text.strip() else "空输出")

    def judge(self, **kwargs):  # prepare 不触达；跑实验才需要
        raise NotImplementedError


register_task(FakeTask)


def fake_domain_toml(tmp: Path) -> Path:
    path = tmp / "fake.toml"
    path.write_text(
        "[domain]\n"
        'name = "fakedom"\n'
        f'grammar = "{GRAMMAR}"\n'
        f'prompt_template = "{PROMPT}"\n'
        'ontology_source = "prompt"\n'
        "[pack]\n"
        'adapter = "fake"\n'
        'task = "fake"\n',
        encoding="utf-8")
    return path


def prepare_args(domain: Path, source: Path, out: Path, **over) -> argparse.Namespace:
    values = dict(domain=str(domain), xlsx=None, source=str(source), out=str(out),
                  pool_strategy="loo", pool=None, col=None, split=None, dev=0,
                  split_seed=42, pool_out=None, allow_cross_family=False)
    values.update(over)
    return argparse.Namespace(**values)


class PrepareViaAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.domain = fake_domain_toml(self.dir)

    def _write_jsonl(self, rows: list[dict], name="src.jsonl") -> Path:
        path = self.dir / name
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def test_prepare_uses_adapter_load_and_validate(self):
        src = self._write_jsonl([{"id": "a", "q": "题甲"}, {"id": "b", "q": "题乙"}])
        out = self.dir / "out.jsonl"
        code = cli.cmd_prepare(prepare_args(self.domain, src, out))
        self.assertEqual(0, code)
        items = datasets.read_jsonl(out)
        self.assertEqual(["a", "b"], [it.item_id for it in items])
        # manifest 走了适配器指纹（fp-a/fp-b），证明指纹接口接入主流水线
        manifest = json.loads((self.dir / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({"a": "fp-a", "b": "fp-b"},
                         manifest["partitions"]["testset"]["items"])

    def test_prepare_adapter_rejection_fails(self):
        src = self._write_jsonl([{"id": "a", "q": "题", "ke": "bad"}])
        with self.assertRaises(datasets.DataError) as caught:
            cli.cmd_prepare(prepare_args(self.domain, src, self.dir / "out.jsonl"))
        self.assertIn("fake 规则", str(caught.exception))

    def test_cross_family_fails_by_default(self):
        """fixed 池含测试题近重复 → 跨分区题族 fail-fast；显式放行才过。"""
        testset = self._write_jsonl([{"id": "t1", "q": "已知双曲线 x²-y²=1，求焦距"},
                                     {"id": "t2", "q": "求椭圆离心率取值范围"}])
        # fixed 池走 datasets.read_jsonl（标准 Item 键），不走适配器
        pool = self._write_jsonl(
            [{"item_id": "p1", "question": "已知双曲线 x²-y²=1，求其焦距", "gold_ke": "ok"},
             {"item_id": "p2", "question": "完全不同的一道题，关于抛物线焦点坐标计算",
              "gold_ke": "ok"}],
            name="pool.jsonl")
        args = prepare_args(self.domain, testset, self.dir / "out.jsonl",
                            pool_strategy="fixed", pool=str(pool))
        self.assertEqual(1, cli.cmd_prepare(args))  # 默认拒绝
        args_ok = prepare_args(self.domain, testset, self.dir / "out.jsonl",
                               pool_strategy="fixed", pool=str(pool),
                               allow_cross_family=True)
        self.assertEqual(0, cli.cmd_prepare(args_ok))  # 显式放行


if __name__ == "__main__":
    unittest.main()
