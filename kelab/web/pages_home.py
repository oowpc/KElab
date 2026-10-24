"""向导首页：把平台的逻辑用一页讲清楚——数据流图、核心概念、三步上手、页面导航。

定位：新用户（或几周没碰的自己）打开看板第一页就能看懂平台怎么工作。
"""
from __future__ import annotations

from .app import WebApp
from .ui import ESC, layout

_FLOW_CSS = """
.flow { display: flex; flex-direction: column; gap: 0; margin: 12px 0; }
.flow .stage { display: flex; align-items: stretch; gap: 12px; }
.flow .box { background: var(--page); border: 1px solid var(--baseline); border-radius: 8px;
  padding: 10px 14px; flex: 1; }
.flow .box b { color: var(--blue); }
.flow .arrow { text-align: center; color: var(--muted); line-height: 1.2; padding: 2px 0; }
.flow .side { flex: 1.2; color: var(--ink2); font-size: 12.5px; padding: 10px 4px; }
.concept td:first-child { white-space: nowrap; font-weight: 600; color: var(--ink); }
.steps { counter-reset: step; }
.steps li { margin: 8px 0; }
"""


def _flow() -> str:
    def stage(box: str, side: str) -> str:
        return f"<div class='stage'><div class='box'>{box}</div><div class='side'>{side}</div></div>"

    arrow = "<div class='arrow'>│<br>▼</div>"
    return f"""
<div class='flow'>
{stage("<b>① 数据准备</b>　标注 xlsx → testset.jsonl（测试集）+ pool.jsonl（候选池）",
       "gold KE 全量过语法/静态校验；<b>泄露检查</b>不过就停工（测试集与池零交集）")}
{arrow}
{stage("<b>② 实验配置</b>　选因子网格：k 档（示例数）或 N 档（池规模）× 检索器 × 3 次重复",
       "k 实验回答「给几条示例」，N 实验回答「从多大的池子挑」。配置哈希=run_id，改配置=新 run")}
{arrow}
{stage("<b>③ run</b>　逐条：池截取 → 检索 top-k → 拼 prompt → LLM 生成 → 落 SQLite",
       "断点续跑只补缺口；单条失败记 error 不中断；prompt 全文逐字落库可审计")}
{arrow}
{stage("<b>④ 判分</b>　validation（语法解析/light 静态检查）+ judgment（完全匹配/覆盖度/LLM judge）",
       "validation 是确定性的「合不合法」；judgment 是「好不好」，语义等价率由 judge 模型打分")}
{arrow}
{stage("<b>⑤ 聚合报告（自动）</b>　3 次重复均值±std → 曲线、拐点 k*/N*、H1–H4 判定、失败分类",
       "打开 run 页就自动重画图表；std&gt;3pp 标 ⚠️；报告按两份实验方案的验收口径写")}
{arrow}
{stage("<b>⑥ 人工抽检</b>　分层抽 ~10% 在线判定 → 人工 vs judge 一致率",
       "judge 和生成是同一家模型，抽检是对冲同源偏差的校准手段")}
</div>"""


def _concepts() -> str:
    rows = [
        ("run（一次运行）", "同一实验配置下所有条件的完整执行。run_id = 实验名 + 配置哈希，"
         "所以改配置自动开新 run，旧数据不动——这也是跨 run 对比的基础"),
        ("条件（condition）", "网格上的一个点：如 k=3 + bm25。报告里每行就是一个条件的聚合"),
        ("重复（rep）", "每个条件跑 3 遍，只有种子不同；均值±std 的 std 按这 3 遍算，>3pp 说明不稳定"),
        ("候选池（pool）", "few-shot 示例的取材范围。冒烟用 LOO（每题池=其余 19 条）；"
         "正式实验用 fixed（独立 pool.jsonl，与测试集零交集）"),
        ("嵌套池", "N 档增大只加不换（5⊂10⊂19），保证「池变大」是唯一变量"),
        ("k 实验 / N 实验", "前篇扫示例数 k（池固定）；本篇扫池规模 N（k=3 固定）；"
         "两篇共用测试集和判分，结果在 (k,N) 网格页缝合"),
        ("validation / judgment", "校验=程序判定合不合法（语法/light）；判分=LLM+人工判定好不好"
         "（语义等价率、完全匹配、覆盖度）"),
        ("light 检查", "冒烟期的轻量静态检查：算子闭词表、参数个数、变量绑定、单 output。"
         "完整类型检查属后续阶段"),
        ("oracle 检索", "用 gold 答案的算子集挑示例——质量上界。oracle 与实测的差距说明瓶颈在检索器还是在池"),
        ("断点续跑", "重跑同配置只补缺的（条件,题,重复）。被中断的 run 自动标记，随时可续"),
        ("revalidate / rejudge", "前者零 token 重跑确定性校验（校验器改进后回溯）；"
         "后者花 token 重跑 LLM judge（judge prompt/模型改后回溯）"),
        ("试跑（--limit）", "只跑前 N 题。改完 prompt 先小跑看效果，满意再全量——省钱工作流的核心"),
    ]
    return "".join(f"<tr><td>{ESC(k)}</td><td>{v}</td></tr>" for k, v in rows)


