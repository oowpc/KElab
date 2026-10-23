"""分享版报告：从 run 数据组装「导师可读」的单文件 HTML。

两种产物：
- share.html         完整独立文档（本地双击/邮件/微信都能看，图表 base64 内嵌）
- share.fragment.html  片段版（无 doctype/html/head/body，供发布为在线页面）

内容纪律：冒烟声明/MDE/口径说明必须在页面上（给导师看的东西更不能夸大）。
"""
from __future__ import annotations

import base64
import html
from datetime import datetime
from pathlib import Path

from .aggregate import aggregate_run, mde_estimate
from .config import DomainConfig, ExperimentConfig
from .interpret import interpret_run, review_agreement
from .store import Store

ESC = html.escape

CSS_TOKENS = """/* 版式：单列窄栏（导师阅读），卡片承载图表，色板语义化 */
:root {
  --bg: #f5f5f7; --surface: #ffffff; --ink: #1d1d1f; --ink2: #6e6e73;
  --muted: #86868b; --line: #e8e8ed; --accent: #0071e3;
  --good: #0ca30c; --warn: #b8860b; --crit: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161617; --surface: #232325; --ink: #f5f5f7; --ink2: #a1a1a6;
    --muted: #8e8e93; --line: #38383a; --accent: #409cff;
    --good: #30d158; --warn: #ffd60a; --crit: #ff6961;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #161617; --surface: #232325; --ink: #f5f5f7; --ink2: #a1a1a6;
  --muted: #8e8e93; --line: #38383a; --accent: #409cff;
  --good: #30d158; --warn: #ffd60a; --crit: #ff6961;
  color-scheme: dark;
}
"""

CSS_BODY = """
body { background: var(--bg); color: var(--ink);
  font: 15px/1.7 "Noto Sans SC", system-ui, -apple-system, "Segoe UI",
        "Microsoft YaHei", sans-serif;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.wrap { max-width: 780px; margin: 0 auto; padding-block: 28px; }
h1 { font-size: 27px; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 6px;
  text-wrap: balance; }
h2 { font-size: 17px; font-weight: 600; margin: 34px 0 10px; letter-spacing: -0.01em; }
h2 .no { color: var(--muted); font-weight: 500; margin-right: 8px; }
.card { background: var(--surface); border-radius: 16px;
  box-shadow: 0 2px 12px rgba(0,0,0,.07); padding: 18px 20px; margin: 14px 0; }
.sub { color: var(--ink2); font-size: 14px; }
.hero { font-size: 44px; font-weight: 700; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums; }
.hero-sub { color: var(--muted); font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 13px;
  font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line); padding: 8px 9px; text-align: left;
  vertical-align: top; }
th { color: var(--ink2); font-size: 12px; letter-spacing: 0.02em; }
tr:last-child td { border-bottom: none; }
.num { text-align: right; }
.mono { font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12px; }
.muted { color: var(--muted); }
img.chart { max-width: 100%; border-radius: 12px; display: block; margin: 8px auto; }
.note { background: rgba(0,113,227,.07); border-radius: 12px; padding: 12px 16px;
  font-size: 13.5px; }
ul.findings { list-style: none; padding: 0; margin: 10px 0 0; }
ul.findings li { padding: 7px 0; border-bottom: 1px solid var(--line); }
ul.findings li:last-child { border-bottom: none; }
.f-ev { color: var(--muted); font-size: 12.5px; }
.footer { color: var(--muted); font-size: 12.5px; margin-top: 40px;
  border-top: 1px solid var(--line); padding-top: 14px; }
@media (max-width: 480px) { .hero { font-size: 34px; } }
"""

_MARK = {"good": "✅", "warn": "⚠️", "bad": "❌", "info": "ℹ️"}


