"""数据准备页：数据源选择（xlsx/jsonl/Conic10K 记录目录）、列映射、试导入预览、
正式导入、测试集/池切分、manifest 与泄露报告链接。

与 CLI prepare 同一条数据路径：pack.adapter.load → 去重 → adapter.validate_gold
+ task 算子 → 切分 → 泄露检查 → manifest（跨分区题族 fail-fast）。
"""
from __future__ import annotations

from pathlib import Path

from .app import WebApp
from .ui import ESC, layout

# ---------------------------------------------------------------- 数据准备（prepare）


def _default_col_map() -> dict[str, str]:
    from ..datasets import DEFAULT_COL_MAP
    return dict(DEFAULT_COL_MAP)


def _domain_col_map(app: WebApp, domain_name: str) -> dict[str, str]:
    """领域 [xlsx] 节的列映射（无则内置默认；目录包读 domain.toml）。"""
    import tomllib

    base = app.root / "configs" / "domains" / domain_name
    path = base / "domain.toml" if base.is_dir() else base.with_suffix(".toml")
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


def _source_candidates(app: WebApp) -> list[str]:
    """文件选择 datalist：项目内 + data/uploads + Desktop 里的可用数据源
    （含 Conic10K train/dev/test.json 与记录目录）。"""
    found: list[str] = []
    for pat in ("*.xlsx", "*.jsonl", "*.json"):
        found += [p.name for p in app.root.glob(pat) if p.is_file()]
    for base_name in ("data",):
        data = app.root / base_name
        if data.is_dir():
            for pat in ("**/*.xlsx", "**/*.jsonl", "**/*.json"):
                for p in data.glob(pat):
                    if p.is_file() and not p.name.startswith("_"):
                        found.append(str(p.relative_to(app.root)))
            for p in data.glob("**/"):
                if p.is_dir() and any(p.glob("*.json")) and not any(p.glob("*.jsonl")):
                    found.append(str(p.relative_to(app.root)) + "/")
    # 外部常见位置（Conic10K 官方 train/dev/test.json 常躺在 Downloads/Desktop 项目目录里）
    import itertools

    home = Path.home()
    for base in (home / "Desktop", home / "Downloads"):
        if not base.is_dir():
            continue
        try:
            for p in itertools.islice(
                    base.glob("*/*/train.json"), 5):  # 项目目录/数据目录/train.json
                found.append(str(p))
            for p in itertools.islice(base.glob("*/train.json"), 5):
                found.append(str(p))
            for p in itertools.islice(base.glob("**/*.xlsx"), 8):
                if "ke" in p.name.lower() or "标注" in p.name or "conic" in p.name.lower():
                    found.append(str(p))
        except OSError:
            continue
    seen: list[str] = []
    for f in found:
        if f not in seen:
            seen.append(f)
    return seen[:80]


def page_prepare(app: WebApp, query: dict) -> bytes:
    from .pages_annotate import _domain_options

    domains = _domain_options(app)
    options = "".join(f"<option>{ESC(d)}</option>" for d in domains)
    datalist = "".join(f"<option value='{ESC(c)}'>" for c in _source_candidates(app))
    # 列映射 placeholder 读当前默认领域的 [xlsx] 节（不再写死「李天音-KE」）
    cm = _domain_col_map(app, domains[0]) if domains else _default_col_map()
    picked = query.get("picked", [""])[0]  # 上传后回填
    flash = f"<div class='flash'>{ESC(query['msg'][0])}</div>" if query.get("msg") else ""
    body = f"""<h1>数据准备（数据源 → 测试集 + 候选池）</h1>{flash}
<div class='card'>
<form method='post' action='/upload' enctype='multipart/form-data' style='margin-bottom:10px'>
<label class='muted'>本地文件不在这台机器的项目目录？直接上传：
<input type='file' name='file' required aria-label='上传数据源文件'
 style='max-width:260px'></label>
<button class='ghost'>上传</button>
<span class='muted'>≤60MB，落 data/uploads/</span>
</form>
<form method='post' action='/prepare'>
<div style='margin:6px 0'><label>领域 <select name='domain'>{options}</select></label>
<label style='margin-left:16px'>数据源 <input type='text' name='xlsx' list='srcfiles' id='srcinput'
  style='width:44%' class='mono' placeholder='xlsx / jsonl / Conic10K 记录目录' required
  value='{ESC(picked)}'
  aria-label='数据源路径'></label></div>
<datalist id='srcfiles'>{datalist}</datalist>
<p class='muted' style='margin:2px 0 8px'>格式由领域适配器解释（conic：xlsx/jsonl；
conic10k：train/dev/test.json、记录目录、jsonl）。外部数据可填绝对路径；
新格式在 kelab/adapters/ 注册后在领域 [pack] 节声明即可。</p>
<div id='colmap'>
<div style='margin:6px 0' class='muted'>列映射（仅 xlsx 源；留空用领域 [xlsx] 节或内置默认）：</div>
<div class='filters'>
<label>item_id <input type='text' name='cm_item_id' placeholder='{ESC(cm["item_id"])}' style='width:90px'></label>
<label>question <input type='text' name='cm_question' placeholder='{ESC(cm["question"])}' style='width:90px'></label>
<label>gold_ke <input type='text' name='cm_gold_ke' placeholder='{ESC(cm["gold_ke"])}' style='width:110px'></label>
<label>source_id <input type='text' name='cm_source_id' placeholder='{ESC(cm["source_id"])}' style='width:90px'></label>
<label>dataset <input type='text' name='cm_dataset' placeholder='{ESC(cm["dataset"])}' style='width:100px'></label>
<label>note <input type='text' name='cm_note' placeholder='{ESC(cm["note"])}' style='width:70px'></label>
</div></div>
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
    # 非 xlsx 源（json/jsonl/目录）没有列映射概念——整块隐藏，免误导。
    # 注意是独立静态字符串：放在 f-string 里花括号会被当插值
    body += """<script>
