import unittest

from kelab import ke_validate

from tests.helpers import load_conic

GOOD_KE = ('output = { focal_length(C) | C: Hyperbola, '
           'get_expression(C) = sympy_expression("Eq(x**2 - y**2, 1)") };')


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain, cls.template, cls.ontology = load_conic()

    def parse(self, text):
        return ke_validate.parse_ke(text, self.domain.grammar_path)

    def test_valid_ke_parses(self):
        tree, error = self.parse(GOOD_KE)
        self.assertIsNone(error)
        self.assertIsNotNone(tree)

    def test_syntax_error_returns_error_not_raise(self):
        tree, error = self.parse("output = { focal_length(C) | C: Hyperbola, };")
        self.assertIsNone(tree)
        self.assertIsNotNone(error)

    def test_garbage_returns_error(self):
        tree, error = self.parse("这不是 KE")
        self.assertIsNone(tree)
        self.assertIsNotNone(error)


class ExtractOperatorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain, _, _ = load_conic()

    def test_extracts_call_names_only(self):
        tree, _ = ke_validate.parse_ke(GOOD_KE, self.domain.grammar_path)
        ops = ke_validate.extract_operators(tree)
        self.assertEqual(ops, ["focal_length", "get_expression", "sympy_expression"])

    def test_sympy_string_content_not_extracted(self):
        ke = ('output = { m | C: Ellipse, m: Real, '
              'get_expression(C) = sympy_expression("Eq(abs(x), max(y))") };')
        tree, error = ke_validate.parse_ke(ke, self.domain.grammar_path)
        self.assertIsNone(error)
        ops = ke_validate.extract_operators(tree)
        self.assertNotIn("abs", ops)
        self.assertNotIn("max", ops)
        self.assertIn("sympy_expression", ops)

    def test_set_constructor_not_an_operator(self):
        ke = ('output = { one_of(F) | C: Ellipse, F: Set[Point], get_focus(C) = F };')
        tree, error = ke_validate.parse_ke(ke, self.domain.grammar_path)
        self.assertIsNone(error)
        ops = ke_validate.extract_operators(tree)
        self.assertNotIn("Set", ops)
        self.assertIn("one_of", ops)
        self.assertIn("get_focus", ops)


class LightCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain, _, cls.ontology = load_conic()

    def validate(self, ke):
        return ke_validate.validate_ke(ke, self.domain.grammar_path, self.ontology)

    def error_kinds(self, result):
        return {i.kind for i in result.issues if i.level == "error"}

    def test_good_ke_passes(self):
        result = self.validate(GOOD_KE)
        self.assertTrue(result.lark_ok)
        self.assertEqual([], [i.message for i in result.issues if i.level == "error"])
        self.assertTrue(result.light_ok)

    def test_unknown_operator_caught(self):
        result = self.validate(GOOD_KE.replace("focal_length", "focus_len"))
        self.assertIn("unknown_operator", self.error_kinds(result))
        self.assertFalse(result.light_ok)

    def test_arity_mismatch_caught(self):
        ke = 'output = { focal_length(C, C) | C: Hyperbola };'
        result = self.validate(ke)
        self.assertIn("arity_mismatch", self.error_kinds(result))

    def test_unbound_name_caught(self):
        ke = 'output = { focal_length(C) | get_expression(C) = sympy_expression("Eq(x, 1)") };'
        # C 没有 membership 绑定（缺 C: Hyperbola）
        result = self.validate(ke)
        self.assertIn("unbound_name", self.error_kinds(result))

    def test_concept_constant_is_legal_bare_ref(self):
        ke = ('output = { get_a_part(Right, C) | C: Hyperbola, '
              'get_expression(C) = sympy_expression("Eq(x**2 - y**2, 1)") };')
        result = self.validate(ke)
        self.assertNotIn("unbound_name", self.error_kinds(result))

    def test_assignment_binds_name(self):
        # F = get_a_focus(Right, C) 是 KE 的定义式绑定写法，后续使用 F 不算未绑定
        ke = ('output = { x_coordinate(F) | C: Ellipse, F = get_a_focus(Right, C) };')
        result = self.validate(ke)
        self.assertNotIn("unbound_name", self.error_kinds(result))
        self.assertTrue(result.light_ok)

    def test_truly_unbound_name_still_caught(self):
        # p1/p2 没有任何声明或定义，必须抓
        ke = ('output = { num_intersection(l, c) | l = line_of(p1, p2), c: Circle, '
              'get_expression(c) = sympy_expression("Eq(x**2 + y**2, 1)") };')
        result = self.validate(ke)
        self.assertIn("unbound_name", self.error_kinds(result))

    def test_missing_output_caught(self):
        result = self.validate("C: Hyperbola;")
        self.assertIn("output_count", self.error_kinds(result))

    def test_parse_failure_short_circuits(self):
        result = self.validate("output = {")
        self.assertFalse(result.lark_ok)
        self.assertFalse(result.light_ok)
        self.assertEqual(result.operators, [])

    def test_bare_name_parses_but_fails_output_check(self):
        # 语法宽松：单个裸名字（中文 NAME）是合法的 arith_individual，
        # lark 能解析，但 light 检查必须因缺 output 判不合格
        result = self.validate("乱写的")
        self.assertTrue(result.lark_ok)
        self.assertFalse(result.light_ok)
        self.assertIn("output_count", {i.kind for i in result.issues})


class GoldKeAllPassTest(unittest.TestCase):
    """20 条真实 gold KE（data/derived/testset20.jsonl）必须全部通过解析与 light 检查。"""

    def test_gold_20_all_valid(self):
        from pathlib import Path
        from kelab import datasets

        jsonl = Path(__file__).resolve().parent.parent / "data" / "derived" / "testset20.jsonl"
        if not jsonl.exists():
            self.skipTest("先跑 prepare 生成 testset20.jsonl")
        domain, _, ontology = load_conic()
        items = datasets.read_jsonl(jsonl)
        self.assertEqual(20, len(items))
        for item in items:
            result = ke_validate.validate_ke(item.gold_ke, domain.grammar_path, ontology)
            self.assertTrue(
                result.light_ok,
                f"{item.item_id}: lark={result.lark_ok} "
                f"issues={[i.message for i in result.issues]}",
            )
            self.assertTrue(item.gold_operators, f"{item.item_id} gold_operators 为空")


if __name__ == "__main__":
    unittest.main()
