"""API 设置（前端配置 base_url/key + 模型下拉）测试。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from kelab import doctor
from kelab.llm import resolve_api
from kelab.settings import load_api_settings, mask_key, save_api_settings
from kelab.web import WebApp, make_handler

from tests.helpers import FakeChatHandler
from tests.test_web import seed_root


class SettingsFileTest(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api.json"
            save_api_settings({"base_url": "http://x/v1", "api_key": "sk-abc"}, path)
            self.assertEqual({"base_url": "http://x/v1", "api_key": "sk-abc"},
                             load_api_settings(path))
        self.assertEqual({}, load_api_settings(Path(tmp) / "不存在.json"))

    def test_mask_key(self):
        # 假 key，真 key 不得进仓库
        self.assertEqual("sk-…abcd", mask_key("sk-FAKE-KEY-0123456789abcd"))
        self.assertEqual("", mask_key(""))

    def test_resolve_api_prefers_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_api_settings({"base_url": "http://file/v1", "api_key": "file-key"},
                              Path(tmp) / "configs" / "api.json")
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with patch.dict("os.environ", {"NL2KE_API_KEY": "env-key",
                                               "NL2KE_BASE_URL": "http://env/v1"}):
                    cfg = resolve_api("m")
            finally:
                os.chdir(cwd)
            self.assertEqual("file-key", cfg.api_key)       # 设置文件 > 环境变量
            self.assertEqual("http://file/v1", cfg.base_url)

    def test_resolve_api_falls_back_to_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)  # 无设置文件
                with patch.dict("os.environ", {"NL2KE_API_KEY": "env-key",
                                               "NL2KE_BASE_URL": "http://env/v1"}):
                    cfg = resolve_api("m")
            finally:
                os.chdir(cwd)
            self.assertEqual("env-key", cfg.api_key)

    def test_embed_config_reads_settings_file(self):
        from kelab.retrieve import _embed_config, DenseError

        with tempfile.TemporaryDirectory() as tmp:
            save_api_settings(
                {"base_url": "http://main/v1", "api_key": "main-key",
                 "embed_base_url": "http://embed/v1", "embed_api_key": "embed-key",
                 "embed_model": "text-embedding-v4"},
                Path(tmp) / "configs" / "api.json")
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                base_url, key, model = _embed_config()
            finally:
                os.chdir(cwd)
            self.assertEqual("http://embed/v1", base_url)
            self.assertEqual("embed-key", key)
            self.assertEqual("text-embedding-v4", model)

    def test_embed_config_fallbacks_and_missing_model(self):
        from kelab.retrieve import _embed_config, DenseError

        with tempfile.TemporaryDirectory() as tmp:
            save_api_settings({"base_url": "http://main/v1", "api_key": "main-key"},
                              Path(tmp) / "configs" / "api.json")
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with self.assertRaises(DenseError):
                    _embed_config()  # 没有 embed_model → 明确报错
                # 补上模型：base_url/key 回落到主配置
                save_api_settings(
                    {"base_url": "http://main/v1", "api_key": "main-key",
                     "embed_model": "m2"},
                    Path(tmp) / "configs" / "api.json")
                base_url, key, model = _embed_config()
            finally:
                os.chdir(cwd)
            self.assertEqual("http://main/v1", base_url)
            self.assertEqual("main-key", key)
            self.assertEqual("m2", model)

    def test_doctor_accepts_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_api_settings({"base_url": "http://x/v1", "api_key": "sk-abc"},
                              Path(tmp) / "configs" / "api.json")
            check = doctor._check_api_key(Path(tmp))
        self.assertTrue(check.ok)
        self.assertIn("configs/api.json", check.detail)


class SettingsPageTest(unittest.TestCase):
    """独立 fixture（不继承 WebTest，避免共享现场与重复跑）。"""

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
        cls.gateway_base = f"http://127.0.0.1:{cls.gateway.server_port}"
        cls.api_file = cls.root / "configs" / "api.json"
        cls.api_file.write_text(
            json.dumps({"base_url": cls.gateway_base + "/v1",
                        "api_key": "sk-testkey12345"}),
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
        import urllib.parse
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

    def restore_api_file(self):
        self.api_file.write_text(
            json.dumps({"base_url": self.gateway_base + "/v1",
                        "api_key": "sk-testkey12345"}),
            encoding="utf-8")

    def test_settings_page_and_models(self):
        status, body = self.get("/settings")
        self.assertEqual(200, status)
        self.assertIn("API 设置", body)
        self.assertIn("gen-model", body)  # 模型列表来自 mock 网关
        self.assertIn("sk-…2345", body)   # key 脱敏显示

    def test_settings_save_writes_file(self):
        try:
            status, _ = self.post("/settings/save", {
                "base_url": "http://new/v9", "api_key": "sk-newkey",
                "embed_base_url": "http://emb/v2", "embed_model": "text-embedding-v4",
            })
            self.assertIn(status, (200, 303))
            saved = json.loads(self.api_file.read_text(encoding="utf-8"))
            self.assertEqual("http://new/v9", saved["base_url"])
            self.assertEqual("sk-newkey", saved["api_key"])
            self.assertEqual("http://emb/v2", saved["embed_base_url"])
            self.assertEqual("text-embedding-v4", saved["embed_model"])
        finally:
            self.restore_api_file()  # 不污染其它测试

    def test_settings_test_connection_via_mock_gateway(self):
        # 显式用 mock 网关测连接（/models 免费验证），不受保存测试影响；
        # post() 已跟随 303 重定向，结果就在返回体里
        status, body = self.post("/settings/test",
                                 {"base_url": self.gateway_base + "/v1",
                                  "api_key": "sk-testkey12345"})
        self.assertIn(status, (200, 303))
        self.assertIn("连接正常", body)
        self.assertIn("3 个可用模型", body)

    def test_config_form_has_model_datalist(self):
        status, body = self.get(
            f"/configs/edit?path={self.root}/configs/domains/conic.toml")
        self.assertEqual(200, status)
        self.assertIn("datalist id='model-list'", body)
        self.assertIn("list='model-list'", body)


if __name__ == "__main__":
    unittest.main()
