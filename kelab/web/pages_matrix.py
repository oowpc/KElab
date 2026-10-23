"""实验矩阵页（协议 §7.7）：每行一次运行批次的全部可溯字段。

领域/模型及版本/k/种子/题数/池版本（数据指纹）/prompt 版本/判分器版本/批次状态。
"""
from __future__ import annotations

import csv
import io

from ..prereg import load_prereg
from .app import WebApp
from .ui import ESC, layout


def _matrix_rows(app: WebApp) -> list[dict]:
    import json as _json

    rows = []
    for r in app.list_runs():
        run_id = r["run_id"]
        store = None
        try:
            store = app.open_run(run_id)
            info = store.run_info(run_id)
            cfg = _json.loads(info["config_json"]) if info else {}
            conds = store.condition_rows(run_id)
            exp = app.find_experiment(run_id)
            reg = load_prereg(app.root, cfg.get("experiment", "")) if cfg.get("experiment") else None
            models = sorted({c["gen_model"] for c in conds if c["gen_model"]})
            rows.append({
                "run_id": run_id,
                "domain": cfg.get("domain", "—"),
                "kind": exp.kind if exp else "—",
                "models": "、".join(models) or "领域默认",
                "repeats": exp.repeats if exp else "—",
                "seeds": ",".join(str(s) for s in (exp.seeds[:exp.repeats] if exp else [])),
                "conditions": len(conds),
                "generations": r["ok"] + r["error"],
                "dataset_md5": (info["dataset_md5"] or "—") if info else "—",
                "prompt_md5": (info["prompt_template_md5"] or "—") if info else "—",
                "prereg": (f"v{reg['version']} {reg['prereg_hash']}" if reg else "未登记"),
                "status": r["status"],
                "started_at": r["started_at"],
            })
        finally:
            if store:
                store.close()
    return rows


def page_matrix(app: WebApp) -> bytes:
    rows = _matrix_rows(app)
    trs = "".join(
        f"<tr><td class='mono'><a href='/run/{ESC(r['run_id'])}'>{ESC(r['run_id'])}</a></td>"
        f"<td>{ESC(r['domain'])}</td><td>{ESC(r['kind'])}</td>"
        f"<td class='mono'>{ESC(r['models'])}</td>"
        f"<td class='num'>{r['repeats']}</td><td class='mono'>{ESC(r['seeds'])}</td>"
        f"<td class='num'>{r['conditions']}</td><td class='num'>{r['generations']}</td>"
        f"<td class='mono'>{ESC(r['dataset_md5'])}</td><td class='mono'>{ESC(r['prompt_md5'])}</td>"
        f"<td class='mono'>{ESC(r['prereg'])}</td><td>{ESC(r['status'])}</td>"
        f"<td class='muted'>{ESC(r['started_at'])}</td></tr>"
        for r in rows
    )
    body = f"""<h1>实验矩阵（协议 §7.7）</h1>
<div class='card'><p class='muted'>每行一次运行批次：领域/模型/k/种子/题数/池版本/prompt 版本/预登记/批次。
<a href='/matrix.csv'>导出 CSV →</a></p>
{"<div style='overflow-x:auto'><table><tr><th>run</th><th>领域</th><th>类型</th><th>模型</th><th>重复</th><th>种子</th><th>条件数</th><th>生成数</th><th>数据指纹</th><th>prompt md5</th><th>预登记</th><th>状态</th><th>开始时间</th></tr>" + trs + "</table></div>" if rows else "<p class='muted'>还没有运行记录。</p>"}
</div>"""
    return layout("实验矩阵", body)


def matrix_csv(app: WebApp) -> bytes:
    rows = _matrix_rows(app)
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = ["run_id", "domain", "kind", "models", "repeats", "seeds", "conditions",
            "generations", "dataset_md5", "prompt_md5", "prereg", "status", "started_at"]
    writer.writerow(cols)
    for r in rows:
        writer.writerow([r[c] for c in cols])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
