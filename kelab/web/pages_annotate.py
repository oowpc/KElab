"""标注工作台：gold KE 标注从「手写」变「模型起草 + 即时校验 + 人工修正」。

流程：贴题目 → 生成草稿（当前领域 prompt + 池示例检索，花 1 次生成 token）
→ lark/light 即时校验 → 人工修正 → 保存入暂存区（error 级 issue 拒绝入库）
→ 攒够了在数据准备页切分进测试集/池。

扩题主通道是「gold 复核队列」（gold_pipeline 的 Web 入口）：
CLI 生成 _review.jsonl → 本页批量复核（改 gold/判 pass/fail）→ 冻结入库。
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import datasets, gold_pipeline, prompts as prompt_mod, retrieve as retrieve_mod, ke_validate
from ..datasets import Item
from ..llm import chat_with_usage, LLMError
from ..ontology import load_ontology
from .app import WebApp
from .ui import ESC, chip, layout

STAGING_DIR = Path("data") / "derived"


def _staging_path(app: WebApp, domain: str) -> Path:
    return app.root / STAGING_DIR / f"annotation_{domain}.jsonl"


def _load_staged(app: WebApp, domain: str) -> list[Item]:
    path = _staging_path(app, domain)
    return datasets.read_jsonl(path) if path.exists() else []


def _save_staged(app: WebApp, domain: str, items: list[Item]) -> None:
    path = _staging_path(app, domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    datasets.write_jsonl(items, path)


def _domain_options(app: WebApp) -> list[str]:
    """领域列表：<name>.toml 与 <name>/domain.toml 目录包都算。"""
    domains_dir = app.root / "configs" / "domains"
    if not domains_dir.is_dir():
        return []
    names = {p.stem for p in domains_dir.glob("*.toml")}
    names |= {p.name for p in domains_dir.iterdir()
              if p.is_dir() and (p / "domain.toml").exists()}
    names.discard("_template")
    return sorted(names)


# ---------------------------------------------------------------- gold 复核队列（gold_pipeline 的 Web 入口）

_REVIEW_PAGE = 20  # 每页 20 条（textarea 多，一页别太重）


def _review_dirs(app: WebApp) -> list[Path]:
    gold_root = app.root / "data" / "gold"
    if not gold_root.is_dir():
        return []
    return sorted(p.parent for p in gold_root.glob(f"*/{gold_pipeline.REVIEW_FILE}"))


def _review_counts(path: Path) -> tuple[int, int, int]:
    """(总数, 已有结论, 规则校验未过)。"""
    rows = gold_pipeline.load_review(path)
    done = sum(1 for r in rows if str(r.get("human_verdict") or "").strip())
    bad = sum(1 for r in rows if not r.get("auto_ok"))
    return len(rows), done, bad


def _queue_section(app: WebApp) -> str:
    dirs = _review_dirs(app)
    if not dirs:
        return ("<h2>gold 复核队列</h2><div class='card muted'>data/gold/ 下还没有复核队列。"
                "用 CLI 生成：<code>python -m kelab.cli gold --domain &lt;领域&gt; "
                "--source &lt;候选文件&gt; --out-dir data/gold/&lt;名&gt;</code></div>")
    rows = []
    for d in dirs:
        try:
            total, done, bad = _review_counts(d / gold_pipeline.REVIEW_FILE)
        except Exception:
            continue
        frozen = (d / gold_pipeline.FROZEN_FILE).exists()
        rows.append(
            f"<tr><td class='mono'>{ESC(d.name)}</td>"
            f"<td class='num'>{total}</td><td class='num'>{done}</td>"
            f"<td class='num'>{bad}</td>"
            f"<td>{chip('已冻结', 'ok') if frozen else chip('复核中', 'warn')}</td>"
            f"<td><a href='/annotate/review?dir={ESC(d.name)}'>打开 →</a></td></tr>"
        )
    return ("<h2>gold 复核队列（扩题主入口）</h2><div class='card'><table>"
            "<tr><th>队列</th><th>总数</th><th>已复核</th><th>规则未过</th><th>状态</th><th></th></tr>"
            + "".join(rows) + "</table>"
            "<p class='muted'>规则未过的条目不能直接冻结——在队列里改 gold KE 文本再标 pass。</p></div>")


def page_gold_review(app: WebApp, query: dict, message: str = "") -> bytes:
    dir_name = query.get("dir", [""])[0]
    queue_dir = app.root / "data" / "gold" / dir_name
    review_path = queue_dir / gold_pipeline.REVIEW_FILE
    if not dir_name or not review_path.exists():
        return layout("复核队列", "<h1>未找到复核队列</h1><p><a href='/annotate'>← 返回</a></p>",
                      active="annotate")
    rows = gold_pipeline.load_review(review_path)
    page = max(1, int(query.get("page", ["1"])[0] or 1))
    pages = max(1, (len(rows) + _REVIEW_PAGE - 1) // _REVIEW_PAGE)
    page = min(page, pages)
    chunk = rows[(page - 1) * _REVIEW_PAGE: page * _REVIEW_PAGE]

    def verdict_select(rid: str, current: str) -> str:
        opts = "".join(
            f"<option value='{v}' {'selected' if current == v else ''}>{t}</option>"
            for v, t in (("", "待复核"), ("pass", "pass ✓"), ("fail", "fail ✗"))
        )
        return f"<select name='verdict__{ESC(rid)}'>{opts}</select>"

    body_rows = []
    for row in chunk:
        rid = row["item_id"]
        auto = (chip("规则 ✓", "ok") if row.get("auto_ok")
                else chip("规则 ✗", "err"))
        errors = ""
        if row.get("auto_errors"):
            errors = ("<div class='issue'>" + "<br>".join(ESC(e) for e in row["auto_errors"])
                      + "</div>")
        meta = row.get("item", {}).get("metadata", {}) if isinstance(row.get("item"), dict) else {}
        meta_line = " ".join(f"{k}={v}" for k, v in meta.items()
                             if k in ("answer", "split", "source_index") and v)
        body_rows.append(
            f"<tr><td class='mono'>{ESC(rid)}<br>{auto}"
            + (f"<br><span class='muted' style='font-size:11px'>{ESC(meta_line)}</span>"
               if meta_line else "")
            + f"</td>"
            f"<td style='min-width:220px'>{ESC(row['question'])}{errors}</td>"
            f"<td style='min-width:300px'><textarea name='goldke__{ESC(rid)}' class='mono' "
            f"style='width:100%;height:88px'>{ESC(row.get('gold_ke') or '')}</textarea></td>"
            f"<td>{verdict_select(rid, str(row.get('human_verdict') or ''))}"
            f"<input type='text' name='note__{ESC(rid)}' placeholder='备注' "
            f"value='{ESC(str(row.get('human_note') or ''))}' style='width:110px'></td></tr>"
        )
    total, done, bad = _review_counts(review_path)
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    pager = " ".join(
        f"<a href='/annotate/review?dir={ESC(dir_name)}&page={p}'>{p}</a>"
        if p != page else f"<b>{p}</b>"
        for p in range(1, pages + 1)
    )
    options = "".join(
        f"<option value='{ESC(d)}' {'selected' if d == 'conic' else ''}>{ESC(d)}</option>"
        for d in _domain_options(app)
    )
    body = f"""<div class='crumbs'><a href='/annotate'>标注</a> › 复核队列</div>
