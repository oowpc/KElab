# 新领域包模板

新增领域 = 复制本目录为 `configs/domains/<name>/` + 改写配置与资源，
**不改 runner/store/report**。

## 目录内容

| 文件 | 作用 | 必填 |
|---|---|---|
| `domain.toml` | 领域配置（语法/prompt/本体/模型/[pack] 声明） | ✓ |
| `prompt.md` | prompt 模板（含 `## 示例`、`## 待转换题目` 两段锚点） | ✓ |
| `grammar.lark` | KE 语法（Lark LALR） | ✓ |
| `ontology.json` | 本体（`ontology_source="json"` 时） | 可选 |

## 步骤

1. 复制本目录改名，把资源文件换成新领域的
2. `domain.toml` 里：
   - `[domain]` 的 `grammar`/`prompt_template` 写**相对本目录**的路径（如 `grammar.lark`）
   - `[pack]` 节声明 `adapter`（数据适配器，见 `kelab/adapters/`）与 `task`
     （任务类型，见 `kelab/tasks/`；NL→KE 用 `nl2ke`，问答类任务另实现 EM/F1 适配器）
3. 新数据格式/新 gold 校验规则 → 在 `kelab/adapters/` 加一个 DatasetAdapter 并注册
4. 跑通 `python -m kelab.cli doctor` 与一次 `--dry-run` 再花钱

## 不通用化的部分（架构评审结论）

- gold 的语义正确性靠 `python -m kelab.cli gold` 流水线的人工复核环节兜底，
  自动化只有「候选→规则校验→复核→冻结」的流水线骨架
- judge/metric 按任务类型走 task 适配器；报告文案仍是 NL→KE 口径，
  新任务类型需要自己的报告模板时再加，不提前抽象
