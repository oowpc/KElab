"""HTTP 层：路由、JSON API、静态产物、服务启动。页面渲染在 pages_*，数据在 app。"""
from __future__ import annotations

import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..datasets import count_lines as _count_lines
from .app import WebApp
from .pages_configs import page_config_edit, page_configs, post_config_save
from .pages_experiments import page_compare, page_domains, page_grid, page_launch
from .pages_home import page_home
from .pages_prepare import page_prepare, post_prepare
from .pages_annotate import (page_annotate, page_annotate_draft, page_gold_review,
                             post_annotate_delete, post_annotate_recheck,
                             post_annotate_save, post_gold_review_create,
                             post_gold_review_freeze, post_gold_review_save)
from .pages_prompts import (page_prompt_diff, page_prompt_edit, page_prompt_history,
                            page_prompt_preview, page_prompts, post_prompt_restore,
                            post_prompt_save)
from .pages_matrix import matrix_csv, page_matrix
from .pages_prereg import page_prereg, page_prereg_edit, post_prereg_save
from .pages_runs import (_failure_categories, page_failures, page_gens, page_log,
                         page_review, page_run, page_runs)
from .pages_settings import (page_settings, post_settings_clear, post_settings_save,
                             post_settings_test)
from .ui import ESC, progress_bar, eta_text