<h1 class='mono' style='font-size:19px'>{ESC(dir_name)}</h1>{flash}
<div class='card'><b>{total} 条 · 已复核 {done} · 规则未过 {bad}</b>
<span class='muted'>进度 {done}/{total}；改 gold KE 直接编辑文本框，保存只写当前页</span></div>
<form method='post' action='/annotate/review_save'>
<input type='hidden' name='dir' value='{ESC(dir_name)}'>
<input type='hidden' name='page' value='{page}'>
<div class='card'><table>
<tr><th>id / 规则校验</th><th>题目</th><th>gold KE（可编辑）</th><th>结论 / 备注</th></tr>
{''.join(body_rows)}</table>
<div style='margin-top:10px'><button>保存本页</button>　<span class='muted'>{pager}</span></div>
</div></form>
<div class='card'><b>冻结</b>（规则校验通过 + 人工 pass 的条目写入 frozen.jsonl + manifest）<br>
<form method='post' action='/annotate/review_freeze' class='filters' style='margin-top:8px'
 data-confirm='冻结后条目进入正式 gold 库，继续？'>
<input type='hidden' name='dir' value='{ESC(dir_name)}'>
<label>领域 <select name='domain'>{options}</select></label>
<label>复核人 <input type='text' name='actor' required style='width:100px'></label>
<label><input type='checkbox' name='allow_unreviewed'> 允许跳过未复核条目</label>
<button>冻结</button></form></div>"""
    return layout(f"复核队列 {dir_name}", body, active="annotate")


def post_gold_review_save(app: WebApp, form: dict) -> str:
    """保存当前页的编辑（verdict/gold_ke/note），写回复核文件。"""
    dir_name = form["dir"][0]
    review_path = app.root / "data" / "gold" / dir_name / gold_pipeline.REVIEW_FILE
    if not review_path.exists():
        return "复核文件不存在"
    rows = gold_pipeline.load_review(review_path)
    edited = 0
    for row in rows:
        rid = row["item_id"]
        vkey = f"verdict__{rid}"
        if vkey not in form:
            continue  # 不在当前页
        row["human_verdict"] = form.get(vkey, [""])[0].strip().lower()
        if row["human_verdict"] not in ("", "pass", "fail"):
            row["human_verdict"] = ""
        row["human_note"] = form.get(f"note__{rid}", [""])[0][:500]
        goldke = form.get(f"goldke__{rid}", [""])[0]
        if goldke.strip() != str(row.get("gold_ke") or "").strip():
            row["gold_ke"] = goldke.strip()
            edited += 1
    with review_path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return f"已保存（本页 {edited} 条 gold KE 有编辑）"


def post_gold_review_freeze(app: WebApp, form: dict) -> str:
    from ..packs import load_pack

    dir_name = form["dir"][0]
    queue_dir = app.root / "data" / "gold" / dir_name
    review_path = queue_dir / gold_pipeline.REVIEW_FILE
    actor = form.get("actor", [""])[0].strip()
    if not actor:
        return "冻结必须填复核人"
    pack = load_pack(form["domain"][0], root=app.root)
    try:
        frozen = gold_pipeline.freeze(
            review_path, pack, queue_dir, actor=actor,
            allow_unreviewed=bool(form.get("allow_unreviewed")),
        )
    except datasets.DataError as exc:
        return f"冻结被拒绝：{exc}"
    return f"冻结完成 → {frozen}"


def _draft(app: WebApp, domain_name: str, question: str) -> tuple[str | None, str, dict | None]:
    """生成 KE 草稿：返回 (ke_text 或 None, 提示消息, validation 摘要)。"""
    domain, _ = app._load_domain_resources(domain_name)
    template = app._read_template(domain)
    ontology = load_ontology(domain.ontology_source, prompt_text=template,
                             json_path=domain.ontology_json)
    # 草稿示例来源：fixed 池 > 测试集 > 暂存区（有什么用什么，bm25 top-3）
    examples: list[Item] = []
    for candidate in (app.root / "data" / "derived" / f"{domain.name}_pool.jsonl",
                      app.root / "data" / "derived" / "testset20.jsonl",
                      _staging_path(app, domain_name)):
        if candidate.exists():
            source = datasets.read_jsonl(candidate)
            probe = Item(item_id="__draft__", question=question, gold_ke="")
            examples = retrieve_mod.retrieve("bm25", probe, source, k=3, seed=101).items
            break
    prompt = prompt_mod.assemble(template, domain.question_format, examples, question)
    try:
        client = app.resolve_client(domain.gen_model, temperature=domain.gen_temperature,
                                    timeout=domain.gen_timeout,
                                    max_retries=domain.gen_max_retries)
        result = chat_with_usage(client, prompt_mod.to_messages(prompt))
    except LLMError as exc:
        return None, f"生成失败：{exc}", None
    from nl2ke import clean_output
    ke = clean_output(result.content)
    validation = ke_validate.validate_ke(ke, domain.grammar_path, ontology)
    summary = {
        "lark_ok": validation.lark_ok,
        "errors": [i.message for i in validation.issues if i.level == "error"],
        "warnings": [i.message for i in validation.issues if i.level == "warning"],
        "tokens": (result.prompt_tokens or 0) + (result.completion_tokens or 0),
    }
    return ke, "草稿已生成", summary


def page_annotate(app: WebApp, query: dict, message: str = "") -> bytes:
    domain_name = query.get("domain", [""])[0] or (_domain_options(app) or [""])[0]
    staged = _load_staged(app, domain_name) if domain_name else []
    options = "".join(
        f"<option value='{ESC(d)}' {'selected' if d == domain_name else ''}>{ESC(d)}</option>"
        for d in _domain_options(app)
    )
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    rows = "".join(
        f"<tr><td class='mono'>{ESC(it.item_id)}</td><td>{ESC(it.question[:60])}</td>"
        f"<td class='mono'>{ESC(it.gold_ke[:80])}</td>"
        f"<td><form class='inline' method='post' action='/annotate/delete'>"
        f"<input type='hidden' name='domain' value='{ESC(domain_name)}'>"
        f"<input type='hidden' name='item_id' value='{ESC(it.item_id)}'>"
        f"<button class='ghost'>删除</button></form></td></tr>"
        for it in staged
    )
    staged_table = (
        f"<h2>暂存区（{len(staged)} 条）</h2><div class='card'>"
        + ("<table><tr><th>id</th><th>题目</th><th>gold KE</th><th></th></tr>"
           + rows + "</table>" if rows else "<p class='muted'>暂存区为空</p>")
        + "<p class='muted'>攒够后：数据准备页切分进测试集/池；或直接把这些 jsonl 当数据源导入。</p></div>"
    )
    body = f"""<h1>标注工作台</h1>{flash}
{_queue_section(app)}
<h2>单题起草</h2>
<div class='card'>
<p class='muted'>模型起草（当前领域 prompt + 池示例，1 次生成 token）→ 即时校验 → 人工修正 → 保存。
<b>error 级问题（语法失败/未知算子/未绑定）不允许入库</b>——gold 必须干净。</p>
<form method='post' action='/annotate/draft'>
<label>领域 <select name='domain'>{options}</select></label>
<div style='margin:10px 0'><textarea name='question' style='width:100%;height:90px'
 class='mono' placeholder='粘贴一道题目…' aria-label='题目'></textarea></div>
