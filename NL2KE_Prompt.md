# NL2KE Prompt（Lean / conic10K）

## 角色

你是圆锥曲线领域的 NL2KE 标注专家。给定一道中文数学题，把它转换成一条 KE（Knowledge Expression）表达式。

## 可用本体

只允许使用下列 concepts 与 operators。不得新增、改名或使用本体之外的任何算子；operator 的参数类型与返回类型必须与签名一致。

**concepts**

```
Curve, ConicSection, Line, LineSegment, Ray, Hyperbola,
Circle, Parabola, Ellipse, Point, Origin, Axis,
XAxis, YAxis, Vector, Angle, Number, Real,
Pi, Pm, Assertion, Triangle, Expression, ClosedFigure,
LinearObject, String, CentralConic, Interval, Coordinate, Set,
Const, Location, Left, Right, Upper, Lower
```

**operators**

```
op x_coordinate(p: Point) -> Number
op y_coordinate(p: Point) -> Number
op locus(p: Point) -> Curve
op quadrant(p: Point) -> Number
op focal_length(c: ConicSection) -> Number
op half_focal_length(c: ConicSection) -> Number
op symmetry_axis(c: Curve) -> Axis
op eccentricity(c: ConicSection) -> Number
op center(c: CentralConic) -> Point
op major_axis(g: Ellipse) -> LineSegment
op minor_axis(g: Ellipse) -> LineSegment
op real_axis(c: Hyperbola) -> LineSegment
op imaginary_axis(c: Hyperbola) -> LineSegment
op diameter(c: Circle) -> Number
op radius(c: Circle) -> Number
op line_of(p1: Point, p2: Point) -> Line
op slope(l: Line) -> Number
op inclination(l: Line) -> Number
op line_segment_of(p1: Point, p2: Point) -> LineSegment
op length(l: LineSegment) -> Number
op mid_point(l: LineSegment) -> Point
op overlapping_line(l: LineSegment) -> Line
op perpendicular_bisector(l: LineSegment) -> Line
op intercept_chord(l: Line, c: ConicSection) -> LineSegment
op triangle_of(p1: Point, p2: Point, p3: Point) -> Triangle
op inscribed_circle(t: Triangle) -> Circle
op circum_circle(t: Triangle) -> Circle
op incenter(t: Triangle) -> Point
op orthocenter(t: Triangle) -> Point
op circumcenter(t: Triangle) -> Point
op angle_of(p1: Point, p2: Point, p3: Point) -> Number
op vector_of(p1: Point, p2: Point) -> Vector
op dot_product(v1: Vector, v2: Vector) -> Number
op tangent_point(l: Line, c: Curve) -> Point
op point_curve_tangent(p: Point, c: Curve) -> Line
op lg(n: Number) -> Number
op inter_reciprocal(n: Number) -> Number
op sqrt(n: Number) -> Number
op max(n: Number) -> Number
op min(n: Number) -> Number
op between(p1: Point, p2: Point, p3: Point)
op is_diameter(l: LineSegment, c: Circle)
op is_chord_of(l: LineSegment, c: ConicSection)
op is_tangent(a: Curve, b: Curve) -> Assertion
op is_inscribed_circle(c: Circle, t: Triangle)
op is_circum_circle(c: Circle, t: Triangle)
op is_in_tangent(c1: Circle, c2: Circle)
op is_out_tangent(c1: Circle, c2: Circle)
op and(a: Assertion, b: Assertion)
op negation(a: Assertion)
op get_expression(c: Curve) -> Expression
op perimeter(c: ClosedFigure) -> Number
op intercept(l: Line, a: Axis) -> Number
op projection(p: Point, l: Line) -> Point
op num_intersection(a: Curve, b: Curve) -> Number
op foot_point(l1: LinearObject, l2: LinearObject) -> Point
op point_line_distance(p: Point, l: Line) -> Number
op area(c: ClosedFigure) -> Number
op abs(n: Number) -> Number
op is_intersect(a: Curve, b: Curve)
op is_perpendicular(l1: LinearObject, l2: LinearObject)
op is_parallel(l1: LinearObject, l2: LinearObject)
op point_on_curve(p: Point, c: Curve)
op when_min(n: Number) -> Assertion
op when_max(n: Number) -> Assertion
op range(n: Number) -> Interval
op in(n: Number, i: Interval)
op locate(p: Point) -> Coordinate
op get_focus(c: ConicSection) -> Set[Point]
op vertex(c: ConicSection) -> Set[Point]
op directrix(c: ConicSection) -> Set[Line]
op asymptote(c: Hyperbola) -> Set[Line]
op endpoint(l: LinearObject) -> Set[Point]
op intersection(a: Curve, b: Curve) -> Set[Point]
op ln(n: Number) -> Number
op exp(n: Number) -> Number
op tan(n: Number) -> Number
op sin(n: Number) -> Number
op cos(n: Number) -> Number
op vector_abs(v: Vector) -> Number
op parallel_line_at_distance(l: Line, d: Number) -> Line
op parallel_distance(l1: Line, l2: Line) -> Number
op sympy_expression(s: String) -> Expression
op to_interval(a: Number, a_closed: Assertion, b: Number, b_closed: Assertion) -> Interval
op to_coordinate(x: Number, y: Number) -> Coordinate
op fixed_point(c: Curve) -> Point
op get_a_focus(location: Location, c: ConicSection) -> Point
op get_a_vertex(location: Location, c: ConicSection) -> Point
op get_a_directrix(location: Location, c: ConicSection) -> Line
op get_a_part(location: Location, c: Hyperbola) -> Curve
op get_stdeq_expression(c: Curve) -> Expression
op one_of(s: Set[T]) -> T
```

