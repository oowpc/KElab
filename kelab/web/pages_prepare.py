"""数据准备页：xlsx 列映射、试导入预览、正式导入、测试集/池切分。"""
from __future__ import annotations

from pathlib import Path

from .app import WebApp
from .ui import ESC, layout

# ---------------------------------------------------------------- 数据准备（prepare）


def _default_col_map() -> dict[str, str]:
    from ..datasets import DEFAULT_COL_MAP
    return dict(DEFAULT_COL_MAP)


def _domain_col_map(app: WebApp, domain_name: str) -> dict[str, str]:
    """领域 [xlsx] 节的列映射（无则内置默认）。"""
    import tomllib

    path = app.root / "configs" / "domains" / f"{domain_name}.toml"
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            if "xlsx" in raw:
                merged = _default_col_map()
                merged.update(raw["xlsx"])
                return merged
        except tomllib.TOMLDecodeError:
            pass
    return _default_col_map()


def page_prepare(app: WebApp, query: dict) -> bytes:
    domains_dir = app.root / "configs" / "domains"
    options = ""
    if domains_dir.is_dir():
        for toml in sorted(domains_dir.glob("*.toml")):
            options += f"<option>{ESC(toml.stem)}</option>"
    flash = f"<div class='flash'>{ESC(query['msg'][0])}</div>" if query.get("msg") else ""
    body = f"""<h1>数据准备（xlsx → 测试集 + 候选池）</h1>{flash}
<div class='card'>
<form method='post' action='/prepare'>
<div style='margin:6px 0'><label>领域 <select name='domain'>{options}</select></label>
<label style='margin-left:16px'>xlsx 路径 <input type='text' name='xlsx' style='width:40%' class='mono'
  placeholder='如 data/题目KE标注.xlsx'></label></div>
<div style='margin:6px 0' class='muted'>列映射（留空的字段用领域 [xlsx] 节或内置默认）：</div>
<div class='filters'>
<label>item_id <input type='text' name='cm_item_id' placeholder='题目' style='width:90px'></label>
<label>question <input type='text' name='cm_question' placeholder='题干' style='width:90px'></label>
<label>gold_ke <input type='text' name='cm_gold_ke' placeholder='李天音-KE' style='width:110px'></label>
<label>source_id <input type='text' name='cm_source_id' placeholder='原始ID' style='width:90px'></label>
<label>dataset <input type='text' name='cm_dataset' placeholder='所属数据集' style='width:100px'></label>
<label>note <input type='text' name='cm_note' placeholder='备注' style='width:70px'></label>
</div>
<div style='margin:10px 0' class='filters'>
<label>切分测试集题数 <input type='text' name='split' placeholder='不切分' style='width:70px'></label>
<label>开发集题数 <input type='text' name='dev' placeholder='0' style='width:56px'></label>
<label>切分种子 <input type='text' name='split_seed' value='42' style='width:60px'></label>
<span class='muted'>协议 §7.3：测试集（主结果）/ 开发集（调试与判分校准）/ 池，按题族整族切分防同构泄露</span>
</div>
<div style='margin:6px 0'><label><input type='checkbox' name='save_colmap'>
把本次列映射保存到领域配置 [xlsx] 节（下次免填）</label></div>
<button name='action' value='preview' class='ghost'>试导入预览（不写文件）</button>
<button name='action' value='commit'>正式导入</button>
</form></div>"""
    return layout("数据准备", body)


