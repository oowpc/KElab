# testset20：生成 KE 与 Gold KE 逐题比较

- Gold 来源：`testset20.xlsx` 的“李天音-KE”
- 生成来源：`testset20_results.md`
- 024 / 037 的 Gold 已按本体签名修正：`get_focus(C) = F` → `one_of(get_focus(C)) = F`

## 汇总

| 类别 | 题数 | 题号 |
|---|---:|---|
| 完全一致 / 语义等价 | 12 | 010, 020, 024, 026, 028, 030, 035, 042, 044, 046, 049, 050 |
| 基本正确（缺约束 / 声明不完整 / 收窄 / 依赖隐含） | 6 | 005, 011, 014, 017, 036, 037 |
| 类型错误 | 1 | 031 |
| 题面自相矛盾 | 1 | 022 |

严格等价 12/20；按“可接受”计 18/20。排除题面矛盾的 022 后，有效题 19 道，可接受 18 道（94.7%）。

## 逐题对照

| # | 题目 | 输出目标 | 与 Gold 的关键差异 | 判定 |
|---:|---|---|---|---|
| 1 | conic10K-005 | `range(perimeter(triangle_of(A,B,F)))` | O: `Origin` → `Point` + `locate(O)=(0,0)`，等价；A/B: Gold 用 `intersection(l,C)={A,B}`，生成用 A/B 同时在 C、l 上，未显式保证 A≠B | 基本正确（依赖 `triangle_of` 隐含 A≠B） |
| 2 | conic10K-010 | `min(1/2*MA+MF)` → `d` + `when_min(d)` | F 的等式方向不同；增加 `d: Number, d=...` | 等价 |
| 3 | conic10K-011 | `range(eccentricity(M))` | 缺 `a>0, b>0`；其余一致 | 基本正确（缺约束） |
| 4 | conic10K-014 | `eccentricity(C)` | 缺 `a>0, b>0`；圆变量 G → O | 基本正确（缺约束） |
| 5 | conic10K-017 | `cos(angle_of(F1,P,F2))` | F1/F2 未显式声明 Point；C2 `Curve` → `Hyperbola`；intersection 用中间变量 S | 基本正确（声明不完整） |
| 6 | conic10K-020 | `num_intersection(l,C)` | `abs` → `Abs`（仅在 sympy 字符串内） | 等价 |
| 7 | conic10K-022 | `get_expression(C)` | 渐近线从 `l1/l2 + y=±x` → 两次 `one_of(asymptote)+slope=±1`；题面 `a>b` 与 `y=±x` 推出 `a=b` | 题面矛盾（忽略矛盾则等价） |
| 8 | conic10K-024 | `locate(P)` | 约束顺序、等式方向不同；其余一致 | 等价 |
| 9 | conic10K-026 | `length(line_segment_of(A,B))` → `length(intercept_chord(l,C))` | 用 `line_of(F1,P)=l` 代替 `point_on_curve(F1,l)`；其余一致 | 等价 |
| 10 | conic10K-028 | `get_expression(locus(O))` → `locus(P)` | 变量名不同；其余一致 | 等价 |
| 11 | conic10K-030 | `max(expr)` → `expr` + `when_max(expr)` | C3 `Curve`→`Hyperbola`，C1/C2 `Curve`→`Circle`，类型更具体 | 等价 |
| 12 | conic10K-031 | `eccentricity(C)` | `point_line_distance(...) = sympy_expression("sqrt(7)*a")`，Number vs Expression；缺 `a>0,b>0` | 类型错误 |
| 13 | conic10K-035 | `locate(A)` | 向量等式 → x/y 坐标等式；F1/F2 等式方向不同 | 等价 |
| 14 | conic10K-036 | `dot_product(vector_of(O,A),vector_of(O,B))` | 焦点 `one_of(get_focus)` → `get_a_focus(Left)`；`inclination=Pi/4` → `slope=1`；交点 → A/B 在 C 上且 `line_of(A,B)=l` | 基本正确（收窄为左焦点，结果相同） |
| 15 | conic10K-037 | `range(eccentricity(E))` | 缺 `a>0,b>0`；`is_perpendicular` → `dot_product(...)=0` | 基本正确（缺约束） |
| 16 | conic10K-042 | `min(d1+d2)` → `d1+d2` + `when_min` | d1 用 `abs(x_coordinate(P))` 代替 `point_line_distance(P,yax)`；`Real`→`Number` | 等价 |
| 17 | conic10K-044 | `range(dot_product(...))` | O `Origin` → `Point` + `center(C)=O`；`get_a_part` 用中间变量 R | 等价 |
| 18 | conic10K-046 | `get_stdeq_expression(C)` | 约束顺序不同；`one_of(...)=l` → `l=one_of(...)` | 等价（顺序不同） |
| 19 | conic10K-049 | `focal_length(C)` | 完全一致 | 完全一致 |
| 20 | conic10K-050 | `max(expr)` → `expr` + `when_max` | `d: Real` → `Number` | 等价 |