(function () {
  const input = document.getElementById('srcinput');
  const block = document.getElementById('colmap');
  function toggle() {
    const v = input.value.trim().toLowerCase();
    block.style.display = v.endsWith('.xlsx') || v.endsWith('.xlsm') || v === '' ? '' : 'none';
  }
  input.addEventListener('input', toggle); toggle();
})();
</script>"""
    return layout("数据准备", body, active="prepare")


def post_prepare(app: WebApp, form: dict) -> bytes:
    """试导入预览 / 正式导入。适配器/gold 校验/泄露/跨族错误直接展示。"""
    from .. import data_pipeline, datasets
    from ..cli import _validate_and_attach
    from ..packs import load_pack

    domain_name = form["domain"][0]
    col_map = _default_col_map()
    for field in ("item_id", "question", "gold_ke", "source_id", "dataset", "note"):
        value = form.get(f"cm_{field}", [""])[0].strip()
        if value:
            col_map[field] = value
    source = Path(form["xlsx"][0])
    if not source.is_absolute():
        source = app.root / source
    split_text = form.get("split", [""])[0].strip()
    split = int(split_text) if split_text else None
    dev_text = form.get("dev", [""])[0].strip()
    dev_size = int(dev_text) if dev_text else 0
    split_seed = int(form.get("split_seed", ["42"])[0] or 42)
    action = form.get("action", ["preview"])[0]

    body = [f"<h1>数据准备：{ESC(domain_name)}</h1>",
            "<p><a href='/prepare'>← 修改参数重新来</a></p>"]
    try:
        pack = load_pack(domain_name, root=app.root)
    except Exception as exc:
        body.append(f"<div class='card'>领域包加载失败：{ESC(str(exc))}</div>")
        return layout("数据准备", "".join(body), active="prepare")
    try:
        items = pack.adapter.load(source, col_map=col_map)
    except Exception as exc:
        body.append(f"<div class='card'>导入失败：{ESC(str(exc))}"
                    f"<br><span class='muted'>多半是列映射不对（xlsx）或源格式与适配器不符——"
                    f"试导入预览可以先看再调。</span></div>")
        return layout("数据准备", "".join(body), active="prepare")
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
        from .. import config_io

        base = app.root / "configs" / "domains" / domain_name
        domain_toml = base / "domain.toml" if base.is_dir() else base.with_suffix(".toml")
        try:
            config_io.save_config_toml(
                domain_toml, {"xlsx": col_map}, app.root / "archive" / "configs",
                kind="domain", header="由 kelab 看板保存；原文件备份于 archive/configs/")
            body.append(f"<div class='card'>列映射已保存到 {ESC(domain_toml.name)} 的 [xlsx] 节 ✓</div>")
        except Exception as exc:
            body.append(f"<div class='card'>列映射保存失败（不影响导入）：{ESC(str(exc))}</div>")

    # gold 校验（adapter.validate_gold + task 算子；失败=有非法 gold，列出并中止）
    gold_error = None
    try:
        _validate_and_attach(items, pack)
    except Exception as exc:
        gold_error = str(exc)

    sample = "".join(
        f"<tr><td class='mono'>{ESC(it.item_id)}</td><td>{ESC(it.question[:60])}</td>"
        f"<td class='mono'>{ESC(it.gold_ke[:80])}</td>"
        f"<td class='num'>{len(it.gold_operators)}</td></tr>"
        for it in items[:3]
    )
    body.append(
        f"<div class='card'><b>导入 {len(items)} 题</b>（适配器 {ESC(pack.adapter.name)}）"
        f"<table><tr><th>item_id</th><th>题干</th><th>gold KE</th><th>算子数</th></tr>"
        f"{sample}</table>"
        f"<span class='muted'>仅预览前 3 条，共 {len(items)} 条</span></div>"
    )
    if gold_error:
        body.append(f"<div class='card'><b style='color:var(--crit)'>gold 校验失败</b>"
                    f"<pre class='mono'>{ESC(gold_error[:2000])}</pre>"
                    f"<span class='muted'>gold 必须全部合法（实验口径）。"
                    f"待标注的候选（gold 为空）请走 <a href='/annotate'>gold 复核队列</a>。</span></div>")
        return layout("数据准备", "".join(body), active="prepare")
    body.append(f"<div class='card'>gold 全部通过适配器规则校验 ✓（grammar md5 "
                f"<span class='mono'>{ESC(pack.grammar_md5)}</span>）</div>")

    if split:
        testset, dev, pool = datasets.split_dataset3(items, split, dev_size=dev_size,
                                                     seed=split_seed)
    else:
        testset, dev, pool = items, [], None

    partitions = {"testset": testset}
    if pool is not None:
        partitions["pool"] = pool
    if dev:
        partitions["dev"] = dev
    manifest = data_pipeline.build_manifest(
        partitions, seed=split_seed if split else None,
        source=str(source), adapter=pack.adapter)
    cross = manifest["cross_partition_families"]
    if cross:
        body.append(
            f"<div class='card'><b style='color:var(--crit)'>跨分区题族 fail-fast</b>："
            f"近重复题落在不同分区 {ESC(str(cross[:10]))}——协议口径这是泄露通道，已中止。"
            f"<br><span class='muted'>调整切分/移除近重复后再导；确认无误可用 CLI "
            f"--allow-cross-family 显式放行。</span></div>")
        return layout("数据准备", "".join(body), active="prepare")

    if action == "preview":
        leak_note = ""
        if pool is not None:
            report = datasets.check_leakage(testset, pool, "fixed")
            leak_note = "✓ 零交集" if report["passed"] else f"✗ {len(report['hits'])} 处泄露"
            body.append(f"<div class='card'>切分预览：测试集 {len(testset)} 题 / 池 {len(pool)} 条"
                        f"（seed {split_seed}），泄露检查 {leak_note}；"
                        f"题族 {manifest['family_count']} 个，无跨分区族 ✓</div>")
        body.append("<div class='card'><b>这是预览，未写任何文件。</b>"
                    "确认无误后回上一页点「正式导入」。</div>")
        return layout("数据准备", "".join(body), active="prepare")

    # commit：写 testset.jsonl（+ pool.jsonl）+ 泄露报告 + manifest
    out_dir = app.root / "data" / "derived"
    out_dir.mkdir(parents=True, exist_ok=True)
    testset_path = out_dir / f"{source.stem}.jsonl"
    datasets.write_jsonl(testset, testset_path)
    data_pipeline.write_manifest(manifest, out_dir / "_manifest.json")
    lines = [f"测试集 {len(testset)} 题 → <span class='mono'>{ESC(str(testset_path))}</span>",
             "<a href='/derived/_manifest.json'>manifest（分区指纹+题族）</a>"]
    if pool is not None:
        pool_path = out_dir / f"{source.stem}_pool.jsonl"
        datasets.write_jsonl(pool, pool_path)
        if dev:
            dev_path = out_dir / f"{source.stem}_dev.jsonl"
            datasets.write_jsonl(dev, dev_path)
            lines.append(f"开发集 {len(dev)} 题 → <span class='mono'>{ESC(str(dev_path))}</span>"
                         "（流程调试/判分校准用，不计主结果）")
        report = datasets.check_leakage(testset, pool, "fixed")
        datasets.save_leakage_report(report, out_dir / "_leakage_report.json")
        lines.append(f"候选池 {len(pool)} 条 → <span class='mono'>{ESC(str(pool_path))}</span>")
        lines.append(
            f"泄露检查：{'<b style=color:var(--good)>通过 ✓</b>' if report['passed'] else '<b style=color:var(--crit)>未通过 ✗（不得进入实验）</b>'}"
            f"　<a href='/derived/_leakage_report.json'>泄露报告</a>"
        )
    body.append(f"<div class='card'>{'<br>'.join(lines)}</div>")
    body.append("<div class='card'>下一步：到「配置」把实验的 testset（和 pool）指向新文件，"
                "然后到「启动实验」开跑。"
                "<div class='cta-row' style='margin-bottom:0'>"
                "<a class='btn ghost' href='/annotate'>去标注/复核 →</a>"
                "<a class='btn' href='/launch'>去启动实验 →</a></div></div>")
    return layout("数据准备", "".join(body), active="prepare")
