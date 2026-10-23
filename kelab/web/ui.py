"""看板 UI 组件：布局、样式、进度条、chip、表单小件。不含业务逻辑。"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from ..store import Store

ESC = html.escape

PAGE_SIZE = 50

STYLE = """
/* 参考 Apple HIG：浅灰页面底 + 白色大圆角卡片 + 柔和投影 + 毛玻璃吸顶导航 */
:root {
  --surface: #ffffff; --page: #f5f5f7; --ink: #1d1d1f; --ink2: #6e6e73;
  --muted: #86868b; --grid: #e8e8ed; --baseline: #d2d2d7;
  --blue: #0071e3; --orange: #eb6834; --aqua: #1baf7a;
  --good: #0ca30c; --warn: #b8860b; --crit: #d03b3b;
  --shadow: 0 2px 12px rgba(0,0,0,.06);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.6 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased; }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
header { position: sticky; top: 0; z-index: 10;
  background: rgba(255,255,255,.82); backdrop-filter: saturate(180%) blur(12px);
  -webkit-backdrop-filter: saturate(180%) blur(12px);
  border-bottom: 1px solid rgba(0,0,0,.06);
  padding: 12px 22px; display: flex; gap: 18px; align-items: baseline; }
header .brand { font-weight: 700; letter-spacing: -0.01em; }
header nav a { color: var(--ink2); margin-right: 14px; font-size: 13.5px; }
header nav a:hover { color: var(--ink); text-decoration: none; }
main { padding: 26px 22px 70px; max-width: 1240px; margin: 0 auto; }
h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; margin: 6px 0 18px; }
h2 { font-size: 17px; font-weight: 600; letter-spacing: -0.01em; margin: 30px 0 12px; }
.card { background: var(--surface); border: none; border-radius: 16px;
  box-shadow: var(--shadow); padding: 18px 20px; margin-bottom: 16px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { border-bottom: 1px solid var(--grid); padding: 9px 10px;
  text-align: left; vertical-align: top; }
th { color: var(--ink2); font-weight: 600; font-size: 12px; letter-spacing: 0.02em;
  white-space: nowrap; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.mono { font-family: ui-monospace, "SF Mono", Consolas, "Cascadia Mono", monospace;
  font-size: 12.5px; }
.muted { color: var(--muted); }
.chip { display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 550; background: var(--page); color: var(--ink2); }
.chip.ok { color: var(--good); background: rgba(12,163,12,.10); }
.chip.err { color: var(--crit); background: rgba(208,59,59,.10); }
.chip.warn { color: var(--warn); background: rgba(250,178,25,.16); }
.progress { background: var(--grid); border-radius: 999px; height: 8px; min-width: 120px; }
.progress > div { background: var(--blue); height: 8px; border-radius: 999px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.ke-box { background: var(--page); border: none; border-radius: 10px;
  padding: 10px 12px; white-space: pre-wrap; word-break: break-all;
  max-height: 220px; overflow: auto; }
details summary { cursor: pointer; color: var(--blue); font-weight: 500; }
form.inline { display: inline; }
input[type=text], input[type=password], select { padding: 7px 10px;
  border: 1px solid var(--baseline); border-radius: 9px; background: var(--surface);
  font: inherit; transition: border-color .15s, box-shadow .15s; }
input:focus, select:focus { outline: none; border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(0,113,227,.18); }
button, .btn { padding: 7px 16px; border: none; background: var(--blue);
  color: #fff; border-radius: 999px; cursor: pointer; font: inherit;
  font-size: 13.5px; font-weight: 550; transition: background .15s; }
button:hover, .btn:hover { background: #0077ed; }
button.ghost, .btn.ghost { background: rgba(0,113,227,.10); color: var(--blue); }
button.ghost:hover { background: rgba(0,113,227,.18); }
.filters { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
img.chart { max-width: 100%; border-radius: 14px; box-shadow: var(--shadow);
  background: var(--surface); margin: 8px 0; }
.flash { padding: 12px 16px; border-radius: 12px; background: rgba(0,113,227,.08);
  margin-bottom: 14px; color: var(--ink); }
.issue { color: var(--crit); font-size: 12.5px; }
.rationale { color: var(--ink2); font-size: 12.5px; }
.gencard .head { display: flex; justify-content: space-between; gap: 10px;
  flex-wrap: wrap; align-items: center; margin-bottom: 4px; }
.gencard .meta { color: var(--muted); font-size: 12.5px; }
.gencard .label { color: var(--muted); font-size: 12px; margin: 8px 0 4px; }
/* gencard 状态色在 Apple 风卡片上用柔和徽标，不再用左边条 */
.gencard.ok { box-shadow: var(--shadow), inset 0 0 0 1.5px rgba(12,163,12,.25); }
.gencard.err { box-shadow: var(--shadow), inset 0 0 0 1.5px rgba(208,59,59,.3); }
"""

# ---------------------------------------------------------------- HTML 组装


def layout(title: str, body: str, poll_script: str = "") -> bytes:
    return f"""<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ESC(title)} · kelab</title>
<style>{STYLE}</style>
</head><body>
<header>
  <span class="brand">kelab 实验看板</span>
  <nav><a href="/">向导</a><a href="/runs">运行</a><a href="/launch">启动实验</a><a href="/annotate">标注</a><a href="/prereg">预登记</a><a href="/matrix">实验矩阵</a><a href="/compare">对比</a><a href="/domains">汇总对比</a><a href="/grid">(k,N)网格</a><a href="/prepare">数据准备</a><a href="/prompts">Prompt</a><a href="/configs">配置</a><a href="/settings">设置</a></nav>
</header>
<main>{body}</main>
{poll_script}
</body></html>""".encode("utf-8")


def chip(text: str, kind: str = "") -> str:
    cls = f"chip {kind}" if kind else "chip"
    return f'<span class="{cls}">{ESC(text)}</span>'


def verdict_chip(verdict: str | None) -> str:
    if verdict == "equivalent":
        return chip("等价", "ok")
    if verdict == "minor":
        return chip("轻微差异", "warn")
    if verdict == "different":
        return chip("不同", "err")
    return chip("—")


def flag_chip(value, ok_text: str, bad_text: str) -> str:
    if value is None:
        return chip("—")
    return chip(ok_text, "ok") if value else chip(bad_text, "err")


def progress_bar(done: int, expected: int | None, eta: str = "") -> str:
    eta_html = f" · 预计剩余 {ESC(eta)}" if eta else ""
    if not expected:
        return f'<span class="muted">{done} 条{eta_html}</span>'
    pct = min(100, round(done / expected * 100))
    return (f'<div class="progress"><div style="width:{pct}%"></div></div>'
            f'<span class="muted">{done}/{expected}（{pct}%）{eta_html}</span>')


def eta_text(store: Store, run_id: str, expected: int | None, done: int) -> str:
    """按已完成条的平均墙钟时间估剩余（含生成+judge+并发，无需配置）。"""
    if not expected or done <= 0:
        return ""
    remaining = expected - done
    if remaining <= 0:
        return ""
    info = store.run_info(run_id)
    if info is None:
        return ""
    try:
        started = datetime.fromisoformat(info["started_at"])
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    except (ValueError, TypeError):
        return ""
    if elapsed <= 0:
        return ""
    eta = elapsed / done * remaining
    if eta < 90:
        return f"约 {int(eta)} 秒"
    return f"约 {eta / 60:.0f} 分钟"


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%" if value is not None else "—"

POLL_JS = """
<script>
(async function poll() {
  try {
    const r = await fetch('/api/run/%s/progress');
    if (r.ok) {
      const d = await r.json();
      if (d.status !== 'running') { location.reload(); return; }
      document.querySelectorAll('[data-progress]').forEach(el => {
        const c = el.dataset.progress, v = d.conditions[c];
        if (v) el.innerHTML = v.html;
      });
      const overall = document.getElementById('overall-progress');
      if (overall) overall.innerHTML = d.overall_html;
    }
  } catch (e) {}
  setTimeout(poll, 3000);
})();
</script>
"""


def _input(name: str, value, label: str, width: int = 40, hint: str = "") -> str:
    hint_html = f" <span class='muted'>{ESC(hint)}</span>" if hint else ""
    return (f"<div style='margin:6px 0'><label>{ESC(label)} "
            f"<input type='text' name='{name}' value='{ESC(str(value))}' "
            f"style='width:{width}%' class='mono'></label>{hint_html}</div>")


def _checkbox(name: str, checked: bool, label: str) -> str:
    return (f"<label style='margin-right:14px'><input type='checkbox' name='{name}' "
            f"{'checked' if checked else ''}> {ESC(label)}</label>")
