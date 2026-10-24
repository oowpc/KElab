"""实验操作页面：启动（单个/批量/试跑）、跨 run 对比、(k,N) 网格、汇总对比。"""
from __future__ import annotations

import statistics
from pathlib import Path

from ..aggregate import aggregate_run
from ..config import ConfigError, load_experiment
from ..datasets import count_lines as _count_lines
from .app import WebApp
from .ui import ESC, chip, layout, _fmt_pct

# ---------------------------------------------------------------- 跨 run 对比

COMPARE_METRICS = [
    ("semantic", "语义等价率", "rate", True),
    ("exact", "完全匹配", "rate", True),
    ("lark", "语法解析", "rate", True),
    ("light", "light检查", "rate", True),
    ("retrieval_coverage", "检索覆盖", "rate", True),
    ("gen_coverage", "生成覆盖", "rate", True),
    ("prompt_tokens", "prompt tokens", "num", False),
    ("latency_ms", "耗时 ms", "num", False),
]
_UNCHANGED_TOL = 0.005  # Δ 在 ±0.5pp 内视为持平


def _delta_chip(delta: float | None, higher_better: bool, is_rate: bool) -> str:
    if delta is None:
        return chip("—")
    text = f"{delta * 100:+.1f}pp" if is_rate else f"{delta:+.0f}"
    if abs(delta) <= (_UNCHANGED_TOL if is_rate else 1e-9):
        return chip(text)
    better = (delta > 0) == higher_better
    return chip(text, "ok" if better else "err")


def chart_compare_overlay(
    app: WebApp, run_a: str, run_b: str, retriever: str,
    metrics_a: dict, metrics_b: dict, kind: str, out_path: Path,
) -> bool:
    """主指标叠加曲线：a=蓝、b=橙（固定顺序），误差棒=3 重复 std。"""
    import matplotlib.pyplot as plt
    from ..report import (_categorical_x, _mean_std, _setup_fonts, _style_ax,
                         SERIES_COLOR, SURFACE)

    _setup_fonts()
    def series(metrics):
        cms = [cm for cm in metrics if cm.retriever == retriever]
        cms.sort(key=lambda cm: cm.k if kind == "k" else cm.n)
        return cms

    cms_a, cms_b = series(metrics_a), series(metrics_b)
    if not cms_a and not cms_b:
        return False
    key = (lambda cm: cm.k) if kind == "k" else (lambda cm: cm.n)
    xs_all = sorted({key(cm) for cm in cms_a + cms_b})
    fig, ax = plt.subplots(figsize=(7.2, 4.4), facecolor=SURFACE)

    for cms, color, label in (
        (cms_a, SERIES_COLOR["random"], f"a: {run_a[:22]}"),
        (cms_b, SERIES_COLOR["bm25"], f"b: {run_b[:22]}"),
    ):
        if not cms:
            continue
        means = {key(cm): _mean_std(cm, "semantic") for cm in cms}
        xs, labels = _categorical_x(xs_all)
        ys = [means.get(x, (None, 0))[0] for x in xs_all]
        errs = [means.get(x, (None, 0))[1] for x in xs_all]
        pts = [(x, y, e) for x, y, e in zip(xs, ys, errs) if y is not None]
        if pts:
            from ..report import _line_with_optional_errors
            _line_with_optional_errors(
                ax, [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts],
                color, label)
    ax.set_xticks(*_categorical_x(xs_all))
    _style_ax(ax, f"语义等价率对比（{retriever}）", "k" if kind == "k" else "N", "比例")
    ax.set_ylim(-0.05, 1.1)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return True


def _condition_ids(app: WebApp, run_id: str) -> set[str]:
    try:
        store = app.open_run(run_id)
        ids = {c["condition_id"] for c in store.condition_rows(run_id)}
        store.close()
        return ids
    except Exception:
        return set()


def _default_compare_pair(app: WebApp, runs: list[dict]) -> tuple[str, str]:
    """默认选最近两个「已完成且条件有交集」的 run（比不了的一对只会误导）。"""
    done = [r for r in runs if r["status"] == "done"][:8]
    conds: dict[str, set[str]] = {}
    for r in done:
        conds[r["run_id"]] = _condition_ids(app, r["run_id"])
    for i, rb in enumerate(done):
        for ra in done[i + 1:]:
            if conds[rb["run_id"]] & conds[ra["run_id"]]:
                return ra["run_id"], rb["run_id"]
    return ("", "")


