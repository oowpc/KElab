"""kelab 命令行：prepare / run / aggregate / report / smoke。

主路径（方案验收）：python -m kelab.cli smoke --domain configs/domains/conic.toml --xlsx data/testset20.xlsx
一条命令从池跑到曲线。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nl2ke import configure_console, LLMError

from . import datasets, runner
from .config import (
    ConfigError,
    DomainConfig,
    ExperimentConfig,
    config_hash,
    file_md5,
    load_experiment,
)
from .datasets import DataError
from .llm import resolve_api
from .report import render_report
from .store import Store

DEFAULT_DOMAIN_DIR = Path("configs/domains")
DEFAULT_DERIVED = Path("data/derived")


class CliError(RuntimeError):
    pass


# ---------------------------------------------------------------- 公共装载


def _load_domain_and_ontology(domain_path: Path) -> tuple[DomainConfig, object, str, str]:
    """返回 (domain_cfg, ontology, template_text, grammar_md5)。grammar md5 不匹配时 fail-fast。"""
    from .packs import load_pack

    pack = load_pack(domain_path)
    return pack.domain, pack.ontology, pack.template, pack.grammar_md5


def _default_domain_path(name: str) -> Path:
    """领域解析：优先 configs/domains/<name>/ 目录包，退回 <name>.toml。"""
    from .packs import resolve_domain_path

    return resolve_domain_path(name)


def _dataset_fingerprint(exp: ExperimentConfig) -> str:
    """数据集版本指纹（实现已收敛到 config.dataset_fingerprint，与预登记同算法）。"""
    from .config import dataset_fingerprint

    return dataset_fingerprint(exp)


def _build_resources(
    exp: ExperimentConfig,
    domain_path: Path,
    *,
    need_api: bool,
    dry_run: bool = False,
) -> tuple[runner.RunResources, str]:
    from .packs import load_pack

    pack = load_pack(domain_path)
    domain, ontology, template, grammar_md5 = (
        pack.domain, pack.ontology, pack.template, pack.grammar_md5)
    testset = datasets.read_jsonl(exp.testset)
    pools = datasets.load_pools(exp.pool_strategy, testset, exp.pool)
    leakage = datasets.check_leakage(testset, pools, exp.pool_strategy)
    if not leakage["passed"]:
        raise CliError(
            "泄露检查未通过（命中即测试失败，不得进入结论）："
            + json.dumps(leakage["hits"], ensure_ascii=False)
        )

    run_id = f"{exp.name}-{config_hash(domain, exp)}"
    store = None
    if not dry_run:
        # dry-run 只估算调用量，不建库建行（否则运行列表里全是幽灵 run）
        store = Store(Path("runs") / run_id / "kelab.sqlite")
        try:
            fingerprint = _dataset_fingerprint(exp)
            template_md5 = file_md5(domain.prompt_template)
            existing = store.run_info(run_id)
            if existing is not None:
                stored = existing["dataset_md5"] if "dataset_md5" in existing.keys() else None
                if stored is None:
                    store.set_dataset_md5(run_id, fingerprint)  # 旧库回填一次
                elif stored != fingerprint:
                    raise CliError(
                        f"数据集已变更（测试集/池内容与 run 创建时不一致，"
                        f"指纹 {stored} → {fingerprint}）。\n"
                        f"为避免一次 run 混入两个版本的数据，平台拒绝续跑。请选择：\n"
                        f"  ① 数据是有意更新的 → 改实验名（或改任一配置）开新 run；\n"
                        f"  ② 确认要覆盖旧数据 → 备份后删除 runs/{run_id} 再跑。"
                    )
                # prompt 模板版本守卫（与数据集守卫同口径）：模板内容变了不许续跑
                stored_tpl = (existing["prompt_template_md5"]
                              if "prompt_template_md5" in existing.keys() else None)
                if stored_tpl is None:
                    store.set_prompt_template_md5(run_id, template_md5)
                elif stored_tpl != template_md5:
                    raise CliError(
                        f"prompt 模板内容已变更（md5 {stored_tpl} → {template_md5}）。\n"
                        f"同一 run 内混入两个 prompt 版本会破坏可复现性。请选择：\n"
                        f"  ① 改动是有意的 → 改实验名（或改任一配置）开新 run；\n"
                        f"  ② 只是回退调试 → 恢复模板内容后再续跑。"
                    )
            store.create_run(
                run_id,
                config_json=json.dumps(
                    {"experiment": exp.name, "domain": domain.name,
                     "limit_items": exp.limit_items},
                    ensure_ascii=False,
                ),
                config_hash=config_hash(domain, exp),
                prompt_template_md5=template_md5,
                grammar_md5=grammar_md5,
                dataset_md5=fingerprint,
            )
        except Exception:
            store.close()  # 拒绝续跑时别泄露连接（Windows 下会锁住目录）
            raise

    gen_clients: dict = {}
    judge_client = None
    if need_api:
        try:
            models = exp.gen_models or ["default"]
            for model in models:
                actual = domain.gen_model if model == "default" else model
                gen_clients[model] = resolve_api(
                    actual,
                    temperature=domain.gen_temperature,
                    timeout=domain.gen_timeout,
                    max_retries=domain.gen_max_retries,
                )
                gen_clients[model].max_tokens = domain.gen_max_tokens
            judge_client = resolve_api(
                domain.judge_model, temperature=0.0, max_retries=domain.gen_max_retries
            )
        except LLMError as exc:
            raise CliError(str(exc)) from exc

    res = runner.RunResources(
        domain=domain,
        exp=exp,
        template=template,
        ontology=ontology,
        testset=testset,
        pools=pools,
        gen_clients=gen_clients,
        judge_client=judge_client,
        store=store,
        run_id=run_id,
        task=pack.task,
    )
    return res, run_id


# ---------------------------------------------------------------- 子命令


def _col_map_for(args: argparse.Namespace) -> dict | None:
    """列映射优先级：CLI --col > 领域配置 [xlsx] 节 > 内置默认。"""
    col_map: dict | None = None
    try:
        import tomllib

        domain_ref = Path(args.domain)
        if domain_ref.is_dir():
            domain_ref = domain_ref / "domain.toml"  # 目录包布局
        raw = tomllib.loads(domain_ref.read_text(encoding="utf-8"))
        if "xlsx" in raw:
            col_map = {k: v for k, v in raw["xlsx"].items()}
    except (OSError, tomllib.TOMLDecodeError):
        pass
    for pair in getattr(args, "col", None) or []:
        if "=" not in pair:
            raise CliError(f"--col 需要 字段=列名 格式，得到 {pair!r}")
        key, value = pair.split("=", 1)
        col_map = col_map or {}
        col_map[key.strip()] = value.strip()
    return col_map


def _validate_and_attach(items, pack) -> None:
    """gold 全量校验（fail-fast）+ 预提取算子。

    两道都过适配器：adapter.validate_gold = 领域规则（gold 能不能用），
    task.validate_output = 任务机制（解析 + 算子提取）。gold 非法即停工。
    """
    failures: list[str] = []
    for item in items:
        result = pack.adapter.validate_gold(item, pack)
        if not result.ok:
            failures.append(f"{item.item_id}: {'; '.join(result.errors)}")
            continue
        task_val = pack.task.validate_output(item.gold_ke, pack.domain, pack.ontology)
        if not task_val.parse_ok:
            failures.append(f"{item.item_id}: 任务层解析失败: {task_val.parse_error}")
            continue
        item.gold_operators = task_val.operators
    if failures:
        raise DataError(
            "gold 校验失败（gold 必须全部合法，否则实验口径不成立）：\n"
            + "\n".join(failures)
        )


def cmd_prepare(args: argparse.Namespace) -> int:
    from .packs import load_pack

    pack = load_pack(args.domain)
    grammar_md5 = pack.grammar_md5
    if not (args.source or args.xlsx):
        raise CliError("需要数据源：--source 或 --xlsx（格式由领域适配器解释）")
    source = Path(args.source or args.xlsx)
    if args.dev and not args.split:
        raise CliError("--dev 需配合 --split 使用（开发集只在三分切分时存在）")
    items = pack.adapter.load(source, col_map=_col_map_for(args))
    items, dedupe = datasets.dedupe_items(items)
    if dedupe["dropped_question_dups"] or dedupe["same_ke_groups"]:
        print(f"去重：{dedupe['in']} → {dedupe['out']} 条"
              f"（题干重复去掉 {len(dedupe['dropped_question_dups'])} 条"
              + (f"：{dedupe['dropped_question_dups']}" if dedupe['dropped_question_dups'] else "")
              + f"；KE 结构相同 {len(dedupe['same_ke_groups'])} 组（保留，人工复核）"
              + (f"：{dedupe['same_ke_groups']}" if dedupe['same_ke_groups'] else "") + "）",
              file=sys.stderr)
    _validate_and_attach(items, pack)
    out = Path(args.out) if args.out else DEFAULT_DERIVED / f"{source.stem}.jsonl"

    pool_path: Path | None = Path(args.pool_out) if args.pool_out else None
    if args.split:
        testset, dev, pool = datasets.split_dataset3(
            items, args.split, dev_size=args.dev, seed=args.split_seed)
        datasets.write_jsonl(testset, out)
        pool_path = pool_path or DEFAULT_DERIVED / f"{source.stem}_pool.jsonl"
        datasets.write_jsonl(pool, pool_path)
        dev_note = ""
        if dev:
            dev_path = DEFAULT_DERIVED / f"{source.stem}_dev.jsonl"
            datasets.write_jsonl(dev, dev_path)
            dev_note = f"；开发集 {len(dev)} 题 → {dev_path}（流程调试/判分器校准用，不计主结果）"
        items_for_leakage = testset
        pools = pool
        strategy = "fixed"
        print(f"切分：测试集 {len(testset)} 题 → {out}；池 {len(pool)} 条 → {pool_path}"
              f"（seed {args.split_seed}）{dev_note}", file=sys.stderr)
    else:
        datasets.write_jsonl(items, out)
        items_for_leakage = items
        strategy = args.pool_strategy
        pools = datasets.load_pools(strategy, items, Path(args.pool) if args.pool else None)
        print(f"导入 {len(items)} 题 → {out}", file=sys.stderr)

    leakage = datasets.check_leakage(items_for_leakage, pools, strategy)
    report_path = out.parent / "_leakage_report.json"
    datasets.save_leakage_report(leakage, report_path)

    # 数据流水线 manifest：分区指纹 + 题族划分 + 逐条指纹（版本可追踪、切分可审计）
    from . import data_pipeline

    partitions = {"testset": testset, "pool": pool} if args.split else {"testset": items}
    if args.split and dev:
        partitions["dev"] = dev
    if not args.split and strategy == "fixed" and isinstance(pools, list):
        partitions["pool"] = pools  # fixed 池也进 manifest：跨分区题族检查才有意义
    manifest = data_pipeline.build_manifest(
        partitions, seed=args.split_seed if args.split else None,
        source=str(source), adapter=pack.adapter)
    manifest_path = data_pipeline.write_manifest(manifest, out.parent / "_manifest.json")
    print(f"manifest：{manifest_path}（题族 {manifest['family_count']} 个）", file=sys.stderr)

    cross = manifest["cross_partition_families"]
    if cross and not getattr(args, "allow_cross_family", False):
        # 协议口径：近重复题跨分区 = 泄露通道，fail-fast（显式放行除外）
        print(f"错误：跨分区题族 {cross}——近重复题落在不同分区，泄露通道未闭合。\n"
              "请调整切分（题族整族切分是默认口径），或确认无误后加 --allow-cross-family 放行。",
              file=sys.stderr)
        return 1

    print(f"gold 全部通过适配器规则校验 + 算子预提取（grammar md5 {grammar_md5}）", file=sys.stderr)
    print(f"泄露检查：{'通过 ✓' if leakage['passed'] else '未通过 ✗'}（{report_path}）", file=sys.stderr)
    if not leakage["passed"]:
        print(json.dumps(leakage["hits"], ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    exp = load_experiment(args.exp)
    if args.limit:
        exp.limit_items = args.limit  # 试跑模式（覆盖配置值，参与配置哈希 → 独立 run）
    domain_path = Path(args.domain) if args.domain else _default_domain_path(exp.domain)
    if args.dry_run:
        res, _ = _build_resources(exp, domain_path, need_api=False, dry_run=True)
        runner.run_experiment(res, dry_run=True)
        return 0
    res, run_id = _build_resources(exp, domain_path, need_api=True)
    stats = runner.run_experiment(res)
    res.store.close()
    print(f"run_id={run_id} 完成：{stats}", file=sys.stderr)
    return 0


def _report_one(exp_path: str, domain_path: Path | None, provisional: bool) -> Path:
    from .packs import load_pack
    from .prereg import load_history, load_prereg

    exp = load_experiment(exp_path)
    domain_path = domain_path or _default_domain_path(exp.domain)
    domain = load_pack(domain_path).domain
    run_id = f"{exp.name}-{config_hash(domain, exp)}"
    store = Store(Path("runs") / run_id / "kelab.sqlite")
    if store.run_info(run_id) is None:
        store.close()
        raise CliError(f"找不到 run {run_id}（先 run 再 report）")
    testset = datasets.read_jsonl(exp.testset)
    report_path = render_report(
        store, run_id, exp, domain, testset, Path("results") / run_id,
        provisional=provisional, prereg=load_prereg(Path("."), exp.name),
        prereg_history=load_history(Path("."), exp.name),
    )
    store.close()
    return report_path


def cmd_aggregate(args: argparse.Namespace) -> int:
    report_path = _report_one(args.exp, Path(args.domain) if args.domain else None,
                              provisional=args.provisional)
    print(f"聚合与报告已生成：{report_path}", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    return cmd_aggregate(args)


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    serve(root=Path("."), port=args.port, open_browser=args.open)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import format_report, run_checks

    checks = run_checks(Path("."), ping=args.ping)
    print(format_report(checks), file=sys.stderr)
    return 0 if all(c.ok for c in checks) else 1


def cmd_gold(args: argparse.Namespace) -> int:
    """gold 流水线：候选导入 → 规则校验 → 人工复核队列 → 冻结 manifest。"""
    from . import gold_pipeline
    from .packs import load_pack

    pack = load_pack(args.domain)
    out_dir = Path(args.out_dir)
    if args.freeze:
        if not args.actor:
            raise CliError("冻结必须声明复核人：--actor 姓名")
        review = Path(args.review) if args.review else out_dir / gold_pipeline.REVIEW_FILE
        if not review.exists():
            raise CliError(f"找不到复核文件 {review}（先不带 --freeze 跑一次生成复核队列）")
        frozen = gold_pipeline.freeze(
            review, pack, out_dir,
            actor=args.actor, allow_unreviewed=args.allow_unreviewed,
        )
        print(f"冻结完成 → {frozen}（manifest: {out_dir / gold_pipeline.MANIFEST_FILE}）",
              file=sys.stderr)
        return 0

    source = args.source or args.xlsx
    if not source:
        raise CliError("需要数据源：--source 候选文件（xlsx/jsonl）")
    col_map = _col_map_for(args)
    items = gold_pipeline.import_candidates(source, pack.adapter, col_map=col_map)
    candidates = gold_pipeline.validate_candidates(items, pack)
    review_path = gold_pipeline.export_review(candidates, out_dir)
    ok = sum(1 for c in candidates if c.auto_ok)
    bad = [c for c in candidates if not c.auto_ok]
    print(f"候选 {len(candidates)} 条：规则校验通过 {ok} 条、未过 {len(bad)} 条", file=sys.stderr)
    for cand in bad[:10]:
        print(f"  ✗ {cand.item.item_id}: {'; '.join(cand.errors)}", file=sys.stderr)
    print(f"人工复核队列 → {review_path}（填 human_verdict=pass/fail，可直接改 gold_ke）",
          file=sys.stderr)
    print("复核完成后冻结：python -m kelab.cli gold --domain <领域> --out-dir "
          f"{out_dir} --freeze --actor <复核人>", file=sys.stderr)
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """一条命令从池跑到曲线：prepare → run(k) → run(n) → report×2 → 汇总。"""
    domain_path = Path(args.domain)
    exp_k = load_experiment(args.exp_k)
    load_experiment(args.exp_n)  # 提前校验 N 实验配置可读（循环里才真正使用）

    # prepare（测试集不存在或 --force-prepare 时重跑）
    if args.force_prepare or not exp_k.testset.exists():
        prepare_args = argparse.Namespace(
            domain=str(domain_path), xlsx=args.xlsx, out=str(exp_k.testset),
            pool_strategy="loo", pool=None, col=None, source=None,
            split=None, dev=0, split_seed=42, pool_out=None,
            allow_cross_family=False,
        )
        code = cmd_prepare(prepare_args)
        if code != 0:
            return code

    reports: list[Path] = []
    for exp_path in (args.exp_k, args.exp_n):
        exp = load_experiment(exp_path)
        res, run_id = _build_resources(exp, domain_path, need_api=True)
        print(f"===== 运行 {exp.name}（run_id={run_id}）=====", file=sys.stderr)
        stats = runner.run_experiment(res)
        res.store.close()
        print(f"===== {exp.name} 完成：{stats} =====", file=sys.stderr)
        reports.append(_report_one(exp_path, domain_path, provisional=True))

    summary = Path("results") / "smoke_summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 冒烟汇总", "",
             "两轮冒烟实验（k 扫描 + N 扫描）的报告：", ""]
    lines += [f"- [{p.parent.name}]({p.parent.name}/report.md)" for p in reports]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"冒烟完成，汇总：{summary}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kelab", description="NL→KE few-shot 实验平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="数据源 → testset.jsonl + gold 校验 + 泄露检查（走领域适配器）")
    p.add_argument("--domain", required=True,
                   help="领域（configs/domains/<name>/ 目录包或 <name>.toml）")
    p.add_argument("--xlsx", help="数据源（xlsx/jsonl/记录目录，格式由领域适配器解释）")
    p.add_argument("--source", help="--xlsx 的别名（领域通用叫法）")
    p.add_argument("--out", help="输出 jsonl 路径（默认 data/derived/<源名>.jsonl）")
    p.add_argument("--pool-strategy", default="loo", choices=["loo", "fixed"])
    p.add_argument("--pool", help="fixed 策略的池 jsonl")
    p.add_argument("--col", action="append",
                   help="列映射覆盖，如 --col gold_ke=KE（可多次；默认读领域 [xlsx] 节）")
    p.add_argument("--split", type=int, help="切分：前 N 题作测试集，其余作池")
    p.add_argument("--dev", type=int, default=0, help="开发集题数（流程调试/判分器校准用）")
    p.add_argument("--split-seed", type=int, default=42)
    p.add_argument("--pool-out", help="切分时池的输出路径")
    p.add_argument("--allow-cross-family", action="store_true",
                   help="显式放行跨分区题族（默认 fail-fast：近重复题跨分区即停工）")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("run", help="跑实验（断点续跑）")
    p.add_argument("--exp", required=True)
    p.add_argument("--domain", help="默认 configs/domains/<exp.domain>.toml")
    p.add_argument("--limit", type=int, help="试跑模式：只跑前 N 题")
    p.add_argument("--dry-run", action="store_true", help="只打印调用量估算，不调 API")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("aggregate", help="聚合 + 出报告（同 report）")
    p.add_argument("--exp", required=True)
    p.add_argument("--domain")
    p.add_argument("--provisional", action="store_true", help="冒烟声明：不做统计推断")
    p.set_defaults(func=cmd_aggregate)

    p = sub.add_parser("report", help="同 aggregate")
    p.add_argument("--exp", required=True)
    p.add_argument("--domain")
    p.add_argument("--provisional", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("doctor", help="环境自检（新手第一步：全部通过再开跑）")
    p.add_argument("--ping", action="store_true",
                   help="额外实调一次 API（约几十个 token），验证模型可用")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("gold", help="gold 流水线：候选导入→规则校验→人工复核→冻结")
    p.add_argument("--domain", required=True, help="领域（configs/domains/<name>/ 或 <name>.toml）")
    p.add_argument("--source", help="候选数据文件（xlsx/jsonl，格式由领域适配器解释）")
    p.add_argument("--xlsx", help="--source 的别名")
    p.add_argument("--out-dir", default="data/gold", help="流水线产物目录（默认 data/gold）")
    p.add_argument("--col", action="append", help="列映射覆盖（xlsx 源，同 prepare）")
    p.add_argument("--freeze", action="store_true", help="冻结：复核文件 → frozen.jsonl + manifest")
    p.add_argument("--review", help="复核文件路径（默认 <out-dir>/_review.jsonl）")
    p.add_argument("--actor", help="复核人/决策人（冻结必填）")
    p.add_argument("--allow-unreviewed", action="store_true",
                   help="允许规则校验通过但未填人工结论的条目冻结（manifest 标记）")
    p.set_defaults(func=cmd_gold)

    p = sub.add_parser("web", help="本地 Web 看板（运行进度/指标/明细/人工抽检）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("smoke", help="一条命令从池跑到曲线（conic 冒烟）")
    p.add_argument("--domain", required=True)
    p.add_argument("--xlsx", required=True)
    p.add_argument("--exp-k", default="configs/experiments/smoke_k.toml")
    p.add_argument("--exp-n", default="configs/experiments/smoke_n.toml")
    p.add_argument("--force-prepare", action="store_true")
    p.set_defaults(func=cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ConfigError, DataError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
