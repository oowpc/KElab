# kelab — NL→KE few-shot 实验平台

> **看不懂平台逻辑？** 先看 [ARCHITECTURE.md](ARCHITECTURE.md)（15 分钟架构导读），
> 或启动看板后打开首页（工作流向导，一页讲清数据流和概念）。
> 这份 README 只放快速开始和指标口径。

支撑两份实验方案：k 实验（few-shot 示例数 vs 质量）与 N 实验（候选池规模 vs 质量）。
两篇共用测试集、判分脚本、prompt 模板，结果在 (k,N) 网格缝合。

## 新手从零开始（第一次用，按这个顺序来）

```powershell
# ① 打开 PowerShell，cd 到项目根目录（含 kelab/、configs/ 的文件夹）
cd "C:\Users\wang\Desktop\NL2KE测试"

# ② 装依赖（一次性）
pip install -r requirements.txt

# ③ 配 API（看板「设置」页填 base_url + key 即可，存 configs/api.json，无需环境变量）
#    也可用环境变量：$env:NL2KE_API_KEY="sk-你的key"（优先级：设置文件 > 环境变量）

# ④ 环境自检——全部 ✓ 再继续；有 ✗ 会告诉你怎么修
python -m kelab.cli doctor
#    想连模型可用性一起验（约几十个 token）：python -m kelab.cli doctor --ping

# ⑤ 打开看板（首页=工作流向导，平台逻辑一页讲清）
python -m kelab.cli web --port 8765 --open
```

之后的工作全在看板里：数据准备 → 启动实验（先试跑几题）→ 看曲线/对比 → 失败分类/人工抽检 → 拿报告。
跑测试验证安装：`python -m unittest discover -s tests`（全部 mock，不调 API）。

## 常用命令速查

```bash
python -m kelab.cli doctor [--ping]     # 环境自检（+可选 API 实调）
python -m kelab.cli prepare --domain configs/domains/conic.toml --xlsx 标注.xlsx [--split 100]
python -m kelab.cli run --exp configs/experiments/smoke_k.toml [--dry-run] [--limit 5]
python -m kelab.cli smoke --domain configs/domains/conic.toml --xlsx testset20.xlsx
python -m kelab.cli web --port 8765 [--open]
```

断点续跑：同配置重跑 `run` 自动只补缺口（run_id = 实验名 + 配置哈希；None 值不进哈希，
加可选字段不断续跑；试跑题数参与哈希 → pilot 是独立 run）。

## 看板页面速查