def _steps() -> str:
    return """
<ol class='steps'>
<li><b>第一次跑通</b>：<a href='/prepare'>数据准备</a>导入题库（试导入预览不写文件）→
<a href='/launch'>启动实验</a>看调用量估算 → 用「试跑题数」先跑 5 题。</li>
<li><b>日常迭代</b>：<a href='/prompts'>Prompt 页</a>改模板（预览不花 token）→ 试跑 →
<a href='/compare'>对比页</a>看新旧 run 的 Δ → 满意再全量。</li>
<li><b>看结果</b>：<a href='/runs'>运行列表</a>进 run 详情（指标表+自动曲线+消耗）→
失败分类看错题 → 抽检队列判 10% → 拿 report.md 和失败 CSV 去写报告。</li>
</ol>"""


def _nav_cards() -> str:
    cards = [
        ("/runs", "运行列表", "所有 run 的进度/状态/备注，运行中自动刷新"),
        ("/launch", "启动实验", "调用量估算 + 试跑 + 批量队列（会花 token 的入口都在这）"),
        ("/compare", "跨 run 对比", "两版配置的效果 diff：指标 Δ 表 + 叠加曲线"),
        ("/domains", "汇总对比", "多 run 总览表 + 同轴叠加曲线 + 分域小多图（跨实验看全局）"),
        ("/grid", "(k,N) 网格", "k 实验 × N 实验的二维缝合，方案的标志性产出"),
        ("/prepare", "数据准备", "xlsx 导入、列映射、切分测试集/池、泄露检查"),
        ("/prompts", "Prompt", "模板编辑/预览/版本历史——prompt 工程全在这"),
        ("/configs", "配置", "领域与实验参数表单（模型、网格、阈值、judge）"),
    ]
    return "".join(
        f"<div class='card' style='margin-bottom:8px'><a href='{href}'><b>{ESC(t)}</b></a>"
        f"<br><span class='muted'>{ESC(d)}</span></div>"
        for href, t, d in cards
    )


def page_home(app: WebApp) -> bytes:
    from ..doctor import run_checks

    checks = run_checks(app.root)
    failed = [c for c in checks if not c.ok]
    if failed:
        items = "".join(
            f"<li><b>{ESC(c.name)}</b>：{ESC(c.detail)}"
            + (f"<br><span class='muted'>修复：{ESC(c.fix)}</span>" if c.fix else "")
            + "</li>"
            for c in failed
        )
        doctor_html = (
            f"<div class='card' style='border-color:var(--crit)'>"
            f"<b>环境自检：{len(failed)} 项未通过</b>（新手请先处理这些再开跑）"
            f"<ul>{items}</ul>"
            f"<span class='muted'>完整自检：终端执行 <code>python -m kelab.cli doctor</code></span></div>"
        )
    else:
        doctor_html = ""

    # 当前状态条：运行中 / 待抽检 / 未预登记（一屏看清「现在该干嘛」）
    runs = app.list_runs()
    running = sum(1 for r in runs if r["status"] == "running")
    pending_review = 0
    for r in runs:
        if r["status"] != "done":
            continue
        try:
            pending_review += app.review_queue_count(r["run_id"])
        except Exception:
            pass
    n_unprereg = 0
    try:
        from ..prereg import load_prereg
        from ..config import load_experiment as _load_exp
        for toml in app.experiments_dir.glob("*.toml"):
            try:
                if load_prereg(app.root, _load_exp(toml).name) is None:
                    n_unprereg += 1
            except Exception:
                continue
    except Exception:
        pass
    status_html = f"""<div class='statstrip'>
<a href='/runs' style='color:inherit'><div class='stat'><b>{running}</b><span>运行中 →</span></div></a>
<a href='/runs' style='color:inherit'><div class='stat'><b>{pending_review}</b><span>待抽检条目 →</span></div></a>
<a href='/prereg' style='color:inherit'><div class='stat'><b>{n_unprereg}</b><span>未预登记实验 →</span></div></a>
</div>
<div class='cta-row'>
<a class='btn' href='/prepare'>准备数据 →</a>
<a class='btn' href='/launch'>启动实验 →</a>
<a class='btn ghost' href='/runs'>看结果 →</a>
</div>"""
    # 系统资源卡（CPU/内存/GPU，3 秒轮询；psutil 可选，stdlib 兜底）
    from .sysmon import SYSMON_JS, render_card, sample

    sys_card = (f"<div class='card'><b>系统状态</b>"
                f"<div data-sys-host style='margin-top:8px'>{render_card(sample(str(app.root)))}</div>"
                f"</div>")
    body = f"""<h1>kelab 实验平台</h1>
{doctor_html}
{status_html}
{sys_card}
<div class='card'>这个平台回答两个问题：<b>few-shot 给几条 gold KE 示例（k）？</b>
<b>从多大的候选池里挑（N）？</b>做法是固定其余一切（prompt 模板、模型、温度、判分），
只动一个因子，看语义等价率怎么变。</div>
<style>{_FLOW_CSS}</style>
<details class='card'><summary><b>数据流图与核心概念</b>（第一次用建议读一遍）</summary>
<h2>数据流（每步点链接可去对应页面）</h2>
{_flow()}
<h2>核心概念（只看这些就够）</h2>
<table class='concept'>{_concepts()}</table>
<h2>三步上手</h2>
{_steps()}
<h2>页面导航</h2>
<div class='grid2'>{_nav_cards()}</div>
</details>
"""
    return layout("工作流向导", body, SYSMON_JS, active="home")