def _img(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'<img class="chart" src="data:image/png;base64,{data}" alt="{ESC(path.stem)}">'


def _pct(v: float | None, digits: int = 1) -> str:
    return f"{v * 100:.{digits}f}%" if v is not None else "—"


def build_share_html(
    store: Store,
    run_id: str,
    exp: ExperimentConfig,
    domain: DomainConfig,
    testset_size: int,
    results_dir: Path,
) -> tuple[str, str]:
    """返回 (标题, 正文 HTML)。正文不含骨架标签，由调用方组装。"""
    metrics = aggregate_run(store, run_id, exp)
    run = store.run_info(run_id)
    mde = mde_estimate(testset_size)
    reviews: dict = {}
    try:
        reviews = {r["gen_id"]: r for r in
                   store._conn.execute("SELECT * FROM reviews").fetchall()}
    except Exception:
        pass
    agree, total_reviewed = review_agreement(store, run_id, reviews)
    findings = interpret_run(metrics, exp, mde,
                             review_agree=agree, review_total=total_reviewed)

    best = None
    for cm in metrics:
        stat = cm.stats.get("semantic")
        if stat and stat.mean is not None and (best is None or stat.mean > best[1]):
            best = (cm, stat.mean)

    # 主指标大字 + 解读
    hero = ""
    if best:
        hero = f"""<div class="card">
<div class="hero">{best[1] * 100:.1f}%</div>
<div class="hero-sub">最佳语义等价率 · 条件 <span class="mono">{ESC(best[0].condition_id)}</span></div>
</div>"""

    findings_html = "".join(
        f"<li>{_MARK[f.level]} {ESC(f.text)}"
        + (f"<div class='f-ev'>依据：{ESC(f.evidence)}</div>" if f.evidence else "")
        + "</li>"
        for f in findings
    )

    # 主结果表
    table_rows = []
    for cm in metrics:
        def m(name: str) -> str:
            stat = cm.stats.get(name)
            return _pct(stat.mean) if stat and stat.mean is not None else "—"
        tok = cm.stats.get("prompt_tokens")
        table_rows.append(
            f"<tr><td class='mono'>{ESC(cm.condition_id)}</td>"
            f"<td class='num'><b>{m('semantic')}</b></td>"
            f"<td class='num'>{m('exact')}</td><td class='num'>{m('lark')}</td>"
            f"<td class='num'>{m('light')}</td>"
            f"<td class='num'>{f'{tok.mean:.0f}' if tok and tok.mean else '—'}</td></tr>"
        )

    # 图（存在才嵌）
    figs_html = ""
    for name, caption in (("k_quality" if exp.kind == "k" else "n_quality", "主曲线：语义等价率与完全匹配率"),
                          ("marginal_gain", "边际增益（拐点阈值 5pp 参考线）"),
                          ("cost_effect", "成本–效果（每点一个条件）"),
                          ("model_comparison", "模型对照"),
                          ("retrieval_coverage", "检索覆盖度机制"),
                          ("resample_scatter", "重采样散点（倒 U 检查）")):
        png = results_dir / f"{name}.png"
        if png.exists():
            figs_html += f"<div class='card'><div class='sub'>{ESC(caption)}</div>{_img(png)}</div>"

    # 失败分类统计
    failure_rows = []
    for cm in metrics:
        if cm.n_gen_error or cm.n_judge_error:
            failure_rows.append(
                f"<tr><td class='mono'>{ESC(cm.condition_id)}</td>"
                f"<td class='num'>{cm.n_gen_error}</td><td class='num'>{cm.n_judge_error}</td></tr>"
            )

    provisional = testset_size < 50
    provisional_html = ""
    if provisional:
        provisional_html = """<div class="note">⚠️ <b>口径声明</b>：本轮为 20 题管线验证（leave-one-out 池），
样本量不足，所有数字仅用于检查管线，不做统计推断；正式结论以每域 100 题主实验为准。</div>"""

    judge_note = ""
    if total_reviewed:
        judge_note = f"judge 一致性人工抽检：{agree}/{total_reviewed}（{agree / total_reviewed:.0%}）"

    kind_cn = "few-shot 示例数 k 扫描" if exp.kind == "k" else "候选池规模 N 扫描"
    title = f"{exp.name} 实验报告"
    body = f"""<div class="wrap">
<h1>{ESC(title)}</h1>
<div class="sub">{ESC(domain.name)} 领域 · {kind_cn} · 生成 {ESC(domain.gen_model)}（温度 {domain.gen_temperature}） ·
judge {ESC(domain.judge_model)} · {testset_size} 题 × {exp.repeats} 重复 ·
{datetime.now():%Y-%m-%d}</div>
{provisional_html}
<h2><span class="no">01</span>结论速读</h2>
{hero}
<div class="card"><ul class="findings">{findings_html}</ul></div>
<h2><span class="no">02</span>主结果</h2>
<div class="card"><table>
<tr><th>条件</th><th class="num">语义等价率</th><th class="num">完全匹配</th>
<th class="num">语法解析</th><th class="num">light 检查</th><th class="num">prompt tokens</th></tr>
{"".join(table_rows)}</table></div>
<h2><span class="no">03</span>图表</h2>
{figs_html}
{"<h2><span class='no'>04</span>失败与异常</h2><div class='card'><table><tr><th>条件</th><th class='num'>生成失败</th><th class='num'>judge 失败</th></tr>" + "".join(failure_rows) + "</table></div>" if failure_rows else ""}
<h2><span class="no">05</span>口径与复现</h2>
<div class="card sub">
判分口径：语义等价率 = LLM judge（equivalent=1.0 / minor=0.5 / different=0.0）{("；" + judge_note) if judge_note else ""}；
统计功效：MDE ≈ {_pct(mde)}（方案锚点 100 题≈5pp 按 1/√n 换算），小于 MDE 的差异不下结论。<br>
复现指纹（run_id <span class="mono">{ESC(run_id)}</span>）：
配置哈希 <span class="mono">{ESC(run["config_hash"] if run else "—")}</span> ·
prompt 模板 md5 <span class="mono">{ESC(run["prompt_template_md5"] or "—" if run else "—")}</span> ·
grammar md5 <span class="mono">{ESC(run["grammar_md5"] or "—" if run else "—")}</span> ·
数据指纹 <span class="mono">{ESC(run["dataset_md5"] or "—" if run else "—")}</span><br>
<span class="muted">由 kelab 实验平台自动生成；prompt 全文与判分逐条落库可审计。</span>
</div>
<div class="footer">kelab · {ESC(run_id)}</div>
</div>"""
    return title, body


def render_share(
    store: Store,
    run_id: str,
    exp: ExperimentConfig,
    domain: DomainConfig,
    testset_size: int,
    results_dir: Path,
) -> tuple[Path, Path]:
    """写出 share.html（独立文档）与 share.fragment.html（发布片段）。返回两路径。"""
    title, body = build_share_html(store, run_id, exp, domain, testset_size, results_dir)
    fonts = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family='
             'Noto+Sans+SC:wght@400;500;700&display=swap">')
    meta = '<meta name="claude:viewport-fit-sides" content="cover">'
    style = f"<style>{CSS_TOKENS}{CSS_BODY}</style>"
    fragment = f"<title>{ESC(title)}</title>\n{meta}\n{fonts}\n{style}\n{body}\n"
    standalone = (
        "<!doctype html>\n<html lang=\"zh\"><head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        f"<title>{ESC(title)}</title>\n{fonts}\n{style}\n</head><body>\n{body}\n</body></html>\n"
    )
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    standalone_path = results_dir / "share.html"
    fragment_path = results_dir / "share.fragment.html"
    standalone_path.write_text(standalone, encoding="utf-8", newline="\n")
    fragment_path.write_text(fragment, encoding="utf-8", newline="\n")
    return standalone_path, fragment_path
