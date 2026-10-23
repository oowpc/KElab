#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_PROMPT = Path(__file__).with_name("NL2KE_Prompt.md")
SECTION_MARKER = "## 待转换题目"


class LLMError(RuntimeError):
    pass


@dataclass
class Config:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    timeout: float = 120.0


def load_prompt(path: Path | str = DEFAULT_PROMPT) -> str:
    return Path(path).read_text(encoding="utf-8")


def build_messages(template: str, question: str) -> list[dict[str, str]]:
    marker = template.find(SECTION_MARKER)
    system = (template[:marker] if marker != -1 else template).rstrip()
    user = f"输入：{question.strip()}\n\n输出："
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def clean_output(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```[^\n]*\n(.*?)\n?```", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def resolve_config(args: argparse.Namespace) -> Config:
    base_url = (
        args.base_url
        or os.getenv("NL2KE_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = (
        args.model
        or os.getenv("NL2KE_MODEL")
        or os.getenv("OPENAI_MODEL")
        or DEFAULT_MODEL
    )
    api_key = (
        args.api_key
        or os.getenv("NL2KE_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    if not api_key:
        raise LLMError(
            "未找到 API key：请设置环境变量 NL2KE_API_KEY"
            "（也支持 DEEPSEEK_API_KEY / OPENAI_API_KEY），"
            "或用 --api-key 传入。"
        )
    return Config(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        temperature=args.temperature,
        timeout=args.timeout,
    )


def chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: Sequence[dict[str, str]],
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = body or str(exc)
        raise LLMError(f"API 请求失败（HTTP {exc.code}）：{message}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"网络错误：{exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"请求超时（{timeout:g} 秒）。") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        preview = json.dumps(data, ensure_ascii=False)[:500]
        raise LLMError(f"无法解析 API 响应：{preview}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMError(f"API 返回了空内容：{json.dumps(data, ensure_ascii=False)[:500]}")
    return content


def ask(question: str, template: str, config: Config) -> str:
    messages = build_messages(template, question)
    raw = chat(
        config.base_url,
        config.api_key,
        config.model,
        messages,
        temperature=config.temperature,
        timeout=config.timeout,
    )
    return clean_output(raw)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用 LLM 把中文圆锥曲线题转换成 NL2KE 表达式。"
    )
    parser.add_argument("question", nargs="?", help="单道题目；不传则进入交互模式")
    parser.add_argument(
        "--file", type=Path, help="批量题目文件，每行一题（空行和 # 开头的行会跳过）"
    )
    parser.add_argument("--output", type=Path, help="批量模式下把结果保存为 Markdown")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT, help="prompt 文件路径")
    parser.add_argument("--base-url", help="OpenAI 兼容 API 地址")
    parser.add_argument("--api-key", help="API key")
    parser.add_argument("--model", help="模型名")
    parser.add_argument("--temperature", type=float, default=0.0, help="采样温度，默认 0")
    parser.add_argument("--timeout", type=float, default=120.0, help="请求超时秒数，默认 120")
    return parser.parse_args(argv)


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def run_interactive(template: str, config: Config) -> int:
    print("输入题目后回车生成 KE；输入空行、q 或 exit 退出。", file=sys.stderr)
    while True:
        try:
            question = input("\n题目> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if not question or question.lower() in {"q", "quit", "exit"}:
            return 0
        try:
            print(ask(question, template, config))
        except LLMError as exc:
            print(f"错误：{exc}", file=sys.stderr)


def run_batch(
    questions: list[str], template: str, config: Config, output: Path | None
) -> int:
    results: list[tuple[str, str]] = []
    for index, question in enumerate(questions, 1):
        print(f"[{index}/{len(questions)}] {question}", file=sys.stderr)
        try:
            ke = ask(question, template, config)
        except LLMError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            ke = f"GENERATION_ERROR: {exc}"
        print(ke)
        print()
        results.append((question, ke))

    if output:
        lines = ["# NL2KE 生成结果", ""]
        for question, ke in results:
            lines.extend(["## 题目", "", question, "", "```", ke, "```", ""])
        output.write_text("\n".join(lines), encoding="utf-8")
        print(f"已保存到 {output}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv)
    try:
        config = resolve_config(args)
        template = load_prompt(args.prompt)
    except (LLMError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.file and args.question:
        print("错误：题目参数与 --file 不能同时使用。", file=sys.stderr)
        return 1

    if args.file:
        try:
            questions = [
                line.strip()
                for line in args.file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except OSError as exc:
            print(f"错误：无法读取题目文件：{exc}", file=sys.stderr)
            return 1
        if not questions:
            print("错误：题目文件为空。", file=sys.stderr)
            return 1
        return run_batch(questions, template, config, args.output)

    if args.question:
        try:
            print(ask(args.question, template, config))
        except LLMError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        return 0

    return run_interactive(template, config)


if __name__ == "__main__":
    raise SystemExit(main())