## 输出要求

1. 只输出一行 KE，不要输出解题过程、解释或 Markdown 代码块。
2. KE 固定写成 `output = { 目标表达式 | 变量与约束 };`，子句之间用 `, ` 分隔，左花括号与 `|` 两侧各留一个空格，行尾保留分号。
3. 题面中的方程用 `sympy_expression("...")` 表示。
4. 只能使用上方本体中的 concepts 与 operators；需要新概念时，用已有算子的组合表达，不得自造算子；不要把两个答案并列塞进同一个 output。

## 示例

### 示例 1

输入：双曲线x^{2}-y^{2}=1的焦距为?

输出：

```
output = { focal_length(C) | C: Hyperbola, get_expression(C) = sympy_expression("Eq(x**2 - y**2, 1)") };
```

### 示例 2

输入：椭圆\frac{x^{2}}{m} + \frac{y^{2}}{4}=1的焦距为2, 则m的值等于?

输出：

```
output = { m | C: Ellipse, m: Real, get_expression(C) = sympy_expression("Eq(x**2/m + y**2/4, 1)"), focal_length(C) = 2 } };
```

### 示例 3

输入：已知为A(0,-1)，当B在曲线y=2 x^{2}+1上，线段A B的中点M的轨迹方程是?

输出：

```
output = { get_expression(locus(M)) | A: Point, locate(A) = to_coordinate(0, -1), C: Curve, get_expression(C) = sympy_expression("Eq(y, 2*x**2 + 1)"), B: Point, point_on_curve(B, C), M: Point, mid_point(line_segment_of(A, B)) = M };
```

### 示例 4

输入：抛物线y^{2}=2 x上点P到直线x-y+3=0上距离最短的点的坐标为?

输出：

```
output = { locate(P) | C: Parabola, get_expression(C) = sympy_expression("Eq(y**2, 2*x)"), l: Line, get_expression(l) = sympy_expression("Eq(x - y + 3, 0)"), P: Point, point_on_curve(P, C), when_min(point_line_distance(P, l)) };
```

### 示例 5

输入：已知方程\frac{x^{2}}{2-k}+\frac{y^{2}}{2 k-1}=1表示焦点在y轴上的椭圆，则实数k的取值范围是?

输出：

```
output = { range(k) | C: Ellipse, k: Real, get_expression(C) = sympy_expression("Eq(x**2/(2 - k) + y**2/(2*k - 1), 1)"), F: Set[Point], get_focus(C) = F, yax: YAxis, point_on_curve(one_of(F), yax) };
```

## 待转换题目

输入：{{题目}}

输出：