def page_compare(app: WebApp, query: dict) -> bytes:
    runs = app.list_runs()
    run_a = query.get("a", [""])[0]
    run_b = query.get("b", [""])[0]
    retriever_q = query.get("retriever", [""])[0]
    # 默认预选最近两个可比 run（完成 + 条件有交集；同 run/不可比没信息量）
    if not run_a and not run_b and len(runs) >= 2:
        run_a, run_b = _default_compare_pair(app, runs)

    options = "".join(
        f"<option value='{ESC(r['run_id'])}' {'selected' if r['run_id'] == run_a else ''}>"
        f"{ESC(r['run_id'])}</option>"
        for r in runs
    )
    options_b = "".join(
        f"<option value='{ESC(r['run_id'])}' {'selected' if r['run_id'] == run_b else ''}>"
        f"{ESC(r['run_id'])}</option>"
        for r in runs
    )
    body = ["<h1>跨 run 对比</h1>",
            f"<div class='card'><form class='filters' method='get'>"
            f"基准 a <select name='a'>{options}</select>"
            f"候选 b <select name='b'>{options_b}</select>"
            f"<button>对比</button>"
            f"<span class='muted'>Δ = b − a；绿=b 更好，红=b 更差，±0.5pp 内算持平</span></form></div>"]
    if not (run_a and run_b) or run_a == run_b:
        if run_a == run_b and run_a:
            body.append("<div class='card'>a 和 b 是同一个 run，请选两个不同的。</div>")
        return layout("跨 run 对比", "".join(body), active="compare")

    try:
        store_a, store_b = app.open_run(run_a), app.open_run(run_b)
    except KeyError as exc:
        return layout("跨 run 对比", f"<h1>run 不存在：{ESC(str(exc))}</h1>")
    exp_a, exp_b = app.find_experiment(run_a), app.find_experiment(run_b)
    if exp_a is None or exp_b is None:
        body.append("<div class='card'>其中一个 run 找不到实验配置，无法聚合。</div>")
        return layout("跨 run 对比", "".join(body), active="compare")
    kind = exp_a.kind
    metrics_a = aggregate_run(store_a, run_a, exp_a)
    metrics_b = aggregate_run(store_b, run_b, exp_b)
    by_a = {cm.condition_id: cm for cm in metrics_a}
    by_b = {cm.condition_id: cm for cm in metrics_b}
    shared = set(by_a) & set(by_b)
    if not shared:
        # 无可比条件：空状态，不画图不画表（画了反而误导）
        body.append(
            "<div class='card'><b>这两个 run 没有可比的条件</b>（条件集合无交集）。<br>"
            "<span class='muted'>通常是一个跑了 k 实验、一个跑了 N 实验，或检索器/模型维度不同。"
            "换一对试试；同实验的两次 run 可比性最好。</span></div>"
        )
        store_a.close()
        store_b.close()
        return layout("跨 run 对比", "".join(body), active="compare")
    cond_ids = sorted(shared)

    # 概要：主指标 semantic 的改善/恶化/持平
    improved = worsened = flat = 0
    deltas: list[float] = []
    for cid in cond_ids:
        ma = by_a.get(cid) and by_a[cid].stats.get("semantic")
        mb = by_b.get(cid) and by_b[cid].stats.get("semantic")
        if not ma or not mb or ma.mean is None or mb.mean is None:
            continue
        d = mb.mean - ma.mean
        deltas.append(d)
        if d > _UNCHANGED_TOL:
            improved += 1
        elif d < -_UNCHANGED_TOL:
            worsened += 1
        else:
            flat += 1
    mean_delta = sum(deltas) / len(deltas) if deltas else None
    summary = (
        f"主指标（语义等价率）平均 Δ <b>{mean_delta * 100:+.1f}pp</b>；"
        f"改善 {improved} 条件 / 恶化 {worsened} / 持平 {flat}"
        if mean_delta is not None else "两个 run 没有可比的条件"
    )
    body.append(f"<div class='card'>{summary}</div>")

    # 叠加曲线（选检索器）
    retrievers = sorted({cm.retriever for cm in metrics_a + metrics_b})
    retriever = retriever_q if retriever_q in retrievers else (retrievers[0] if retrievers else "")
    if retrievers:
        tabs = " ".join(
            f"<b>{ESC(r)}</b>" if r == retriever else
            f"<a href='/compare?a={ESC(run_a)}&b={ESC(run_b)}&retriever={ESC(r)}'>{ESC(r)}</a>"
            for r in retrievers
        )
        png = app.results_dir / "_compare" / f"{run_a}_vs_{run_b}_{retriever}.png"
        try:
            ok = chart_compare_overlay(app, run_a, run_b, retriever,
                                       metrics_a, metrics_b, kind, png)
        except Exception:
            ok = False
        if ok:
            import time
            body.append(
                f"<h2>叠加曲线（检索器：{tabs}）</h2>"
                f"<div class='card'><img class='chart' src='/files/results/_compare/"
                f"{ESC(png.name)}?v={int(time.time())}' alt='{ESC(run_a)} vs {ESC(run_b)} 叠加曲线'></div>"
            )

    # Δ 表
    header = "".join(f"<th>{ESC(label)}</th>" for _, label, _, _ in COMPARE_METRICS)
    rows_html = []
    for cid in cond_ids:
        cm_a, cm_b = by_a.get(cid), by_b.get(cid)
        cells = [f"<td class='mono'>{ESC(cid)}</td>"]
        for metric, _label, fmt, higher_better in COMPARE_METRICS:
            sa = cm_a and cm_a.stats.get(metric)
            sb = cm_b and cm_b.stats.get(metric)
            va = sa.mean if sa and sa.mean is not None else None
            vb = sb.mean if sb and sb.mean is not None else None
            is_rate = fmt == "rate"
            def fmt_v(v):
                return _fmt_pct(v) if is_rate else (f"{v:.0f}" if v is not None else "—")
            delta = (vb - va) if (va is not None and vb is not None) else None
            cells.append(
                f"<td class='num'>{fmt_v(va)} / {fmt_v(vb)} {_delta_chip(delta, higher_better, is_rate)}</td>"
            )
        rows_html.append("<tr>" + "".join(cells) + "</tr>")
    body.append(
        "<h2>条件 × 指标（a / b / Δ）</h2>"
        f"<div class='card'><table><tr><th>条件</th>{header}</tr>"
        + "".join(rows_html) + "</table></div>"
    )
    store_a.close()
    store_b.close()
    return layout("跨 run 对比", "".join(body), active="compare")