<button>生成草稿（1 次调用）</button>
</form></div>
{staged_table}"""
    return layout("标注工作台", body, active="annotate")


def page_annotate_draft(app: WebApp, form: dict) -> bytes:
    """草稿页：生成 KE 可编辑 textarea + 校验结果 + 保存/再校验。"""
    domain_name = form["domain"][0]
    question = form["question"][0].strip()
    if not question:
        return page_annotate(app, {"domain": [domain_name]}, "题目为空")
    ke, msg, summary = _draft(app, domain_name, question)
    if ke is None:
        return page_annotate(app, {"domain": [domain_name]}, msg)

    def issues_html(s):
        if not s:
            return ""
        errs = "".join(f"<span class='issue'>[error] {ESC(e)}</span><br>" for e in s["errors"])
        warns = "".join(f"<span class='muted'>[warn] {ESC(w)}</span><br>" for w in s["warnings"])
        if not errs and not warns:
            return "<span class='chip ok'>语法 ✓ light ✓</span>"
        return errs + warns

    body = f"""<h1>标注工作台 · 草稿</h1>
<div class='card'>
<div class='label'>题目</div><div class='ke-box'>{ESC(question)}</div>
<div class='label'>草稿校验（{summary['tokens']} tokens）</div>
<div style='margin:6px 0'>{issues_html(summary)}</div>
<form method='post' action='/annotate/recheck'>
<input type='hidden' name='domain' value='{ESC(domain_name)}'>
<input type='hidden' name='question' value='{ESC(question)}'>
<div class='label'>KE（可直接改）</div>
<textarea name='ke' style='width:100%;height:130px' class='mono'>{ESC(ke)}</textarea>
<div style='margin-top:8px'>
<button formaction='/annotate/recheck' class='ghost'>再校验</button>
<button formaction='/annotate/save'>保存为 gold</button>
</div></form></div>
<p><a href='/annotate?domain={ESC(domain_name)}'>← 返回工作台</a></p>"""
    return layout("标注草稿", body, active="annotate")


def _recheck(app: WebApp, domain_name: str, ke: str) -> dict:
    domain, _ = app._load_domain_resources(domain_name)
    template = app._read_template(domain)
    ontology = load_ontology(domain.ontology_source, prompt_text=template,
                             json_path=domain.ontology_json)
    validation = ke_validate.validate_ke(ke, domain.grammar_path, ontology)
    return {
        "lark_ok": validation.lark_ok,
        "errors": [i.message for i in validation.issues if i.level == "error"],
        "warnings": [i.message for i in validation.issues if i.level == "warning"],
        "tokens": 0,
    }


def post_annotate_recheck(app: WebApp, form: dict) -> bytes:
    domain_name = form["domain"][0]
    question = form["question"][0]
    ke = form["ke"][0]
    summary = _recheck(app, domain_name, ke)
    # 复用草稿页渲染（ke 用当前编辑内容，不再生成）
    errs = "".join(f"<span class='issue'>[error] {ESC(e)}</span><br>" for e in summary["errors"])
    warns = "".join(f"<span class='muted'>[warn] {ESC(w)}</span><br>" for w in summary["warnings"])
    issues_html = (errs + warns) if (errs or warns) else "<span class='chip ok'>语法 ✓ light ✓</span>"
    body = f"""<h1>标注工作台 · 草稿（已再校验）</h1>
