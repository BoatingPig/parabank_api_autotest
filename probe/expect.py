"""语义预期表：回答「这个值应该得到什么结果」。

三份文件的分工
--------------
values.py    试哪些值        （输入侧）
expect.py    什么算对        （判定标准）  ← 本文件
analyze.py   差在哪、算哪类缺陷（判定执行）

五种预期
--------
noop    魔法值，语义就是「不过滤」→ 结果必须与 baseline 完全一致（All）
filter  生效并收窄 → 结果应是 baseline 的真子集
key     键值匹配（amount）→ 结果与 baseline 无关，但每条记录必须命中该值
empty   合法值，库里确实没有匹配 → 200 + 0 条
reject  非法输入 → 必须 4xx

为什么一个值可以写多个预期
--------------------------
`month="Nov"` 到底该死报错，还是该等价于「11月」？文档没说，两种都有道理。
测试能确定的是：**绝不能静默返回全量**。所以写 ("filter", "reject")：
两种都放行，noop 和 500 会被抓出来。
把不确定的部分显式写下来，比猜一个「正确的实现」然后误报一堆要诚实。

为什么 int 的通用预期放行「empty」
----------------------------------
accountId 传 0（不存在但能解析），有的接口给 400、有的给 200+空。
单看一个接口，「不存在就返回空列表」是站得住的设计，不该判错；
但五个接口各唱各的调是契约缺陷 —— 那件事由 analyze 的
CROSS_API_INCONSISTENT 横向检查负责，不在这里重复报。

为什么日期必须按参数位分开写
----------------------------
同一个值 11-11-2026 在三个参数位上语义不同，而且三个都可能是对的：
    onDate=11-11-2026   → 0 条（这一天确实没有交易）
    fromDate=11-11-2026 → 1 条（11-12 那条 ≥ 11-11）
    toDate=11-11-2026   → 4 条（≤ 11-11 的四条）
只用 ("date", 值) 做预期，必然把其中两个判成错的。
"""

from __future__ import annotations

# 注入载荷：这些值如果返回 200 且结果集异常，直接按安全缺陷处理
INJECTION_VALUES = frozenset({"1 or 1=1", "0' OR '1'='1", "'"})


# ----------------------------------------------------------------------
# 第一优先级：按「参数位 + 值」精确匹配
# 只放那些换个参数位语义就变的值（目前只有日期）
# ----------------------------------------------------------------------
EXPECT_BY_PARAM: dict[tuple[str, str], tuple[str, ...]] = {
    # ---- onDate：点查询，只匹配当天 ----
    ("onDate", "11-11-2026"): ("empty",),      # 合法格式，库里这天没交易 → 正确的空
    ("onDate", "01-01-2026"): ("empty",),      # 合法格式，无数据 → 正常的空结果基准
    ("onDate", "2026-11-11"): ("reject",),     # 格式错（lenient 下解析成公元 179 年）
    ("onDate", "13-45-2026"): ("reject",),     # 非法月日
    ("onDate", "02-30-2026"): ("reject",),     # 2 月 30 日不存在
    ("onDate", "00-00-0000"): ("reject",),
    ("onDate", ""): ("reject",),

    # ---- fromDate：下界，结果 = date >= 该日 ----
    ("fromDate", "11-11-2026"): ("filter",),        # 只剩 11-12 那条 → 真子集
    ("fromDate", "01-01-2026"): ("noop", "filter"),  # 下界拉到 1 月，覆盖全部 → 全等也正确
    ("fromDate", "2026-11-11"): ("reject",),
    ("fromDate", "13-45-2026"): ("reject",),
    ("fromDate", "02-30-2026"): ("reject",),
    ("fromDate", "00-00-0000"): ("reject",),
    ("fromDate", ""): ("reject",),

    # ---- toDate：上界，结果 = date <= 该日 ----
    ("toDate", "11-11-2026"): ("filter",),      # 只剩 11-05/11-09×2/11-10 四条
    ("toDate", "01-01-2026"): ("empty",),       # 上界早于所有数据 → 空
    ("toDate", "2026-11-11"): ("reject",),
    ("toDate", "13-45-2026"): ("reject",),
    ("toDate", "02-30-2026"): ("reject",),
    ("toDate", "00-00-0000"): ("reject",),
    ("toDate", ""): ("reject",),
}


