"""配置编辑页：domain/experiment TOML 的表单化编辑（备份+回读校验）。"""
from __future__ import annotations

from pathlib import Path

from ..config import file_md5
from .app import WebApp
from .ui import ESC, layout, _checkbox, _input

# ---------------------------------------------------------------- 配置编辑


def page_configs(app: WebApp, message: str = "") -> bytes:
    rows = []
    for sub, label in (("domains", "领域"), ("experiments", "实验")):
        dir_path = app.root / "configs" / sub
        if not dir_path.is_dir():
            continue
        for toml in sorted(dir_path.glob("*.toml")):
            rows.append(
                f"<tr><td>{label}</td><td class='mono'>{ESC(toml.name)}</td>"
                f"<td><a href='/configs/edit?path={ESC(str(toml))}'>编辑</a></td></tr>"
            )
    table = ("<table><tr><th>类型</th><th>文件</th><th></th></tr>" + "".join(rows) + "</table>") \
        if rows else "<p class='muted'>没有配置文件。</p>"
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    return layout("配置", f"<h1>配置文件</h1>{flash}"
                  f"<p><a class='btn' href='/configs/new?type=experiment'>新建实验配置</a> "
                  f"<a class='btn ghost' href='/configs/new?type=domain'>新建领域配置</a></p>"
                  f"<div class='card'>{table}"
                  "<p class='muted'>保存前自动备份到 archive/configs/，写出后回读校验（与 CLI 同一套 schema）。</p></div>")


def _input(name: str, value, label: str, width: int = 40, hint: str = "",
           datalist: bool = False) -> str:
    hint_html = f" <span class='muted'>{ESC(hint)}</span>" if hint else ""
    list_attr = " list='model-list'" if datalist else ""
    return (f"<div style='margin:6px 0'><label>{ESC(label)} "
            f"<input type='text' name='{name}' value='{ESC(str(value))}' "
            f"style='width:{width}%' class='mono'{list_attr}></label>{hint_html}</div>")


def _checkbox(name: str, checked: bool, label: str) -> str:
    return (f"<label style='margin-right:14px'><input type='checkbox' name='{name}' "
            f"{'checked' if checked else ''}> {ESC(label)}</label>")


def _new_config_defaults(app: WebApp, kind: str) -> dict:
    """新建配置的表单默认值。"""
    if kind == "experiment":
        first_domain = ""
        domains_dir = app.root / "configs" / "domains"
        if domains_dir.is_dir():
            tomls = sorted(domains_dir.glob("*.toml"))
            if tomls:
                first_domain = tomls[0].stem
        return {"experiment": {
            "name": "", "domain": first_domain, "kind": "k",
            "testset": "data/derived/testset.jsonl", "pool_strategy": "loo", "pool": None,
            "ks": [0, 1, 2, 3, 5, 8, 12, 20], "ns": [5, 10, 20, 50, 100], "fixed_k": 3,
            "retrievers": ["bm25"], "nested": True, "repeats": 3, "seeds": [101, 202, 303],
            "knee_threshold": 0.05, "std_alert": 0.03, "oracle_gap_threshold": 0.05,
        }}
    return {
        "domain": {"name": "", "grammar": "", "grammar_md5": "",
                   "prompt_template": "", "ontology_source": "prompt"},
        "generation": {"model": "deepseek-flash", "temperature": 0.0,
                       "max_concurrency": 4, "max_retries": 3, "timeout": 120.0},
        "judge": {"model": "deepseek-chat", "review_sample_rate": 0.1},
        "execution": {"enabled": False, "ke2sql_cmd": ""},
    }


