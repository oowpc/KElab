"""共享测试工具：真实 conic 域资源加载 + OpenAI 兼容 mock server。"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAIN_TOML = ROOT / "configs" / "domains" / "conic.toml"
PROMPT_MD = ROOT / "NL2KE_Prompt.md"
TESTSET_XLSX = ROOT / "testset20.xlsx"

from kelab.config import load_domain  # noqa: E402
from kelab.ontology import load_ontology  # noqa: E402


def load_conic():
    domain = load_domain(DOMAIN_TOML)
    template = PROMPT_MD.read_text(encoding="utf-8")
    ontology = load_ontology("prompt", prompt_text=template)
    return domain, template, ontology


class FakeChatHandler(BaseHTTPRequestHandler):
    """按请求体里的 model 路由响应：gen-model→KE，judge-model→JSON verdict。"""

    requests: list[dict] = []
    status = 200
    ke_content = 'output = { focal_length(C) | C: Hyperbola, get_expression(C) = sympy_expression("Eq(x**2 - y**2, 1)") };'
    verdict_content = '{"verdict": "equivalent", "rationale": "mock 判等"}'
    fail_on_marker: str | None = None  # user 消息含该标记时返回 500
    embed_short = False  # /embeddings 故意少返回一条（错位测试）

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).requests.append(json.loads(body))
        if self.path.endswith("/embeddings"):
            inputs = json.loads(body).get("input", [])
            data = [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(inputs))]
            if type(self).embed_short and data:
                data = data[:-1]  # 故意少一条
            self._respond(200, {"data": data})
            return
        model = json.loads(body).get("model", "")
        if type(self).fail_on_marker and type(self).fail_on_marker in body:
            self._respond(500, {"error": {"message": "injected failure"}})
            return
        content = self.ke_content if model.startswith("gen") else self.verdict_content
        self._respond(type(self).status, {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 42},
        })

    def do_GET(self):
        if self.path.endswith("/models"):
            self._respond(200, {"data": [{"id": "gen-model"}, {"id": "gen-model-2"},
                                         {"id": "judge-model"}]})
        else:
            self._respond(404, {"error": {"message": "not found"}})

    def _respond(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class MockServerMixin(unittest.TestCase):
    def start_mock(self) -> str:
        FakeChatHandler.requests = []
        FakeChatHandler.status = 200
        FakeChatHandler.fail_on_marker = None
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/v1"
