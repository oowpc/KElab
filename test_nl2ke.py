import argparse
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import nl2ke


class FakeChatHandler(BaseHTTPRequestHandler):
    requests = []
    status = 200
    response = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        payload = json.dumps(type(self).response, ensure_ascii=False).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class BuildMessagesTest(unittest.TestCase):
    def test_splits_examples_from_question_section(self):
        template = "系统说明\n## 示例\n示例内容\n## 待转换题目\n\n输入：{{题目}}\n\n输出：\n"

        messages = nl2ke.build_messages(template, "双曲线x^{2}-y^{2}=1的焦距为?")

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("示例内容", messages[0]["content"])
        self.assertNotIn("待转换题目", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "输入：双曲线x^{2}-y^{2}=1的焦距为?\n\n输出：")

    def test_system_prompt_keeps_ontology(self):
        template = "本体\n## 待转换题目\n输入：{{题目}}"
        messages = nl2ke.build_messages(template, "题目")
        self.assertIn("本体", messages[0]["content"])


class CleanOutputTest(unittest.TestCase):
    def test_unwraps_markdown_code_fence(self):
        text = "```\noutput = { focal_length(C) | C: Hyperbola };\n```"
        self.assertEqual(
            nl2ke.clean_output(text),
            "output = { focal_length(C) | C: Hyperbola };",
        )

    def test_strips_whitespace_from_plain_output(self):
        self.assertEqual(nl2ke.clean_output("  output = { m };\n"), "output = { m };")


class ResolveConfigTest(unittest.TestCase):
    def make_args(self, **overrides):
        values = {
            "base_url": None,
            "api_key": None,
            "model": None,
            "temperature": 0.0,
            "timeout": 120.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_uses_defaults_and_environment(self):
        env = {
            "NL2KE_API_KEY": "env-key",
            "NL2KE_BASE_URL": "https://example.com/v1",
            "NL2KE_MODEL": "env-model",
        }
        with patch.dict("os.environ", env, clear=True):
            config = nl2ke.resolve_config(self.make_args())

        self.assertEqual(config.api_key, "env-key")
        self.assertEqual(config.base_url, "https://example.com/v1")
        self.assertEqual(config.model, "env-model")

    def test_default_model_is_deepseek_flash(self):
        with patch.dict("os.environ", {"NL2KE_API_KEY": "env-key"}, clear=True):
            config = nl2ke.resolve_config(self.make_args())

        self.assertEqual(config.model, "deepseek-flash")

    def test_cli_values_override_environment(self):
        env = {"NL2KE_API_KEY": "env-key"}
        with patch.dict("os.environ", env, clear=True):
            config = nl2ke.resolve_config(
                self.make_args(api_key="cli-key", model="cli-model")
            )

        self.assertEqual(config.api_key, "cli-key")
        self.assertEqual(config.model, "cli-model")

    def test_missing_api_key_is_explained(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(nl2ke.LLMError) as caught:
                nl2ke.resolve_config(self.make_args())

        self.assertIn("NL2KE_API_KEY", str(caught.exception))


class AskTest(unittest.TestCase):
    def setUp(self):
        FakeChatHandler.requests = []
        FakeChatHandler.status = 200
        FakeChatHandler.response = {
            "choices": [
                {
                    "message": {
                        "content": "```\noutput = { m | C: Ellipse };\n```"
                    }
                }
            ]
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.addCleanup(self.thread.join, 1)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_returns_cleaned_ke_and_sends_examples(self):
        config = nl2ke.Config(
            base_url=self.base_url,
            api_key="sk-test",
            model="test-model",
            temperature=0.0,
            timeout=30.0,
        )
        template = "示例\n## 待转换题目\n输入：{{题目}}"

        result = nl2ke.ask("题目内容", template, config)

        self.assertEqual(result, "output = { m | C: Ellipse };")
        body = json.loads(FakeChatHandler.requests[-1]["body"])
        self.assertIn("示例", body["messages"][0]["content"])
        self.assertIn("题目内容", body["messages"][1]["content"])


class ChatTest(unittest.TestCase):
    def setUp(self):
        FakeChatHandler.requests = []
        FakeChatHandler.status = 200
        FakeChatHandler.response = {
            "choices": [
                {"message": {"content": "output = { focal_length(C) | C: Hyperbola };"}}
            ]
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.addCleanup(self.thread.join, 1)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_posts_openai_compatible_request_and_returns_content(self):
        messages = [{"role": "user", "content": "测试"}]

        content = nl2ke.chat(self.base_url, "sk-test", "test-model", messages)

        self.assertEqual(content, "output = { focal_length(C) | C: Hyperbola };")
        request = FakeChatHandler.requests[-1]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["authorization"], "Bearer sk-test")
        body = json.loads(request["body"])
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"], messages)
        self.assertFalse(body["stream"])

    def test_reports_api_error_message(self):
        FakeChatHandler.status = 401
        FakeChatHandler.response = {"error": {"message": "invalid api key"}}

        with self.assertRaises(nl2ke.LLMError) as caught:
            nl2ke.chat(self.base_url, "bad-key", "test-model", [])

        self.assertIn("401", str(caught.exception))
        self.assertIn("invalid api key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
