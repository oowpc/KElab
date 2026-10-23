"""实验配置加载：domain TOML + experiment TOML。

domain 配置描述一个领域的固定资源（语法、prompt 模板、本体来源、生成/判分模型）；
experiment 配置描述一次实验的因子网格（k 或 N、检索器、重复次数、种子）。
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class DomainConfig:
    name: str
    grammar_path: Path
    prompt_template: Path
    ontology_source: str = "prompt"  # prompt | json
    grammar_md5: str | None = None
    ontology_json: Path | None = None
    question_format: str = "输入：{question}\n\n输出："
    # 生成
    gen_model: str = "deepseek-flash"
    gen_temperature: float = 0.0
    gen_max_concurrency: int = 4
    gen_max_retries: int = 3
    gen_timeout: float = 120.0
    # 判分
    judge_model: str = "deepseek-chat"
    judge_score_map: dict[str, float] = field(
        default_factory=lambda: {"equivalent": 1.0, "minor": 0.5, "different": 0.0}
    )
    review_sample_rate: float = 0.10
    # judge prompt 覆盖（None = 用 judge.py 内置模板；None 不进配置哈希）
    judge_system: str | None = None
    judge_user_template: str | None = None
    # 执行比对（SQL 域专用）
    execution_enabled: bool = False
    ke2sql_cmd: str = ""
    # 标注成本（池构建成本换算，¥/条 gold KE；None = 只按条数，不进配置哈希）
    annotation_cost_per_item: float | None = None
    # 本体注入对照：linked 模式下无论如何都保留的算子（None = 空，不进配置哈希）
    onto_always_include: list[str] | None = None
    # 生成 max_tokens 上限（防失控长输出烧钱；None = 不限制，不进哈希）
    gen_max_tokens: int | None = None
    # 领域包声明（[pack] 节）：数据适配器 / 任务适配器名。
    # None = 旧配置未声明，按平台默认 conic/nl2ke 解析（None 不进配置哈希）
    adapter: str | None = None
    task: str | None = None


@dataclass
class ExperimentConfig:
    name: str
    domain: str
    kind: str  # "k" | "n"
    testset: Path
    pool_strategy: str = "loo"  # loo | fixed
    pool: Path | None = None
    ks: list[int] = field(default_factory=list)
    ns: list[int] = field(default_factory=list)
    fixed_k: int = 3
    retrievers: list[str] = field(default_factory=lambda: ["bm25"])
    nested: bool = True
    repeats: int = 3
    seeds: list[int] = field(default_factory=lambda: [101, 202, 303])
    # 试跑模式：只跑前 N 题（省钱验证 prompt/参数）；None = 全量。None 不进配置哈希
    limit_items: int | None = None
    # 本体注入对照（k 方案 P0 B）：条件叉乘维度，full=全量本体 / linked=每题相关概念裁剪
    # None = 只跑 full（不进配置哈希，老配置不受影响）
    onto_modes: list[str] | None = None
    # linked 模式的相关性来源：retriever=检索到的示例算子并集（泄露安全，默认）/ gold=诊断上界
    onto_linking: str | None = None
    # 模型对照（k 方案 C / n 方案 I）：条件叉乘维度，None = 只用领域默认模型（不进配置哈希）
    gen_models: list[str] | None = None
    # 质量门槛（协议 §7.6 选择规则）：语义等价率须 ≥ 该值才进入「最低成本 k」选择；
    # None = 不设门槛，只报曲线不宣称最优 k。None 不进配置哈希
    quality_gate: float | None = None
    # 预算上限（生成调用数；None = 不限。超限自动停跑并写 run 备注。None 不进哈希）
    budget_calls: int | None = None
    # 拐点判定阈值（边际增益首次低于该值的档位）
    knee_threshold: float = 0.05
    std_alert: float = 0.03
    oracle_gap_threshold: float = 0.05


def _require(table: dict, keys: list[str], where: str) -> None:
    missing = [k for k in keys if k not in table]
    if missing:
        raise ConfigError(f"{where} 缺少必填字段: {', '.join(missing)}")


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def load_domain(path: Path | str) -> DomainConfig:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取 domain 配置 {path}: {exc}") from exc

    _require(raw, ["domain"], str(path))
    d = raw["domain"]
    _require(d, ["name", "grammar", "prompt_template"], f"{path} [domain]")

    gen = raw.get("generation", {})
    judge = raw.get("judge", {})
    exe = raw.get("execution", {})
    pack = raw.get("pack", {})

    cfg = DomainConfig(
        name=d["name"],
        grammar_path=Path(d["grammar"]),
        grammar_md5=d.get("grammar_md5"),
        prompt_template=Path(d["prompt_template"]),
        ontology_source=d.get("ontology_source", "prompt"),
        ontology_json=_path(d.get("ontology_json")),
        question_format=d.get("question_format", DomainConfig.question_format),
        gen_model=gen.get("model", "deepseek-flash"),
        gen_temperature=float(gen.get("temperature", 0.0)),
        gen_max_concurrency=int(gen.get("max_concurrency", 4)),
        gen_max_retries=int(gen.get("max_retries", 3)),
        gen_timeout=float(gen.get("timeout", 120.0)),
        judge_model=judge.get("model", "deepseek-chat"),
        judge_score_map={
            k: float(v)
            for k, v in judge.get(
                "score_map", {"equivalent": 1.0, "minor": 0.5, "different": 0.0}
            ).items()
        },
        review_sample_rate=float(judge.get("review_sample_rate", 0.10)),
        judge_system=judge.get("system"),
        judge_user_template=judge.get("user_template"),
        execution_enabled=bool(exe.get("enabled", False)),
        ke2sql_cmd=exe.get("ke2sql_cmd", ""),
        annotation_cost_per_item=(
            float(raw.get("pricing", {})["annotation_cost_per_item"])
            if raw.get("pricing", {}).get("annotation_cost_per_item") is not None else None
        ),
        onto_always_include=(
            [str(x) for x in gen["onto_always_include"]]
            if gen.get("onto_always_include") else None
        ),
        gen_max_tokens=(
            int(gen["max_tokens"]) if gen.get("max_tokens") else None
        ),
        adapter=str(pack["adapter"]) if pack.get("adapter") else None,
        task=str(pack["task"]) if pack.get("task") else None,
    )
    if cfg.ontology_source not in {"prompt", "json"}:
        raise ConfigError(f"ontology_source 只能是 prompt|json，得到 {cfg.ontology_source!r}")
    if cfg.ontology_source == "json" and not cfg.ontology_json:
        raise ConfigError("ontology_source=json 时必须给 ontology_json")
    unknown = set(cfg.judge_score_map) - {"equivalent", "minor", "different"}
    if unknown:
        raise ConfigError(f"judge.score_map 含未知 verdict: {sorted(unknown)}")
    return cfg


def load_experiment(path: Path | str) -> ExperimentConfig:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取 experiment 配置 {path}: {exc}") from exc

    _require(raw, ["experiment"], str(path))
    e = raw["experiment"]
    _require(e, ["name", "domain", "kind", "testset"], f"{path} [experiment]")

    cfg = ExperimentConfig(
        name=e["name"],
        domain=e["domain"],
        kind=e["kind"],
        testset=Path(e["testset"]),
        pool_strategy=e.get("pool_strategy", "loo"),
        pool=_path(e.get("pool")),
        ks=[int(x) for x in e.get("ks", [])],
        ns=[int(x) for x in e.get("ns", [])],
        fixed_k=int(e.get("fixed_k", 3)),
        retrievers=list(e.get("retrievers", ["bm25"])),
        nested=bool(e.get("nested", True)),
        repeats=int(e.get("repeats", 3)),
        seeds=[int(s) for s in e.get("seeds", [101, 202, 303])],
        limit_items=int(e["limit_items"]) if e.get("limit_items") else None,
        onto_modes=[str(x) for x in e["onto_modes"]] if e.get("onto_modes") else None,
        onto_linking=str(e["onto_linking"]) if e.get("onto_linking") else None,
        gen_models=[str(x) for x in e["gen_models"]] if e.get("gen_models") else None,
        quality_gate=float(e["quality_gate"]) if e.get("quality_gate") is not None else None,
        budget_calls=int(e["budget_calls"]) if e.get("budget_calls") else None,
        knee_threshold=float(e.get("knee_threshold", 0.05)),
        std_alert=float(e.get("std_alert", 0.03)),
        oracle_gap_threshold=float(e.get("oracle_gap_threshold", 0.05)),
    )
    if cfg.kind not in {"k", "n"}:
        raise ConfigError(f"kind 只能是 k|n，得到 {cfg.kind!r}")
    if cfg.kind == "k" and not cfg.ks:
        raise ConfigError("k 实验必须给 ks")
    if cfg.kind == "n" and not cfg.ns:
        raise ConfigError("n 实验必须给 ns")
    if cfg.pool_strategy not in {"loo", "fixed"}:
        raise ConfigError(f"pool_strategy 只能是 loo|fixed，得到 {cfg.pool_strategy!r}")
    if cfg.pool_strategy == "fixed" and not cfg.pool:
        raise ConfigError("pool_strategy=fixed 时必须给 pool 文件路径")
    if len(cfg.seeds) < cfg.repeats:
        raise ConfigError(
            f"seeds 数量({len(cfg.seeds)})少于 repeats({cfg.repeats})，"
            "每次重复必须有独立种子"
        )
    valid_retrievers = {"random", "bm25", "dense", "hybrid", "oracle", "frozen"}
    bad = set(cfg.retrievers) - valid_retrievers
    if bad:
        raise ConfigError(f"未知检索器: {sorted(bad)}（可选 {sorted(valid_retrievers)}）")
    if cfg.onto_modes:
        bad_modes = set(cfg.onto_modes) - {"full", "linked"}
        if bad_modes:
            raise ConfigError(f"onto_modes 只能是 full|linked，得到 {sorted(bad_modes)}")
        if "linked" in cfg.onto_modes and cfg.onto_linking not in (None, "retriever", "gold"):
            raise ConfigError("onto_linking 只能是 retriever|gold")
    if cfg.gen_models is not None and not cfg.gen_models:
        raise ConfigError("gen_models 要么不写（用领域默认模型），要么至少一个模型名")
    if cfg.quality_gate is not None and not 0.0 <= cfg.quality_gate <= 1.0:
        raise ConfigError(f"quality_gate 须在 [0,1]，得到 {cfg.quality_gate}")
    if cfg.budget_calls is not None and cfg.budget_calls < 1:
        raise ConfigError(f"budget_calls 须 ≥1，得到 {cfg.budget_calls}")
    return cfg


def config_hash(*cfgs: object) -> str:
    """配置的规范化哈希，作为 run_id 的一部分（同配置同哈希）。

    纪律：值为 None 的键不进哈希——新增可选配置字段（默认 None）不会改变既有
    配置的哈希，断点续跑不受字段扩展影响。
    """
    payload = json.dumps(
        [asdict(c, dict_factory=_hashable_factory) for c in cfgs],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _hashable_factory(items: list[tuple]) -> dict:
    return {
        k: (str(v) if isinstance(v, Path) else v)
        for k, v in items
        if v is not None  # None 不进哈希（见 config_hash docstring）
    }


def file_md5(path: Path | str) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def dataset_fingerprint(exp: "ExperimentConfig") -> str:
    """数据集版本指纹：测试集（+ fixed 池）内容的哈希。
    预登记与 run 守卫共用同一算法（此前两处各算各的，对不上）。"""
    parts = [file_md5(exp.testset)]
    if exp.pool_strategy == "fixed" and exp.pool:
        parts.append(file_md5(exp.pool))
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:12]


def verify_grammar_md5(domain: DomainConfig) -> str:
    """校验 grammar 文件 md5 与配置一致（防止误用旧版语法），返回实际 md5。"""
    if not domain.grammar_path.exists():
        raise ConfigError(
            f"grammar 文件不存在：{domain.grammar_path}。"
            "请检查 domain 配置的 grammar 路径（应指向 KE-matcher 版 ke_syntax.lark）"
        )
    actual = file_md5(domain.grammar_path)
    if domain.grammar_md5 and actual != domain.grammar_md5.lower():
        raise ConfigError(
            f"grammar 文件 md5 不匹配：配置 {domain.grammar_md5}，实际 {actual}。"
            f"请确认指向 KE-matcher 版 ke_syntax.lark（旧版 kelang 语法解析 conic KE 全失败）。"
        )
    return actual
