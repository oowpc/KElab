"""SQLite 结果存储：runs / conditions / generations / validations / judgments。

断点续跑的核心：generations 有 UNIQUE(run_id, condition_id, item_id, rep)，
pending() 用 LEFT JOIN 查缺，中断后重跑只补缺口。写操作用单锁串行化（并发安全）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_template_md5 TEXT,
    grammar_md5 TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    note TEXT,
    dataset_md5 TEXT
);
CREATE TABLE IF NOT EXISTS conditions (
    condition_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    k INTEGER,
    n INTEGER,
    retriever TEXT NOT NULL,
    static_prompt_hash TEXT,
    onto TEXT DEFAULT 'full',
    gen_model TEXT
);
CREATE TABLE IF NOT EXISTS generations (
    gen_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    rep INTEGER NOT NULL,
    seed INTEGER,
    prompt_text TEXT,
    prompt_hash TEXT,
    retrieved_ids TEXT,
    retrieval_warning TEXT,
    raw_output TEXT,
    ke_text TEXT,
    latency_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, condition_id, item_id, rep)
);
CREATE TABLE IF NOT EXISTS validations (
    gen_id INTEGER UNIQUE NOT NULL,
    lark_ok INTEGER,
    lark_error TEXT,
    light_ok INTEGER,
    issues_json TEXT,
    gen_operators TEXT
);
CREATE TABLE IF NOT EXISTS judgments (
    gen_id INTEGER UNIQUE NOT NULL,
    exact_match REAL,
    retrieval_coverage REAL,
    gen_coverage REAL,
    llm_verdict TEXT,
    llm_score REAL,
    llm_rationale TEXT,
    judge_model TEXT,
    judge_error TEXT,
    judge_prompt_tokens INTEGER,
    judge_completion_tokens INTEGER
);
"""

