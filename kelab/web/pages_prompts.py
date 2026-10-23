"""Prompt 页：模板编辑/校验、组装预览、版本历史/diff/恢复。"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ConfigError, file_md5, load_experiment
from .app import WebApp
from .ui import ESC, chip, layout

# ---------------------------------------------------------------- Prompt 编辑与预览


def _find_testset_for_domain(app: WebApp, domain_name: str) -> Path | None:
    if app.experiments_dir.is_dir():
        for toml in sorted(app.experiments_dir.glob("*.toml")):
            try:
                exp = load_experiment(toml)
            except ConfigError:
                continue
            if exp.domain == domain_name and (app.root / exp.testset).exists():
                return app.root / exp.testset
    return None


def page_prompts(app: WebApp, message: str = "") -> bytes:
    rows = []
    domains_dir = app.root / "configs" / "domains"
    if domains_dir.is_dir():
        for toml in sorted(domains_dir.glob("*.toml")):
            try:
                from ..config import load_domain
                domain = load_domain(toml)
            except ConfigError:
                continue
            tpl = Path(domain.prompt_template)
            if not tpl.is_absolute():
                tpl = app.root / tpl
            exists = tpl.exists()
            rows.append(
                f"<tr><td>{ESC(domain.name)}</td>"
                f"<td class='mono'>{ESC(str(domain.prompt_template))}</td>"
                f"<td>{chip('存在', 'ok') if exists else chip('缺失', 'err')}</td>"
                f"<td><a href='/prompts/edit?domain={ESC(domain.name)}'>编辑</a> · "
                f"<a href='/prompts/preview?domain={ESC(domain.name)}'>组装预览</a> · "
                f"<a href='/prompts/history?domain={ESC(domain.name)}'>历史版本</a></td></tr>"
            )
    table = ("<table><tr><th>领域</th><th>prompt 模板</th><th></th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else "<p class='muted'>没有领域配置。</p>"
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    return layout("Prompt", f"<h1>Prompt 模板</h1>{flash}<div class='card'>{table}"
                  "<p class='muted'>编辑保存前自动校验结构（示例区/题目标记、本体可解析），"
                  "原版本自动备份到 archive/prompts/。模板 md5 每次运行落库，改版本不影响旧 run 的可追溯性。</p></div>")


def _load_domain_by_name(app: WebApp, name: str):
    from ..config import load_domain

    path = app.root / "configs" / "domains" / f"{name}.toml"
    if not path.exists():
        raise KeyError(name)
    return load_domain(path), path


def page_prompt_edit(app: WebApp, query: dict, message: str = "") -> bytes:
    domain_name = query.get("domain", [""])[0]
    try:
        domain, _ = _load_domain_by_name(app, domain_name)
    except KeyError:
        return layout("未找到", f"<h1>未找到领域 {ESC(domain_name)}</h1>")
    tpl = Path(domain.prompt_template)
    if not tpl.is_absolute():
        tpl = app.root / tpl
    content = tpl.read_text(encoding="utf-8") if tpl.exists() else ""
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    body = f"""<h1>编辑 Prompt：{ESC(domain.name)}</h1>{flash}
<div class='card'>
<p class='muted'>文件：<span class='mono'>{ESC(str(tpl))}</span>
结构要求：需含 <code>## 示例</code> 与 <code>## 待转换题目</code> 标记；本体从 <code>**concepts**</code> /
<code>**operators**</code> 代码块解析（ontology_source={ESC(domain.ontology_source)}）。</p>
<form method='post' action='/prompts/save'>
<input type='hidden' name='domain' value='{ESC(domain.name)}'>
<textarea name='content' style='width:100%;height:60vh' class='mono'>{ESC(content)}</textarea>
<div style='margin-top:8px'><button>校验并保存</button>
<a class='btn ghost' href='/prompts/preview?domain={ESC(domain.name)}'>组装预览 →</a></div>
</form></div>"""
    return layout(f"编辑 Prompt {domain.name}", body)


def page_prompt_preview(app: WebApp, query: dict) -> bytes:
    from .. import datasets, prompts as prompt_mod, retrieve as retrieve_mod

    domain_name = query.get("domain", [""])[0]
    try:
        domain, _ = _load_domain_by_name(app, domain_name)
    except KeyError:
        return layout("未找到", f"<h1>未找到领域 {ESC(domain_name)}</h1>")

    testset_path = _find_testset_for_domain(app, domain_name)
    items = datasets.read_jsonl(testset_path) if testset_path else []
    item_id = query.get("item", [""])[0] or (items[0].item_id if items else "")
    k = int(query.get("k", ["3"])[0] or 3)
    retriever = query.get("retriever", ["bm25"])[0]

    item_opts = "".join(
        f"<option value='{ESC(it.item_id)}' {'selected' if it.item_id == item_id else ''}>"
        f"{ESC(it.item_id)}</option>"
        for it in items
    )
    retriever_opts = "".join(
        f"<option value='{r}' {'selected' if r == retriever else ''}>{r}</option>"
        for r in ("random", "bm25", "oracle")
    )
    form = (f"<div class='card'><form class='filters' method='get'>"
            f"<input type='hidden' name='domain' value='{ESC(domain_name)}'>"
            f"<select name='item'>{item_opts}</select>"
            f"k=<input type='text' name='k' value='{k}' style='width:50px'>"
            f"<select name='retriever'>{retriever_opts}</select>"
            f"<button>预览</button><span class='muted'>本地组装，不消耗 token</span></form></div>")

    body = [f"<h1>Prompt 组装预览：{ESC(domain_name)}</h1>", form]
    if not items:
        body.append("<p class='muted'>找不到该领域的测试集（先跑 prepare）。</p>")
        return layout("Prompt 预览", "".join(body))

    item = next((it for it in items if it.item_id == item_id), items[0])
    tpl = Path(domain.prompt_template)
    if not tpl.is_absolute():
        tpl = app.root / tpl
    template = tpl.read_text(encoding="utf-8")
    pool = datasets.loo_pools(items)[item.item_id]
    result = retrieve_mod.retrieve(retriever, item, pool, k, seed=101)
    assembled = prompt_mod.assemble(template, domain.question_format, result.items, item.question)

    retrieved_list = "、".join(it.item_id for it in result.items) or "（无，k=0 零样本）"
    warn = f"　<span class='chip warn'>{ESC(result.warning)}</span>" if result.warning else ""
    body.append(f"<div class='card'><b>检索示例（{retriever} top-{k}）</b>：{ESC(retrieved_list)}{warn}"
                f"<br><span class='muted'>prompt_hash <span class='mono'>{assembled.prompt_hash}</span>"
                f"　system {len(assembled.system)} 字符　user {len(assembled.user)} 字符</span></div>")
    body.append("<h2>system 消息</h2>"
                f"<div class='card'><div class='ke-box mono' style='max-height:50vh;overflow:auto'>"
                f"{ESC(assembled.system)}</div></div>")
    body.append("<h2>user 消息</h2>"
                f"<div class='card'><div class='ke-box mono'>{ESC(assembled.user)}</div></div>")
    body.append(f"<p><a class='btn ghost' href='/prompts/edit?domain={ESC(domain_name)}'>去编辑模板 →</a></p>")
    return layout("Prompt 预览", "".join(body))


def post_prompt_save(app: WebApp, form: dict) -> str:
    """校验新模板内容 → 备份旧版 → 写盘。返回提示消息（可能抛错转 500 页）。"""
    domain_name = form["domain"][0]
    content = form["content"][0].replace("\r\n", "\n")
    domain, _ = _load_domain_by_name(app, domain_name)

    problems = _validate_prompt_content(domain, content)
    if problems:
        return "校验未通过，未保存：" + "；".join(problems)

    tpl = Path(domain.prompt_template)
    if not tpl.is_absolute():
        tpl = app.root / tpl
    backup = backup_prompt(tpl, app.root / "archive" / "prompts")
    tpl.write_text(content, encoding="utf-8", newline="\n")
    msg = f"已保存 {tpl.name}（新 md5 {file_md5(tpl)[:8]}…）"
    if backup:
        msg += f"，旧版备份 {backup.name}"
    return msg


def _validate_prompt_content(domain, content: str) -> list[str]:
    """prompt 模板结构校验：标记齐全 + 本体可解析（ontology_source=prompt 时）。"""
    from .. import prompts as prompt_mod
    from ..ontology import parse_ontology_from_prompt

    problems: list[str] = []
    try:
        prompt_mod.split_template(content)
    except prompt_mod.PromptError as exc:
        problems.append(str(exc))
    if domain.ontology_source == "prompt":
        try:
            parse_ontology_from_prompt(content)  # 解析为空会抛 OntologyError
        except Exception as exc:
            problems.append(f"本体解析失败: {exc}")
    return problems


# ---------------------------------------------------------------- Prompt 版本历史


def page_prompt_history(app: WebApp, query: dict) -> bytes:
    domain_name = query.get("domain", [""])[0]
    try:
        domain, _ = _load_domain_by_name(app, domain_name)
    except KeyError:
        return layout("未找到", f"<h1>未找到领域 {ESC(domain_name)}</h1>")
    tpl = Path(domain.prompt_template)
    if not tpl.is_absolute():
        tpl = app.root / tpl

    import datetime as dt

    rows = []
    if tpl.exists():
        stat = tpl.stat()
        rows.append(
            f"<tr><td>{chip('当前', 'ok')}</td><td class='mono'>{ESC(tpl.name)}</td>"
            f"<td>{dt.datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}</td>"
            f"<td class='mono'>{file_md5(tpl)[:8]}</td><td class='muted'>（基准）</td></tr>"
        )
    archive = app.root / "archive" / "prompts"
    if archive.is_dir():
        for bak in sorted(archive.glob(tpl.name + ".*.bak"),
                          key=lambda p: p.stat().st_mtime, reverse=True):
            stat = bak.stat()
            rows.append(
                f"<tr><td>{chip('历史')}</td><td class='mono'>{ESC(bak.name)}</td>"
                f"<td>{dt.datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}</td>"
                f"<td class='mono'>{file_md5(bak)[:8]}</td>"
                f"<td><a href='/prompts/diff?domain={ESC(domain_name)}&file={ESC(bak.name)}'>"
                f"与当前 diff</a> · "
                f"<form class='inline' method='post' action='/prompts/restore'>"
                f"<input type='hidden' name='domain' value='{ESC(domain_name)}'>"
                f"<input type='hidden' name='file' value='{ESC(bak.name)}'>"
                f"<button class='ghost'>恢复此版本</button></form></td></tr>"
            )
    table = ("<table><tr><th></th><th>文件</th><th>修改时间</th><th>md5</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else "<p class='muted'>没有历史版本。</p>"
    flash = ""
    if query.get("msg", [""])[0]:
        flash = f"<div class='flash'>{ESC(query['msg'][0])}</div>"
    body = (f"<h1>Prompt 版本历史：{ESC(domain_name)}</h1>{flash}<div class='card'>{table}</div>"
            f"<p><a href='/prompts/edit?domain={ESC(domain_name)}'>← 返回编辑</a></p>")
    return layout(f"Prompt 历史 {domain_name}", body)


def page_prompt_diff(app: WebApp, query: dict) -> bytes:
    import difflib

    domain_name = query.get("domain", [""])[0]
    bak_name = query.get("file", [""])[0]
    try:
        domain, _ = _load_domain_by_name(app, domain_name)
    except KeyError:
        return layout("未找到", f"<h1>未找到领域 {ESC(domain_name)}</h1>")
    tpl = Path(domain.prompt_template)
    if not tpl.is_absolute():
        tpl = app.root / tpl
    bak = app.root / "archive" / "prompts" / Path(bak_name).name  # 防路径穿越：只取文件名
    if not bak.exists():
        return layout("未找到", f"<h1>未找到历史版本 {ESC(bak_name)}</h1>")
    old = bak.read_text(encoding="utf-8").splitlines()
    new = tpl.read_text(encoding="utf-8").splitlines() if tpl.exists() else []
    diff = list(difflib.unified_diff(
        old, new, fromfile=f"{bak.name}（历史）", tofile=f"{tpl.name}（当前）", lineterm="",
    ))
    if not diff:
        content = "<p class='muted'>两个版本内容完全一致。</p>"
    else:
        lines = []
        for line in diff:
            if line.startswith("+++") or line.startswith("---"):
                lines.append(f"<span style='color:var(--muted)'>{ESC(line)}</span>")
            elif line.startswith("+"):
                lines.append(f"<span style='color:var(--good)'>{ESC(line)}</span>")
            elif line.startswith("-"):
                lines.append(f"<span style='color:var(--crit)'>{ESC(line)}</span>")
            elif line.startswith("@@"):
                lines.append(f"<span style='color:var(--blue)'>{ESC(line)}</span>")
            else:
                lines.append(ESC(line))
        content = f"<div class='ke-box mono' style='max-height:70vh;overflow:auto'>" \
                  + "<br>".join(lines) + "</div>"
    body = (f"<h1>Prompt diff：{ESC(domain_name)}</h1><div class='card'>{content}</div>"
            f"<p><a href='/prompts/history?domain={ESC(domain_name)}'>← 返回历史</a></p>")
    return layout(f"Prompt diff {domain_name}", body)


def post_prompt_restore(app: WebApp, form: dict) -> str:
    """把历史版本恢复为当前模板（走与保存相同的校验+备份流程）。"""
    domain_name = form["domain"][0]
    bak_name = Path(form["file"][0]).name  # 防路径穿越
    domain, _ = _load_domain_by_name(app, domain_name)
    bak = app.root / "archive" / "prompts" / bak_name
    if not bak.exists():
        return f"未找到历史版本 {bak_name}"
    content = bak.read_text(encoding="utf-8")
    problems = _validate_prompt_content(domain, content)
    if problems:
        return "该历史版本未通过当前校验，未恢复：" + "；".join(problems)
    tpl = Path(domain.prompt_template)
    if not tpl.is_absolute():
        tpl = app.root / tpl
    backup_prompt(tpl, app.root / "archive" / "prompts")
    tpl.write_text(content, encoding="utf-8", newline="\n")
    return f"已恢复 {bak_name} 为当前模板（原当前版已备份）"


def backup_prompt(path: Path, archive_dir: Path) -> Path | None:
    import shutil
    from datetime import datetime

    if not path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = archive_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup)
    return backup