def page_config_edit(app: WebApp, query: dict, message: str = "") -> bytes:
    from .. import config_io

    new_kind = query.get("new", [""])[0]
    creating = new_kind in {"experiment", "domain"}
    if creating:
        raw = _new_config_defaults(app, new_kind)
        path = None
    else:
        path = Path(query.get("path", [""])[0])
        if not query.get("path", [""])[0] or not path.exists() or not path.is_file():
            return layout("未找到", f"<h1>配置文件不存在：{ESC(str(path))}</h1>"
                          f"<p><a href='/configs'>← 返回配置列表</a></p>")
        raw = config_io.load_raw_toml(path)
    flash = f"<div class='flash'>{ESC(message)}</div>" if message else ""
    title = f"新建{'实验' if creating and new_kind == 'experiment' else '领域'}配置" \
        if creating else f"编辑配置：{path.name}"
    body = [f"<h1>{ESC(title)}</h1>{flash}",
            "<div class='card'><form method='post' action='/configs/save'>"]
    if creating:
        body.append(f"<input type='hidden' name='new' value='{new_kind}'>")
    else:
        body.append(f"<input type='hidden' name='path' value='{ESC(str(path))}'>")

    if "experiment" in raw:
        e = raw["experiment"]
        body.append("<h2>实验</h2>")
        body.append(_input("name", e.get("name", ""), "实验名", 30))
        body.append(_input("domain", e.get("domain", ""), "领域", 20,
                           "对应 configs/domains/<名>.toml"))
        body.append("<div style='margin:6px 0'>类型："
                    + "".join(
                        f"<label style='margin-right:14px'><input type='radio' name='kind' "
                        f"value='{v}' {'checked' if e.get('kind') == v else ''}> {t}</label>"
                        for v, t in (("k", "k 扫描"), ("n", "N 扫描"))
                    ) + "</div>")
        body.append(_input("testset", e.get("testset", ""), "测试集 jsonl", 60))
        body.append("<div style='margin:6px 0'>池策略："
                    + "".join(
                        f"<label style='margin-right:14px'><input type='radio' name='pool_strategy' "
                        f"value='{v}' {'checked' if e.get('pool_strategy', 'loo') == v else ''}> {t}</label>"
                        for v, t in (("loo", "LOO（冒烟）"), ("fixed", "fixed（真实池）"))
                    ) + "</div>")
        body.append(_input("pool", e.get("pool", "") or "", "fixed 池 jsonl（LOO 时留空）", 60))
        body.append(_input("ks", ", ".join(str(x) for x in e.get("ks", [])), "k 档位", 40, "逗号分隔"))
        body.append(_input("ns", ", ".join(str(x) for x in e.get("ns", [])), "N 档位", 40, "逗号分隔"))
        body.append(_input("fixed_k", e.get("fixed_k", 3), "固定 k（N 实验）", 10))
        body.append("<div style='margin:6px 0'>检索器："
                    + "".join(_checkbox(f"ret_{r}", r in e.get("retrievers", []), r)
                              for r in ("random", "bm25", "dense", "hybrid", "oracle"))
                    + "</div>")
        body.append("<div style='margin:6px 0'>"
                    + _checkbox("nested", bool(e.get("nested", True)), "嵌套池（只增不减）") + "</div>")
        onto_modes = e.get("onto_modes") or ["full"]
        body.append("<div style='margin:6px 0'>本体注入（k 方案 B 对照）："
                    + _checkbox("onto_full", "full" in onto_modes, "full（全量本体）")
                    + _checkbox("onto_linked", "linked" in onto_modes, "linked（每题相关概念）")
                    + "</div>")
        body.append(_input("onto_linking", e.get("onto_linking", "") or "",
                           "linked 相关性来源", 15, "retriever | gold（留空=retriever）"))
        body.append(_input("gen_models", ", ".join(e.get("gen_models", []) or []),
                           "模型对照（逗号分隔 ≥2 个）", 50,
                           "留空=领域默认模型；填则条件带 -m- 后缀（k 方案 C / n 方案 I）",
                           datalist=True))
        body.append(_input("repeats", e.get("repeats", 3), "重复次数", 10))
        body.append(_input("seeds", ", ".join(str(x) for x in e.get("seeds", [])), "种子", 40,
                           "数量须 ≥ 重复次数"))
        body.append("<h2>阈值</h2>")
        body.append(_input("knee_threshold", e.get("knee_threshold", 0.05), "拐点阈值", 10, "边际增益低于此值即拐点"))
        body.append(_input("std_alert", e.get("std_alert", 0.03), "std 报警", 10, "3 次重复 std 超过即标 ⚠️"))
        body.append(_input("oracle_gap_threshold", e.get("oracle_gap_threshold", 0.05), "oracle 差距阈值", 10))
    else:
        d = raw.get("domain", {})
        g = raw.get("generation", {})
        j = raw.get("judge", {})
        x = raw.get("execution", {})
        body.append("<h2>领域</h2>")
        body.append(_input("name", d.get("name", ""), "领域名", 20))
        body.append(_input("grammar", d.get("grammar", ""), "KE 语法文件（ke_syntax.lark）", 70,
                           "必须 KE-matcher 版"))
        body.append(_input("grammar_md5", d.get("grammar_md5", ""), "grammar md5", 30,
                           "留空保存时自动计算"))
        body.append(_input("prompt_template", d.get("prompt_template", ""), "prompt 模板", 50,
                           "可到 Prompt 页编辑内容"))
        body.append(_input("ontology_source", d.get("ontology_source", "prompt"), "本体来源", 15,
                           "prompt | json"))
        body.append(_input("ontology_json", d.get("ontology_json", "") or "", "本体 JSON（来源=json 时）", 50))
        body.append(f"<div style='margin:6px 0'><label>题目格式（user 消息模板）<br>"
                    f"<textarea name='question_format' style='width:50%' class='mono'>"
                    f"{ESC(d.get('question_format', '输入：{question}\\n\\n输出：'))}</textarea></label></div>")
        body.append("<h2>生成</h2>")
        body.append(_input("gen_model", g.get("model", ""), "生成模型", 25, datalist=True))
        body.append(_input("gen_temperature", g.get("temperature", 0.0), "温度", 10))
        body.append(_input("gen_max_concurrency", g.get("max_concurrency", 4), "并发", 10))
        body.append(_input("gen_max_retries", g.get("max_retries", 3), "重试", 10))
        body.append(_input("gen_timeout", g.get("timeout", 120.0), "超时秒", 10))
        body.append(_input("onto_always_include",
                           ", ".join(g.get("onto_always_include", []) or []),
                           "linked 模式必含算子", 40, "逗号分隔，如 sympy_expression；留空=无"))
        body.append("<h2>判分</h2>")
        body.append(_input("judge_model", j.get("model", ""), "judge 模型", 25, datalist=True))
        body.append(_input("review_sample_rate", j.get("review_sample_rate", 0.1), "人工抽检比例", 10))
        body.append(f"<div style='margin:6px 0'><label>judge system prompt（留空=内置默认）<br>"
                    f"<textarea name='judge_system' style='width:70%;height:100px' class='mono'>"
                    f"{ESC(j.get('system', '') or '')}</textarea></label></div>")
        body.append(f"<div style='margin:6px 0'><label>judge user 模板（需含 "
                    f"{{question}}/{{gold_ke}}/{{gen_ke}} 占位；留空=内置默认）<br>"
                    f"<textarea name='judge_user_template' style='width:70%;height:80px' class='mono'>"
                    f"{ESC(j.get('user_template', '') or '')}</textarea></label></div>")
        body.append("<h2>执行比对（SQL 域）</h2>")
        body.append("<div style='margin:6px 0'>"
                    + _checkbox("execution_enabled", bool(x.get("enabled", False)), "启用 ke2sql 执行比对")
                    + "</div>")
        body.append(_input("ke2sql_cmd", x.get("ke2sql_cmd", ""), "ke2sql 命令", 50))
        p = raw.get("pricing", {})
        body.append("<h2>价格（可选，看板自动换算 ¥）</h2>")
        body.append(_input("gen_price_per_mtok", p.get("gen_price_per_mtok", "") or "",
                           "生成模型单价（¥/百万 tokens）", 20, "留空则不换算"))
        body.append(_input("judge_price_per_mtok", p.get("judge_price_per_mtok", "") or "",
                           "judge 模型单价（¥/百万 tokens）", 20))
        body.append(_input("annotation_cost_per_item", p.get("annotation_cost_per_item", "") or "",
                           "标注成本单价（¥/条 gold KE）", 20, "用于 N 实验的池构建成本换算"))

    body.append("<div style='margin-top:10px'><button>校验并保存</button></div>")
    body.append(app.models_datalist())
    body.append("</form></div>")
    return layout(title, "".join(body))