<div class='card'>
<div class='label'>题目</div><div class='ke-box'>{ESC(question)}</div>
<div class='label'>校验结果</div><div style='margin:6px 0'>{issues_html}</div>
<form method='post' action='/annotate/recheck'>
<input type='hidden' name='domain' value='{ESC(domain_name)}'>
<input type='hidden' name='question' value='{ESC(question)}'>
<div class='label'>KE（可直接改）</div>
<textarea name='ke' style='width:100%;height:130px' class='mono'>{ESC(ke)}</textarea>
<div style='margin-top:8px'>
<button formaction='/annotate/recheck' class='ghost'>再校验</button>
<button formaction='/annotate/save'>保存为 gold</button>
</div></form></div>"""
    return layout("标注草稿", body, active="annotate")


def post_annotate_save(app: WebApp, form: dict) -> str:
    """保存到暂存区：error 级问题拒绝入库（gold 必须干净）。"""
    domain_name = form["domain"][0]
    question = form["question"][0]
    ke = form["ke"][0].strip()
    summary = _recheck(app, domain_name, ke)
    if not summary["lark_ok"] or summary["errors"]:
        return "拒绝入库：存在 error 级问题（语法/闭词表/绑定），请修正后再保存"
    staged = _load_staged(app, domain_name)
    if any(it.question == question for it in staged):
        return "暂存区已有同一题目，未重复保存"
    item = Item(
        item_id=f"ann-{domain_name}-{len(staged) + 1:03d}",
        question=question,
        gold_ke=" ".join(ke.split()),
        gold_operators=[],
        source_id="annotation-workbench",
        dataset=domain_name,
    )
    # 预提取算子（与 prepare 同口径）
    domain, _ = app._load_domain_resources(domain_name)
    template = app._read_template(domain)
    ontology = load_ontology(domain.ontology_source, prompt_text=template,
                             json_path=domain.ontology_json)
    result = ke_validate.validate_ke(item.gold_ke, domain.grammar_path, ontology)
    item.gold_operators = result.operators
    staged.append(item)
    _save_staged(app, domain_name, staged)
    return f"已入库暂存区（{item.item_id}，共 {len(staged)} 条）"


def post_annotate_delete(app: WebApp, form: dict) -> str:
    domain_name = form["domain"][0]
    item_id = form["item_id"][0]
    staged = [it for it in _load_staged(app, domain_name) if it.item_id != item_id]
    _save_staged(app, domain_name, staged)
    return f"已删除 {item_id}"
