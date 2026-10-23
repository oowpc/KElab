"""运行相关页面：运行列表、run 详情、生成明细、失败分类、人工抽检、日志。"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from ..aggregate import aggregate_run
from ..datasets import count_lines as _count_lines
from ..store import Store
from .app import WebApp
from .ui import (ESC, PAGE_SIZE, POLL_JS, chip, eta_text, flag_chip, layout,
                 progress_bar, verdict_chip, _fmt_pct)

def page_runs(app: WebApp) -> bytes:
    runs = app.list_runs()
    rows = []
    status_chips = {
        "running": lambda: chip("运行中", "warn"),
        "done": lambda: chip("done", "ok"),
        "interrupted": lambda: chip("已中断", "err"),
    }
    for r in runs:
        status = status_chips.get(r["status"], lambda: chip(r["status"]))()
        note = f"<br><span class='muted'>{ESC(r['note'])}</span>" if r.get("note") else ""
        best = _best_semantic_quick(app, r["run_id"])
        rows.append(
            f"<tr><td class='mono'><a href='/run/{ESC(r['run_id'])}'>{ESC(r['run_id'])}</a>{note}</td>"
            f"<td>{ESC(r['started_at'])}</td><td>{status}</td>"
            f"<td class='num'>{_fmt_pct(best) if best is not None else '—'}</td>"
            f"<td>{progress_bar(r['ok'] + r['error'], r['expected'])}</td>"
            f"<td class='num'>{r['ok']}</td><td class='num'>{r['error']}</td>"
            f"<td><a href='/run/{ESC(r['run_id'])}/gens'>明细</a> · "
            f"<a href='/run/{ESC(r['run_id'])}/review'>抽检</a> · "
            f"<a href='/run/{ESC(r['run_id'])}/log'>日志</a></td></tr>"
        )
    table = ("<table><tr><th>run_id</th><th>开始时间</th><th>状态</th><th>最佳语义</th><th>进度</th>"
             "<th>成功</th><th>失败</th><th></th></tr>" + "".join(rows) + "</table>") \
        if rows else "<p class='muted'>还没有运行记录。到 <a href='/launch'>启动实验</a> 跑一个。</p>"
    refresh = ("<script>setTimeout(() => location.reload(), 30000);</script>"
               if any(r["status"] == "running" for r in runs) else "")
    return layout("运行列表", f"<h1>运行列表</h1><div class='card'>{table}</div>", refresh)


def _best_semantic_quick(app: WebApp, run_id: str) -> float | None:
    """运行列表用的轻量查询：各条件 llm_score 均值的最大值（不做全量聚合）。"""
    try:
        store = app.open_run(run_id)
    except KeyError:
        return None
    try:
        rows = store._conn.execute(
            "SELECT AVG(j.llm_score) m FROM generations g"
            " JOIN judgments j ON j.gen_id=g.gen_id"
            " WHERE g.run_id=? AND g.status='ok' AND j.llm_score IS NOT NULL"
            " GROUP BY g.condition_id", (run_id,),
        ).fetchall()
        values = [r["m"] for r in rows if r["m"] is not None]
        return max(values) if values else None
    except Exception:
        return None
    finally:
        store.close()

def page_run(app: WebApp, run_id: str, query: dict | None = None) -> bytes:
    try:
        store = app.open_run(run_id)
    except KeyError:
        return layout("未找到", f"<h1>未找到 run {ESC(run_id)}</h1>")
    info = store.run_info(run_id)
    exp = app.find_experiment(run_id)
    query = query or {}
    body = [f"<h1>{ESC(run_id)}</h1>"]

    msg = query.get("msg", [""])[0]
    if msg:
        body.append(f"<div class='flash'>{ESC(msg)}</div>")
    if info["status"] == "running":
        body.append("<div class='flash'>运行中，页面每 3 秒自动刷新进度；图表随数据更新自动重画。</div>")
    if info["status"] == "interrupted":
        body.append("<div class='flash'>该 run 已被中断（进程终止）。"
                    "断点续跑：到「启动实验」用同一配置再次启动即可只补缺口。</div>")

    # 备注（实验管理：记下这个 run 改了什么）
    note = info["note"] or "" if "note" in info.keys() else ""
    body.append(
        f"<div class='card'><form class='filters' method='post' action='/note'>"
        f"<input type='hidden' name='run_id' value='{ESC(run_id)}'>"
        f"<b>备注</b> <input type='text' name='note' value='{ESC(note)}' "
        f"placeholder='例如：改了 judge prompt v2 / 试跑验证' style='width:60%'>"
        f"<button class='ghost'>保存</button></form></div>"
    )

    # 概要
    counts = app._progress_counts(store, run_id)
    done = counts["ok"] + counts["error"]
    overall = progress_bar(done, counts["expected"],
                           eta_text(store, run_id, counts["expected"], done)
                           if info["status"] == "running" else "")
    stop_form = ""
    if info["status"] == "running" and run_id in app.launched \
            and app.launched[run_id].poll() is None:
        stop_form = (
            f"<form class='inline' method='post' action='/stop' style='float:right'>"
            f"<input type='hidden' name='run_id' value='{ESC(run_id)}'>"
            f"<button class='ghost' style='border-color:var(--crit);color:var(--crit)'>停止运行</button></form>"
        )
    body.append(
        f"<div class='card'>{stop_form}<b>总进度</b> <span id='overall-progress'>{overall}</span><br>"
        f"<span class='muted'>开始 {ESC(info['started_at'])}　状态 {ESC(info['status'])}　"
        f"模板 md5 {ESC(info['prompt_template_md5'] or '—')}　"
        f"grammar md5 {ESC(info['grammar_md5'] or '—')}　"
        f"数据指纹 {ESC(info['dataset_md5'] or '—' if 'dataset_md5' in info.keys() else '—')}"
        f"</span></div>"
    )

    # 成本卡：生成/judge 的 token 与调用数（[pricing] 配置后自动换算 ¥）
    cost = app.cost_summary(run_id)
    if cost:
        gen_total = cost["gen_prompt_tokens"] + cost["gen_completion_tokens"]
        parts = [
            f"生成 {cost['gen_calls']} 次 / {gen_total:,} tokens"
            f"（输入 {cost['gen_prompt_tokens']:,} + 输出 {cost['gen_completion_tokens']:,}）"
        ]
        if cost.get("judge_tokens_missing"):
            parts.append("judge token 未追踪（旧库，下次 run 自动迁移）")
        else:
            judge_total = cost.get("judge_prompt_tokens", 0) + cost.get("judge_completion_tokens", 0)
            parts.append(
                f"judge {cost.get('judge_calls', 0)} 次 / {judge_total:,} tokens"
            )
            pricing = app.pricing_for(run_id)
            yuan = 0.0
            if pricing.get("gen_price_per_mtok"):
                yuan += gen_total / 1e6 * pricing["gen_price_per_mtok"]
            if pricing.get("judge_price_per_mtok"):
                yuan += judge_total / 1e6 * pricing["judge_price_per_mtok"]
            if yuan:
                parts.append(f"估算成本 <b>¥{yuan:.2f}</b>（按领域配置 [pricing] 单价）")
            else:
                parts.append("在领域配置 [pricing] 填单价可自动换算 ¥")
        body.append(f"<div class='card'><b>消耗</b>　{'　·　'.join(parts)}</div>")

    # 解读卡：主指标大字 + 规则化人话判定（每条附依据）
    if exp is not None:
        from ..aggregate import mde_estimate
        from ..interpret import interpret_run, review_agreement

        n_items = 0
        testset_path = app.root / exp.testset
        if testset_path.exists():
            n_items = _count_lines(testset_path)
        mde = mde_estimate(n_items)
        reviews = app.get_reviews(run_id)
        agree, total_reviewed = review_agreement(store, run_id, reviews)
        metrics_for_interp = aggregate_run(store, run_id, exp)
        findings = interpret_run(metrics_for_interp, exp, mde,
                                 review_agree=agree, review_total=total_reviewed)
        best_semantic = None
        for cm in metrics_for_interp:
            stat = cm.stats.get("semantic")
            if stat and stat.mean is not None and \
                    (best_semantic is None or stat.mean > best_semantic):
                best_semantic = stat.mean
        hero = (f"<span style='font-size:28px;font-weight:650;color:var(--ink)'>"
                f"{best_semantic * 100:.1f}%</span> "
                f"<span class='muted'>最佳语义等价率</span>") if best_semantic is not None else ""
        level_chip = {"good": ("✓", "ok"), "warn": ("⚠", "warn"),
                      "bad": ("✗", "err"), "info": ("ℹ", "")}
        items = "".join(
            f"<li>{chip(mark, kind)} {ESC(f.text)}"
            + (f"<br><span class='muted' style='font-size:12px'>依据：{ESC(f.evidence)}</span>"
               if f.evidence else "")
            + "</li>"
            for f in findings
            for mark, kind in [level_chip[f.level]]
        )
        body.append(
            f"<div class='card' style='border-left:3px solid var(--blue)'>"
            f"<div>{hero}</div><ul style='margin:8px 0 0;padding-left:18px'>{items}</ul></div>"
        )

    # 条件×指标表
    if exp is not None:
        metrics = aggregate_run(store, run_id, exp)
        rows = []
        for cm in metrics:
            def ms(metric: str) -> str:
                stat = cm.stats.get(metric)
                if stat is None or stat.mean is None:
                    return "<td class='num'>—</td>"
                mark = " ⚠️" if stat.alert else ""
                return f"<td class='num'>{stat.mean * 100:.1f}±{stat.std * 100:.1f}{mark}</td>"

            tok = cm.stats.get("prompt_tokens")
            lat = cm.stats.get("latency_ms")
            rows.append(
                f"<tr><td class='mono'>{ESC(cm.condition_id)}</td>"
                f"{ms('semantic')}{ms('exact')}{ms('lark')}{ms('light')}"
                f"{ms('retrieval_coverage')}{ms('gen_coverage')}"
                f"<td class='num'>{f'{tok.mean:.0f}' if tok and tok.mean is not None else '—'}</td>"
                f"<td class='num'>{f'{lat.mean:.0f}' if lat and lat.mean is not None else '—'}</td>"
                f"<td class='num'>{cm.n_gen_error}</td><td class='num'>{cm.n_judge_error}</td></tr>"
            )
        body.append(
            "<h2>条件 × 指标（3 次重复均值±std，%）</h2><div class='card'><table>"
            "<tr><th>条件</th><th>语义等价</th><th>完全匹配</th><th>语法解析</th><th>light检查</th>"
            "<th>检索覆盖</th><th>生成覆盖</th><th>tokens</th><th>ms</th>"
            "<th>生成失败</th><th>judge失败</th></tr>"
            + "".join(rows) + "</table></div>"
        )
    else:
        body.append("<p class='muted'>未找到对应实验配置，仅显示原始计数。</p>")

    # 图表：数据有更新就自动重画（后台线程），img 加版本号防浏览器缓存旧图
    if query.get("charts") == ["force"]:
        chart_status = app.force_render_charts(run_id)
    else:
        chart_status = app.maybe_render_charts(run_id)
    version = app._chart_version(run_id)
    figs = sorted(app.results_dir.glob(f"{run_id}/*.png"))
    auto_reload = ""
    if figs:
        imgs = "".join(
            f"<div><div class='muted'>{ESC(p.name)}</div>"
            f"<img class='chart' src='/files/results/{run_id}/{ESC(p.name)}?v={version}'></div>"
            for p in figs
        )
        body.append(f"<h2>图表</h2><div class='card'>{imgs}</div>")
    elif chart_status == "rendering":
        body.append("<h2>图表</h2><div class='card muted'>图表生成中，几秒后自动刷新…</div>")
        auto_reload = "<script>setTimeout(() => location.reload(), 5000);</script>"
    elif chart_status == "failed":
        body.append(
            "<h2>图表</h2><div class='card'>图表生成失败（详见控制台）。"
            f"<a href='/run/{ESC(run_id)}?charts=force'>重试 →</a></div>"
        )
    report = app.results_dir / run_id / "report.md"
    if report.exists():
        body.append(f"<p><a href='/files/results/{run_id}/report.md'>查看 report.md</a> · "
                    f"<a href='/files/results/{run_id}/metrics.csv'>metrics.csv</a> · "
                    f"<a href='/run/{run_id}/review.csv'>人工判定导出</a></p>")

    body.append(f"<p><a class='btn ghost' href='/run/{run_id}/gens'>生成明细浏览器 →</a> "
                f"<a class='btn ghost' href='/run/{run_id}/failures'>失败案例分类 →</a> "
                f"<a class='btn ghost' href='/run/{run_id}/review'>人工抽检队列 →</a> "
                f"<a class='btn ghost' href='/run/{run_id}/log'>日志 →</a> "
                f"<a class='btn ghost' href='/compare?b={run_id}'>与其他 run 对比 →</a></p>"
                f"<form class='inline' method='post' action='/share'>"
                f"<input type='hidden' name='run_id' value='{ESC(run_id)}'>"
                f"<button class='ghost'>导出分享版（给导师的单文件报告）</button></form>"
                f"<span class='muted'>　生成 share.html，发我一句「发布」我给你在线链接</span>")
    body.append(
                f"<form class='inline' method='post' action='/revalidate'>"
                f"<input type='hidden' name='run_id' value='{ESC(run_id)}'>"
                f"<button class='ghost'>重新校验（零 token）</button></form>"
                f"<span class='muted'>　校验规则更新后，用它回溯旧数据的 lark/light/覆盖度</span>")
    rejudge_status = app.rejudge_status(run_id)
    n_judge_errors = app.rejudge_count(run_id, "errors")
    n_all_ok = app.rejudge_count(run_id, "all")
    body.append(
        f"<form class='inline' method='post' action='/rejudge'>"
        f"<input type='hidden' name='run_id' value='{ESC(run_id)}'>"
        f"<button class='ghost'>重判 judge</button>"
        f"<select name='scope'>"
        f"<option value='errors'>仅 judge 失败（{n_judge_errors} 条）</option>"
        f"<option value='all'>全部（{n_all_ok} 条）</option>"
        f"</select></form>"
        f"<span class='muted'>　judge prompt/模型改了之后重判（花 token，条数即调用数）"
        f"　{ESC(rejudge_status)}</span>"
    )
    n_gen_errors = sum(cm.n_gen_error for cm in metrics) if exp is not None else 0
    if n_gen_errors > 0:
        body.append(
            f"<form class='inline' method='post' action='/retry_errors'>"
            f"<input type='hidden' name='run_id' value='{ESC(run_id)}'>"
            f"<button class='ghost'>重试失败（{n_gen_errors} 条）</button></form>"
            f"<span class='muted'>　删除失败记录后断点续跑——限流/网络抖动失败用它，"
            "token 只花在失败条上</span>"
        )
    store.close()

    poll = POLL_JS % run_id if info["status"] == "running" else ""
    return layout(run_id, "".join(body), poll + auto_reload)


def page_gens(app: WebApp, run_id: str, query: dict) -> bytes:
    store = app.open_run(run_id)
    conds = [c["condition_id"] for c in store.condition_rows(run_id)]

    cond = query.get("condition", [""])[0]
    status = query.get("status", [""])[0]
    verdict = query.get("verdict", [""])[0]
    flag = query.get("flag", [""])[0]
    item_q = query.get("item", [""])[0].strip()
    text_q = query.get("q", [""])[0].strip()
    page = max(1, int(query.get("page", ["1"])[0] or 1))

    # gold KE 对照需要测试集题目：从 exp 配置加载（题干搜索也要用）
    gold_by_item: dict[str, dict] = {}
    exp = app.find_experiment(run_id)
    if exp is not None:
        testset = app.root / exp.testset
        if testset.exists():
            from ..datasets import read_jsonl
            for it in read_jsonl(testset):
                gold_by_item[it.item_id] = {"question": it.question, "gold_ke": it.gold_ke}

    where, params = ["g.run_id=?"], [run_id]
    if cond:
        where.append("g.condition_id=?"); params.append(cond)
    if status:
        where.append("g.status=?"); params.append(status)
    if verdict:
        where.append("j.llm_verdict=?"); params.append(verdict)
    if flag == "lark_fail":
        where.append("v.lark_ok=0")
    elif flag == "light_fail":
        where.append("v.light_ok=0")
    elif flag == "judge_error":
        where.append("j.judge_error IS NOT NULL")
    if item_q:
        where.append("g.item_id LIKE ?"); params.append(f"%{item_q}%")
    if text_q:
        matched = [iid for iid, g in gold_by_item.items() if text_q in g["question"]]
        if matched:
            placeholders = ",".join("?" for _ in matched)
            where.append(f"g.item_id IN ({placeholders})"); params.extend(matched)
        else:
            where.append("1=0")  # 题干无匹配 → 空结果
    where_sql = " AND ".join(where)

    total = store._conn.execute(
        f"SELECT COUNT(*) c FROM generations g"
        f" LEFT JOIN validations v ON v.gen_id=g.gen_id"
        f" LEFT JOIN judgments j ON j.gen_id=g.gen_id WHERE {where_sql}", params,
    ).fetchone()["c"]
    rows = store._conn.execute(
        f"SELECT g.*, v.lark_ok, v.light_ok, v.issues_json,"
        f" j.llm_verdict, j.llm_score, j.llm_rationale, j.exact_match"
        f" FROM generations g"
        f" LEFT JOIN validations v ON v.gen_id=g.gen_id"
        f" LEFT JOIN judgments j ON j.gen_id=g.gen_id"
        f" WHERE {where_sql} ORDER BY g.gen_id LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()

    # gold KE 对照已在上方加载（gold_by_item）
    options = "<option value=''>全部条件</option>" + "".join(
        f"<option {'selected' if c == cond else ''}>{ESC(c)}</option>" for c in conds
    )
    status_opts = "".join(
        f"<option value='{s}' {'selected' if s == status else ''}>{t}</option>"
        for s, t in (("", "全部状态"), ("ok", "成功"), ("error", "失败"))
    )
    verdict_opts = "".join(
        f"<option value='{s}' {'selected' if s == verdict else ''}>{t}</option>"
        for s, t in (("", "全部判分"), ("equivalent", "等价"), ("minor", "轻微差异"), ("different", "不同"))
    )
    flag_opts = "".join(
        f"<option value='{s}' {'selected' if s == flag else ''}>{t}</option>"
        for s, t in (("", "全部校验"), ("lark_fail", "语法失败"), ("light_fail", "light失败"),
                     ("judge_error", "judge失败"))
    )
    body = [
        f"<h1>{ESC(run_id)} · 生成明细</h1>",
        f"<div class='card'><form class='filters' method='get'>"
        f"<select name='condition'>{options}</select>"
        f"<select name='status'>{status_opts}</select>"
        f"<select name='verdict'>{verdict_opts}</select>"
        f"<select name='flag'>{flag_opts}</select>"
        f"<input type='text' name='item' placeholder='item_id 包含' value='{ESC(item_q)}'>"
        f"<input type='text' name='q' placeholder='题干包含' value='{ESC(text_q)}'>"
        f"<button>过滤</button><span class='muted'>共 {total} 条</span></form></div>",
    ]

    for r in rows:
        gold = gold_by_item.get(r["item_id"], {})
        issues = ""
        if r["issues_json"]:
            try:
                issue_list = json.loads(r["issues_json"])
                issues = " ".join(
                    f"<span class='issue'>[{ESC(i['kind'])}] {ESC(i['message'])}</span>"
                    for i in issue_list
                )
            except (json.JSONDecodeError, KeyError):
                pass
        ok = r["status"] == "ok"
        gen_text = r["ke_text"] or r["error"] or "—"
        # 生成 KE 直接可见（不折叠）；题目/gold 对照收进 details
        head = (
            f"<div class='head'><span><b>#{r['gen_id']}</b> "
            f"<span class='mono'>{ESC(r['condition_id'])}</span> · "
            f"<span class='mono'>{ESC(r['item_id'])}</span> · rep{r['rep']}</span>"
            f"<span>{chip(r['status'], 'ok' if ok else 'err')} "
            f"{flag_chip(r['lark_ok'], '语法✓', '语法✗')} {flag_chip(r['light_ok'], 'light✓', 'light✗')} "
            f"{verdict_chip(r['llm_verdict'])}"
            + (" <span class='chip ok'>完全匹配✓</span>" if r["exact_match"] == 1.0 else "")
            + "</span></div>"
        )
        meta = (f"tokens {r['prompt_tokens'] or '—'}+{r['completion_tokens'] or '—'} · "
                f"{r['latency_ms'] or '—'}ms · "
                f"<a href='/run/{run_id}/review?gen_id={r['gen_id']}'>人工判定 →</a>")
        detail = (
            f"<details><summary class='muted'>题目与 gold KE 对照 ▾</summary>"
            f"<div class='grid2' style='margin-top:8px'>"
            f"<div><div class='label'>题目</div><div class='ke-box'>{ESC(gold.get('question', '—'))}</div></div>"
            f"<div><div class='label'>gold KE</div><div class='ke-box mono'>{ESC(gold.get('gold_ke', '—'))}</div></div>"
            f"</div></details>"
        )
        body.append(
            f"<div class='card gencard {'ok' if ok else 'err'}'>{head}"
            f"<div class='label'>生成 KE</div>"
            f"<div class='ke-box mono'>{ESC(gen_text)}</div>"
            + (f"<div style='margin-top:6px'>{issues}</div>" if issues else "")
            + (f"<div class='rationale'>judge：{ESC(r['llm_rationale'])}</div>" if r["llm_rationale"] else "")
            + detail
            + f"<div class='meta' style='margin-top:8px'>{meta}</div></div>"
        )

    # 分页
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if pages > 1:
        qs = {k: v[0] for k, v in query.items() if k != "page" and v[0]}
        base = f"/run/{run_id}/gens?" + urllib.parse.urlencode(qs)
        links = []
        for p in range(1, pages + 1):
            links.append(f"<b>{p}</b>" if p == page else f"<a href='{base}&page={p}'>{p}</a>")
        body.append("<div class='card'>" + " ".join(links) + "</div>")
    store.close()
    return layout(f"{run_id} 明细", "".join(body))


# ---------------------------------------------------------------- 失败案例分类


def _failure_categories(store: Store, run_id: str) -> list[dict]:
    """失败案例分类（页面与 CSV 共用）：返回 [{title, rows, note, link}]。"""
    categories: list[dict] = []

    def add(title: str, rows: list, note: str = "", link: str = "") -> None:
        if rows:
            categories.append({"title": title, "rows": rows, "note": note, "link": link})

    # 生成失败（API/网络）
    rows = store._conn.execute(
        "SELECT gen_id, condition_id, item_id, rep, error FROM generations"
        " WHERE run_id=? AND status='error' ORDER BY gen_id", (run_id,),
    ).fetchall()
    add("生成失败（API/网络/超时）", rows, link=f"/run/{run_id}/gens?status=error")

    # 语法解析失败
    rows = store._conn.execute(
        "SELECT g.gen_id, g.condition_id, g.item_id, g.rep, v.lark_error AS error"
        " FROM generations g JOIN validations v ON v.gen_id=g.gen_id"
        " WHERE g.run_id=? AND v.lark_ok=0 ORDER BY g.gen_id", (run_id,),
    ).fetchall()
    add("语法解析失败（lark 不过）", rows, link=f"/run/{run_id}/gens?flag=lark_fail")

    # light 检查问题（按 issue kind 聚类；lark 失败单列，不算 light 失败）
    issue_rows = store._conn.execute(
        "SELECT g.gen_id, g.condition_id, g.item_id, g.rep,"
        " v.issues_json, v.issues_json AS error"
        " FROM generations g JOIN validations v ON v.gen_id=g.gen_id"
        " WHERE g.run_id=? AND v.light_ok=0 AND v.lark_ok=1 ORDER BY g.gen_id", (run_id,),
    ).fetchall()
    by_kind: dict[str, list] = {}
    for r in issue_rows:
        try:
            issues = json.loads(r["issues_json"] or "[]")
        except json.JSONDecodeError:
            issues = []
        kinds = sorted({i.get("kind", "unknown") for i in issues if i.get("level") == "error"})
        by_kind.setdefault(" / ".join(kinds) or "unknown", []).append(r)
    kind_names = {
        "unknown_operator": "未知算子（闭词表外）",
        "arity_mismatch": "算子参数个数不匹配",
        "unbound_name": "未绑定变量/名字",
        "output_count": "output 缺失或多于一个",
        "unknown_constructor": "未知类型构造子",
    }
    for kinds, kind_rows in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        label = " / ".join(kind_names.get(k, k) for k in kinds.split(" / "))
        add(f"light 检查失败：{label}", kind_rows,
            note=f"原始 kind: {kinds}", link=f"/run/{run_id}/gens?flag=light_fail")

    # judge 判不同 / judge 失败
    rows = store._conn.execute(
        "SELECT g.gen_id, g.condition_id, g.item_id, g.rep, j.llm_rationale AS error"
        " FROM generations g JOIN judgments j ON j.gen_id=g.gen_id"
        " WHERE g.run_id=? AND j.llm_verdict='different' ORDER BY g.gen_id", (run_id,),
    ).fetchall()
    add("judge 判不同（语义错误）", rows, link=f"/run/{run_id}/gens?verdict=different")
    rows = store._conn.execute(
        "SELECT g.gen_id, g.condition_id, g.item_id, g.rep, j.judge_error AS error"
        " FROM generations g JOIN judgments j ON j.gen_id=g.gen_id"
        " WHERE g.run_id=? AND j.judge_error IS NOT NULL ORDER BY g.gen_id", (run_id,),
    ).fetchall()
    add("judge 失败（判分输出异常）", rows, link=f"/run/{run_id}/gens?flag=judge_error")
    return categories


def page_failures(app: WebApp, run_id: str) -> bytes:
    """失败案例分类清单（对应方案验收）：按失败类型聚类，附样本与过滤链接。"""
    store = app.open_run(run_id)
    categories = _failure_categories(store, run_id)
    store.close()

    body = [f"<h1>{ESC(run_id)} · 失败案例分类</h1>"]
    total = sum(len(c["rows"]) for c in categories)
    if total == 0:
        body.append("<div class='card'>🎉 没有任何失败案例。</div>")
    else:
        body.append(f"<div class='card'>共 {total} 条失败/异常，按类型分 {len(categories)} 类。"
                    f"此页即方案验收的「失败案例分类清单」；"
                    f"<a href='/run/{run_id}/failures.csv'>下载 CSV →</a></div>")
    for cat in categories:
        rows_html = []
        for r in cat["rows"][:10]:
            err = (r["error"] or "")[:120]
            rows_html.append(
                f"<tr><td class='mono'>#{r['gen_id']}</td>"
                f"<td class='mono'>{ESC(r['condition_id'])}</td>"
                f"<td class='mono'><a href='/run/{run_id}/gens?item={ESC(r['item_id'])}'>"
                f"{ESC(r['item_id'])}</a></td><td class='num'>{r['rep']}</td>"
                f"<td class='muted'>{ESC(err)}</td></tr>"
            )
        more = f"（仅列前 10 条，<a href='{cat['link']}'>查看全部 {len(cat['rows'])} 条 →</a>）" \
            if len(cat["rows"]) > 10 and cat["link"] else \
            (f"<a href='{cat['link']}'>在明细浏览器中过滤 →</a>" if cat["link"] else "")
        body.append(
            f"<h2>{ESC(cat['title'])}（{len(cat['rows'])}）</h2>"
            f"<div class='card'><table><tr><th>gen</th><th>条件</th><th>题目</th>"
            f"<th>rep</th><th>错误/理由</th></tr>{''.join(rows_html)}</table>{more}</div>"
        )
    body.append(f"<p><a href='/run/{ESC(run_id)}'>← 返回 run 详情</a></p>")
    return layout(f"{run_id} 失败分类", "".join(body))

# ---------------------------------------------------------------- rejudge（judge 校准闭环）


def page_log(app: WebApp, run_id: str) -> bytes:
    content = app.read_log(run_id)
    if not content:
        inner = "<div class='card muted'>没有日志文件（只有从看板启动的 run 才有 console.log）。</div>"
    else:
        inner = (f"<div class='card'><div class='ke-box mono' "
                 f"style='max-height:70vh;overflow:auto'>{ESC(content)}</div></div>")
    return layout(f"{run_id} 日志",
                  f"<h1>{ESC(run_id)} · 日志（尾部 200 行）</h1>{inner}"
                  f"<p><a href='/run/{ESC(run_id)}'>← 返回 run 详情</a></p>")

REVIEW_VERDICTS = [
    ("", "（未判定）"),
    ("correct", "正确"),
    ("acceptable", "基本正确"),
    ("wrong", "错误"),
    ("undecidable", "无法判定"),
]


def _review_form(run_id: str, r, review, item: dict,
                 reviewer: str = "A", blind: bool = True) -> str:
    current = review["human_verdict"] if review else ""
    opts = "".join(
        f"<option value='{v}' {'selected' if v == current else ''}>{t}</option>"
        for v, t in REVIEW_VERDICTS
    )
    note = review["human_note"] if review else ""
    if blind:
        # 盲评（协议 §7.4）：标注者不知道 k、模型配置与 judge 结论——只给题目/gold/生成
        head = f"#{r['gen_id']} · rep{r['rep']}（盲评：不显示条件与 judge 结论）"
        judge_part = ""
    else:
        head = (f"#{r['gen_id']} {ESC(r['condition_id'])} · {ESC(r['item_id'])} · rep{r['rep']} "
                f"{verdict_chip(r['llm_verdict'])}")
        judge_part = (f"<div class='rationale'>judge：{ESC(r['llm_verdict'] or '—')} — "
                      f"{ESC(r['llm_rationale'] or '')}</div>")
    done_chip = chip('已判定', 'ok') if current else ""
    return f"""