# ---------------------------------------------------------------- 汇总对比（分领域小多图 + 跨 run 总览）


def _run_summary(app: WebApp, run_id: str, exp, store) -> dict:
    """单个 run 的总览行：最佳语义（及档位）、拐点、通过率、消耗、judge 一致率。"""
    from ..aggregate import aggregate_run, detect_knee
    from ..report import _mean_std

    metrics = aggregate_run(store, run_id, exp)
    key = (lambda cm: cm.k) if exp.kind == "k" else (lambda cm: cm.n)
    full = [cm for cm in metrics if cm.onto == "full"]
    best = None
    for cm in full:
        mean, _ = _mean_std(cm, "semantic")
        if mean is not None and (best is None or mean > best[1]):
            best = (cm, mean)
    knees: list[str] = []
    for retriever in sorted({cm.retriever for cm in full}):
        cms = sorted([cm for cm in full if cm.retriever == retriever], key=key)
        xs = [key(cm) for cm in cms]
        means = [_mean_std(cm, "semantic")[0] for cm in cms]
        if all(m is not None for m in means) and len(means) >= 2:
            knee = detect_knee([float(x) for x in xs],
                               [float(m) for m in means], exp.knee_threshold)
            if knee is not None:
                knees.append(f"{retriever}:{knee:g}")
    def avg(metric_name):
        vals = [_mean_std(cm, metric_name)[0] for cm in full]
        vals = [v for v in vals if v is not None]
        return statistics.fmean(vals) if vals else None

    cost = app.cost_summary(run_id)
    gen_tokens = (cost.get("gen_prompt_tokens", 0) + cost.get("gen_completion_tokens", 0))
    judge_tokens = (cost.get("judge_prompt_tokens", 0) + cost.get("judge_completion_tokens", 0))
    # judge 一致率（有人工判定时）
    reviews = app.get_reviews(run_id)
    agree = total = 0
    if reviews:
        mapping = {"correct": "equivalent", "acceptable": "minor", "wrong": "different"}
        rows = store._conn.execute(
            "SELECT g.gen_id, j.llm_verdict FROM generations g"
            " JOIN judgments j ON j.gen_id=g.gen_id WHERE g.run_id=?", (run_id,),
        ).fetchall()
        verdict = {r["gen_id"]: r["llm_verdict"] for r in rows}
        for gen_id, rev in reviews.items():
            mapped = mapping.get(rev["human_verdict"])
            if mapped and verdict.get(gen_id):
                total += 1
                agree += mapped == verdict[gen_id]
    return {
        "best_cond": best[0].condition_id if best else "—",
        "best_semantic": best[1] if best else None,
        "best_axis": key(best[0]) if best else None,
        "knees": "、".join(knees) or "未出现",
        "exact": avg("exact"),
        "lark": avg("lark"),
        "light": avg("light"),
        "gen_tokens": gen_tokens,
        "judge_tokens": judge_tokens,
        "agree": f"{agree}/{total}" if total else "—",
        "finding": _one_finding(app, exp, metrics, agree, total),
    }


