# NL2KE 生成结果

## 题目

设椭圆C: \frac{x^{2}}{4}+\frac{y^{2}}{3}=1的右焦点为F，过原点O的动直线l与椭圆C交于A、B两点，那么\triangle A B F的周长的取值范围为?

```
output = { range(perimeter(triangle_of(A, B, F))) | C: Ellipse, get_expression(C) = sympy_expression("Eq(x**2/4 + y**2/3, 1)"), F: Point, get_a_focus(Right, C) = F, O: Point, locate(O) = to_coordinate(0, 0), l: Line, point_on_curve(O, l), A: Point, B: Point, point_on_curve(A, C), point_on_curve(B, C), point_on_curve(A, l), point_on_curve(B, l) };
```

## 题目

F是椭圆C: \frac{x^{2}}{4}+\frac{y^{2}}{3}=1的右焦点，定点A(-1,1), M是椭圆上的动点，则\frac{1}{2}|M A|+|M F|的最小值为?

```
output = { d | C: Ellipse, get_expression(C) = sympy_expression("Eq(x**2/4 + y**2/3, 1)"), F: Point, F = get_a_focus(Right, C), A: Point, locate(A) = to_coordinate(-1, 1), M: Point, point_on_curve(M, C), d: Number, d = length(line_segment_of(M, A))/2 + length(line_segment_of(M, F)), when_min(d) };
```

## 题目

已知双曲线M: \frac{x^{2}}{a^{2}}-\frac{y^{2}}{b^{2}}=1(a>0, b>0)的左、右焦点分别为F_{1}, F_{2},|F_{1} F_{2}|=2 c. 若双曲线M的右支上存在点P，使\frac{a}{\sin \angle P F_{1} F_{2}}=\frac{3 c}{\sin \angle P F_{2} F_{1}}，则双曲线M的离心率的取值范围为?

```
output = { range(eccentricity(M)) | M: Hyperbola, a: Real, b: Real, c: Real, get_expression(M) = sympy_expression("Eq(x**2/a**2 - y**2/b**2, 1)"), F1: Point, F2: Point, get_a_focus(Left, M) = F1, get_a_focus(Right, M) = F2, length(line_segment_of(F1, F2)) = 2*c, P: Point, point_on_curve(P, get_a_part(Right, M)), a / sin(angle_of(P, F1, F2)) = 3*c / sin(angle_of(P, F2, F1)) };
```

## 题目

已知双曲线C: \frac{x^{2}}{a^{2}}-\frac{y^{2}}{b^{2}}=1(a>0, b>0)的左、右焦点分别为F_{1}、F_{2}、P为C的右支上一点，直线P F_{1}与圆x^{2}+y^{2}=a^{2}相切，且|P F_{2}|=|F_{1} F_{2}|，则C的离心率为?

```
output = { eccentricity(C) | C: Hyperbola, a: Real, b: Real, get_expression(C) = sympy_expression("Eq(x**2/a**2 - y**2/b**2, 1)"), F1: Point, F2: Point, get_a_focus(Left, C) = F1, get_a_focus(Right, C) = F2, P: Point, point_on_curve(P, get_a_part(Right, C)), O: Circle, get_expression(O) = sympy_expression("Eq(x**2 + y**2, a**2)"), is_tangent(line_of(P, F1), O), length(line_segment_of(P, F2)) = length(line_segment_of(F1, F2)) };
```

## 题目

设F_{1}、F_{2}为曲线C_{1}: \frac{x^{2}}{6}+\frac{y^{2}}{2}=1的焦点，P是曲线C_{2}: \frac{x^{2}}{3}-y^{2}=1与C_{1}的一个交点，则\cos \angle F_{1} P F_{2}的值是?

```
output = { cos(angle_of(F1, P, F2)) | C1: Ellipse, get_expression(C1) = sympy_expression("Eq(x**2/6 + y**2/2, 1)"), C2: Hyperbola, get_expression(C2) = sympy_expression("Eq(x**2/3 - y**2, 1)"), get_a_focus(Left, C1) = F1, get_a_focus(Right, C1) = F2, intersection(C1, C2) = S, S: Set[Point], one_of(S) = P };
```

## 题目

直线y=2 x+5与曲线\frac{x |x|}{9}+\frac{y^{2}}{25}=1的交点个数为?

```
output = { num_intersection(l, C) | l: Line, get_expression(l) = sympy_expression("Eq(y, 2*x + 5)"), C: Curve, get_expression(C) = sympy_expression("Eq(x*Abs(x)/9 + y**2/25, 1)") };
```

## 题目

