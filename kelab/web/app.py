"""看板数据层 WebApp：运行发现、进度、成本、reviews、启动/队列、图表渲染调度、
rejudge/revalidate、状态自愈。所有页面通过它拿数据，不直接碰 SQLite。"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ..config import ConfigError, load_experiment
from ..store import Store
from .ui import ESC

# ---------------------------------------------------------------- 数据访问


class WebApp:
    def __init__(self, root: Path):
        self.root = root
        self.runs_dir = root / "runs"
        self.results_dir = root / "results"
        self.experiments_dir = root / "configs" / "experiments"
        self.launched: dict[str, subprocess.Popen] = {}
        self._exp_cache: dict[str, object] = {}
        self._exp_path_cache: dict[str, Path] = {}
        self._chart_lock = threading.Lock()  # matplotlib 全局状态，渲染串行化
        self._chart_state: dict[str, dict] = {}
        self._rejudge_state: dict[str, dict] = {}
        self._launch_queue: list[tuple[str, int | None]] = []
        self._queue_worker_alive = False
        self._models_cache: dict = {}

    # ---- 运行发现 ----

    def reconcile_runs(self) -> None:
        """状态自愈：扫描所有 run，把僵死的 running 标为 interrupted。
        本会话启动且进程仍活的 run 豁免（它们是真在跑）。"""
        if not self.runs_dir.is_dir():
            return
        for db in self.runs_dir.glob("*/kelab.sqlite"):
            run_id = db.parent.name
            proc = self.launched.get(run_id)
            if proc is not None and proc.poll() is None:
                continue  # 本会话的活进程
            try:
                store = Store(db)  # 写模式（含迁移）
                store.mark_interrupted_stale_runs()
                store.close()
            except sqlite3.Error:
                continue

    def set_note(self, run_id: str, note: str) -> None:
        store = Store(self.runs_dir / run_id / "kelab.sqlite")
        try:
            store.set_note(run_id, note[:200])
        finally:
            store.close()

    def read_log(self, run_id: str, tail: int = 200) -> str:
        """读 runs/<id>/console.log 尾部（web 启动的 run 才有日志文件）。"""
        log = self.runs_dir / run_id / "console.log"
        if not log.exists():
            return ""
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def list_runs(self) -> list[dict]:
        out: list[dict] = []
        if not self.runs_dir.is_dir():
            return out
        for db in sorted(self.runs_dir.glob("*/kelab.sqlite"),
                         key=lambda p: p.stat().st_mtime, reverse=True):
            run_id = db.parent.name
            try:
                store = Store(db, readonly=True)
                info = store.run_info(run_id)
                counts = self._progress_counts(store, run_id)
                store.close()
            except sqlite3.Error:
                continue
            if info is None:
                continue
            out.append({
                "run_id": run_id,
                "started_at": info["started_at"],
                "status": info["status"],
                "note": info["note"] or "" if "note" in info.keys() else "",
                **counts,
            })
        return out

    def open_run(self, run_id: str) -> Store:
        db = self.runs_dir / run_id / "kelab.sqlite"
        if not db.exists():
            raise KeyError(run_id)
        return Store(db, readonly=True)

    def _progress_counts(self, store: Store, run_id: str) -> dict:
        rows = store._conn.execute(
            "SELECT status, COUNT(*) c FROM generations WHERE run_id=? GROUP BY status",
            (run_id,),
        ).fetchall()
        counts = {r["status"]: r["c"] for r in rows}
        exp = self.find_experiment(run_id)
        expected = None
        if exp is not None:
            testset = self.root / exp.testset
            if testset.exists():
                n_items = sum(1 for line in testset.open(encoding="utf-8") if line.strip())
                limit = exp.limit_items or self._run_limit(store, run_id)
                if limit:
                    n_items = min(n_items, limit)
                n_conds = len(store.condition_rows(run_id))
                expected = n_items * exp.repeats * max(n_conds, 1)
        return {
            "ok": counts.get("ok", 0),
            "error": counts.get("error", 0),
            "expected": expected,
        }

    def retry_errors(self, run_id: str) -> str:
        """重试失败条：删除 status=error 的 generation（连同其 validation/judgment），
        再按同配置断点续跑——error 条目会重新生成。
        适用场景：长跑中被限流/网络抖出的失败。返回提示消息。"""
        exp = self.find_experiment(run_id)
        if exp is None:
            return "找不到实验配置，无法重试"
        store = Store(self.runs_dir / run_id / "kelab.sqlite")
        try:
            with store._lock, store._conn:
                error_ids = [r["gen_id"] for r in store._conn.execute(
                    "SELECT gen_id FROM generations WHERE run_id=? AND status='error'",
                    (run_id,),
                ).fetchall()]
                if not error_ids:
                    return "没有失败条需要重试"
                placeholders = ",".join("?" for _ in error_ids)
                store._conn.execute(
                    f"DELETE FROM validations WHERE gen_id IN ({placeholders})", error_ids)
                store._conn.execute(
                    f"DELETE FROM judgments WHERE gen_id IN ({placeholders})", error_ids)
                store._conn.execute(
                    f"DELETE FROM generations WHERE gen_id IN ({placeholders})", error_ids)
        finally:
            store.close()
        # 找到实验 toml 路径再启动（同配置 → 断点续跑只补刚删掉的缺口）
        exp_path = self._exp_path_cache.get(exp.name)
        if exp_path is None:
            self.find_experiment(run_id)  # 扫描会填充路径缓存
            exp_path = self._exp_path_cache.get(exp.name)
        if exp_path is None:
            return f"已删除 {len(error_ids)} 条失败记录，但找不到实验配置文件，请手动启动"
        limit = None
        info_store = self.open_run(run_id)
        limit = self._run_limit(info_store, run_id)
        info_store.close()
        self.launch(str(exp_path), limit=limit)
        return f"已删除 {len(error_ids)} 条失败记录并重新启动（断点续跑只补这些缺口）"

    def _run_limit(self, store: Store, run_id: str) -> int | None:
        """从 runs.config_json 读试跑题数（CLI --limit 启动的 run 也能识别）。"""
        info = store.run_info(run_id)
        if info is None:
            return None
        try:
            return json.loads(info["config_json"]).get("limit_items")
        except (json.JSONDecodeError, TypeError):
            return None

    def find_experiment(self, run_id: str):
        """run_id = <实验名>-<hash>，按实验名在 configs/experiments 里找配置（缓存）。
        同时缓存实验名 → toml 路径（重试失败/重启需要）。"""
        name = run_id.rsplit("-", 1)[0]
        if name in self._exp_cache:
            return self._exp_cache[name]
        if self.experiments_dir.is_dir():
            for toml in self.experiments_dir.glob("*.toml"):
                try:
                    exp = load_experiment(toml)
                except ConfigError:
                    continue
                self._exp_cache.setdefault(exp.name, exp)
                self._exp_path_cache.setdefault(exp.name, toml)
        return self._exp_cache.get(name)

    # ---- reviews（独立短事务，避免与 runner 写锁冲突）----

    def _reviews_conn(self, run_id: str, create: bool = False) -> sqlite3.Connection:
        db = self.runs_dir / run_id / "kelab.sqlite"
        conn = sqlite3.connect(db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if create:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reviews ("
                " gen_id INTEGER PRIMARY KEY, human_verdict TEXT, human_note TEXT, reviewed_at TEXT)"
            )
        return conn

    def save_review(self, run_id: str, gen_id: int, verdict: str, note: str) -> None:
        conn = self._reviews_conn(run_id, create=True)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO reviews(gen_id, human_verdict, human_note, reviewed_at)"
                    " VALUES (?,?,?,?)",
                    (gen_id, verdict, note,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
        finally:
            conn.close()

    def get_reviews(self, run_id: str) -> dict[int, sqlite3.Row]:
        conn = self._reviews_conn(run_id)
        try:
            rows = conn.execute("SELECT * FROM reviews").fetchall()
            return {r["gen_id"]: r for r in rows}
        except sqlite3.OperationalError:
            return {}  # reviews 表还没建（尚未保存过任何人工判定）
        finally:
            conn.close()

    # ---- 图表自动渲染 ----

    def maybe_render_charts(self, run_id: str, sync: bool = False) -> str:
        """数据有更新就（重新）生成图表与报告。返回 fresh|rendering|skipped|failed。

        判定口径：当前 generation 总数 ≠ 上次出图时的总数，或 PNG 不存在。
        渲染放后台线程（matplotlib 全局锁串行），页面先展示旧图或占位。
        """
        exp = self.find_experiment(run_id)
        if exp is None:
            return "skipped"
        try:
            store = self.open_run(run_id)
        except KeyError:
            return "skipped"
        total = store._conn.execute(
            "SELECT COUNT(*) c FROM generations WHERE run_id=?", (run_id,)
        ).fetchone()["c"]
        store.close()
        n_reviews = len(self.get_reviews(run_id))
        if total == 0:
            return "skipped"
        figs_exist = any(self.results_dir.glob(f"{run_id}/*.png"))
        state = self._chart_state.get(run_id, {})
        if state.get("count") == -1 and not figs_exist:
            return "failed"
        # 版本 = 生成数 + 抽检数：人工判定变了也要重画（解读卡/报告含一致率）
        if figs_exist and state.get("count") == total \
                and state.get("reviews", 0) == n_reviews:
            return "fresh"
        if state.get("rendering"):
            return "rendering"
        state["rendering"] = True
        self._chart_state[run_id] = state

        def work() -> None:
            try:
                with self._chart_lock:
                    self._render_report_sync(run_id, exp)
                self._chart_state[run_id] = {"count": total, "reviews": n_reviews,
                                             "rendering": False}
            except Exception:
                self._chart_state[run_id] = {"count": -1, "reviews": n_reviews,
                                             "rendering": False}

        if sync:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
        return "rendering"

    def force_render_charts(self, run_id: str) -> str:
        """清状态强制重画（'图表生成失败'后的手动重试入口）。"""
        self._chart_state.pop(run_id, None)
        return self.maybe_render_charts(run_id)

    def _render_report_sync(self, run_id: str, exp) -> None:
        from ..config import load_domain
        from ..datasets import read_jsonl
        from ..report import render_report

        domain = load_domain(self.root / "configs" / "domains" / f"{exp.domain}.toml")
        testset = read_jsonl(self.root / exp.testset)
        store = self.open_run(run_id)
        try:
            render_report(
                store, run_id, exp, domain, testset, self.results_dir / run_id,
                provisional=len(testset) < 50,  # 小样本自动加冒烟声明
            )
        finally:
            store.close()

    def _chart_version(self, run_id: str) -> int:
        """图表缓存破坏参数：用 generation 总数作版本号，数据变 → 浏览器重新取图。"""
        return int(self._chart_state.get(run_id, {}).get("count", 0) or 0)

    def rejudge_count(self, run_id: str, scope: str) -> int:
        try:
            store = self.open_run(run_id)
        except KeyError:
            return 0
        try:
            if scope == "errors":
                row = store._conn.execute(
                    "SELECT COUNT(*) c FROM generations g JOIN judgments j ON j.gen_id=g.gen_id"
                    " WHERE g.run_id=? AND g.status='ok' AND j.judge_error IS NOT NULL",
                    (run_id,),
                ).fetchone()
            else:
                row = store._conn.execute(
                    "SELECT COUNT(*) c FROM generations WHERE run_id=? AND status='ok'",
                    (run_id,),
                ).fetchone()
            return row["c"] or 0
        finally:
            store.close()

    def rejudge_run(self, run_id: str, scope: str, judge_client=None) -> str:
        """用当前 judge 配置重判（花 token：scope=errors 只重判 judge 失败的，all 全部重判）。
        后台线程执行；进度写 _rejudge_state。judge_client 可注入（测试用 mock）。"""
        if self._rejudge_state.get(run_id, {}).get("running"):
            return "重判已在进行中"
        exp = self.find_experiment(run_id)
        if exp is None:
            return "找不到实验配置，无法重判"
        from .. import datasets, judge as judge_mod
        from ..config import load_domain

        domain = load_domain(self.root / "configs" / "domains" / f"{exp.domain}.toml")
        if judge_client is None:
            judge_client = self.resolve_client(domain.judge_model, temperature=0.0,
                                               max_retries=domain.gen_max_retries)
        testset = {it.item_id: it for it in datasets.read_jsonl(self.root / exp.testset)}
        store = Store(self.runs_dir / run_id / "kelab.sqlite")
        if scope == "errors":
            rows = store._conn.execute(
                "SELECT g.gen_id, g.item_id, g.ke_text FROM generations g"
                " JOIN judgments j ON j.gen_id=g.gen_id"
                " WHERE g.run_id=? AND g.status='ok' AND j.judge_error IS NOT NULL",
                (run_id,),
            ).fetchall()
        else:
            rows = store._conn.execute(
                "SELECT g.gen_id, g.item_id, g.ke_text FROM generations g"
                " WHERE g.run_id=? AND g.status='ok'", (run_id,),
            ).fetchall()
        total = len(rows)
        self._rejudge_state[run_id] = {"done": 0, "total": total, "running": True}

        def work() -> None:
            changed = 0
            try:
                for row in rows:
                    item = testset.get(row["item_id"])
                    if item is None:
                        continue
                    judged = judge_mod.llm_judge(
                        judge_client, domain, item.question, item.gold_ke, row["ke_text"] or ""
                    )
                    with store._lock, store._conn:
                        cur = store._conn.execute(
                            "UPDATE judgments SET llm_verdict=?, llm_score=?, llm_rationale=?,"
                            " judge_error=?, judge_model=?, judge_prompt_tokens=?,"
                            " judge_completion_tokens=? WHERE gen_id=?",
                            (judged.verdict, judged.score, judged.rationale, judged.error,
                             domain.judge_model, judged.prompt_tokens,
                             judged.completion_tokens, row["gen_id"]),
                        )
                        changed += cur.rowcount if cur.rowcount else 0
                    self._rejudge_state[run_id]["done"] += 1
            finally:
                store.close()
                self._rejudge_state[run_id]["running"] = False
                self._rejudge_state[run_id]["changed"] = changed
                self.force_render_charts(run_id)

        threading.Thread(target=work, daemon=True).start()
        return f"重判已启动：{total} 条（judge 模型 {domain.judge_model}，后台进行，刷新看进度）"

    # ---- 批量启动队列（串行，避免并发打爆 API 限流）----

    def enqueue_launch(self, items: list[tuple[str, int | None]]) -> str:
        self._launch_queue.extend(items)
        if not self._queue_worker_alive:
            self._queue_worker_alive = True
            threading.Thread(target=self._queue_worker, daemon=True).start()
        return f"已加入队列 {len(items)} 个实验（队列共 {len(self._launch_queue)} 个，串行执行）"

    def _queue_worker(self) -> None:
        try:
            while self._launch_queue:
                exp_path, limit = self._launch_queue.pop(0)
                try:
                    run_id, _msg = self.launch(exp_path, limit=limit)
                    proc = self.launched.get(run_id)
                    if proc is not None:
                        proc.wait()  # 串行：等前一个跑完再启下一个
                except Exception:
                    continue  # 单个失败不阻塞队列
        finally:
            self._queue_worker_alive = False

    def queue_status(self) -> str:
        if self._launch_queue:
            return f"队列中 {len(self._launch_queue)} 个实验待启动"
        if self._queue_worker_alive:
            return "队列执行中（最后一个）"
        return ""

    # ---- rejudge 状态 ----

    def rejudge_status(self, run_id: str) -> str:
        state = self._rejudge_state.get(run_id)
        if not state:
            return ""
        if state.get("running"):
            return f"重判中 {state['done']}/{state['total']}"
        if "changed" in state:
            return f"重判完成：{state['changed']} 条已更新"
        return ""

    # ---- 启动实验 ----

    def estimate_calls(self, exp) -> tuple[int, int, int]:
        """启动前的调用量估算（零 token）：返回 (条件数, 生成调用, judge 调用)。"""
        testset = self.root / exp.testset
        n_items = 0
        if testset.exists():
            n_items = sum(1 for line in testset.open(encoding="utf-8") if line.strip())
        if exp.limit_items:
            n_items = min(n_items, exp.limit_items)
        if exp.kind == "k":
            n_conds = len(exp.ks) * len(exp.retrievers)
        else:
            n_conds = len(exp.ns) * len(exp.retrievers)
        gens = n_conds * n_items * exp.repeats
        return n_conds, gens, gens  # judge 与生成一一对应（生成失败才跳过）

    def cost_summary(self, run_id: str) -> dict:
        """run 的实际消耗：生成/judge 的 token 与调用数。旧库无 judge token 列时降级。"""
        try:
            store = self.open_run(run_id)
        except KeyError:
            return {}
        out: dict = {}
        try:
            row = store._conn.execute(
                "SELECT COUNT(*) n, SUM(prompt_tokens) pt, SUM(completion_tokens) ct"
                " FROM generations WHERE run_id=? AND status='ok'", (run_id,),
            ).fetchone()
            out["gen_calls"] = row["n"] or 0
            out["gen_prompt_tokens"] = row["pt"] or 0
            out["gen_completion_tokens"] = row["ct"] or 0
            try:
                row = store._conn.execute(
                    "SELECT COUNT(*) n, SUM(j.judge_prompt_tokens) pt,"
                    " SUM(j.judge_completion_tokens) ct"
                    " FROM judgments j JOIN generations g ON g.gen_id=j.gen_id"
                    " WHERE g.run_id=?", (run_id,),
                ).fetchone()
                out["judge_calls"] = row["n"] or 0
                out["judge_prompt_tokens"] = row["pt"] or 0
                out["judge_completion_tokens"] = row["ct"] or 0
            except sqlite3.OperationalError:
                out["judge_tokens_missing"] = True  # 旧库未迁移 judge token 列
        finally:
            store.close()
        return out

    def pricing_for(self, run_id: str) -> dict:
        """读领域配置 [pricing] 节（可选）。找不到返回 {}。"""
        exp = self.find_experiment(run_id)
        if exp is None:
            return {}
        toml_path = self.root / "configs" / "domains" / f"{exp.domain}.toml"
        if not toml_path.exists():
            return {}
        import tomllib

        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        return raw.get("pricing", {})

    # ---- API 设置与模型列表 ----

    def _api_endpoint(self) -> tuple[str, str]:
        """(base_url, api_key)：设置文件 > 环境变量 > 默认。"""
        import os

        from ..settings import load_api_settings

        saved = load_api_settings(self.root / "configs" / "api.json")
        base_url = (saved.get("base_url") or os.getenv("NL2KE_BASE_URL")
                    or "https://api.llm.ustc.edu.cn/v1")
        api_key = (saved.get("api_key") or os.getenv("NL2KE_API_KEY")
                   or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
        return base_url, api_key

    def invalidate_models_cache(self) -> None:
        self._models_cache = {}

    def list_models(self, refresh: bool = False) -> list[str]:
        """从网关 /models 拉取可用模型（5 分钟缓存；失败返回 []，表单退化为手输）。"""
        import time

        now = time.time()
        cache = self._models_cache
        if not refresh and cache and now - cache.get("at", 0) < 300:
            return cache["models"]
        base_url, api_key = self._api_endpoint()
        if not api_key:
            return []
        request = urllib.request.Request(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))
        except Exception:
            return cache.get("models", [])  # 拉取失败用旧缓存兜底
        self._models_cache = {"at": now, "models": models}
        return models

    def models_html(self) -> str:
        models = self.list_models()
        if not models:
            return ("<span class='muted'>拉取失败（检查 base_url/key 或网络），"
                    "配置页模型字段将退回手输模式</span>")
        return " ".join(f"<span class='chip mono'>{ESC(m)}</span>" for m in models)

    def models_datalist(self) -> str:
        """配置表单用的 <datalist>（可输入可下拉，零 JS）。"""
        models = self.list_models()
        if not models:
            return ""
        options = "".join(f"<option value='{ESC(m)}'></option>" for m in models)
        return f"<datalist id='model-list'>{options}</datalist>"

    # ---- 领域资源（标注工作台/Prompt 页共用） ----

    def _load_domain_resources(self, name: str):
        from ..config import load_domain

        path = self.root / "configs" / "domains" / f"{name}.toml"
        if not path.exists():
            raise KeyError(name)
        return load_domain(path), path

    def _read_template(self, domain) -> str:
        tpl = Path(domain.prompt_template)
        if not tpl.is_absolute():
            tpl = self.root / tpl
        return tpl.read_text(encoding="utf-8")

    def resolve_client(self, model: str, *, temperature: float = 0.0,
                       timeout: float = 120.0, max_retries: int = 3):
        """Web 场景下的 API 客户端：走本 app.root 的设置文件 + 环境变量
        （绝不用 cwd 相对路径——测试/其它目录下会误读真实 credentials）。"""
        from ..llm import ClientConfig, LLMError

        base_url, api_key = self._api_endpoint()
        if not api_key:
            raise LLMError("未配置 API key：到「设置」页填 base_url 和 key")
        return ClientConfig(base_url=base_url, api_key=api_key, model=model,
                            temperature=temperature, timeout=timeout,
                            max_retries=max_retries)

    # ---- 分享版报告 ----

    def share_run(self, run_id: str) -> str:
        """导出单文件分享版报告（share.html + share.fragment.html）。返回消息。"""
        from ..datasets import read_jsonl
        from ..share import render_share

        exp = self.find_experiment(run_id)
        if exp is None:
            return "找不到实验配置，无法导出"
        # 图表先确保最新
        self.maybe_render_charts(run_id, sync=True)
        domain, _ = self._load_domain_resources(exp.domain)
        testset = read_jsonl(self.root / exp.testset)
        store = self.open_run(run_id)
        try:
            standalone, _fragment = render_share(
                store, run_id, exp, domain, len(testset),
                self.results_dir / run_id,
            )
        finally:
            store.close()
        return f"分享版已导出：results/{run_id}/share.html（单文件，图表内嵌）"

    def stop_run(self, run_id: str) -> str:
        """停止本会话启动的运行中子进程。返回提示消息。"""
        proc = self.launched.get(run_id)
        if proc is None:
            return "该 run 不是从本看板启动的（或看板已重启），请到终端停止"
        if proc.poll() is not None:
            return f"进程已自行结束（退出码 {proc.returncode}）"
        proc.terminate()
        return "已发送终止信号；断点续跑可随时继续"

    def revalidate_run(self, run_id: str) -> str:
        """用当前校验规则重跑 run 内全部 validation（本地计算，零 token）。

        校验器改进（如赋值式绑定不再误报）后，旧数据可一键回溯；
        同步重算 judgments.gen_coverage 并触发图表重画。
        """
        from .. import datasets, judge as judge_mod, ke_validate
        from ..config import load_domain
        from ..ontology import load_ontology

        exp = self.find_experiment(run_id)
        if exp is None:
            return "找不到实验配置，无法重新校验"
        domain = load_domain(self.root / "configs" / "domains" / f"{exp.domain}.toml")
        template_path = Path(domain.prompt_template)
        if not template_path.is_absolute():
            template_path = self.root / template_path
        template = template_path.read_text(encoding="utf-8")
        ontology = load_ontology(
            domain.ontology_source, prompt_text=template, json_path=domain.ontology_json
        )
        testset = {it.item_id: it for it in datasets.read_jsonl(self.root / exp.testset)}
        # fixed 池模式下，示例来自 pool.jsonl 而非测试集，覆盖度重算需要它
        ops_source = dict(testset)
        if exp.pool_strategy == "fixed" and exp.pool:
            pool_path = Path(exp.pool)
            if not pool_path.is_absolute():
                pool_path = self.root / pool_path
            if pool_path.exists():
                for it in datasets.read_jsonl(pool_path):
                    ops_source[it.item_id] = it

        store = Store(self.runs_dir / run_id / "kelab.sqlite")  # 写模式
        total = flipped = 0
        try:
            rows = store._conn.execute(
                "SELECT g.gen_id, g.item_id, g.ke_text, g.retrieved_ids,"
                " v.lark_ok, v.light_ok"
                " FROM generations g LEFT JOIN validations v ON v.gen_id=g.gen_id"
                " WHERE g.run_id=? AND g.status='ok'", (run_id,),
            ).fetchall()
            for r in rows:
                total += 1
                result = ke_validate.validate_ke(
                    r["ke_text"] or "", domain.grammar_path, ontology
                )
                store.insert_validation(
                    r["gen_id"],
                    {
                        "lark_ok": int(result.lark_ok),
                        "lark_error": result.lark_error,
                        "light_ok": int(result.light_ok),
                        "issues_json": json.dumps(
                            [i.to_dict() for i in result.issues], ensure_ascii=False
                        ),
                        "gen_operators": json.dumps(result.operators, ensure_ascii=False),
                    },
                )
                if int(result.lark_ok) != (r["lark_ok"] or 0) or \
                        int(result.light_ok) != (r["light_ok"] or 0):
                    flipped += 1
                gold = testset.get(r["item_id"])
                if gold is not None:
                    gen_ops = set(result.operators) if result.lark_ok else None
                    cov = (
                        judge_mod.jaccard(gen_ops, set(gold.gold_operators))
                        if gen_ops is not None else None
                    )
                    updates = {"gen_coverage": cov}
                    # 检索覆盖度也可离线重算（retrieved_ids 落库时就存了）
                    try:
                        retrieved_ids = json.loads(r["retrieved_ids"] or "[]")
                    except json.JSONDecodeError:
                        retrieved_ids = []
                    if retrieved_ids:
                        pool_ops: set[str] = set()
                        for rid in retrieved_ids:
                            source = ops_source.get(rid)
                            if source is not None:
                                pool_ops |= set(source.gold_operators)
                        updates["retrieval_coverage"] = judge_mod.jaccard(
                            pool_ops, set(gold.gold_operators)
                        )
                    with store._lock, store._conn:
                        store._conn.execute(
                            "UPDATE judgments SET gen_coverage=?,"
                            " retrieval_coverage=COALESCE(?, retrieval_coverage)"
                            " WHERE gen_id=?",
                            (updates["gen_coverage"],
                             updates.get("retrieval_coverage"), r["gen_id"]),
                        )
        finally:
            store.close()
        self.force_render_charts(run_id)
        return f"重新校验完成：{total} 条，其中 {flipped} 条校验结果有变化（图表已触发重画）"

    def launch(self, exp_path: str, limit: int | None = None) -> tuple[str, str]:
        """启动 run 子进程，返回 (run_id, message)。已完成的配置直接复用 run_id（断点续跑）。
        limit = 试跑题数（参与配置哈希，成为独立 run）。"""
        exp = load_experiment(exp_path)
        from ..config import config_hash, load_domain

        if limit:
            exp.limit_items = limit
        domain_path = self.root / "configs" / "domains" / f"{exp.domain}.toml"
        domain = load_domain(domain_path)
        run_id = f"{exp.name}-{config_hash(domain, exp)}"
        proc = self.launched.get(run_id)
        if proc is not None and proc.poll() is None:
            return run_id, "该实验已在运行中"
        log_path = self.runs_dir / run_id / "console.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8")
        cmd = [sys.executable, "-X", "utf8", "-m", "kelab.cli", "run", "--exp", exp_path]
        if limit:
            cmd += ["--limit", str(limit)]
        proc = subprocess.Popen(
            cmd, cwd=self.root, stdout=log, stderr=subprocess.STDOUT,
        )
        self.launched[run_id] = proc
        return run_id, f"已启动（日志 {log_path}）"