def _one_finding(app: WebApp, exp, metrics, agree: int, total: int) -> str:
    """每个 run 的一句话解读（总览表用）：取第一条非 info 的规则判定。"""
    from ..aggregate import mde_estimate
    from ..interpret import interpret_run

    n_items = 0
    testset_path = app.root / exp.testset
    if testset_path.exists():
        n_items = _count_lines(testset_path)
    findings = interpret_run(metrics, exp, mde_estimate(n_items),
                             review_agree=agree, review_total=total)
    for f in findings:
        if f.level != "info":
            return f.text
    return findings[0].text if findings else "—"


def page_domains(app: WebApp, query: dict) -> bytes:
    """汇总对比页：勾选多个 run → 总览汇总表 + 同轴叠加图 + 小多图 + 各域拐点表。"""
    from ..aggregate import aggregate_run, detect_knee
    from ..report import _mean_std, chart_domain_small_multiples, chart_runs_overlay

    runs = app.list_runs()
    selected = query.get("runs", [])
    boxes = "".join(
        f"<label style='margin-right:14px'><input type='checkbox' name='runs' "
        f"value='{ESC(r['run_id'])}' {'checked' if r['run_id'] in selected else ''}> "
        f"<span class='mono'>{ESC(r['run_id'])}</span></label>"
        for r in runs
    )
    body = ["<h1>汇总对比</h1>",
            f"<div class='card'><form method='get'>{boxes}"
            f"<div style='margin-top:8px'><button>汇总（勾选 ≥1 个 run）</button></div></form>"
            f"<span class='muted'>总览表 → 同轴叠加图 → 分域小多图 → 各域拐点。"
            f"只有同测试集的 run 才可横向比（不同实验/对照 arms 请分别读）。"
            f"两两精细 Δ 请走 <a href='/compare'>跨 run 对比</a>。</span></div>"]
    if not selected:
        return layout("汇总对比", "".join(body), active="domains")

    panels: list[dict] = []
    knee_rows: list[tuple[str, str, str, float | None]] = []
    summary_rows: list[tuple[str, str, dict]] = []
    overlay_groups: dict[tuple[str, str], dict] = {}
    for run_id in selected:
        exp = app.find_experiment(run_id)
        if exp is None:
            continue
        store = app.open_run(run_id)
        summary_rows.append((run_id, exp.domain, _run_summary(app, run_id, exp, store)))
        metrics = aggregate_run(store, run_id, exp)
        key = (lambda cm: cm.k) if exp.kind == "k" else (lambda cm: cm.n)
        series: dict[str, tuple] = {}
        for retriever in sorted({cm.retriever for cm in metrics}):
            cms = sorted([cm for cm in metrics
                          if cm.retriever == retriever and cm.onto == "full"], key=key)
            if not cms:
                continue
            xs = [key(cm) for cm in cms]
            ys = []
            errs = []
            valid = True
            for cm in cms:
                mean, std = _mean_std(cm, "semantic")
                if mean is None:
                    valid = False
                ys.append(mean if mean is not None else float("nan"))
                errs.append(std)
            series[retriever] = (xs, ys, errs)
            # 叠加图分组：同 (kind, retriever) 的 run 叠一张图
            group = overlay_groups.setdefault(
                (exp.kind, retriever),
                {"title": f"{'k' if exp.kind == 'k' else 'N'} × {retriever}",
                 "kind": exp.kind, "lines": []})
            group["lines"].append({"label": f"{exp.domain}:{run_id[:14]}",
                                   "xs": xs, "ys": ys, "errs": errs})
            knee = detect_knee([float(x) for x in xs], ys, exp.knee_threshold) \
                if valid and len(xs) >= 2 else None
            knee_rows.append((exp.domain, retriever, exp.kind, knee))
        # 空 run（条件在但没数据）不进小多图，避免空坐标轴误导
        has_data = any(
            y == y for _xs, ys, _errs in series.values() for y in ys
        )
        if has_data:
            panels.append({"label": f"{exp.domain}（{run_id[:18]}…）", "kind": exp.kind,
                           "series": series})
        store.close()
    if not panels:
        body.append("<div class='card'>勾选的 run 都找不到实验配置，无法汇总。</div>")
        return layout("汇总对比", "".join(body), active="domains")

    # ---- ① 总览汇总表（按最佳语义降序）
    def pct(v):
        return f"{v * 100:.1f}%" if v is not None else "—"
    summary_rows.sort(key=lambda r: (r[2]["best_semantic"] or -1), reverse=True)

    rows_html = "".join(
        f"<tr><td class='mono'>{ESC(run_id)}</td><td>{ESC(domain)}</td>"
        f"<td class='num'><b>{pct(s['best_semantic'])}</b></td>"
        f"<td class='mono'>{ESC(s['best_cond'])}</td>"
        f"<td>{ESC(s['knees'])}</td>"
        f"<td class='num'>{pct(s['exact'])}</td>"
        f"<td class='num'>{pct(s['lark'])}</td>"
        f"<td class='num'>{pct(s['light'])}</td>"
        f"<td class='num'>{s['gen_tokens'] + s['judge_tokens']:,}</td>"
        f"<td class='num'>{ESC(s['agree'])}</td>"
        f"<td style='max-width:360px'>{ESC(s['finding'])}</td>"
        f"<td><a href='/run/{ESC(run_id)}'>详情</a></td></tr>"
        for run_id, domain, s in summary_rows
    )
    body.append(
        "<h2>① 总览（按最佳语义降序）</h2>"
        "<div class='card'><table><tr><th>run</th><th>领域</th><th>最佳语义</th>"
        "<th>所在条件</th><th>拐点</th><th>完全匹配</th><th>语法</th><th>light</th>"
        "<th>总 tokens</th><th>judge 一致</th><th>解读</th><th></th></tr>"
        + rows_html + "</table></div>"
    )

    # ---- ② 同轴叠加图（同 kind×retriever 的 run 叠一张图）
    import hashlib
    digest = hashlib.sha256("|".join(sorted(selected)).encode()).hexdigest()[:10]
    overlay_png = app.results_dir / "_domains" / f"overlay_{digest}.png"
    if chart_runs_overlay(list(overlay_groups.values()), overlay_png):
        body.append("<h2>② 同轴叠加（同类型同检索器）</h2>"
                    f"<div class='card'><img class='chart' src='/files/results/_domains/"
                    f"{ESC(overlay_png.name)}?v={len(selected)}' alt='多 run 同轴叠加曲线'></div>")

    # ---- ③ 分域小多图
    png = app.results_dir / "_domains" / f"domains_{digest}.png"
    ok = chart_domain_small_multiples(panels, png)
    if ok:
        body.append("<h2>③ 分域小多图</h2>"
                    f"<div class='card'><img class='chart' src='/files/results/_domains/"
                    f"{ESC(png.name)}?v={len(panels)}' alt='分领域小多图'></div>")

    # ---- ④ 各域拐点表 + 跨域一致性（H3 视图）
    rows_html = "".join(
        f"<tr><td>{ESC(domain)}</td><td>{ESC(retriever)}</td><td>{kind}</td>"
        f"<td class='num'>{f'{knee:g}' if knee is not None else '未出现/数据不足'}</td></tr>"
        for domain, retriever, kind, knee in knee_rows
    )
    n_knees = [k for _, _, kind, k in knee_rows if k is not None and kind == "n"]
    k_knees = [k for _, _, kind, k in knee_rows if k is not None and kind == "k"]
    present = n_knees or k_knees
    if len(present) >= 2:
        spread = max(present) - min(present)
        note = (f"各域拐点分布在 {min(present):g} ~ {max(present):g}（极差 {spread:g}）；"
                "极差小支持「拐点跨域一致」（H3 不支持），差异大支持「分域给不同拐点」（H3 支持）。"
                "判读须结合 MDE 与重复 std。")
    else:
        note = "数据不足：出现拐点的域少于 2 个，无法比对跨域一致性。"
    body.append(
        "<h2>④ 各域拐点</h2>"
        f"<div class='card'><table><tr><th>领域</th><th>检索器</th><th>实验</th><th>拐点</th></tr>"
        f"{rows_html}</table><span class='muted'>{ESC(note)}</span></div>"
    )
    return layout("汇总对比", "".join(body), active="domains")