# ---------------------------------------------------------------- HTTP 层


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, content: bytes, content_type: str = "text/html; charset=utf-8",
                  status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = urllib.parse.parse_qs(parsed.query)

                if path == "/":
                    return self._send(page_home(app))
                if path == "/runs":
                    return self._send(page_runs(app))
                if path == "/launch":
                    return self._send(page_launch(app, query.get("msg", [""])[0]))
                if path == "/prompts":
                    return self._send(page_prompts(app, query.get("msg", [""])[0]))
                if path == "/prompts/edit":
                    return self._send(page_prompt_edit(app, query, query.get("msg", [""])[0]))
                if path == "/prompts/preview":
                    return self._send(page_prompt_preview(app, query))
                if path == "/prompts/history":
                    return self._send(page_prompt_history(app, query))
                if path == "/prompts/diff":
                    return self._send(page_prompt_diff(app, query))
                if path == "/configs":
                    return self._send(page_configs(app, query.get("msg", [""])[0]))
                if path == "/settings":
                    return self._send(page_settings(app, query.get("msg", [""])[0]))
                if path == "/prereg":
                    return self._send(page_prereg(app, query))
                if path == "/prereg/edit":
                    return self._send(page_prereg_edit(app, query))
                if path == "/matrix":
                    return self._send(page_matrix(app))
                if path == "/matrix.csv":
                    return self._send(matrix_csv(app), "text/csv; charset=utf-8")
                if path == "/configs/new":
                    q = dict(query)
                    q["new"] = [query.get("type", query.get("kind", [""]))[0]]
                    return self._send(page_config_edit(app, q))
                if path == "/compare":
                    return self._send(page_compare(app, query))
                if path == "/domains":
                    return self._send(page_domains(app, query))
                if path == "/grid":
                    return self._send(page_grid(app, query))
                if path == "/prepare":
                    return self._send(page_prepare(app, query))
                if path == "/annotate":
                    return self._send(page_annotate(app, query, query.get("msg", [""])[0]))
                if path == "/annotate/review":
                    return self._send(page_gold_review(app, query, query.get("msg", [""])[0]))
                if path == "/configs/edit":
                    return self._send(page_config_edit(app, query, query.get("msg", [""])[0]))

                parts = path.split("/")
                if len(parts) >= 3 and parts[1] == "run":
                    run_id = parts[2]
                    if len(parts) == 3:
                        return self._send(page_run(app, run_id, query))
                    if parts[3] == "gens":
                        return self._send(page_gens(app, run_id, query))
                    if parts[3] == "failures":
                        return self._send(page_failures(app, run_id))
                    if parts[3] == "failures.csv":
                        return self._send_failures_csv(run_id)
                    if parts[3] == "review":
                        return self._send(page_review(app, run_id, query))
                    if parts[3] == "log":
                        return self._send(page_log(app, run_id))
                    if parts[3] == "review.csv":
                        return self._send_review_csv(run_id)
                if len(parts) == 5 and parts[1] == "api" and parts[2] == "run" \
                        and parts[4] == "progress":
                    return self._send(api_progress(app, parts[3]),
                                      "application/json; charset=utf-8")
                if path.startswith("/files/results/"):
                    return self._send_file(path[len("/files/results/"):])
                if path.startswith("/derived/"):
                    return self._send_derived(path[len("/derived/"):])
                self._send(b"<h1>404</h1>", status=404)
            except Exception as exc:  # 页面级兜底：返回错误页而不是断开
                self._send(
                    f"<h1>500</h1><pre class='mono'>{ESC(type(exc).__name__)}: {ESC(str(exc))}</pre>".encode("utf-8"),
                    status=500,
                )

        def do_POST(self):  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/upload":
                    return self._handle_upload()  # multipart：body 自己读，不走表单解析
                length = int(self.headers.get("Content-Length", "0"))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
                if parsed.path == "/review":
                    run_id = form["run_id"][0]
                    app.save_review(
                        run_id,
                        int(form["gen_id"][0]),
                        form.get("human_verdict", [""])[0],
                        form.get("human_note", [""])[0],
                        reviewer=form.get("reviewer", ["A"])[0],
                        blind=form.get("blind", ["1"])[0] == "1",
                    )
                    reviewer = form.get("reviewer", ["A"])[0]
                    blind = form.get("blind", ["1"])[0]
                    return self._redirect(
                        f"/run/{run_id}/review?gen_id={form['gen_id'][0]}"
                        f"&reviewer={reviewer}&blind={blind}")
                if parsed.path == "/launch":
                    limit_text = form.get("limit", [""])[0].strip()
                    try:
                        limit = int(limit_text) if limit_text else None
                    except ValueError:
                        return self._redirect("/launch?msg=" + urllib.parse.quote(
                            f"试跑题数必须是整数，得到 {limit_text!r}"))
                    prereg_ok = form.get("prereg_ok", [""])[0] == "on"
                    run_id, msg = app.launch(form["exp"][0], limit=limit,
                                             prereg_ok=prereg_ok)
                    if run_id is None:  # 启动守卫拒绝
                        return self._redirect("/launch?msg=" + urllib.parse.quote(msg))
                    return self._redirect(f"/run/{run_id}")
                if parsed.path == "/stop":
                    run_id = form["run_id"][0]
                    _msg = app.stop_run(run_id)
                    return self._redirect(f"/run/{run_id}")
                if parsed.path == "/revalidate":
                    run_id = form["run_id"][0]
                    msg = app.revalidate_run(run_id)
                    return self._redirect(f"/run/{run_id}?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/share":
                    run_id = form["run_id"][0]
                    try:
                        msg = app.share_run(run_id)
                    except Exception as exc:
                        msg = f"导出失败: {exc}"
                    return self._redirect(f"/run/{run_id}?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/rejudge":
                    run_id = form["run_id"][0]
                    try:
                        msg = app.rejudge_run(run_id, form.get("scope", ["errors"])[0])
                    except Exception as exc:
                        msg = f"重判启动失败: {exc}"
                    return self._redirect(f"/run/{run_id}?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/retry_errors":
                    run_id = form["run_id"][0]
                    try:
                        msg = app.retry_errors(run_id)
                    except Exception as exc:
                        msg = f"重试失败: {exc}"
                    return self._redirect(f"/run/{run_id}?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/note":
                    run_id = form["run_id"][0]
                    app.set_note(run_id, form.get("note", [""])[0])
                    return self._redirect(f"/run/{run_id}")
                if parsed.path == "/prepare":
                    return self._send(post_prepare(app, form))
                if parsed.path == "/annotate/draft":
                    return self._send(page_annotate_draft(app, form))
                if parsed.path == "/annotate/recheck":
                    return self._send(post_annotate_recheck(app, form))
                if parsed.path == "/annotate/save":
                    msg = post_annotate_save(app, form)
                    return self._redirect(
                        f"/annotate?domain={form['domain'][0]}&msg={urllib.parse.quote(msg)}")
                if parsed.path == "/annotate/delete":
                    msg = post_annotate_delete(app, form)
                    return self._redirect(
                        f"/annotate?domain={form['domain'][0]}&msg={urllib.parse.quote(msg)}")
                if parsed.path == "/annotate/review_save":
                    msg = post_gold_review_save(app, form)
                    return self._redirect(
                        f"/annotate/review?dir={form['dir'][0]}&page={form.get('page', ['1'])[0]}"
                        f"&msg={urllib.parse.quote(msg)}")
                if parsed.path == "/annotate/review_freeze":
                    msg = post_gold_review_freeze(app, form)
                    return self._redirect(
                        f"/annotate/review?dir={form['dir'][0]}&msg={urllib.parse.quote(msg)}")
                if parsed.path == "/annotate/review_create":
                    msg = post_gold_review_create(app, form)
                    return self._redirect("/annotate?msg=" + urllib.parse.quote(msg))
                if parsed.path == "/launch_batch":
                    items = [(p, None) for p in form.get("exp", [])]
                    if not items:
                        return self._redirect(
                            "/launch?msg=" + urllib.parse.quote("请先勾选要批量启动的实验"))
                    msg = app.enqueue_launch(
                        items, prereg_ok=form.get("prereg_ok", [""])[0] == "on")
                    return self._redirect("/launch?msg=" + urllib.parse.quote(msg))
                if parsed.path == "/prompts/save":
                    msg = post_prompt_save(app, form)
                    domain = form["domain"][0]
                    return self._redirect(
                        f"/prompts/edit?domain={domain}&msg={urllib.parse.quote(msg)}"
                    )
                if parsed.path == "/prompts/restore":
                    msg = post_prompt_restore(app, form)
                    domain = form["domain"][0]
                    return self._redirect(
                        f"/prompts/history?domain={domain}&msg={urllib.parse.quote(msg)}"
                    )
                if parsed.path == "/configs/save":
                    msg = post_config_save(app, form)
                    return self._redirect(f"/configs?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/settings/save":
                    msg = post_settings_save(app, form)
                    return self._redirect(f"/settings?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/settings/test":
                    msg = post_settings_test(app, form)
                    return self._redirect(f"/settings?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/settings/clear":
                    msg = post_settings_clear(app)
                    return self._redirect(f"/settings?msg={urllib.parse.quote(msg)}")
                if parsed.path == "/prereg/save":
                    msg = post_prereg_save(app, form)
                    return self._redirect(f"/prereg?msg={urllib.parse.quote(msg)}")
                self._send(b"<h1>404</h1>", status=404)
            except Exception as exc:
                self._send(
                    f"<h1>500</h1><pre class='mono'>{ESC(type(exc).__name__)}: {ESC(str(exc))}</pre>".encode("utf-8"),
                    status=500,
                )

        def _send_failures_csv(self, run_id: str) -> None:
            import csv
            import io

            store = app.open_run(run_id)
            categories = _failure_categories(store, run_id)
            store.close()
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["category", "gen_id", "condition_id", "item_id", "rep", "error"])
            for cat in categories:
                for r in cat["rows"]:
                    writer.writerow([cat["title"], r["gen_id"], r["condition_id"],
                                     r["item_id"], r["rep"], (r["error"] or "")])
            self._send(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"),
                       "text/csv; charset=utf-8")

        def _send_review_csv(self, run_id: str) -> None:
            import csv
            import io

            store = app.open_run(run_id)
            finals = app.get_reviews(run_id)      # 最终判定（C>一致>单人）
            raw = app.get_reviews_raw(run_id)      # 全部逐评审员行（含分歧证据）
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["gen_id", "condition_id", "item_id", "rep", "llm_verdict",
                             "reviewer", "human_verdict", "human_note", "blind",
                             "is_final", "reviewed_at"])
            for r in raw:
                row = store._conn.execute(
                    "SELECT g.condition_id, g.item_id, g.rep, j.llm_verdict"
                    " FROM generations g LEFT JOIN judgments j ON j.gen_id=g.gen_id"
                    " WHERE g.gen_id=?", (r["gen_id"],),
                ).fetchone()
                fin = finals.get(r["gen_id"])
                is_final = bool(fin and fin["reviewer"] == r["reviewer"]
                                and fin["human_verdict"] == r["human_verdict"])
                writer.writerow([
                    r["gen_id"], row["condition_id"] if row else "", row["item_id"] if row else "",
                    row["rep"] if row else "", row["llm_verdict"] if row else "",
                    r["reviewer"], r["human_verdict"], r["human_note"],
                    r["blind"] if isinstance(r, dict) else r["blind"],
                    int(is_final), r["reviewed_at"],
                ])
            store.close()
            self._send(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"),
                       "text/csv; charset=utf-8")

        def _send_file(self, rel: str) -> None:
            target = (app.results_dir / rel).resolve()
            if not str(target).startswith(str(app.results_dir.resolve())) or not target.is_file():
                return self._send(b"<h1>404</h1>", status=404)
            mime = {".png": "image/png", ".csv": "text/csv; charset=utf-8",
                    ".md": "text/markdown; charset=utf-8"}.get(target.suffix, "application/octet-stream")
            self._send(target.read_bytes(), mime)

        def _send_derived(self, rel: str) -> None:
            """data/derived 下的只读产物（manifest/泄露报告等），穿越防护同 _send_file。"""
            base = (app.root / "data" / "derived").resolve()
            target = (base / rel).resolve()
            if not str(target).startswith(str(base)) or not target.is_file():
                return self._send(b"<h1>404</h1>", status=404)
            mime = {".json": "application/json; charset=utf-8",
                    ".jsonl": "text/plain; charset=utf-8",
                    ".csv": "text/csv; charset=utf-8"}.get(target.suffix, "text/plain; charset=utf-8")
            self._send(target.read_bytes(), mime)

        def _handle_upload(self) -> None:
            """数据源上传（stdlib 手写 multipart 解析；≤60MB；落 data/uploads/）。"""
            import re

            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype or "boundary=" not in ctype:
                return self._send("<h1>400</h1>需要 multipart/form-data".encode("utf-8"), status=400)
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 60 * 1024 * 1024:
                return self._send("<h1>413</h1>空文件或超过 60MB 上限".encode("utf-8"), status=413)
            body = self.rfile.read(length)
            boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
            filename = None
            content = None
            for part in body.split(b"--" + boundary):
                if b"content-disposition" not in part.lower():
                    continue
                head, _, payload = part.partition(b"\r\n\r\n")
                if not payload:
                    continue
                match = re.search(rb'filename="([^"]*)"', head)
                if match and match.group(1):
                    filename = match.group(1).decode("utf-8", "replace")
                    content = payload.rstrip(b"\r\n")  # 去掉结尾 CRLF（boundary 前的）
            if not filename or content is None:
                return self._redirect("/prepare?msg=" + urllib.parse.quote("未收到文件"))
            safe = re.sub(r"[^A-Za-z0-9_.\-一-鿿]", "_", Path(filename).name)
            dest_dir = app.root / "data" / "uploads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / safe
            dest.write_bytes(content)
            rel = dest.relative_to(app.root).as_posix()
            return self._redirect(
                "/prepare?picked=" + urllib.parse.quote(rel)
                + "&msg=" + urllib.parse.quote(f"已上传 {safe}（{len(content) // 1024} KB），已填入数据源"))

    return Handler


def api_progress(app: WebApp, run_id: str) -> bytes:
    try:
        store = app.open_run(run_id)
    except KeyError:
        return json.dumps({"status": "missing"}).encode("utf-8")
    info = store.run_info(run_id)
    exp = app.find_experiment(run_id)
    conditions: dict[str, dict] = {}
    total_done = 0
    for cond in store.condition_rows(run_id):
        rows = store._conn.execute(
            "SELECT status, COUNT(*) c FROM generations"
            " WHERE run_id=? AND condition_id=? GROUP BY status",
            (run_id, cond["condition_id"]),
        ).fetchall()
        counts = {r["status"]: r["c"] for r in rows}
        done = sum(counts.values())
        total_done += done
        expected = None
        if exp is not None:
            testset = app.root / exp.testset
            if testset.exists():
                n_items = _count_lines(testset)
                limit = exp.limit_items or app._run_limit(store, run_id)
                if limit:
                    n_items = min(n_items, limit)
                expected = n_items * exp.repeats
        conditions[cond["condition_id"]] = {
            "done": done, "expected": expected,
            "html": progress_bar(done, expected),
        }
    counts = app._progress_counts(store, run_id)
    eta = ""
    if info and info["status"] == "running":
        eta = eta_text(store, run_id, counts["expected"], total_done)
    store.close()
    return json.dumps({
        "status": info["status"] if info else "unknown",
        "conditions": conditions,
        "overall_html": progress_bar(total_done, counts["expected"], eta),
    }, ensure_ascii=False).encode("utf-8")


def serve(root: Path, port: int = 8765, open_browser: bool = False) -> None:
    app = WebApp(root)
    app.reconcile_runs()  # 启动整理：僵死 running 标记 interrupted
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}/"
    print(f"kelab 看板: {url}（Ctrl+C 停止）", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in app.launched.values():
            if proc.poll() is None:
                proc.terminate()
        server.server_close()
