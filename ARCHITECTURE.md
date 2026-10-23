# kelab 架构说明

> 读这份文件的目标：15 分钟看懂平台每一层干什么、数据往哪流、关键设计为什么这样定。
> 配合看板首页（`/`，工作流向导）一起读效果最好。快速上手只看 [README.md](README.md)。

## 一句话

kelab 测量「few-shot 示例数 k」和「候选池规模 N」对 NL→KE 生成质量（语义等价率）的影响，
方法是**固定其余一切、只动一个因子、用 gold KE 判分**。两份实验方案共享同一套测试集、判分与 prompt 模板。

## 数据流总览

```
标注 xlsx ─prepare→ testset.jsonl + pool.jsonl ─┐
                                                ▼
实验配置 TOML（k/N 网格×检索器×3重复）──▶ runner ──▶ runs/<run_id>/kelab.sqlite
                                                （每条：检索→拼prompt→生成→validation→judgment 全落库）
                                                ▼
                        aggregate（3重复均值±std）──▶ results/<run_id>/（图+report.md+CSV）
                                                ▼
                              Web 看板（只读为主 + 少量写：reviews/note/revalidate/rejudge）
```

## 分层与模块

### 后端核心（`kelab/`，与 UI 无关）

| 模块 | 一句话职责 | 关键点 |
|---|---|---|
| `config.py` | domain/experiment TOML → dataclass + `config_hash` | **哈希纪律：None 值不进哈希**——加可选字段不断 run_id 续跑 |
| `ontology.py` | 本体解析（prompt 内嵌 op 块 / JSON） | conic 本体以 prompt 为准（conic_onto.json 命名体系不符，不用） |
| `datasets.py` | xlsx 导入、JSONL、LOO/fixed 池、**切分 split**、**泄露检查** | gold 必须 100% 过校验（fail-fast）；泄露命中即停工 |
| `retrieve.py` | 检索器 random/bm25/dense/hybrid(RRF)/oracle | BM25 纯 Python；oracle 用 gold 算子集=质量上界；k>池兜底不中断 |
| `prompts.py` | 模板三段切分（`## 示例`/`## 待转换题目`）+ few-shot 渲染 + prompt_hash | 渲染全文逐字落库（可审计） |
| `llm.py` | OpenAI 兼容客户端：usage 捕获、429/5xx 退避重试 | 生成与 judge 共用，复用 `nl2ke.py` 的错误模式 |
| `ke_validate.py` | lark 解析 + 算子提取 + light 静态检查 | **grammar 必须 KE-matcher 版**（md5 校验）；light=闭词表/arity/绑定（含赋值式绑定）/单 output |
| `judge.py` | 完全匹配、Jaccard 覆盖度、LLM judge（严格 JSON） | judge 坏输出重试 1 次再记 error（不静默记 0）；judge prompt 可被领域配置覆盖 |
| `store.py` | SQLite（runs/conditions/generations/validations/judgments）+ 迁移 + 断点查询 | `UNIQUE(run,condition,item,rep)` 是续跑的基础；WAL 供看板并发读；只读模式带热日志自恢复 |
| `runner.py` | 条件网格×题×重复 编排：并发限流、单条失败落库不中断、断点续跑 | process_one 任何异常 → status=error，绝不中断批次 |
| `aggregate.py` | 每条件×指标 均值±std（按 3 个 rep mean）、边际增益、拐点 | std>3pp 标 ⚠️（方案稳定性门槛） |
| `report.py` | matplotlib 图（dataviz 规范）+ markdown 报告（方案验收口径：拐点/相对提升/H1–H4 三态/成本换算） | 冒烟期（<50 题）自动加「不做统计推断」声明 |
| `cli.py` | prepare/run/aggregate/report/smoke/web 子命令 | `smoke` = 一条命令从池跑到曲线（方案主路径验收） |
| `web/` | 看板（见下） | |

### 看板（`kelab/web/`，零新增依赖，stdlib http.server）