# ---------------------------------------------------------------- (k, N) 二维网格

def page_grid(app: WebApp, query: dict) -> bytes:
    """(k, N) 网格：k 实验与 N 实验（同测试集）拼二维语义等价率表——方案要求的缝合产物。
    k 实验贡献 (k, 全池) 行，N 实验贡献 (fixed_k, N) 列，缺格标「未测」。"""
    runs = app.list_runs()
    k_run_id = query.get("k", [""])[0]
    n_run_id = query.get("n", [""])[0]
    retriever_q = query.get("retriever", [""])[0]

    options = "".join(f"<option value='{ESC(r['run_id'])}' "
                      f"{'selected' if r['run_id'] == k_run_id else ''}>{ESC(r['run_id'])}</option>"
                      for r in runs)
    options_n = "".join(f"<option value='{ESC(r['run_id'])}' "
                        f"{'selected' if r['run_id'] == n_run_id else ''}>{ESC(r['run_id'])}</option>"
                        for r in runs)
    body = ["<h1>(k, N) 网格</h1>",
            f"<div class='card'><form class='filters' method='get'>"
            f"k 实验 run <select name='k'>{options}</select>"
            f"N 实验 run <select name='n'>{options_n}</select>"
            f"<button>拼网格</button>"
            f"<span class='muted'>单元格=语义等价率（3 重复均值），颜色越深越高；缺格=未测</span>"
            f"</form></div>"]
    if not (k_run_id and n_run_id):
        return layout("(k, N) 网格", "".join(body), active="grid")

    store_k, store_n = app.open_run(k_run_id), app.open_run(n_run_id)
    exp_k, exp_n = app.find_experiment(k_run_id), app.find_experiment(n_run_id)
    if exp_k is None or exp_n is None:
        body.append("<div class='card'>找不到实验配置，无法聚合。</div>")
        return layout("(k, N) 网格", "".join(body), active="grid")
    metrics = aggregate_run(store_k, k_run_id, exp_k) + aggregate_run(store_n, n_run_id, exp_n)

    retrievers = sorted({cm.retriever for cm in metrics})
    retriever = retriever_q if retriever_q in retrievers else (
        "bm25" if "bm25" in retrievers else retrievers[0] if retrievers else "")
    tabs = " ".join(
        f"<b>{ESC(r)}</b>" if r == retriever else
        f"<a href='/grid?k={ESC(k_run_id)}&n={ESC(n_run_id)}&retriever={ESC(r)}'>{ESC(r)}</a>"
        for r in retrievers
    )

    # 单元格收集：(k, n) -> semantic mean
    cells: dict[tuple[int, int], float] = {}
    for cm in metrics:
        if cm.retriever != retriever:
            continue
        stat = cm.stats.get("semantic")
        if stat is None or stat.mean is None or cm.k is None or cm.n is None:
            continue
        key = (int(cm.k), int(cm.n))
        # 同一格可能被两个 run 覆盖到（如 k=3,N=全池）：保留先到的（k 实验 run 优先）
        cells.setdefault(key, stat.mean)
    ks = sorted({k for k, _ in cells})
    ns = sorted({n for _, n in cells})

    def cell_bg(value: float) -> str:
        alpha = max(0.0, min(1.0, value)) * 0.72
        return f"background: rgba(42,120,214,{alpha:.2f})"

    header = "".join(f"<th>N={n}</th>" for n in ns)
    rows_html = []
    for k in ks:
        tds = [f"<td class='num'><b>k={k}</b></td>"]
        for n in ns:
            value = cells.get((k, n))
            if value is None:
                tds.append("<td class='num muted'>—</td>")
            else:
                tds.append(f"<td class='num' style='{cell_bg(value)}'>{value * 100:.1f}%</td>")
        rows_html.append("<tr>" + "".join(tds) + "</tr>")
    body.append(
        f"<h2>语义等价率（检索器：{tabs}）</h2>"
        f"<div class='card'><table><tr><th></th>{header}</tr>{''.join(rows_html)}</table>"
        f"<span class='muted'>k 实验行 = 该 run 的全池 N（{', '.join(str(cm.n) for cm in metrics if cm.kind == 'k') or '—'}）；"
        f"N 实验列 = 固定 k={exp_n.fixed_k}。H 组对照（N* 处扫 k）可在 N 实验列与 k 实验行的交叉格读取。</span></div>"
    )
    store_k.close()
    store_n.close()
    return layout("(k, N) 网格", "".join(body), active="grid")

