"""预登记页（协议 §7.5/7.6）：跑主实验前由决策人冻结 δ、质量门槛、抽检比例、判分口径。

防事后解释：内容哈希 + 时间戳落 configs/prereg/<实验>.json；改动留历史不覆盖。
"""
from __future__ import annotations

from pathlib import Path

from ..config import load_experiment
from ..prereg import create_prereg, load_prereg
from .app import WebApp
from .ui import ESC, layout


def page_prereg(app: WebApp, query: dict) -> bytes:
    rows = []
    exp_dir = app.root / "configs" / "experiments"
    if exp_dir.is_dir():
        for toml in sorted(exp_dir.glob("*.toml")):
            try:
                exp = load_experiment(toml)
            except Exception:
                continue
            reg = load_prereg(app.root, exp.name)
            if reg:
                status = (f"<span class='chip ok'>已登记 v{reg['version']}</span> "
                          f"<span class='mono muted'>{reg['prereg_hash']}</span> "
                          f"<span class='muted'>{ESC(reg['created_at'])} · {ESC(reg['actor'])}</span>")
                action = (f"<a href='/prereg/edit?exp={ESC(exp.name)}'>改（留历史）</a>")
            else:
                status = "<span class='chip warn'>未登记</span>"
                action = f"<a href='/prereg/edit?exp={ESC(exp.name)}'>去登记 →</a>"
            rows.append(
                f"<tr><td class='mono'>{ESC(exp.name)}</td>"
                f"<td>{ESC(exp.domain)}</td><td>{exp.kind}</td><td>{status}</td>"
                f"<td>{action}</td></tr>"
            )
    table = ("<table><tr><th>实验</th><th>领域</th><th>类型</th><th>预登记状态</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else "<p class='muted'>没有实验配置。</p>"
    flash = f"<div class='flash'>{ESC(query['msg'][0])}</div>" if query.get("msg") else ""
    return layout("预登记", f"<h1>预登记（跑主实验前冻结口径）</h1>{flash}"
                f"<div class='card'>{table}"
                "<p class='muted'>协议 §7.5：δ 与最小实际意义差异须在查看测试集结果前由决策人确认。"
                "登记内容：拐点阈值 δ、质量门槛、抽检比例、判分口径、配置与数据指纹哈希。"
                "改动留历史（version 自增），不覆盖旧版。</p></div>")


def page_prereg_edit(app: WebApp, query: dict) -> bytes:
    name = query.get("exp", [""])[0]
    toml_path = app.root / "configs" / "experiments" / f"{name}.toml"
    if not toml_path.exists():
        return layout("未找到", f"<h1>未找到实验 {ESC(name)}</h1>")
    exp = load_experiment(toml_path)
    reg = load_prereg(app.root, name) or {}
    v = lambda key, default: reg.get(key, default)  # noqa: E731
    body = f"""<h1>预登记：{ESC(name)}</h1>
<div class='card'>
<p class='muted'>配置快照：k={exp.ks or '—'} / N={exp.ns or '—'}，检索器 {ESC(', '.join(exp.retrievers))}，
重复 {exp.repeats}（种子 {exp.seeds[:exp.repeats]}），池 {exp.pool_strategy}，
测试集 <span class='mono'>{ESC(str(exp.testset))}</span>（数据指纹在保存时计算并入档）。</p>
<form method='post' action='/prereg/save'>
<input type='hidden' name='exp' value='{ESC(name)}'>
<div style='margin:8px 0'><label>决策人 <input type='text' name='actor'
 value='{ESC(str(v('actor', '')))}' placeholder='姓名'></label></div>
<div style='margin:8px 0'><label>拐点阈值 δ（连续两档边际增益均低于它即拐点）<br>
<input type='text' name='delta' value='{v('delta', exp.knee_threshold)}' style='width:90px' class='mono'>
</label><span class='muted'>当前配置 knee_threshold={exp.knee_threshold}</span></div>
<div style='margin:8px 0'><label>质量门槛（语义等价率 ≥；留空=不设，只报曲线不宣称最优）<br>
<input type='text' name='quality_gate' value='{v('quality_gate', '') or ''}' style='width:90px' class='mono'
 placeholder='如 0.85'></label></div>
<div style='margin:8px 0'><label>人工抽检比例 <input type='text' name='review_rate'
 value='{v('review_rate', 0.1)}' style='width:90px' class='mono'></label></div>
<div style='margin:8px 0'><label>判分口径（冻结文本）<br>
<textarea name='scoring_protocol' style='width:70%;height:110px' class='mono'
>{ESC(str(v('scoring_protocol', '语义等价以人工盲评为准（A/B 双人独立 + 分歧 C 裁决）；LLM judge 仅辅助信号')))}</textarea></label></div>
<div style='margin:8px 0'><label>变更理由（首次登记可留空；改已登记内容必填）<br>
<input type='text' name='reason' value='' style='width:60%'></label></div>
<button>冻结登记</button>
</form></div>
<p><a href='/prereg'>← 返回预登记列表</a></p>"""
    return layout(f"预登记 {name}", body)


def post_prereg_save(app: WebApp, form: dict) -> str:
    name = form["exp"][0]
    exp = load_experiment(app.root / "configs" / "experiments" / f"{name}.toml")
    actor = form.get("actor", [""])[0].strip()
    if not actor:
        return "决策人必填（协议 §7.5）"
    existing = load_prereg(app.root, name)
    if existing and not form.get("reason", [""])[0].strip():
        return "改动已登记内容必须填写变更理由"
    try:
        delta = float(form["delta"][0])
    except (ValueError, IndexError, KeyError):
        return "δ 必须是数字"
    qg_text = form.get("quality_gate", [""])[0].strip()
    try:
        quality_gate = float(qg_text) if qg_text else None
        review_rate = float(form.get("review_rate", ["0.1"])[0])
    except ValueError:
        return "质量门槛/抽检比例必须是数字"
    from ..config import file_md5
    dataset_md5 = file_md5(app.root / exp.testset) if (app.root / exp.testset).exists() else ""
    payload = create_prereg(
        app.root, exp, dataset_md5=dataset_md5, delta=delta,
        quality_gate=quality_gate, review_rate=review_rate,
        scoring_protocol=form.get("scoring_protocol", [""])[0],
        actor=actor, reason=form.get("reason", [""])[0],
    )
    return f"已冻结登记 v{payload['version']}（hash {payload['prereg_hash']}）"