| 文件 | 职责 |
|---|---|
| `app.py` | **数据层 WebApp**：页面都通过它拿数据（运行发现/进度/成本/启动/队列/图表渲染调度/revalidate/rejudge/状态自愈），不直接碰 SQL |
| `ui.py` | 布局、样式、chip、进度条/ETA、表单小件 |
| `pages_home.py` | **向导首页**（概念模型图+三步上手）——先读这页再看别的 |
| `pages_runs.py` | 运行列表、run 详情、生成明细、失败分类、人工抽检、日志 |
| `pages_experiments.py` | 启动（单个/批量/试跑）、跨 run 对比、(k,N) 网格 |
| `pages_prepare.py` | 数据准备（列映射、试导入预览、切分） |
| `pages_prompts.py` | prompt 编辑/预览/版本历史 diff/恢复 |
| `pages_configs.py` | domain/experiment 配置表单（备份+回读校验） |
| `server.py` | 路由、JSON API、静态产物、服务启动 |

## SQLite schema（runs/<run_id>/kelab.sqlite）

```
runs(run_id PK, started_at, config_json, config_hash, prompt_template_md5, grammar_md5, status, note)
conditions(condition_id PK, run_id, kind[k|n], k, n, retriever, static_prompt_hash)
generations(gen_id PK, run_id, condition_id, item_id, rep, seed,
            prompt_text, prompt_hash, retrieved_ids, raw_output, ke_text,
            latency_ms, prompt_tokens, completion_tokens, status[ok|error], error, created_at,
            UNIQUE(run_id, condition_id, item_id, rep))   ← 断点续跑的键
validations(gen_id UNIQUE, lark_ok, lark_error, light_ok, issues_json, gen_operators)
judgments(gen_id UNIQUE, exact_match, retrieval_coverage, gen_coverage,
          llm_verdict, llm_score, llm_rationale, judge_model, judge_error,
          judge_prompt_tokens, judge_completion_tokens)
reviews_v2(gen_id, reviewer, human_verdict, human_note, blind, reviewed_at,
           PRIMARY KEY(gen_id, reviewer))   ← 看板人工抽检（双人盲评+C裁决）
reviews(gen_id PK, human_verdict, human_note, reviewed_at)   ← 旧表（迁移自动回填 v2）
```

加列一律走 `store.MIGRATIONS`（旧库自动 ALTER）。

## 关键设计决策（为什么这样）

1. **一切可复现**：prompt 全文、检索到的示例 id、种子、模板/grammar md5、配置哈希全部落库。报告任何数字都能回溯到具体 prompt。
2. **run_id = 实验名 + 配置哈希**：改配置=新 run（天然隔离），同配置=断点续跑（不重复扣费）。试跑题数参与哈希 → pilot 是独立 run。
3. **失败不中断**：单条生成失败记 error 落库继续；校验/判分只记录不阻断；池不足 fail-fast（那是配置错误）。
4. **判分分两层**：确定性的（语法/静态/覆盖度/完全匹配）归程序；语义的归 LLM judge + 10% 人工抽检校准（对冲 judge 与生成同源偏差）。
5. **看板只读为主**：写操作为 reviews/note/revalidate/rejudge/设置/配置/标注/预登记/启动九类，
   全部短事务+busy_timeout，与 runner 写进程（WAL）共存。
6. **零新增运行时依赖**：BM25 自实现、embedding 走 API、不引 pandas/faiss——平台在哪台机器都能直接跑。

## 目录约定

```
configs/domains/<域>.toml        语法/prompt模板/本体来源/生成与judge模型/[pricing]/[xlsx]列映射
configs/experiments/<实验>.toml   kind(k|n)、ks/ns、retrievers、repeats、seeds、阈值、limit_items
data/derived/                    prepare 产物（testset/pool jsonl、泄露报告、embedding 缓存）
runs/<run_id>/                   kelab.sqlite + console.log（web 启动的）
results/<run_id>/                metrics.csv、*.png、report.md、review_sample.csv（看板自动更新）
archive/{prompts,configs}/       编辑前自动备份（时间戳 .bak）
tests/                           unittest（mock API server 复刻 test_nl2ke.py 模式）
```
