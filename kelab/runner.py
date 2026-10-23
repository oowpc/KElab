"""实验编排：条件网格 × 题 × 重复，并发限流，断点续跑，单条失败落库不中断。

条件网格：
- k 实验：ks × retrievers（n 记录为实际池规模）
- n 实验：ns × retrievers × fixed_k（嵌套池：同 seed 前缀，只增不减）
每个 (条件, 题, 重复) 的流程：池截取 → 检索 → prompt 组装 → 生成 → validation → judgment。
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from nl2ke import clean_output, LLMError

from . import datasets, judge, ke_validate, prompts, retrieve
from .config import DomainConfig, ExperimentConfig
from .datasets import Item
from .llm import ClientConfig, chat_with_usage
from .ontology import Ontology
from .store import Store


@dataclass
class Condition:
    condition_id: str
    kind: str
    k: int
    n: int | None
    retriever: str
    onto_mode: str = "full"  # full=全量本体 / linked=每题相关概念裁剪（k 方案 B 对照）
    gen_model: str | None = None  # None = 领域默认模型（模型对照 C/I 时显式给出）


@dataclass
class RunResources:
    domain: DomainConfig
    exp: ExperimentConfig
    template: str
    ontology: Ontology
    testset: list[Item]
    pools: dict[str, list[Item]] | list[Item]
    gen_clients: dict[str, ClientConfig]  # 模型名 → 客户端（模型对照时多个；键 "default" = 领域默认模型）
    judge_client: ClientConfig
    store: Store | None  # dry-run 时为 None（不建库）
    run_id: str

    def client_for(self, condition: Condition) -> ClientConfig:
        return self.gen_clients[condition.gen_model or "default"]


def expand_conditions(exp: ExperimentConfig, pool_size: int) -> list[Condition]:
    conditions: list[Condition] = []
    onto_modes = exp.onto_modes or ["full"]
    gen_models = exp.gen_models or [None]  # None = 领域默认模型

    def cid(base: str, onto: str, model: str | None) -> str:
        if onto == "linked":
            base += "-linked"
        if model:
            base += f"-m-{model}"
        return base

    if exp.kind == "k":
        for retriever in exp.retrievers:
            for onto in onto_modes:
                for model in gen_models:
                    for k in exp.ks:
                        conditions.append(
                            Condition(
                                condition_id=cid(f"k-{retriever}-k{k}", onto, model),
                                kind="k", k=k, n=pool_size,
                                retriever=retriever, onto_mode=onto, gen_model=model,
                            )
                        )
    else:
        for retriever in exp.retrievers:
            for onto in onto_modes:
                for model in gen_models:
                    for n in exp.ns:
                        conditions.append(
                            Condition(
                                condition_id=cid(f"n-{retriever}-n{n}", onto, model),
                                kind="n", k=exp.fixed_k, n=n,
                                retriever=retriever, onto_mode=onto, gen_model=model,
                            )
                        )
    return conditions


def _pool_for(res: RunResources, item: Item) -> list[Item]:
    if isinstance(res.pools, dict):
        pool = res.pools.get(item.item_id)
        if pool is None:
            raise datasets.DataError(f"LOO 池缺少题目 {item.item_id}")
        return pool
    return res.pools


def _static_prompt_hash(res: RunResources) -> str:
    probe = prompts.assemble(res.template, res.domain.question_format, [], "probe")
    return probe.static_hash


def process_one(
    res: RunResources,
    condition: Condition,
    item: Item,
    rep: int,
    static_hash: str,
) -> str:
    """处理一条 (条件, 题, 重复)。返回状态：ok | error。绝不向外抛异常。"""
    store = res.store
    seed = res.exp.seeds[rep]
    gen_row: dict = {
        "run_id": res.run_id,
        "condition_id": condition.condition_id,
        "item_id": item.item_id,
        "rep": rep,
        "seed": seed,
        "status": "error",
    }
    try:
        pool = _pool_for(res, item)
        if condition.kind == "n":
            assert condition.n is not None
            pool = datasets.pool_subset(
                pool, condition.n, seed, item.item_id, nested=res.exp.nested
            )

        if condition.retriever == "frozen":
            # 协议 §7.2：预先冻结的嵌套示例序列，全题共用同一套、顺序固定。
            # LOO：从全局序列剔除题目自身（防自泄露）；
            # N 实验：只在当前 N 子集成员内取（保持嵌套一致与 N 的控制语义）。
            full = res.testset if isinstance(res.pools, dict) else pool
            order = retrieve._retrieve_frozen(full, seed)
            pool_ids = {p.item_id for p in pool}  # N 实验时 pool 已是嵌套子集
            picked = [x for x in order
                      if x.item_id != item.item_id and x.item_id in pool_ids][: condition.k]
            warning = None
            if condition.k > len(picked):
                warning = (f"pool_smaller_than_k: frozen 序列可用 {len(picked)} 条 < k="
                           f"{condition.k}，返回全部")
            retrieval = retrieve.RetrievalResult(items=picked, warning=warning)
        else:
            retrieval = retrieve.retrieve(
                condition.retriever, item, pool, condition.k, seed,
                universe=res.testset if isinstance(res.pools, dict) else None,
            )

        # 本体注入对照：linked 模式按相关性来源裁剪本体（默认检索式，泄露安全）
        template = res.template
        if condition.onto_mode == "linked":
            always = list(res.domain.onto_always_include or [])
            linking = res.exp.onto_linking or "retriever"
            if linking == "gold":
                subset_ops = set(item.gold_operators) | set(always)
            else:  # retriever：检索到的示例算子并集
                subset_ops = set(always)
                for ex in retrieval.items:
                    subset_ops |= set(ex.gold_operators)
            template = prompts.build_linked_template(template, res.ontology,
                                                     sorted(subset_ops))

        prompt = prompts.assemble(
            template, res.domain.question_format, retrieval.items, item.question
        )
        gen_row.update(
            {
                "prompt_text": prompt.system + "\n\n---USER---\n\n" + prompt.user,
                "prompt_hash": prompt.prompt_hash,
                "retrieved_ids": json.dumps(
                    [it.item_id for it in retrieval.items], ensure_ascii=False
                ),
                "retrieval_warning": retrieval.warning,
            }
        )

        result = chat_with_usage(
            res.client_for(condition), prompts.to_messages(prompt)
        )
        ke_text = clean_output(result.content)
        gen_row.update(
            {
                "status": "ok",
                "raw_output": result.content,
                "ke_text": ke_text,
                "latency_ms": result.latency_ms,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            }
        )
    except (LLMError, datasets.DataError, ValueError, OSError) as exc:
        gen_row["error"] = str(exc)[:1000]
        store.insert_generation(gen_row)
        return "error"
    except Exception as exc:  # 兜底：单条任何异常都落库，绝不中断批次
        gen_row["error"] = f"未捕获异常 {type(exc).__name__}: {exc}"[:1000]
        store.insert_generation(gen_row)
        return "error"

    gen_id = store.insert_generation(gen_row)

    # validation + judgment（同样不中断批次；异常则把该行标为 error，可一键重试）
    try:
        # validation（只记录不中断）
        validation = ke_validate.validate_ke(
            ke_text, res.domain.grammar_path, res.ontology
        )
        store.insert_validation(
            gen_id,
            {
                "lark_ok": int(validation.lark_ok),
                "lark_error": validation.lark_error,
                "light_ok": int(validation.light_ok),
                "issues_json": json.dumps(
                    [i.to_dict() for i in validation.issues], ensure_ascii=False
                ),
                "gen_operators": json.dumps(validation.operators, ensure_ascii=False),
            },
        )

        # judgment
        gold_ops = set(item.gold_operators)
        retrieved_ops: set[str] = set()
        for ex in retrieval.items:
            retrieved_ops |= set(ex.gold_operators)
        gen_ops = set(validation.operators) if validation.lark_ok else None

        judged = judge.llm_judge(
            res.judge_client, res.domain, item.question, item.gold_ke, ke_text
        )
        store.insert_judgment(
            gen_id,
            {
                "exact_match": judge.exact_match(ke_text, item.gold_ke),
                "retrieval_coverage": judge.jaccard(retrieved_ops, gold_ops) if condition.k > 0 else None,
                "gen_coverage": judge.jaccard(gen_ops, gold_ops) if gen_ops is not None else None,
                "llm_verdict": judged.verdict,
                "llm_score": judged.score,
                "llm_rationale": judged.rationale,
                "judge_model": res.domain.judge_model,
                "judge_error": judged.error,
                "judge_prompt_tokens": judged.prompt_tokens,
                "judge_completion_tokens": judged.completion_tokens,
            },
        )
    except Exception as exc:
        # 校验/判分阶段异常：标 error 可重试，不留「ok 但无判分」的脏行
        store.mark_generation_error(gen_id, f"校验/判分异常 {type(exc).__name__}: {exc}")
        return "error"
    return "ok"


def dry_run_report(exp: ExperimentConfig, testset: list[Item], pool_size: int) -> str:
    conditions = expand_conditions(exp, pool_size)
    n_items = len(testset[: exp.limit_items]) if exp.limit_items else len(testset)
    n_gen = len(conditions) * n_items * exp.repeats
    limit_note = f"（试跑模式：只跑前 {n_items} 题）" if exp.limit_items else ""
    lines = [
        f"[dry-run] 实验 {exp.name}（kind={exp.kind}）{limit_note}",
        f"  条件数: {len(conditions)}（{', '.join(c.condition_id for c in conditions)}）",
        f"  测试题: {n_items}，重复: {exp.repeats}",
        f"  生成调用: {n_gen}，judge 调用: 至多 {n_gen}（生成失败的跳过）",
        f"  合计 API 调用上限: {2 * n_gen}",
    ]
    return "\n".join(lines)


_print_lock = threading.Lock()


def run_experiment(res: RunResources, dry_run: bool = False) -> dict:
    exp = res.exp
    pool_size = (
        len(next(iter(res.pools.values()))) if isinstance(res.pools, dict) else len(res.pools)
    )
    if dry_run:
        print(dry_run_report(exp, res.testset, pool_size), file=sys.stderr)
        return {"status": "dry-run"}

    conditions = expand_conditions(exp, pool_size)
    static_hash = _static_prompt_hash(res)
    res.store.mark_running(res.run_id)  # 续跑时状态归位（原 run 可能是 done/interrupted）
    for cond in conditions:
        res.store.add_condition(
            res.run_id, cond.condition_id, cond.kind, cond.k, cond.n,
            cond.retriever, static_hash, onto=cond.onto_mode, gen_model=cond.gen_model,
        )

    item_ids = [it.item_id for it in res.testset]
    if exp.limit_items:
        item_ids = item_ids[: exp.limit_items]  # 试跑模式：只跑前 N 题
    items_by_id = {it.item_id: it for it in res.testset}
    stats = {"ok": 0, "error": 0, "skipped": 0}

    for cond in conditions:
        # 预算上限（协议 §7.6：成本超预算先暂停；已到的不受影响）
        if exp.budget_calls:
            spent = res.store.total_generations(res.run_id)
            if spent >= exp.budget_calls:
                res.store.set_note(
                    res.run_id,
                    f"预算上限触发：{spent}/{exp.budget_calls} 次调用已满，"
                    f"条件 {cond.condition_id} 起未跑（补预算后断点续跑即可）",
                )
                _log(f"[预算上限] 已用 {spent} 次调用 ≥ 预算 {exp.budget_calls}，停止后续条件")
                break
        pending = res.store.pending(res.run_id, cond.condition_id, item_ids, exp.repeats)
        skipped = len(item_ids) * exp.repeats - len(pending)
        stats["skipped"] += skipped
        if not pending:
            _log(f"[{cond.condition_id}] 全部完成（续跑跳过 {skipped} 条）")
            continue
        _log(f"[{cond.condition_id}] 待跑 {len(pending)} 条（跳过已完成 {skipped} 条）")
        done = 0
        with ThreadPoolExecutor(max_workers=res.domain.gen_max_concurrency) as pool:
            futures = {
                pool.submit(process_one, res, cond, items_by_id[iid], rep, static_hash): (iid, rep)
                for iid, rep in pending
            }
            for future in as_completed(futures):
                iid, rep = futures[future]
                try:
                    status = future.result()
                except Exception as exc:  # 兜底：process_one 之外的意外也不中断批次
                    _log(f"  !! 未捕获异常 {iid} rep{rep}: {exc!r}")
                    status = "error"
                stats[status] += 1
                done += 1
                if done % 20 == 0 or done == len(pending):
                    _log(f"  [{cond.condition_id}] {done}/{len(pending)}")
    res.store.finish_run(res.run_id)
    return stats


def _log(message: str) -> None:
    with _print_lock:
        print(message, file=sys.stderr, flush=True)