def post_prepare(app: WebApp, form: dict) -> bytes:
    """试导入预览 / 正式导入。列映射/gold 校验/泄露检查的错误直接展示。"""
    from .. import datasets
    from ..config import load_domain
    from ..ontology import load_ontology

    domain_name = form["domain"][0]
    domain = load_domain(app.root / "configs" / "domains" / f"{domain_name}.toml")
    col_map = _default_col_map()
    for field in ("item_id", "question", "gold_ke", "source_id", "dataset", "note"):
        value = form.get(f"cm_{field}", [""])[0].strip()
        if value:
            col_map[field] = value
    xlsx = Path(form["xlsx"][0])
    if not xlsx.is_absolute():
        xlsx = app.root / xlsx
    split_text = form.get("split", [""])[0].strip()
    split = int(split_text) if split_text else None
    dev_text = form.get("dev", [""])[0].strip()
    dev_size = int(dev_text) if dev_text else 0
    split_seed = int(form.get("split_seed", ["42"])[0] or 42)
    action = form.get("action", ["preview"])[0]

    body = [f"<h1>数据准备：{ESC(domain_name)}</h1>",
            f"<p><a href='/prepare'>← 修改参数重新来</a></p>"]
    try:
        items = datasets.import_xlsx(xlsx, col_map=col_map)
    except Exception as exc:
        body.append(f"<div class='card'>导入失败：{ESC(str(exc))}"
                    f"<br><span class='muted'>多半是列映射不对——试导入预览可以先看表头再调。</span></div>")
        return layout("数据准备", "".join(body))
    items, dedupe = datasets.dedupe_items(items)
    if dedupe["dropped_question_dups"] or dedupe["same_ke_groups"]:
        body.append(
            f"<div class='card'>去重：{dedupe['in']} → {dedupe['out']} 条。"
            f"题干重复去掉 {len(dedupe['dropped_question_dups'])} 条"
            f"（{ESC(str(dedupe['dropped_question_dups'])[:200])}）；"
            f"KE 结构相同 {len(dedupe['same_ke_groups'])} 组已保留"
            f"（{ESC(str(dedupe['same_ke_groups'])[:200])}，建议人工复核）</div>"
        )
    if form.get("save_colmap") == ["on"]:
        # 列映射存到领域配置 [xlsx] 节（下次该域自动生效）
        from .. import config_io

        domain_toml = app.root / "configs" / "domains" / f"{domain_name}.toml"
        try:
            config_io.save_config_toml(
                domain_toml, {"xlsx": col_map}, app.root / "archive" / "configs",
                kind="domain", header="由 kelab 看板保存；原文件备份于 archive/configs/")
            body.append(f"<div class='card'>列映射已保存到 {ESC(domain_toml.name)} 的 [xlsx] 节 ✓</div>")
        except Exception as exc:
            body.append(f"<div class='card'>列映射保存失败（不影响导入）：{ESC(str(exc))}</div>")

    # gold 校验（attach 失败=有非法 gold，列出并中止）
    tpl = Path(domain.prompt_template)
    if not tpl.is_absolute():
        tpl = app.root / tpl
    ontology = load_ontology(domain.ontology_source,
                             prompt_text=tpl.read_text(encoding="utf-8"),
                             json_path=domain.ontology_json)
    gold_error = None
    try:
        datasets.attach_gold_operators(items, domain.grammar_path, ontology)
    except Exception as exc:
        gold_error = str(exc)

    sample = "".join(
        f"<tr><td class='mono'>{ESC(it.item_id)}</td><td>{ESC(it.question[:60])}</td>"
        f"<td class='mono'>{ESC(it.gold_ke[:80])}</td>"
        f"<td class='num'>{len(it.gold_operators)}</td></tr>"
        for it in items[:3]
    )
    body.append(
        f"<div class='card'><b>导入 {len(items)} 题</b>（列映射生效）"
        f"<table><tr><th>item_id</th><th>题干</th><th>gold KE</th><th>算子数</th></tr>"
        f"{sample}</table>"
        f"<span class='muted'>仅预览前 3 条，共 {len(items)} 条</span></div>"
    )
    if gold_error:
        body.append(f"<div class='card'><b style='color:var(--crit)'>gold 校验失败</b>"
                    f"<pre class='mono'>{ESC(gold_error[:2000])}</pre>"
                    f"<span class='muted'>gold 必须全部合法（实验口径）。修正数据或语法后再导入。</span></div>")
        return layout("数据准备", "".join(body))
    body.append("<div class='card'>gold KE 全部通过 lark 解析 + light 检查 ✓</div>")

    if split:
        testset, dev, pool = datasets.split_dataset3(items, split, dev_size=dev_size,
                                                     seed=split_seed)
    else:
        testset, dev, pool = items, [], None

    if action == "preview":
        leak_note = ""
        if pool is not None:
            report = datasets.check_leakage(testset, pool, "fixed")
            leak_note = "✓ 零交集" if report["passed"] else f"✗ {len(report['hits'])} 处泄露"
            body.append(f"<div class='card'>切分预览：测试集 {len(testset)} 题 / 池 {len(pool)} 条"
                        f"（seed {split_seed}），泄露检查 {leak_note}</div>")
        body.append("<div class='card'><b>这是预览，未写任何文件。</b>"
                    "确认无误后回上一页点「正式导入」。</div>")
        return layout("数据准备", "".join(body))

    # commit：写 testset.jsonl（+ pool.jsonl）+ 泄露报告
    out_dir = app.root / "data" / "derived"
    out_dir.mkdir(parents=True, exist_ok=True)
    testset_path = out_dir / f"{xlsx.stem}.jsonl"
    datasets.write_jsonl(testset, testset_path)
    lines = [f"测试集 {len(testset)} 题 → <span class='mono'>{ESC(str(testset_path))}</span>"]
    if pool is not None:
        pool_path = out_dir / f"{xlsx.stem}_pool.jsonl"
        datasets.write_jsonl(pool, pool_path)
        if dev:
            dev_path = out_dir / f"{xlsx.stem}_dev.jsonl"
            datasets.write_jsonl(dev, dev_path)
            lines.append(f"开发集 {len(dev)} 题 → <span class='mono'>{ESC(str(dev_path))}</span>"
                         "（流程调试/判分校准用，不计主结果）")
        report = datasets.check_leakage(testset, pool, "fixed")
        datasets.save_leakage_report(report, out_dir / "_leakage_report.json")
        lines.append(f"候选池 {len(pool)} 条 → <span class='mono'>{ESC(str(pool_path))}</span>")
        lines.append(
            f"泄露检查：{'<b style=color:var(--good)>通过 ✓</b>' if report['passed'] else '<b style=color:var(--crit)>未通过 ✗（不得进入实验）</b>'}"
        )
    body.append(f"<div class='card'>{'<br>'.join(lines)}</div>")
    body.append("<div class='card'>下一步：到「配置」把实验的 testset（和 pool）指向新文件，"
                "然后到「启动实验」开跑。</div>")
    return layout("数据准备", "".join(body))
