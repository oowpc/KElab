import unittest

from kelab import llm
from kelab.llm import ClientConfig, LLMError

from tests.helpers import FakeChatHandler, MockServerMixin


class ChatWithUsageTest(MockServerMixin):
    def make_client(self, model="gen-model", max_retries=3):
        return ClientConfig(
            base_url=self.start_mock(),
            api_key="sk-test",
            model=model,
            temperature=0.0,
            timeout=10.0,
            max_retries=max_retries,
        )

    def test_returns_content_and_usage(self):
        client = self.make_client()
        result = llm.chat_with_usage(client, [{"role": "user", "content": "题"}])
        self.assertIn("output = {", result.content)
        self.assertEqual(100, result.prompt_tokens)
        self.assertEqual(42, result.completion_tokens)
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertEqual(1, result.attempts)

    def test_judge_model_routes_to_verdict(self):
        client = self.make_client(model="judge-model")
        result = llm.chat_with_usage(client, [{"role": "user", "content": "判分"}])
        self.assertIn("equivalent", result.content)

    def test_429_retried_then_success(self):
        client = self.make_client()
        # 前两次 429，第三次成功：用一个会变的 class 属性模拟
        calls = {"n": 0}
        original = FakeChatHandler._respond

        def flaky(handler, status, payload):
            calls["n"] += 1
            if calls["n"] <= 2:
                original(handler, 429, {"error": {"message": "rate limit"}})
            else:
                original(handler, status, payload)

        FakeChatHandler._respond = flaky
        self.addCleanup(setattr, FakeChatHandler, "_respond", original)
        orig_sleep = llm.time.sleep
        llm.time.sleep = lambda s: None  # 不等真实退避
        self.addCleanup(setattr, llm.time, "sleep", orig_sleep)

        result = llm.chat_with_usage(client, [{"role": "user", "content": "题"}])
        self.assertIn("output = {", result.content)
        self.assertEqual(3, result.attempts)

    def test_401_not_retried(self):
        client = self.make_client()
        FakeChatHandler.status = 401  # start_mock 会重置为 200，必须在建客户端之后设置
        with self.assertRaises(LLMError) as caught:
            llm.chat_with_usage(client, [{"role": "user", "content": "题"}])
        self.assertIn("401", str(caught.exception))
        self.assertEqual(1, len(FakeChatHandler.requests))

    def test_max_tokens_sent_when_set(self):
        client = self.make_client()
        client.max_tokens = 256
        llm.chat_with_usage(client, [{"role": "user", "content": "题"}])
        self.assertEqual(256, FakeChatHandler.requests[-1].get("max_tokens"))

    def test_max_tokens_absent_by_default(self):
        client = self.make_client()
        llm.chat_with_usage(client, [{"role": "user", "content": "题"}])
        self.assertNotIn("max_tokens", FakeChatHandler.requests[-1])


if __name__ == "__main__":
    unittest.main()