已知双曲线\frac{x^{2}}{a^{2}}-\frac{y^{2}}{b^{2}}=1(a>b, b>0)的渐近线方程为y=\pm x，且经过点(\sqrt{2}, 1)，则该双曲线的方程为?

```
output = { get_expression(C) | C: Hyperbola, a: Real, b: Real, P: Point, get_expression(C) = sympy_expression("Eq(x**2/a**2 - y**2/b**2, 1)"), a > b, b > 0, slope(one_of(asymptote(C))) = 1, slope(one_of(asymptote(C))) = -1, locate(P) = to_coordinate(sqrt(2), 1), point_on_curve(P, C) };
```

## 题目

若点A的坐标为(3 , 2) , F为抛物线y^{2}=2 x的焦点，点P在该抛物线上移动，为使得PA+PF取得最小值，则P点的坐标为?

```
output = { locate(P) | A: Point, locate(A) = to_coordinate(3, 2), C: Parabola, get_expression(C) = sympy_expression("Eq(y**2, 2*x)"), F: Point, F = one_of(get_focus(C)), P: Point, point_on_curve(P, C), when_min(length(line_segment_of(P, A)) + length(line_segment_of(P, F))) };
```

## 题目

经过椭圆\frac{x^{2}}{2}+y^{2}=1的左焦点F_{1}作倾斜角为60^{\circ}的直线l，直线l与椭圆相交于A、B两点，则线段A B的长为?

```
output = { length(intercept_chord(l, C)) | C: Ellipse, get_expression(C) = sympy_expression("Eq(x**2/2 + y**2, 1)"), F1: Point, get_a_focus(Left, C) = F1, l: Line, inclination(l) = Pi/3, P: Point, line_of(F1, P) = l };
```

## 题目

已知动圆M与圆C_{1}:(x+5)^{2}+y^{2}=16外切，与圆C_{2}:(x-5)^{2}+y^{2}=16内切，则动圆圆心的轨迹方程为?

```
output = { get_expression(locus(P)) | C1: Circle, C2: Circle, M: Circle, P: Point, get_expression(C1) = sympy_expression("Eq((x + 5)**2 + y**2, 16)"), get_expression(C2) = sympy_expression("Eq((x - 5)**2 + y**2, 16)"), is_out_tangent(M, C1), is_in_tangent(M, C2), center(M) = P };
```

## 题目

点P在曲线C_{3}: \frac{x^{2}}{16}-\frac{y^{2}}{9}=1上，点Q在曲线C_{1}:(x+5)^{2}+y^{2}=1上，点R在曲线C_{2}:(x-5)^{2}+y^{2}=1上，则|P Q|-|P R|最大值是?

```
output = { length(line_segment_of(P, Q)) - length(line_segment_of(P, R)) | C3: Hyperbola, get_expression(C3) = sympy_expression("Eq(x**2/16 - y**2/9, 1)"), C1: Circle, get_expression(C1) = sympy_expression("Eq((x + 5)**2 + y**2, 1)"), C2: Circle, get_expression(C2) = sympy_expression("Eq((x - 5)**2 + y**2, 1)"), P: Point, point_on_curve(P, C3), Q: Point, point_on_curve(Q, C1), R: Point, point_on_curve(R, C2), when_max(length(line_segment_of(P, Q)) - length(line_segment_of(P, R))) };
```

## 题目

设F_{1}、F_{2}分别为双曲线\frac{x^{2}}{a^{2}}-\frac{y^{2}}{b^{2}}=1(a>0, b>0)的左、右焦点若在双曲线左支上存在点P，满足|P F_{1}|=|F_{1} F_{2}|，且F_{1}到直线P F_{2}的距离为\sqrt{7} a，则该双曲线的离心率e=?

```
output = { eccentricity(C) | C: Hyperbola, get_expression(C) = sympy_expression("Eq(x**2/a**2 - y**2/b**2, 1)"), a: Real, b: Real, F1: Point, F2: Point, get_a_focus(Left, C) = F1, get_a_focus(Right, C) = F2, P: Point, point_on_curve(P, get_a_part(Left, C)), length(line_segment_of(P, F1)) = length(line_segment_of(F1, F2)), point_line_distance(F1, line_of(P, F2)) = sympy_expression("sqrt(7)*a") };
```

## 题目

设F_{1}、F_{2}分别为椭圆\frac{x^{2}}{3}+y^{2}=1的左、右焦点:点A、B在椭圆上,若\overrightarrow{F_{1} A}=5 \overrightarrow{F_{2} B},则点A的坐标是?