| 页面 | 干什么 |
|---|---|
| `/` 向导 | **平台逻辑一页讲清（数据流图+概念+三步上手），迷路就看这页** |
| `/runs` 运行列表 | 进度/ETA/状态自愈/run 备注/**最佳语义列** |
| `/run/<id>` run 详情 | **解读卡**（主指标大字 + 规则化人话判定，每条附依据）、指标表、自动曲线、消耗卡、失败/抽检/日志/重校验/重判/**重试失败条**/**导出分享版**/停止 |
| `/compare` 对比 | 两版配置的指标 Δ + 叠加曲线（prompt 迭代必看） |
| `/domains` 汇总对比 | **多 run 一页看全局**：总览表（最佳语义/拐点/通过率/总 tokens/judge 一致率/**一句话解读**，降序）+ 同轴叠加曲线 + 分域小多图 + 各域拐点 |
| `/grid` (k,N) 网格 | k 实验 × N 实验二维缝合（方案标志性产出） |
| `/prepare` 数据准备 | xlsx 列映射、试导入预览、切分测试集/池、泄露检查 |
| `/prompts` Prompt | 模板编辑/组装预览（零 token）/版本历史 diff/恢复 |
| `/configs` 配置 | 领域与实验参数表单（**新建/编辑**均可，保存自动备份+回读校验；模型字段带网关模型下拉） |
| `/settings` 设置 | **API 全配置**：base_url/key/embedding 端点（存 configs/api.json，全程无需环境变量）、连接测试（零 token）、模型列表 |
| `/launch` 启动实验 | 调用量估算、试跑题数、批量串行队列（**花 token 的唯一入口**） |

## 指标口径（与方案对齐）

| 方案指标 | 实现 | 优先级 |
|---|---|---|
| 语义等价率 | LLM judge（equivalent=1.0/minor=0.5/different=0.0）+ 10% 分层人工抽检校准 | P0 |
| 检索覆盖度（N 实验） | top-k 示例算子集与 gold 算子集 Jaccard | P0 |
| 语法可解析率 | Lark LALR（KE-matcher 版 ke_syntax.lark，md5 校验防误用旧版） | P1 |
| 类型检查通过率 | 冒烟期 = light 静态检查（闭词表/arity/绑定/单 output） | P1 |
| 完全匹配率 | 归一化后字符串相等 | P1 |
| 端到端可执行率 | ke2sql 接口预留（SQL 域接入时启用） | P1 |
| token / 耗时 | 生成与 judge 双侧 usage 逐条落库，[pricing] 可换算 ¥ | P2 |

## 硬性控制（方案要求）

- **零泄露**：测试集与池 id/题干交集检查，命中 fail-fast；**池内去重**（题干+KE 结构双查，prepare 自动执行）
- **数据版本守卫**：run 记录测试集（+池）内容指纹，数据变更后拒绝续跑（防一次 run 混入两个版本），run 页可见数据指纹
- **三分数据集**（§7.3）：测试集/开发集/池，按题族整族切分防同构泄露；开发集只用于流程调试与判分校准
- **嵌套池**：`nested=true` 同 seed 前缀只增不减；`nested=false` 每档独立随机重采样（方案 B 对照开关）
- **冻结示例序列**（§7.2 主实验口径）：检索器选 `frozen` = 预先冻结的嵌套序列，全题共用、顺序固定、随复现包保存
- **本体注入对照**（k 方案 B 对照）：`onto_modes=["full","linked"]` 同跑全量与「每题相关概念裁剪」，相关性默认取检索结果算子并集（泄露安全，`onto_linking="gold"` 为诊断上界）；报告自动出逐档语义 Δ + token 节省与判定
- **模型对照**（k 方案 C / n 方案 I）：`gen_models=["小模型","大模型"]` 双模型同跑（judge 固定），报告出**双线对照图**（分检索器小多图）+ 逐档 Δ + 「收益是否随模型能力变化」判定
- **prompt 冻结**：模板/grammar md5 + prompt 全文逐字落库
- **重复与统计**：≥3 个预登记种子；均值 + **95% CI（逐题 t 区间 / 比例 Wilson）**；**跨 k 配对 CI + McNemar**；std>3pp 标 ⚠️；重采样拐点一致性
- **拐点协议定义**（§7.5）：连续两档增益 < δ 且配对 CI 未达最小实际差异；报告附 **δ 敏感性分析**表
- **预登记**（§7.5）：跑主实验前冻结 δ/质量门槛/抽检比例/判分口径（哈希+时间戳，改动留历史）
- **选择规则**（§7.6）：设了质量门槛 → 达标档中成本最低者；未设 → 只报曲线不宣称最优
- **判分口径**（§7.4）：语义等价以**人工盲评**为准（A/B 双人独立 + 分歧 C 裁决，盲评不显示 k/模型/judge 结论）；**LLM judge 仅辅助信号**
- **统计功效**：报告固定输出 **MDE 估算**（以方案锚点 100 题≈5pp 按 1/√n 换算），小于 MDE 的差异一律「数据不足」
- **抽检口径统一**：看板队列与 report 导出的 review_sample.csv 是同一套样本（按条件分层等距抽样，比例取领域配置 review_sample_rate）
- **异常兜底**：单条失败记 error 不中断；断点续跑；被 kill 的 run 自动标「已中断」

## 换领域

1. 加 `configs/domains/<域>.toml`（语法、prompt 模板、本体来源 prompt|json、judge 配置）
2. `/prepare` 导入该域数据（列映射存领域 `[xlsx]` 节）+ 切分出测试集与 fixed 池
3. 加 `configs/experiments/<实验>.toml` → `/launch` 启动

## 待接（阶段 3）

dense/hybrid 检索（接口已留，配 NL2KE_EMBED_* 即可）、ke2sql 执行比对（SQL 域）、
KE-matcher 等价漏斗、vendored 完整类型检查、多域回归。