def post_config_save(app: WebApp, form: dict) -> str:
    import re

    from .. import config_io

    creating = bool(form.get("new"))
    if creating:
        kind = form["new"][0]
        subdir = "experiments" if kind == "experiment" else "domains"
        name = form["name"][0].strip()
        safe = re.sub(r'[\\/:*?"<>|\s]+', "-", name).strip("-.")
        if not safe:
            raise config_io.SaveError("配置名不能为空（且需能作文件名）")
        path = app.root / "configs" / subdir / f"{safe}.toml"
        if path.exists():
            raise config_io.SaveError(f"已存在同名配置 {path.name}，请换名或去编辑它")
        raw = {}
        is_experiment = kind == "experiment"
    else:
        path = Path(form["path"][0])
        raw = config_io.load_raw_toml(path)
        is_experiment = "experiment" in raw

    if is_experiment:
        kind = form.get("kind", ["k"])[0] or "k"
        pool_strategy = form.get("pool_strategy", ["loo"])[0] or "loo"
        retrievers = [r for r in ("random", "bm25", "dense", "hybrid", "oracle")
                      if form.get(f"ret_{r}") == ["on"]]
        flat = {
            "name": form["name"][0], "domain": form["domain"][0], "kind": kind,
            "testset": form["testset"][0], "pool_strategy": pool_strategy,
            "pool": form.get("pool", [""])[0],
            "ks": form.get("ks", [""])[0], "ns": form.get("ns", [""])[0],
            "fixed_k": form.get("fixed_k", ["3"])[0],
            "nested": "on" if form.get("nested") == ["on"] else "",
            "repeats": form.get("repeats", ["3"])[0],
            "seeds": form.get("seeds", [""])[0],
            "knee_threshold": form.get("knee_threshold", ["0.05"])[0],
            "std_alert": form.get("std_alert", ["0.03"])[0],
            "oracle_gap_threshold": form.get("oracle_gap_threshold", ["0.05"])[0],
            "retrievers": retrievers,
            "onto_modes": [m for m in ("full", "linked") if form.get(f"onto_{m}") == ["on"]],
            "onto_linking": form.get("onto_linking", [""])[0],
            "gen_models": form.get("gen_models", [""])[0],
        }
        updates = config_io.experiment_updates_from_form(flat)
        if flat["pool"]:
            updates["experiment"]["pool"] = flat["pool"]
        header = "由 kelab 看板保存；原文件备份于 archive/configs/"
        config_io.save_config_toml(path, updates, app.root / "archive" / "configs",
                                   kind="experiment", header=header)
        verb = "已创建" if creating else "已保存"
        return f"实验配置{verb}：{path.name}（run_id 随配置哈希变化，新参数=新 run）"

    grammar_md5 = form.get("grammar_md5", [""])[0].strip()
    grammar_path = form["grammar"][0]
    if not grammar_md5 and Path(grammar_path).exists():
        grammar_md5 = file_md5(grammar_path)
    flat = {
        "name": form["name"][0], "grammar": grammar_path, "grammar_md5": grammar_md5,
        "prompt_template": form["prompt_template"][0],
        "ontology_source": form.get("ontology_source", ["prompt"])[0],
        "ontology_json": form.get("ontology_json", [""])[0],
        "question_format": form.get("question_format", [""])[0],
        "gen_model": form.get("gen_model", [""])[0],
        "gen_temperature": form.get("gen_temperature", ["0"])[0],
        "gen_max_concurrency": form.get("gen_max_concurrency", ["4"])[0],
        "gen_max_retries": form.get("gen_max_retries", ["3"])[0],
        "gen_timeout": form.get("gen_timeout", ["120"])[0],
        "onto_always_include": form.get("onto_always_include", [""])[0],
        "judge_model": form.get("judge_model", [""])[0],
        "review_sample_rate": form.get("review_sample_rate", ["0.1"])[0],
        "judge_system": form.get("judge_system", [""])[0],
        "judge_user_template": form.get("judge_user_template", [""])[0],
        "execution_enabled": "on" if form.get("execution_enabled") == ["on"] else "",
        "ke2sql_cmd": form.get("ke2sql_cmd", [""])[0],
        "gen_price_per_mtok": form.get("gen_price_per_mtok", [""])[0],
        "judge_price_per_mtok": form.get("judge_price_per_mtok", [""])[0],
        "annotation_cost_per_item": form.get("annotation_cost_per_item", [""])[0],
    }
    updates = config_io.domain_updates_from_form(flat)
    header = "由 kelab 看板保存；原文件备份于 archive/configs/"
    config_io.save_config_toml(path, updates, app.root / "archive" / "configs",
                               kind="domain", header=header)
    verb = "已创建" if creating else "已保存"
    return f"领域配置{verb}：{path.name}（grammar md5 已校验/自动计算）"