# 旧库迁移：后加的列在此登记（CREATE TABLE IF NOT EXISTS 不会更新已有表）
MIGRATIONS = [
    "ALTER TABLE judgments ADD COLUMN judge_prompt_tokens INTEGER",
    "ALTER TABLE judgments ADD COLUMN judge_completion_tokens INTEGER",
    "ALTER TABLE runs ADD COLUMN note TEXT",
    "ALTER TABLE conditions ADD COLUMN onto TEXT DEFAULT 'full'",
    "ALTER TABLE conditions ADD COLUMN gen_model TEXT",
    "ALTER TABLE runs ADD COLUMN dataset_md5 TEXT",
    # 协议 §7.4：双人盲评 + 第三人裁决（reviews_v2 带评审员与盲评标记）
    "CREATE TABLE IF NOT EXISTS reviews_v2 ("
    " gen_id INTEGER NOT NULL, reviewer TEXT NOT NULL DEFAULT 'A',"
    " human_verdict TEXT, human_note TEXT, blind INTEGER DEFAULT 1,"
    " reviewed_at TEXT, PRIMARY KEY(gen_id, reviewer))",
    "INSERT OR IGNORE INTO reviews_v2(gen_id, human_verdict, human_note, reviewed_at)"
    " SELECT gen_id, human_verdict, human_note, reviewed_at FROM reviews",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path | str, readonly: bool = False):
        self.db_path = Path(db_path)
        self.readonly = readonly
        if readonly:
            # Web 看板等只读场景：mode=ro + busy_timeout，与 runner 写进程共存
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            self._conn = self._open_readonly_with_recovery(uri)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        if not readonly:
            with self._lock, self._conn:
                self._conn.executescript(SCHEMA)
                # WAL：写进程（runner）与读进程（Web 看板）并发不互锁
                self._conn.execute("PRAGMA journal_mode=WAL")
                for statement in MIGRATIONS:
                    try:
                        self._conn.execute(statement)
                    except sqlite3.OperationalError:
                        pass  # 列已存在

    def _open_readonly_with_recovery(self, uri: str) -> sqlite3.Connection:
        """只读打开；若 runner 被 kill 留下热日志导致回滚失败，先读写打开恢复再重开。

        注意必须用真实的目录查询（sqlite_master）探测——SELECT 1 不触库，探不出来。
        """
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError:
            conn.close()
        recovery = sqlite3.connect(self.db_path, timeout=10)
        try:
            recovery.execute("PRAGMA busy_timeout=5000")
            recovery.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        finally:
            recovery.close()
        return sqlite3.connect(uri, uri=True, check_same_thread=False)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- runs / conditions ----

    def create_run(
        self,
        run_id: str,
        config_json: str,
        config_hash: str,
        prompt_template_md5: str | None,
        grammar_md5: str | None,
        dataset_md5: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO runs(run_id, started_at, config_json, config_hash,"
                " prompt_template_md5, grammar_md5, dataset_md5) VALUES (?,?,?,?,?,?,?)",
                (run_id, utc_now(), config_json, config_hash,
                 prompt_template_md5, grammar_md5, dataset_md5),
            )

    def set_dataset_md5(self, run_id: str, dataset_md5: str) -> None:
        """回填数据指纹（旧库 run 无此列值时补记一次）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET dataset_md5=? WHERE run_id=?", (dataset_md5, run_id)
            )

    def set_prompt_template_md5(self, run_id: str, md5: str) -> None:
        """回填 prompt 模板指纹（旧库回填一次）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET prompt_template_md5=? WHERE run_id=?", (md5, run_id)
            )

    def finish_run(self, run_id: str, status: str = "done") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET status=? WHERE run_id=?", (status, run_id)
            )

    def mark_running(self, run_id: str) -> None:
        """续跑开始时把状态从 done/interrupted 归位 running——
        否则断点续跑的 run 在进度轮询里被误读为「已完成」。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET status='running' WHERE run_id=? AND status != 'running'",
                (run_id,),
            )

    def set_note(self, run_id: str, note: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET note=? WHERE run_id=?", (note, run_id)
            )

    def mark_interrupted_stale_runs(self, stale_seconds: int = 300) -> int:
        """状态自愈：status=running 但超过 stale_seconds 没有新行的 run 标记为 interrupted
        （进程被 kill 后 runs 表不会自己更新）。返回标记条数。"""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT r.run_id, MAX(g.created_at) last_at FROM runs r"
                " LEFT JOIN generations g ON g.run_id=r.run_id"
                " WHERE r.status='running' GROUP BY r.run_id"
            ).fetchall()
            now = datetime.now(timezone.utc)
            marked = 0
            for row in rows:
                last_at = row["last_at"]
                if last_at:
                    try:
                        age = (now - datetime.fromisoformat(last_at)).total_seconds()
                    except ValueError:
                        continue
                else:
                    age = stale_seconds + 1  # 没有任何 generation 的 running 视为中断
                if age > stale_seconds:
                    self._conn.execute(
                        "UPDATE runs SET status='interrupted' WHERE run_id=?",
                        (row["run_id"],),
                    )
                    marked += 1
            return marked

    def add_condition(
        self,
        run_id: str,
        condition_id: str,
        kind: str,
        k: int | None,
        n: int | None,
        retriever: str,
        static_prompt_hash: str,
        onto: str = "full",
        gen_model: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO conditions(condition_id, run_id, kind, k, n, retriever,"
                " static_prompt_hash, onto, gen_model) VALUES (?,?,?,?,?,?,?,?,?)",
                (condition_id, run_id, kind, k, n, retriever, static_prompt_hash, onto, gen_model),
            )

    # ---- generations ----

    def insert_generation(self, row: dict) -> int:
        """插入一条 generation，返回 gen_id。重复 (run,condition,item,rep) 会被唯一约束拒绝。"""
        row = dict(row)
        row.setdefault("created_at", utc_now())
        cols = ",".join(row.keys())
        placeholders = ",".join("?" for _ in row)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO generations({cols}) VALUES ({placeholders})",
                tuple(_json_safe(v) for v in row.values()),
            )
            return int(cur.lastrowid)

    def insert_validation(self, gen_id: int, result: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO validations(gen_id, lark_ok, lark_error, light_ok,"
                " issues_json, gen_operators) VALUES (?,?,?,?,?,?)",
                (
                    gen_id,
                    result.get("lark_ok"),
                    result.get("lark_error"),
                    result.get("light_ok"),
                    result.get("issues_json"),
                    result.get("gen_operators"),
                ),
            )

    def insert_judgment(self, gen_id: int, result: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO judgments(gen_id, exact_match, retrieval_coverage,"
                " gen_coverage, llm_verdict, llm_score, llm_rationale, judge_model, judge_error,"
                " judge_prompt_tokens, judge_completion_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    gen_id,
                    result.get("exact_match"),
                    result.get("retrieval_coverage"),
                    result.get("gen_coverage"),
                    result.get("llm_verdict"),
                    result.get("llm_score"),
                    result.get("llm_rationale"),
                    result.get("judge_model"),
                    result.get("judge_error"),
                    result.get("judge_prompt_tokens"),
                    result.get("judge_completion_tokens"),
                ),
            )

    # ---- 断点续跑 ----

    def mark_generation_error(self, gen_id: int, error: str) -> None:
        """生成后的校验/判分阶段出异常时把该行标记为 error（retry_errors 可重试），
        不留「ok 但无判分」的脏行。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE generations SET status='error', error=? WHERE gen_id=?",
                (error[:1000], gen_id),
            )

    def n_distinct_items(self, run_id: str) -> int:
        """run 实际评估过的题目数（试跑/中断时不等于测试集大小——报告/MDE 用它）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(DISTINCT item_id) c FROM generations"
                " WHERE run_id=? AND status='ok'", (run_id,),
            ).fetchone()["c"]

    def total_generations(self, run_id: str) -> int:
        """run 的全部生成行数（预算上限/进度用）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) c FROM generations WHERE run_id=?", (run_id,),
            ).fetchone()["c"]

    def pending(
        self, run_id: str, condition_id: str, item_ids: list[str], repeats: int
    ) -> list[tuple[str, int]]:
        """返回该条件下缺失的 (item_id, rep) 列表。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, rep FROM generations WHERE run_id=? AND condition_id=?",
                (run_id, condition_id),
            ).fetchall()
        done = {(r["item_id"], r["rep"]) for r in rows}
        return [
            (item_id, rep)
            for item_id in item_ids
            for rep in range(repeats)
            if (item_id, rep) not in done
        ]

    # ---- 聚合查询 ----

    def condition_rows(self, run_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM conditions WHERE run_id=? ORDER BY kind, n, k, retriever",
                (run_id,),
            ).fetchall()

    def generation_join_rows(self, run_id: str, condition_id: str) -> list[sqlite3.Row]:
        """generations LEFT JOIN validations LEFT JOIN judgments，供聚合用。"""
        with self._lock:
            return self._conn.execute(
                "SELECT g.*, v.lark_ok, v.light_ok, v.gen_operators,"
                " j.exact_match, j.retrieval_coverage, j.gen_coverage,"
                " j.llm_verdict, j.llm_score, j.llm_rationale, j.judge_error"
                " FROM generations g"
                " LEFT JOIN validations v ON v.gen_id = g.gen_id"
                " LEFT JOIN judgments j ON j.gen_id = g.gen_id"
                " WHERE g.run_id=? AND g.condition_id=?",
                (run_id, condition_id),
            ).fetchall()

    def run_info(self, run_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()


def _json_safe(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value
