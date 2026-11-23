"""探测用例定义 —— 只放数据，不放任何请求或判定逻辑。

设计原则
--------
1. **一条 Case 只描述「要做什么」**：打哪个接口、传什么参、观察哪些账户。
   不写「期望什么」——期望属于契约表（人工结论），不属于探测（事实采集）。
2. **不做笛卡尔积**。写接口的维度是「业务场景」，不是「参数取值」：
   amount=100 与 amount=200 没有本质差别，但 amount=100 与 amount=超余额 是两回事。
3. **destructive 标记**：会让账户进入不可读状态的 case（精度）必须排在最后，
   且执行完立刻 reset，否则后续所有探测都会因「账户 500」而失效、并被误判成接口挂了。

账户账本（initializeDB 之后的实测值，种子见 insert.sql:4-15）
--------------------------------------------------------------
    12345  客户 12212  -2300.00  7 条流水  CHECKING  ← 负余额
    12456  客户 12212     10.45  0 条流水  CHECKING  ← 小额，天然金额边界
    12900  客户 12212      0.00  2 条流水  CHECKING  ← 唯一的「零余额」样本
    12567  客户 12212    100.00  0 条流水  CHECKING  ← 余额充足的收款方
    13122  客户 12212   1100.00  3 条流水  CHECKING  ← 余额充足，幂等/并发专用（不触发透支）
    13455  客户 12323   2014.76  0 条流水  CHECKING  ← 另一个客户
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

BASE_URL = "http://localhost:8080/parabank/services/bank"

# ---- 账户角色 ----------------------------------------------------------
NEG = 12345          # 负余额，流水最多，适合做对账主场景
SMALL = 12456        # 10.45，金额边界
ZERO = 12900         # 0.00，「余额不足」的唯一干净样本
RICH = 12567         # 100.00，收款方
FUNDED = 13122       # 1100.00，余额充足：幂等/并发用，避免与「透支」维度混杂
OTHER_CUST = 13455   # 客户 12323，跨客户转账
MISSING = 99999      # 不存在的账户


@dataclass(frozen=True)
class Case:
    """一条探测用例。"""

    cid: str                       # 编号，D1 / W3 / T8 / I2
    group: str                     # 分组：deposit / withdraw / transfer / idempotent
    endpoint: str                  # 路径，如 /deposit
    params: dict[str, Any]         # 实际传参（原样，含负号与小数位）
    watch: tuple[int, ...]         # 需要观测余额与流水的账户（去重后）
    note: str                      # 场景说明，写进 CSV 备查
    repeat: int = 1                # 连续发送次数，>1 用于幂等探测
    destructive: bool = False      # True = 执行后账户会不可读，必须排最后并立即 reset


# ---------------------------------------------------------------------------
# A 组 · deposit（7 条）—— 观察账户 SMALL(10.45)
# ---------------------------------------------------------------------------
DEPOSIT = [
    Case("D1", "deposit", "/deposit", {"accountId": SMALL, "amount": 100}, (SMALL,),
         "正常存款 +100"),
    Case("D2", "deposit", "/deposit", {"accountId": SMALL, "amount": -100}, (SMALL,),
         "负数存款（期望拒绝，实际可能反向扣款）"),
    Case("D3", "deposit", "/deposit", {"accountId": SMALL, "amount": 0}, (SMALL,),
         "零元存款（期望拒绝或无流水）"),
    Case("D4", "deposit", "/deposit", {"accountId": SMALL, "amount": 0.001}, (SMALL,),
         "三位小数存款（期望 400）", destructive=True),
    Case("D5", "deposit", "/deposit", {"accountId": SMALL, "amount": 999999999}, (SMALL,),
         "超大金额存款（无上限校验）"),
    Case("D6", "deposit", "/deposit", {"accountId": 0, "amount": 1}, (SMALL,),
         "账户不存在（期望 400）"),
    Case("D7", "deposit", "/deposit", {"accountId": SMALL}, (SMALL,),
         "缺 amount 参数（期望 400）"),
]

# ---------------------------------------------------------------------------
# B 组 · withdraw（7 条）
# ---------------------------------------------------------------------------
WITHDRAW = [
    Case("W1", "withdraw", "/withdraw", {"accountId": SMALL, "amount": 100}, (SMALL,),
         "正常取款 +100"),
    Case("W2", "withdraw", "/withdraw", {"accountId": SMALL, "amount": -100}, (SMALL,),
         "负数取款（期望拒绝，实际可能反向加钱）"),
    Case("W3", "withdraw", "/withdraw", {"accountId": SMALL, "amount": 0}, (SMALL,),
         "零元取款"),
    Case("W4", "withdraw", "/withdraw", {"accountId": SMALL, "amount": 0.001}, (SMALL,),
         "三位小数取款（期望 400）", destructive=True),
    Case("W5", "withdraw", "/withdraw", {"accountId": ZERO, "amount": 100}, (ZERO,),
         "零余额账户取 100（唯一干净的「余额不足」样本）"),
    Case("W6", "withdraw", "/withdraw", {"accountId": 0, "amount": 1}, (ZERO,),
         "账户不存在（期望 400）"),
    Case("W7", "withdraw", "/withdraw", {"accountId": SMALL}, (SMALL,),
         "缺 amount 参数（期望 400）"),
]

# ---------------------------------------------------------------------------
# C 组 · transfer（9 条）
# ---------------------------------------------------------------------------
TRANSFER = [
    Case("T1", "transfer", "/transfer",
         {"fromAccountId": SMALL, "toAccountId": RICH, "amount": 100}, (SMALL, RICH),
         "正常转账 +100（借贷守恒的正向基线）"),
    Case("T2", "transfer", "/transfer",
         {"fromAccountId": SMALL, "toAccountId": RICH, "amount": -100}, (SMALL, RICH),
         "负数转账（期望拒绝，实际可能反向）"),
    Case("T3", "transfer", "/transfer",
         {"fromAccountId": SMALL, "toAccountId": RICH, "amount": 0}, (SMALL, RICH),
         "零元转账"),
    Case("T4", "transfer", "/transfer",
         {"fromAccountId": SMALL, "toAccountId": RICH, "amount": 0.001}, (SMALL, RICH),
         "三位小数转账（期望 400）", destructive=True),
    Case("T5", "transfer", "/transfer",
         {"fromAccountId": ZERO, "toAccountId": RICH, "amount": 100}, (ZERO, RICH),
         "零余额账户转出 100（超余额）"),
    Case("T6", "transfer", "/transfer",
         {"fromAccountId": MISSING, "toAccountId": RICH, "amount": 100}, (RICH,),
         "from 不存在（期望 400 + 整体回滚）"),
    Case("T7", "transfer", "/transfer",
         {"fromAccountId": SMALL, "toAccountId": MISSING, "amount": 100}, (SMALL,),
         "to 不存在（期望 400 + 整体回滚，源账户不应被扣）"),
    Case("T8", "transfer", "/transfer",
         {"fromAccountId": SMALL, "toAccountId": SMALL, "amount": 100}, (SMALL,),
         "自己转自己（期望拒绝或零净额）"),
    Case("T9", "transfer", "/transfer",
         {"fromAccountId": SMALL, "toAccountId": OTHER_CUST, "amount": 100}, (SMALL, OTHER_CUST),
         "跨客户转账（REST 层无鉴权，观察是否拦截）"),
]

# ---------------------------------------------------------------------------
# D 组 · 幂等（2 条）—— repeat=2，同一请求连发两次
# ---------------------------------------------------------------------------
IDEMPOTENT = [
    # 付款方用 FUNDED(1100.00) 而不是 SMALL(10.45)：
    # 两笔 100 全程不透支，Δ=-200 只可能表示「执行了两次」。
    # 若用 SMALL，「第二次因余额不足被拒」也会得到 Δ=-100，看起来像幂等生效 —— 假阴性。
    Case("I1", "idempotent", "/transfer",
         {"fromAccountId": FUNDED, "toAccountId": RICH, "amount": 100}, (FUNDED, RICH),
         "同参数转账连发 2 次（幂等应只生效 1 次，合计扣 100，实际扣 200）", repeat=2),
    Case("I2", "idempotent", "/deposit",
         {"accountId": SMALL, "amount": 100}, (SMALL,),
         "同参数存款连发 2 次（期望合计 +100 或幂等拒绝）", repeat=2),
]

# 执行顺序：非破坏性在前，破坏性在后（后者会毁账户，必须最后跑且跑完立即 reset）
CASES: list[Case] = DEPOSIT + WITHDRAW + TRANSFER + IDEMPOTENT

CONCURRENCY_SETTINGS = [
    # (线程数, 单笔金额, 说明)
    (10, 10, "10 并发 × 10 元"),
    (20, 5, "20 并发 × 5 元"),
    (30, 1, "30 并发 × 1 元"),
]
CONCURRENCY_FROM = SMALL
CONCURRENCY_TO = RICH