## 需要重点看的题

### 005：A/B 绑定偏弱

Gold：
```text
A: Point, B: Point, intersection(l, C) = {A, B}
```

生成：
```text
A: Point, B: Point, point_on_curve(A, C), point_on_curve(B, C), point_on_curve(A, l), point_on_curve(B, l)
```

生成只保证 A、B 同时在椭圆和直线上，没有显式保证 A≠B，也没有直接绑定“交点集合”。
若 `triangle_of` 在语义上要求三个顶点互异，则 A≠B，生成与 Gold 等价；若 `triangle_of` 不强制互异，则生成允许 A=B，周长范围会变大。因此判“基本正确”，但这是全套里最值得复核的一处。

### 011 / 014：缺 `a > 0, b > 0`

Gold 都显式写了 `a > 0, b > 0`，生成省略。由于方程中只用 `a^2`、`b^2`，符号通常不影响最终解；作为 KE 仍属条件缺失，建议补上。

### 017：F1/F2 未显式声明

Gold：
```text
F1: Point, F2: Point, get_focus(C1) = {F1, F2}
```

生成：
```text
get_a_focus(Left, C1) = F1, get_a_focus(Right, C1) = F2
```

生成依赖算子返回类型推断 F1/F2 为 Point。若系统要求显式声明，这就是类型错误；若允许推断，则其余语义与 Gold 等价。

### 031：唯一的硬类型错误

Gold：
```text
point_line_distance(F1, line_of(P, F2)) = sqrt(7)*a
```

生成：
```text
point_line_distance(F1, line_of(P, F2)) = sympy_expression("sqrt(7)*a")
```

`point_line_distance` 返回 `Number`，`sympy_expression` 返回 `Expression`，等号两边类型不一致。应去掉 `sympy_expression`，写成 `= sqrt(7)*a`。另外同样缺 `a > 0, b > 0`。

### 036：把“一个焦点”收窄为“左焦点”

Gold 用 `one_of(get_focus(C)) = F`，生成用 `get_a_focus(Left, C) = F`。
这确实收窄了条件。对本题 `x^2/2 + y^2 = 1`，斜率 1 的直线过左焦点或右焦点时，`OA·OB` 都等于 `-1/3`，所以最终答案相同；但 KE 本身不是“任取一个焦点”的完整表达。

### 037：缺 `a > 0, b > 0`

Gold 显式给出 `a > 0, b > 0`，生成省略。抛物线 `y^2 = 8ax` 的开口方向依赖 a 的符号，建议补上。
另外生成用 `dot_product(vector_of(A,P), vector_of(F,P)) = 0` 替代 `is_perpendicular(line_of(A,P), line_of(F,P))`，在非退化情况下等价。

### 022：题面本身矛盾

题面同时给出：
- `a > b`
- 渐近线 `y = ±x`

而标准双曲线的渐近线斜率是 `±b/a`，`y=±x` 推出 `a=b`，与 `a>b` 矛盾。
Gold 和生成 KE 都把这个矛盾原样编码了，因此两边都不可满足。这不是模型生成错误，建议从测试集移除或修正题面。