<div class='card'>
<details {'open' if not current else ''}>
<summary>{head} {done_chip}</summary>
<div class='grid2'>
  <div><div class='label'>题目</div><div class='ke-box'>{ESC(item.get('question', '—'))}</div>
  <div class='label'>gold KE</div><div class='ke-box mono'>{ESC(item.get('gold_ke', '—'))}</div></div>
  <div><div class='label'>生成 KE</div><div class='ke-box mono'>{ESC(r['ke_text'] or '—')}</div>
  {judge_part}</div>
</div>
<form method='post' action='/review'>
  <input type='hidden' name='run_id' value='{ESC(run_id)}'>
  <input type='hidden' name='gen_id' value='{r['gen_id']}'>
  <input type='hidden' name='reviewer' value='{ESC(reviewer)}'>
  <input type='hidden' name='blind' value='{'1' if blind else '0'}'>
  <select name='human_verdict'>{opts}</select>
  <input type='text' name='human_note' placeholder='备注（错误类型/原因）' value='{ESC(note or '')}' style='width:50%'>
  <button>保存</button>
</form>
</details>
</div>"""


def page_review(app: WebApp, run_id: str, query: dict) -> bytes:
    store = app.open_run(run_id)
    exp = app.find_experiment(run_id)
    # 抽检比例取领域配置（此前误写死 0.10）
    rate = 0.10
    if exp is not None:
        try:
            from ..config import load_domain
            rate = load_domain(
                app.root / "configs" / "domains" / f"{exp.domain}.toml"
            ).review_sample_rate
        except Exception:
            pass
    reviews = app.get_reviews(run_id)  # 最终判定视图
    raw_reviews = app.get_reviews_raw(run_id)
    reviewer = query.get("reviewer", ["A"])[0].upper()[:1] or "A"
    blind = query.get("blind", ["1"])[0] != "0"

    gen_id = query.get("gen_id", [""])[0]
    rows = []
    if gen_id:
        rows = store._conn.execute(
            "SELECT g.*, j.llm_verdict, j.llm_rationale FROM generations g"
            " LEFT JOIN judgments j ON j.gen_id=g.gen_id"
            " WHERE g.run_id=? AND g.gen_id=? AND g.status='ok'",
            (run_id, int(gen_id)),
        ).fetchall()
    else:
        # 分层确定性抽样：每条件按 gen_id 排序后等距抽取 stride 条
        for cond in store.condition_rows(run_id):
            cond_rows = store._conn.execute(
                "SELECT g.*, j.llm_verdict, j.llm_rationale FROM generations g"
                " LEFT JOIN judgments j ON j.gen_id=g.gen_id"
                " WHERE g.run_id=? AND g.condition_id=? AND g.status='ok' ORDER BY g.gen_id",
                (run_id, cond["condition_id"]),
            ).fetchall()
            stride = max(1, round(1 / rate))
            rows.extend(cond_rows[::stride] or cond_rows[:1])

    gold_by_item: dict[str, dict] = {}
    if exp is not None:
        testset = app.root / exp.testset
        if testset.exists():
            from ..datasets import read_jsonl
            for it in read_jsonl(testset):
                gold_by_item[it.item_id] = {"question": it.question, "gold_ke": it.gold_ke}

    if blind and not gen_id and rows:
        # 盲评防位置泄露：gen_id 按条件分块，能从位置推断 k——
        # 用固定种子打散展示顺序（A/B/C 看到同一顺序，保证独立判定可比）
        import random as _random
        rows = list(rows)
        _random.Random(f"blind-order:{run_id}").shuffle(rows)

    done = sum(1 for r in rows if r["gen_id"] in reviews)
    reviewer_links = " · ".join(
        f"<b>评审员 {r_}</b>" if r_ == reviewer else
        f"<a href='/run/{run_id}/review?reviewer={r_}&blind={'1' if blind else '0'}'>评审员 {r_}</a>"
        for r_ in ("A", "B", "C")
    )
    blind_link = (f"<a href='/run/{run_id}/review?reviewer={reviewer}&blind=0'>关闭盲评</a>"
                  if blind else
                  f"<a href='/run/{run_id}/review?reviewer={reviewer}&blind=1'>开启盲评</a>")
    body = [
        f"<h1>{ESC(run_id)} · 人工抽检队列</h1>",
        f"<div class='card'>按条件分层等距抽样（约 {rate:.0%}），共 {len(rows)} 条，"
        f"已判定 {done} 条。当前身份：{reviewer_links}　|　{blind_link}"
        f"（盲评=不显示 k/模型/judge 结论，协议 §7.4）<br>"
        f"双人独立盲评 + 第三人裁决：A/B 分别判完同一批，分歧自动进 C 的裁决队列。"
        f"判完可 <a href='/run/{run_id}/review.csv'>导出 CSV</a>。</div>",
    ]

    # 裁决队列：A/B 分歧且 C 未裁
    pending_c = app.adjudication_pending(run_id)
    if pending_c:
        links = " ".join(
            f"<a href='/run/{run_id}/review?gen_id={g}&reviewer=C'>#{g}</a>"
            for g in pending_c
        )
        body.append(
            f"<div class='card' style='border-left:3px solid var(--warn)'>"
            f"<b>待第三人裁决（C）：{len(pending_c)} 条</b>（A/B 判定不一致）{links}</div>"
        )

    # 评审一致性（A vs B）+ judge 对照（最终判定 vs judge）
    by_gen: dict[int, dict] = {}
    for r in raw_reviews:
        by_gen.setdefault(r["gen_id"], {})[r["reviewer"]] = r
    ab_both = [(rs["A"], rs["B"]) for rs in by_gen.values() if "A" in rs and "B" in rs]
    if ab_both:
        ab_agree = sum(1 for a, b in ab_both if a["human_verdict"] == b["human_verdict"])
        body.append(
            f"<div class='card'><b>评审一致性（A vs B，协议要报）</b>："
            f"{ab_agree}/{len(ab_both)}（{ab_agree / len(ab_both):.0%}）</div>"
        )

    # 人工 vs judge 一致率（judge 校准）
    human_to_llm = {"correct": "equivalent", "acceptable": "minor", "wrong": "different"}
    compared, agree, disagree = 0, 0, []
    for r in rows:
        review = reviews.get(r["gen_id"])
        if not review or not review["human_verdict"] or not r["llm_verdict"]:
            continue
        mapped = human_to_llm.get(review["human_verdict"])
        if mapped is None:
            continue
        compared += 1
        if mapped == r["llm_verdict"]:
            agree += 1
        else:
            disagree.append((r, review))
    if compared:
        pct = agree / compared * 100
        body.append(
            f"<div class='card'><b>人工 vs judge 一致率</b>（judge 仅辅助信号）："
            f"{agree}/{compared}（{pct:.0f}%）"
            + (f"，分歧 {len(disagree)} 条：" if disagree else "，无分歧 ✓")
            + " ".join(
                f"<a href='/run/{run_id}/review?gen_id={r['gen_id']}&blind=0'>#{r['gen_id']}</a>"
                f"(judge={r['llm_verdict']}/人工={rev['human_verdict']})"
                for r, rev in disagree[:12]
            )
            + "<br><span class='muted'>口径：正确≈equivalent、基本正确≈minor、错误≈different；"
            "「无法判定」不参与。协议口径：语义等价以人工盲评为准，LLM judge 只作辅助信号。</span></div>"
        )
    body.extend(
        _review_form(run_id, r, by_gen.get(r["gen_id"], {}).get(reviewer),
                     gold_by_item.get(r["item_id"], {}), reviewer=reviewer, blind=blind)
        for r in rows
    )
    store.close()
    return layout(f"{run_id} 抽检", "".join(body))