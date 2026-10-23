# testset20 生成效果评估

- 模型：`deepseek-flash`（DeepSeek-V4.1-Flash，思考模式默认开启）
- 结果文件：`testset20_results.md`
- 测试集：`testset20.xlsx`

## 汇总

| 判定 | 题数 |
|---|---:|
| 完全正确 | 15 |
| 基本正确（有轻微缺省） | 3 |
| 类型错误 | 1 |
| 题面自相矛盾，无法判定 | 1 |

严格全对 15/20；按“可接受”计 18/20。排除题面矛盾的 022 后，有效题为 19 道，可接受 18 道（94.7%）。

## 逐题结果

| # | 题目 | 判定 | 说明 |
|---:|---|---|---|
| 1 | conic10K-005 | 正确 | 用 `point_on_curve(A/B, C)` + `point_on_curve(A/B, l)` 等价替代 `intersection`；O 用 Point+坐标而非 Origin，语义不变 |
| 2 | conic10K-010 | 正确 | 输出 `d` + `when_min(d)`，等价于 Gold 的 `min(...)`；属于等价写法 |
| 3 | conic10K-011 | 正确 | 与 Gold 基本一致 |
| 4 | conic10K-014 | 基本正确 | 缺 `a > 0, b > 0`；圆变量名用 O 不影响语义 |
| 5 | conic10K-017 | 基本正确 | `F1`、`F2` 未显式声明为 Point；其余正确，可能可由算子返回类型推断 |
| 6 | conic10K-020 | 正确 | `Abs` 与 Gold 的 `abs` 等价 |
| 7 | conic10K-022 | 题面矛盾 | 题面 `a > b` 与渐近线 `y=±x` 推出 `a = b`，Gold 与生成 KE 都不可满足；建议换题 |
| 8 | conic10K-024 | 正确 | 已用 `one_of(get_focus(C)) = F`，类型闭合 |
| 9 | conic10K-026 | 正确 | 用 `intercept_chord` + `line_of` 定义弦，等价于交点写法 |
| 10 | conic10K-028 | 正确 | 动圆圆心用 P 代替 Gold 的 O，语义正确 |
| 11 | conic10K-030 | 正确 | 输出表达式 + `when_max(...)`，等价于 Gold 的 `max(...)` |
| 12 | conic10K-031 | 类型错误 | `point_line_distance(...) = sympy_expression("sqrt(7)*a")`，Number 与 Expression 相等；应为 `= sqrt(7)*a`；另缺 `a > 0, b > 0` |
| 13 | conic10K-035 | 正确 | 把向量等式展开成 x/y 坐标等式，等价 |
| 14 | conic10K-036 | 正确 | 用 `slope(l) = 1` + `line_of` 定义直线；左/右焦点计算结果相同 |
| 15 | conic10K-037 | 基本正确 | 缺 `a > 0, b > 0`；`dot_product(...) = 0` 等价于 `is_perpendicular` |
| 16 | conic10K-042 | 正确 | 用 `abs(x_coordinate(P))` 表示到 y 轴距离，等价 |
| 17 | conic10K-044 | 正确 | 用 `R: Curve` + `get_a_part(Right, C)` 表示右支，等价 |
| 18 | conic10K-046 | 正确 | 约束顺序不同，语义正确 |
| 19 | conic10K-049 | 正确 | 与 Gold 基本一致 |
| 20 | conic10K-050 | 正确 | 输出表达式 + `when_max(...)`，等价于 Gold 的 `max(...)` |

## 主要发现

1. 模型已稳定使用本体中的 `get_focus` / `get_a_focus` / `get_a_part` / `one_of`，不再调用早期数据里的 `focus`、`left_focus` 等未声明算子。
2. 最典型错误只有一处：031 把 `sympy_expression(...)` 当成 Number 用，属于返回类型混用。
3. 模型倾向用等价写法而非复刻 Gold：
   - `d + when_min(d)` 代替 `min(expr)`
   - `when_max(expr)` 代替 `max(expr)`
   - `intercept_chord` 代替交点 + `length`
   - 坐标展开代替 `vector_of`
   这些写法语义正确，但如果评测用字符串完全匹配，会显著低估效果。
4. 022 是数据问题而非模型问题：题面条件本身矛盾，建议从测试集替换（可用 039 或 047）。