def page_launch(app: WebApp, message: str = "") -> bytes:
    exps = []
    if app.experiments_dir.is_dir():
        for toml in sorted(app.experiments_dir.glob("*.toml")):
            try:
                exp = load_experiment(toml)
                exps.append((toml, exp))
            except ConfigError:
                continue
    # 历史均值校准 token 粗估（没有历史就用冒烟经验值：生成 ~2500 tok/条、judge ~900 tok/条）
    avg_gen, avg_judge = _historical_token_means(app)
    any_gated = False
    rows = []
    cards = []
    for toml, exp in exps:
        grid = f"k={exp.ks}" if exp.kind == "k" else f"N={exp.ns} × k={exp.fixed_k}"
        n_conds, gens, judges = app.estimate_calls(exp)
        if gens:
            tok_est = gens * avg_gen + judges * avg_judge
            estimate = (f"{n_conds} 条件 → 约 {gens} 生成 + {judges} judge ≈ {gens + judges} 次调用"
                        f"<br><span class='muted'>≈{tok_est / 1e4:.1f} 万 tokens（历史均值粗估）</span>")
        else:
            estimate = "测试集缺失或为空，无法估算"
        label, kind, invalid = app.prereg_status(exp)
        reg_html = chip(label, kind)
        if invalid:
            any_gated = True
            gate = ("<label style='font-size:12px'><input type='checkbox' name='prereg_ok'> "
                    "未登记/无效，按试跑口径（≤5 题）</label>")
        else:
            gate = ""
        rows.append(
            f"<tr><td><input type='checkbox' name='exp' form='batchform' "
            f"value='{ESC(str(toml))}' aria-label='选择 {ESC(exp.name)}'></td>"
            f"<td class='mono'>{ESC(exp.name)}<br>{reg_html}</td><td>{ESC(exp.domain)}</td>"
            f"<td>{exp.kind}</td><td>{ESC(grid)}</td>"
            f"<td>{ESC(', '.join(exp.retrievers))}</td><td class='num'>{exp.repeats}</td>"
            f"<td class='muted'>{estimate}</td>"
            f"<td><a href='/configs/edit?path={ESC(str(toml))}'>改参数</a></td>"
            f"<td><form class='inline' method='post' action='/launch'"
            f"{' data-needs-prereg' if invalid else ''}"
            f" data-confirm='{ESC('确认启动 ' + exp.name + '？将按上方估算消耗 token。')}'>"
            f"<input type='hidden' name='exp' value='{ESC(str(toml))}'>"
            f"<label class='muted' style='font-size:12px'>试跑题数 "
            f"<input type='text' name='limit' placeholder='全量' "
            f"style='width:52px' aria-label='试跑题数（留空=全量）'></label>"
            f"{gate}"
            f"<button{' disabled' if invalid else ''}>启动</button></form></td></tr>"
        )
        # 移动端卡片：启动按钮 + 试跑题数直接在手上，不用横滑
        cards.append(
            f"<div class='rcard'><b class='mono'>{ESC(exp.name)}</b> {reg_html}<br>"
            f"<span class='muted'>{ESC(grid)} · {ESC(', '.join(exp.retrievers))} · "
            f"重复 {exp.repeats}</span><br>"
            f"<span class='muted' style='font-size:12.5px'>{estimate}</span>"
            f"<div class='ops'><form class='inline' method='post' action='/launch'"
            f"{' data-needs-prereg' if invalid else ''}"
            f" data-confirm='{ESC('确认启动 ' + exp.name + '？')}'>"
            f"<input type='hidden' name='exp' value='{ESC(str(toml))}'>"
            f"<input type='text' name='limit' placeholder='全量' style='width:56px'"
            f" aria-label='试跑题数（留空=全量）'>"
            f"{gate}"
            f"<button{' disabled' if invalid else ''}>启动</button> "
            f"<a href='/configs/edit?path={ESC(str(toml))}'>改参数</a></form></div></div>"
        )
    if not rows:
        table_note = "<p class='muted'>configs/experiments/ 下没有实验配置。</p>"
        rows = []
    else:
        table_note = ""
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    queue_note = app.queue_status()
    queue_html = f"<div class='flash'>{ESC(queue_note)}</div>" if queue_note else ""
    batch_gate = ("<label style='font-size:13px'><input type='checkbox' name='prereg_ok'> "
                  "含未登记/无效实验：按试跑口径（各 limit=5）</label><br>") if any_gated else ""
    body = f"""<h1>启动实验</h1>{flash}{queue_html}
<div class='card'>{table_note}
<table class='rtable'><tr><th></th><th>实验</th><th>领域</th><th>类型</th><th>网格</th>
<th>检索器</th><th>重复</th><th>调用量估算</th><th></th><th></th></tr>{"".join(rows)}</table>
<div class='rcards'>{''.join(cards)}</div>
<p class='muted'>相同配置重复启动 = 断点续跑，已完成的 (条件,题,重复) 不会重跑，启动后自动跳转进度页。
改参数走「改参数」表单（保存自动备份），prompt 模板在 <a href='/prompts'>Prompt</a> 页编辑。
没有想要的实验？<a href='/configs/new?type=experiment'>新建实验配置 →</a>
<b>启动会消耗 LLM token</b>，请先用 <a href='/prompts'>Prompt 预览</a> 和 CLI --dry-run 确认；
主实验请先 <a href='/prereg'>预登记</a>（未登记/配置漂移的实验须勾选「试跑口径」，服务端强制 limit≤5）。</p>
<form method='post' action='/launch_batch' id='batchform'
 data-confirm='确认批量启动勾选实验？将串行依次消耗 token。'>
{batch_gate}<button>批量启动勾选实验（串行队列）</button>
</form></div>"""
    return layout("启动实验", body, active="launch")


def _historical_token_means(app: WebApp) -> tuple[int, int]:
    """从历史 run 取每条的平均 token（生成/judge），供启动页粗估；无历史用经验值。"""
    for r in app.list_runs()[:5]:
        try:
            store = app.open_run(r["run_id"])
            row = store._conn.execute(
                "SELECT AVG(g.prompt_tokens + g.completion_tokens) g,"
                " AVG(j.judge_prompt_tokens + j.judge_completion_tokens) j"
                " FROM generations g JOIN judgments j ON j.gen_id=g.gen_id"
                " WHERE g.run_id=? AND g.status='ok'", (r["run_id"],),
            ).fetchone()
            store.close()
            if row and row["g"]:
                return int(row["g"]), int(row["j"] or 900)
        except Exception:
            continue
    return 2500, 900
