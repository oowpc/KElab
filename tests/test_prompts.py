import unittest

from kelab import prompts
from kelab.datasets import Item

from tests.helpers import load_conic


def ex(item_id: str, question: str, ke: str) -> Item:
    return Item(item_id=item_id, question=question, gold_ke=ke)


class SplitTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.template, _ = load_conic()

    def test_head_contains_role_and_ontology_not_examples(self):
        head = prompts.split_template(self.template)
        self.assertIn("NL2KE 标注专家", head)
        self.assertIn("**operators**", head)
        self.assertIn("## 输出要求", head)
        self.assertNotIn("### 示例 1", head)
        self.assertNotIn("## 待转换题目", head)

    def test_missing_markers_raises(self):
        with self.assertRaises(prompts.PromptError):
            prompts.split_template("没有标记的模板")


class AssembleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain, cls.template, _ = load_conic()

    def assemble(self, examples, question="双曲线x^{2}-y^{2}=1的焦距为?"):
        return prompts.assemble(self.template, self.domain.question_format, examples, question)

    def test_k0_has_no_examples_section(self):
        prompt = self.assemble([])
        self.assertNotIn("## 示例", prompt.system)
        self.assertIn("NL2KE 标注专家", prompt.system)
        self.assertEqual("输入：双曲线x^{2}-y^{2}=1的焦距为?\n\n输出：", prompt.user)

    def test_examples_rendered_in_order_with_original_layout(self):
        examples = [ex("a", "题甲", "KE_A"), ex("b", "题乙", "KE_B")]
        prompt = self.assemble(examples)
        self.assertIn("## 示例", prompt.system)
        self.assertIn("### 示例 1\n\n输入：题甲\n\n输出：\n\n```\nKE_A\n```", prompt.system)
        self.assertIn("### 示例 2\n\n输入：题乙\n\n输出：\n\n```\nKE_B\n```", prompt.system)
        # 顺序保持检索返回顺序
        self.assertLess(prompt.system.index("KE_A"), prompt.system.index("KE_B"))

    def test_same_input_same_hash(self):
        examples = [ex("a", "题甲", "KE_A")]
        p1 = self.assemble(examples)
        p2 = self.assemble(examples)
        self.assertEqual(p1.prompt_hash, p2.prompt_hash)
        self.assertEqual(p1.static_hash, p2.static_hash)

    def test_different_examples_different_prompt_hash_same_static(self):
        p1 = self.assemble([ex("a", "题甲", "KE_A")])
        p2 = self.assemble([ex("b", "题乙", "KE_B")])
        self.assertNotEqual(p1.prompt_hash, p2.prompt_hash)
        self.assertEqual(p1.static_hash, p2.static_hash)  # 静态部分与示例无关

    def test_messages_structure(self):
        prompt = self.assemble([ex("a", "题甲", "KE_A")])
        messages = prompts.to_messages(prompt)
        self.assertEqual(["system", "user"], [m["role"] for m in messages])
        self.assertIn("KE_A", messages[0]["content"])
        self.assertNotIn("KE_A", messages[1]["content"])


class LinkedTemplateTest(unittest.TestCase):
    """本体注入对照：build_linked_template 裁剪渲染。"""

    @classmethod
    def setUpClass(cls):
        _, cls.template, cls.ontology = load_conic()

    def test_subset_rendering_keeps_only_selected(self):
        subset = ["focal_length", "get_expression", "sympy_expression"]
        linked = prompts.build_linked_template(self.template, self.ontology, subset)
        self.assertIn("op focal_length(c: ConicSection) -> Number", linked)
        self.assertIn("op get_expression(c: Curve) -> Expression", linked)
        self.assertNotIn("op asymptote", linked)  # 未选中的算子不得出现
        self.assertNotIn("op eccentricity", linked)

    def test_closure_includes_signature_concepts(self):
        subset = ["focal_length"]
        linked = prompts.build_linked_template(self.template, self.ontology, subset)
        # focal_length(c: ConicSection) -> Number → ConicSection 与 Number 在概念块
        concepts_block = linked.split("**concepts**")[1].split("**operators**")[0]
        self.assertIn("ConicSection", concepts_block)
        self.assertIn("Number", concepts_block)
        self.assertNotIn("Hyperbola", concepts_block)  # 签名未涉及的概念不出现

    def test_rest_of_template_verbatim(self):
        subset = ["focal_length", "get_expression", "sympy_expression"]
        linked = prompts.build_linked_template(self.template, self.ontology, subset)
        # 角色与输出要求逐字保留；示例区与题目标记仍在（供 assemble 切分）
        self.assertIn("NL2KE 标注专家", linked)
        self.assertIn("## 输出要求", linked)
        self.assertIn("## 示例", linked)
        self.assertIn("## 待转换题目", linked)
        # 且能被 assemble 正常切分使用
        prompt = prompts.assemble(linked, "输入：{question}\n\n输出：", [], "题")
        self.assertNotIn("op asymptote", prompt.system)

    def test_concepts_closure_order_and_types(self):
        closure = prompts.concepts_closure(["get_a_focus"], self.ontology)
        # get_a_focus(location: Location, c: ConicSection) -> Point
        self.assertIn("Location", closure)
        self.assertIn("ConicSection", closure)
        self.assertIn("Point", closure)
        # 顺序按本体原顺序（Concept 块定义序）
        original = self.ontology.ordered_concepts()
        idx = [original.index(c) for c in closure]
        self.assertEqual(idx, sorted(idx))


if __name__ == "__main__":
    unittest.main()