# ----------------------------------------------------------------------
# 第二优先级：按「值类别 + 值」匹配（跨参数位复用）
# ----------------------------------------------------------------------
EXPECT: dict[tuple[str, str], tuple[str, ...]] = {
    # int：所有变体都不是合法 ID。要么拒绝，要么返回空，绝不能返回数据
    ("int", "*"): ("reject", "empty"),

    # ---- decimal（amount 是精确等值匹配，不是范围）----
    ("decimal", "0"): ("key",),            # 库里真有一条 0.0000 的流水
    ("decimal", "-100"): ("reject", "empty"),  # 负金额查询：理想是 400，返回空也可接受
    ("decimal", "99"): ("empty",),         # 合法值，无匹配
    ("decimal", "abc"): ("reject",),       # 非数字 → 转换层 404
    ("decimal", "100.0000"): ("key",),     # 精度等价于 100，DECIMAL 比较不受 scale 影响
    ("decimal", "300.00"): ("key",),       # 等价于基线 300，结果应逐条一致
    ("decimal", "1e10"): ("empty",),       # 科学计数法合法，无匹配
    ("decimal", ""): ("reject",),

    # ---- enum（实测合法值首字母大写，传全大写直接 500）----
    ("enum", "Credit"): ("filter",),
    ("enum", "Debit"): ("filter",),
    ("enum", "All"): ("noop",),            # 魔法值：不过滤
    ("enum", "CREDIT"): ("filter", "reject"),  # 大写不该导致 500；过滤或报错都行
    ("enum", "credit"): ("filter", "reject"),
    ("enum", ""): ("reject",),
    ("enum", "'"): ("reject",),            # 单引号：字符串裸拼 SQL 的最强指纹
    ("enum", "0' OR '1'='1"): ("reject",),

    # ---- month（源码用 SimpleDateFormat("MMM")，只认中文缩写）----
    ("month", "11月"): ("filter",),
    ("month", "12月"): ("filter",),
    ("month", "All"): ("noop",),
    # 以下几种写法都表达「11 月」，实现不认。认就过滤，不认就报错，但不能当没传
    ("month", "11"): ("filter", "reject"),
    ("month", "Nov"): ("filter", "reject"),
    ("month", "nov"): ("filter", "reject"),
    ("month", "2026-11"): ("filter", "reject"),
    ("month", ""): ("reject",),
    ("month", "'"): ("reject",),
}


def expect_for(param: str, kind: str, value: str) -> tuple[str, ...] | None:
    """三级查找：参数位精确 → 值类别精确 → 值类别通配。都找不到返回 None。

    返回 None 不是「通过」，而是「预期缺失」，analyze 会把它报成 NO_EXPECTATION，
    让它显式地浮出来 —— 静默通过是最危险的，你永远不知道有多少条没被判过。
    """
    for key in ((param, value), (kind, value), (kind, "*")):
        hit = EXPECT_BY_PARAM.get(key) or EXPECT.get(key)
        if hit:
            return hit
    return None


# ----------------------------------------------------------------------
# 等价组：语义相同、写法不同，结果必须一致
# 这是唯一不需要「预期」就能判定缺陷的检查 —— 同一个语义两种结果，
# 必然至少有一个是错的，跟 PRD 怎么写无关。
# ----------------------------------------------------------------------
EQUIV_GROUPS = [
    {
        "name": "amount 精度等价（300 vs 300.00）",
        "endpoint": "tx_by_account_amount",
        "param": "amount",
        "values": ["300", "300.00"],
    },
    {
        "name": "type 大小写（Credit vs credit vs CREDIT）",
        "endpoint": "tx_by_account_month_type",
        "param": "type",
        "values": ["Credit", "credit", "CREDIT"],
    },
    {
        "name": "month 语言与格式（11月 vs 11 vs Nov vs nov vs 2026-11）",
        "endpoint": "tx_by_account_month_type",
        "param": "month",
        "values": ["11月", "11", "Nov", "nov", "2026-11"],
    },
    {
        "name": "不存在的账户（0 vs -1 vs 1）",
        "endpoint": "tx_by_account",
        "param": "accountId",
        "values": ["0", "-1", "1"],
    },
]