```
output = { locate(A) | C: Ellipse, get_expression(C) = sympy_expression("Eq(x**2/3 + y**2, 1)"), F1: Point, F1 = get_a_focus(Left, C), F2: Point, F2 = get_a_focus(Right, C), A: Point, point_on_curve(A, C), B: Point, point_on_curve(B, C), x_coordinate(A) - x_coordinate(F1) = 5 * (x_coordinate(B) - x_coordinate(F2)), y_coordinate(A) - y_coordinate(F1) = 5 * (y_coordinate(B) - y_coordinate(F2)) };
```

## 题目

经过椭圆\frac{x^{2}}{2}+y^{2}=1的一个焦点作倾斜角为45^{\circ}的直线l，交椭圆于A、B两点，设O为坐标原点，则\overrightarrow{O A} \cdot \overrightarrow{O B}=?

```
output = { dot_product(vector_of(O, A), vector_of(O, B)) | C: Ellipse, get_expression(C) = sympy_expression("Eq(x**2/2 + y**2, 1)"), F: Point, get_a_focus(Left, C) = F, Q: Point, l: Line, l = line_of(F, Q), slope(l) = 1, A: Point, B: Point, point_on_curve(A, C), point_on_curve(B, C), line_of(A, B) = l, O: Point, locate(O) = to_coordinate(0, 0) };
```

## 题目

已知双曲线E: \frac{x^{2}}{a^{2}}-\frac{y^{2}}{b^{2}}=1(a>0, b>0)的右顶点为A，抛物线C: y^{2}=8 a x的焦点为F. 若在E的渐近线上存在点P，使得\overrightarrow{A P} \perp \overrightarrow{F P}，则双曲线E的离心率的取值范围是?

```
output = { range(eccentricity(E)) | E: Hyperbola, a: Real, b: Real, C: Parabola, get_expression(E) = sympy_expression("Eq(x**2/a**2 - y**2/b**2, 1)"), get_expression(C) = sympy_expression("Eq(y**2, 8*a*x)"), A: Point, get_a_vertex(Right, E) = A, F: Point, F = one_of(get_focus(C)), P: Point, L: Line, L = one_of(asymptote(E)), point_on_curve(P, L), dot_product(vector_of(A, P), vector_of(F, P)) = 0 };
```

## 题目

已知抛物线方程为y^{2}=4 x，直线l的方程为x-y+4=0，在抛物线上有一动点P到y轴的距离为d_{1} , P到直线l的距离为d_{2}，则d_{1}+d_{2}的最小值为?

```
output = { d1 + d2 | C: Parabola, get_expression(C) = sympy_expression("Eq(y**2, 4*x)"), l: Line, get_expression(l) = sympy_expression("Eq(x - y + 4, 0)"), P: Point, point_on_curve(P, C), d1: Number, d1 = abs(x_coordinate(P)), d2: Number, d2 = point_line_distance(P, l), when_min(d1 + d2) };
```

## 题目

若点O和点F分别为双曲线\frac{x^{2}}{3}-y^{2}=1的中心和左焦点，点P为双曲线右支上的任意一点，则\overrightarrow{O P} \cdot \overrightarrow{F P}的取值范围为?

```
output = { range(dot_product(vector_of(O, P), vector_of(F, P))) | C: Hyperbola, get_expression(C) = sympy_expression("Eq(x**2/3 - y**2, 1)"), O: Point, center(C) = O, F: Point, get_a_focus(Left, C) = F, R: Curve, get_a_part(Right, C) = R, P: Point, point_on_curve(P, R) };
```

## 题目

离心率e=\frac{\sqrt{5}}{3} ,一条准线为x=3的椭圆的标准方程是?

```
output = { get_stdeq_expression(C) | C: Ellipse, eccentricity(C) = sqrt(5)/3, l: Line, get_expression(l) = sympy_expression("Eq(x, 3)"), l = one_of(directrix(C)) };
```

## 题目

双曲线C: \frac{y^{2}}{9}-\frac{x^{2}}{16}=1的焦距是?

```
output = { focal_length(C) | C: Hyperbola, get_expression(C) = sympy_expression("Eq(y**2/9 - x**2/16, 1)") };
```

## 题目

设椭圆C: \frac{x^{2}}{2}+y^{2}=1的左焦点为F，直线l: x-y+2=0.动点P在椭圆C上，记点P到直线l的距离为d，则|P F |-d的最大值是?

```
output = { length(line_segment_of(P, F)) - d | C: Ellipse, get_expression(C) = sympy_expression("Eq(x**2/2 + y**2, 1)"), l: Line, get_expression(l) = sympy_expression("Eq(x - y + 2, 0)"), F: Point, get_a_focus(Left, C) = F, P: Point, point_on_curve(P, C), d: Number, d = point_line_distance(P, l), when_max(length(line_segment_of(P, F)) - d) };
```
