import unittest
from pathlib import Path

from kelab import doctor
from kelab.config import file_md5, load_domain

ROOT = Path(__file__).resolve().parent.parent


class DoctorTest(unittest.TestCase):
    def test_vendored_grammar_matches_domain_md5(self):
        # 内嵌语法必须与 domain 配置锁定的 md5 一致（防漂移）
        domain = load_domain(ROOT / "configs" / "domains" / "conic.toml")
        grammar = ROOT / "kelab" / "grammar" / "ke_syntax.lark"
        self.assertTrue(grammar.exists(), "内嵌语法文件应存在")
        self.assertEqual(domain.grammar_md5.lower(), file_md5(grammar))

    def test_checks_on_this_repo(self):
        checks = doctor.run_checks(ROOT)
        by_name = {c.name: c for c in checks}
        self.assertTrue(by_name["Python 版本"].ok)
        self.assertTrue(by_name["Python 依赖包"].ok)
        self.assertTrue(by_name["运行目录"].ok)
        self.assertTrue(by_name["数据目录可写"].ok)
        # 领域检查存在且通过（内嵌语法 + 模板就位）
        domain_checks = [c for c in checks if c.name.startswith("领域 conic")]
        self.assertTrue(domain_checks)
        self.assertTrue(all(c.ok for c in domain_checks),
                        "; ".join(c.detail for c in domain_checks))

    def test_cwd_check_fails_elsewhere(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            check = doctor._check_cwd(Path(tmp))
        self.assertFalse(check.ok)
        self.assertIn("cd 到项目根目录", check.fix)

    def test_report_marks_failures_with_fix(self):
        checks = [doctor.Check("假检查", False, "坏了", "这样修")]
        report = doctor.format_report(checks)
        self.assertIn("✗ 假检查", report)
        self.assertIn("修复：这样修", report)
        self.assertIn("1 项未通过", report)


class RequirementsFileTest(unittest.TestCase):
    def test_requirements_lists_runtime_deps(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for dep in ("lark", "matplotlib", "openpyxl", "numpy"):
            self.assertIn(dep, text)


if __name__ == "__main__":
    unittest.main()
