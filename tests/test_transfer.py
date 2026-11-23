"""资金类接口测试（对应 `docs/资金类接口契约表.md`）。

和流水查询最大的区别：**响应体给不了判据**。
`transfer` 只回一句 `Successfully transferred $100 from account #13122 to account #12567`，
没有交易号、没有余额、没有状态字段。所以每条用例都必须「写后回查」：

    操作前 = 余额 + 流水  →  发请求  →  操作后 = 余额 + 流水

判定用的是**增量**，不是绝对值：
    Δ余额      after - before
    新增流水   流水 id 集合差 —— 不能用条数差，条数差说不出「新增的是哪一条」

三条不变量（契约表 §3.4）：
    ① 借贷守恒   ΔA + ΔC == 0
    ② 扣款到位   ΔA == -amount × 次数
    ③ 流水对账   新增流水金额之和 == |ΔA|

分组按「同一根因一个类」，不按接口堆。`G1`–`G10` 是契约表第七节的缺口编号。

**并发那两条默认不跑**（`pytest.ini` 里 `-m "not concurrency"`）：
它会改数据、必须串行、结果还不稳定（每次丢失量不同），不适合当日常回归。
需要时显式 `python -m pytest tests/test_transfer.py -m concurrency`。

账户角色见契约表 §2。用哪个账户是这个文件里最容易被忽略的决策：
`ZERO`（0.00）是唯一能干净演「余额不足」的样本，`FUNDED`（1100.00）
是唯一能保证「全程不透支」的付款方 —— 混用会让结论出现第二种解释。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

import pytest
import requests

pytestmark = pytest.mark.transfer

# ---- 账户角色（契约表 §2，余额是种子固定值，每次 initializeDB 复原）----
SMALL = 12456       # 10.45    金额边界样本
ZERO = 12900        # 0.00     唯一的「余额不足」干净样本
RICH = 12567        # 100.00   常规收款方
FUNDED = 13122      # 1100.00  余额充足：幂等 / 并发专用，全程不透支
CROSS = 13455       # 2014.76  属于客户 12323，与 12212 不同客户
SAVINGS = 12678     # -100.00  账户类型 SAVINGS，用于跨类型转账
MISSING = 99999     # 不存在的账户

ZERO_D = Decimal("0")
HUNDRED = Decimal("100")


# --------------------------------------------------------------------------
# 观测工具：把「写后回查」这件事收敛成两个函数
# --------------------------------------------------------------------------
def fmt(value: Optional[Decimal]) -> str:
    return "N/A" if value is None else f"{value:+.2f}"


@dataclass
class Change:
    """一个账户在一次操作前后的变化。"""

    before_bal: Optional[Decimal]
    after_bal: Optional[Decimal]
    new: list = field(default_factory=list)

    @property
    def d_bal(self) -> Optional[Decimal]:
        if self.before_bal is None or self.after_bal is None:
            return None
        return self.after_bal - self.before_bal

    @property
    def corrupted(self) -> bool:
        """写之前能读、写之后读不到 —— 账户被这次写操作打坏了。"""
        return self.before_bal is not None and self.after_bal is None

    @property
    def tx_sum(self) -> Decimal:
        return sum((Decimal(str(t["amount"])) for t in self.new), ZERO_D)

    @property
    def tx_types(self) -> list:
        return [t["type"] for t in self.new]

    def __str__(self) -> str:
        return (
            f"Δ={fmt(self.d_bal)} 余额 {fmt(self.before_bal)}→{fmt(self.after_bal)} "
            f"新增{len(self.new)}条{self.tx_types}"
        )


def state(accounts, *account_ids) -> dict:
    return {aid: (accounts.balance(aid), accounts.transactions(aid)) for aid in account_ids}


def delta(before: dict, after: dict, account_id) -> Change:
    b_bal, b_txs = before[account_id]
    a_bal, a_txs = after[account_id]
    old_ids = {t["id"] for t in b_txs}
    return Change(b_bal, a_bal, [t for t in a_txs if t["id"] not in old_ids])


def observe(accounts, action: Callable[[], object], *account_ids):
    """操作前后各抓一次状态，返回 (响应, {账户: Change})。

    写成函数而不是夹具，是因为「操作」必须夹在两次观测之间 ——
    夹具做不到把被测动作插进自己内部。
    """
    before = state(accounts, *account_ids)
    resp = action()
    after = state(accounts, *account_ids)
    return resp, {aid: delta(before, after, aid) for aid in account_ids}


# --------------------------------------------------------------------------
# 一、正常路径：先证明「基准行为是对的」，后面才有参照系
# --------------------------------------------------------------------------
@pytest.mark.happy
class TestHappyPath:
    def test_deposit_adds_balance_and_credit_tx(self, bank, accounts):
        resp, d = observe(accounts, lambda: bank.deposit(SMALL, 100), SMALL)
        c = d[SMALL]

        assert resp.status_code == 200, resp.brief()
        assert c.d_bal == HUNDRED, f"存款后余额应 +100，实际 {c}"
        assert len(c.new) == 1, f"应新增 1 条流水，实际 {len(c.new)} 条"
        assert c.tx_types == ["Credit"], f"存款应记 Credit，实际 {c.tx_types}"
        assert c.tx_sum == HUNDRED, f"流水金额应为 100，实际 {c.tx_sum}"
        assert c.new[0]["description"] == "Deposit via Web Service"

    def test_withdraw_subtracts_balance_and_debit_tx(self, bank, accounts):
        resp, d = observe(accounts, lambda: bank.withdraw(FUNDED, 100), FUNDED)
        c = d[FUNDED]

        assert resp.status_code == 200, resp.brief()
        assert c.d_bal == -HUNDRED, f"取款后余额应 -100，实际 {c}"
        assert len(c.new) == 1
        assert c.tx_types == ["Debit"], f"取款应记 Debit，实际 {c.tx_types}"
        assert c.new[0]["description"] == "Withdraw via Web Service"

    def test_transfer_moves_money_on_both_sides(self, bank, accounts):
        resp, d = observe(accounts, lambda: bank.transfer(FUNDED, RICH, 100), FUNDED, RICH)

        assert resp.status_code == 200, resp.brief()
        assert d[FUNDED].d_bal == -HUNDRED, f"付款方 {d[FUNDED]}"
        assert d[RICH].d_bal == HUNDRED, f"收款方 {d[RICH]}"
        assert len(d[FUNDED].new) == 1 and len(d[RICH].new) == 1
        assert d[FUNDED].tx_types == ["Debit"]
        assert d[RICH].tx_types == ["Credit"]
        assert d[FUNDED].new[0]["description"] == "Funds Transfer Sent"
        assert d[RICH].new[0]["description"] == "Funds Transfer Received"

    def test_transfer_conserves_total(self, bank, accounts):
        """不变量 ①：借贷守恒。不需要知道余额该是多少，只需知道和不可能不为 0。"""
        _, d = observe(accounts, lambda: bank.transfer(FUNDED, RICH, 100), FUNDED, RICH)

        total = d[FUNDED].d_bal + d[RICH].d_bal
        assert total == ZERO_D, f"不守恒：{fmt(d[FUNDED].d_bal)} + {fmt(d[RICH].d_bal)} = {fmt(total)}"

    def test_transfer_tx_amounts_match_delta(self, bank, accounts):
        """不变量 ③：流水对账。新增流水金额之和应等于余额变动量。"""
        _, d = observe(accounts, lambda: bank.transfer(FUNDED, RICH, 100), FUNDED, RICH)

        assert d[FUNDED].tx_sum == abs(d[FUNDED].d_bal), (
            f"付款方流水合计 {fmt(d[FUNDED].tx_sum)} ≠ |Δ| {fmt(abs(d[FUNDED].d_bal))}"
        )
        assert d[RICH].tx_sum == d[RICH].d_bal

    def test_deposit_to_zero_balance_account(self, bank, accounts):
        """零余额账户能收款 —— 这是 ZERO 账户唯一的正向用法。"""
        resp, d = observe(accounts, lambda: bank.deposit(ZERO, 100), ZERO)

        assert resp.status_code == 200, resp.brief()
        assert d[ZERO].d_bal == HUNDRED
        assert d[ZERO].after_bal == HUNDRED, f"0.00 + 100 应为 100.00，实际 {fmt(d[ZERO].after_bal)}"

    def test_transfer_across_account_types(self, bank, accounts):
        """CHECKING → SAVINGS 应当可行，账户类型不构成限制。"""
        resp, d = observe(accounts, lambda: bank.transfer(FUNDED, SAVINGS, 100), FUNDED, SAVINGS)

        assert resp.status_code == 200, resp.brief()
        assert d[FUNDED].d_bal == -HUNDRED
        assert d[SAVINGS].d_bal == HUNDRED


# --------------------------------------------------------------------------
# 二、响应契约：写接口的响应体几乎没有信息量，这本身要固化成事实
# --------------------------------------------------------------------------
@pytest.mark.contract
class TestResponseContract:
    def test_success_body_is_a_plain_sentence(self, bank):
        resp = bank.deposit(SMALL, 1)

        assert resp.status_code == 200, resp.brief()
        assert resp.text.startswith("Successfully"), f"成功文案变了：{resp.text!r}"

    @pytest.mark.xfail(strict=True, reason="G9：Content-Type 声明 application/json，body 却是裸文本")
    def test_success_declares_json_but_is_not_json(self, bank):
        resp = bank.deposit(SMALL, 1)

        assert resp.is_json, f"声明类型变了：{resp.content_type}"
        assert resp.json is not None, f"声明 JSON 却解析失败：body={resp.text!r}"

    def test_error_400_is_plain_text_with_account_id(self, bank):
        """业务层拒绝用 text/plain + 一句话，不是 JSON 错误对象。"""
        resp = bank.deposit_raw({"accountId": 0, "amount": 1})

        assert resp.status_code == 400, resp.brief()
        assert resp.content_type == "text/plain", f"期望 text/plain，实际 {resp.content_type}"
        assert "Could not find account number 0" in resp.text
        assert resp.json is None

    def test_error_404_has_empty_body(self, bank):
        """框架层类型转换失败 —— 没进 Java，连错误消息都没有。"""
        resp = bank.transfer_raw({"fromAccountId": "abc", "toAccountId": RICH, "amount": 100})

        assert resp.status_code == 404, resp.brief()
        assert resp.text == "", f"404 应无 body，实际 {resp.text[:80]!r}"


# --------------------------------------------------------------------------
# 三、参数校验：错误分层（404 / 400 / 500 三条不同路径）
# --------------------------------------------------------------------------
@pytest.mark.validation
class TestParameterValidation:
    @pytest.mark.parametrize("bad_value", ["abc", "1.5", "2147483648", "1 or 1=1"])
    def test_unparseable_account_id_returns_404(self, bank, bad_value):
        resp = bank.deposit_raw({"accountId": bad_value, "amount": 1})

        assert resp.status_code == 404, f"accountId={bad_value!r} → {resp.brief()}"
        assert resp.text == ""

    @pytest.mark.parametrize("bad_value", ["abc", "1 2", "12a", "1,5", "--1", "NaN"])
    def test_unparseable_amount_returns_404(self, bank, bad_value):
        """注意 `1.5` 不在此列 —— amount 是 BigDecimal，一位小数完全合法。

        这里和流水查询不同：那边的 `amount` 是 int，`1.5` 非法；
        这边是 decimal，只有「根本解析不出数」才 404。
        """
        resp = bank.deposit_raw({"accountId": SMALL, "amount": bad_value})

        assert resp.status_code == 404, f"amount={bad_value!r} → {resp.brief()}"

    def test_amount_accepts_scientific_notation(self, bank, accounts):
        r"""`1e3` 被解析成 1000 —— 金额格式比直觉宽松，客户端不该假设只有 `\d+(\.\d+)?`。"""
        resp, d = observe(accounts, lambda: bank.deposit(SMALL, "1e3"), SMALL)

        assert resp.status_code == 200, resp.brief()
        assert d[SMALL].d_bal == Decimal("1000"), f"{d[SMALL]}"

    def test_nonexistent_account_returns_400(self, bank):
        """能解析、但记录不存在 —— 进了业务层才被拒，和 404 是两条路径。"""
        resp = bank.deposit_raw({"accountId": MISSING, "amount": 1})

        assert resp.status_code == 400, resp.brief()
        assert str(MISSING) in resp.text, f"错误消息应指出账号：{resp.text!r}"

    @pytest.mark.parametrize(
        "endpoint",
        ["deposit", "withdraw", "transfer"],
    )
    @pytest.mark.xfail(strict=True, reason="G8：缺 amount 抛 NPE，500 而非 400")
    def test_missing_amount_returns_500(self, bank, endpoint):
        params = {"accountId": SMALL} if endpoint != "transfer" else {
            "fromAccountId": FUNDED, "toAccountId": RICH
        }
        resp = getattr(bank, f"{endpoint}_raw")(params)

        assert resp.status_code == 400, f"缺 amount 应答 400，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="G8：amount= 空串同样 500，校验无统一入口")
    def test_empty_amount_returns_500(self, bank):
        resp = bank.deposit_raw({"accountId": SMALL, "amount": ""})

        assert resp.status_code == 400, f"amount 为空应答 400，实际 {resp.brief()}"


# --------------------------------------------------------------------------
# 四、金额语义：符号 / 零 / 精度 / 量级 / 透支
# --------------------------------------------------------------------------
@pytest.mark.boundary
class TestAmountSemantics:
    @pytest.mark.xfail(strict=True, reason="G1：负数存款变成扣款（余额 -100，流水 type 仍为 Credit）")
    def test_deposit_negative_reverses_direction(self, bank, accounts):
        """G1：存款传负数，实际变成扣款。"""
        resp, d = observe(accounts, lambda: bank.deposit(SMALL, -100), SMALL)
        c = d[SMALL]

        assert resp.status_code == 400 or c.d_bal == ZERO_D, f"负数存款被接受并生效：{c}"

    @pytest.mark.xfail(strict=True, reason="G1：负数取款变成加钱（余额 +100，流水 type 仍为 Debit）")
    def test_withdraw_negative_reverses_direction(self, bank, accounts):
        """G1：取款传负数，实际变成加钱。"""
        resp, d = observe(accounts, lambda: bank.withdraw(SMALL, -100), SMALL)
        c = d[SMALL]

        assert resp.status_code == 400 or c.d_bal == ZERO_D, f"负数取款被接受并生效：{c}"

    @pytest.mark.xfail(strict=True, reason="G1：负数转账方向反转，钱从收款方流向付款方")
    def test_transfer_negative_reverses_direction(self, bank, accounts):
        """G1：转账传负数，钱从收款方流向付款方。"""
        resp, d = observe(accounts, lambda: bank.transfer(FUNDED, RICH, -100), FUNDED, RICH)

        assert resp.status_code == 400 or d[FUNDED].d_bal <= ZERO_D, (
            f"负数转账：付款方 {fmt(d[FUNDED].d_bal)}、收款方 {fmt(d[RICH].d_bal)} —— 方向反转"
        )

    @pytest.mark.parametrize("endpoint, watchers", [
        ("deposit", (SMALL,)),
        ("withdraw", (FUNDED,)),
        ("transfer", (FUNDED, RICH)),
    ])
    @pytest.mark.xfail(strict=True, reason="G2：零金额仍生成流水（余额不动，流水 +1）")
    def test_zero_amount_creates_no_transaction(self, bank, accounts, endpoint, watchers):
        actions = {
            "deposit": lambda: bank.deposit(SMALL, 0),
            "withdraw": lambda: bank.withdraw(FUNDED, 0),
            "transfer": lambda: bank.transfer(FUNDED, RICH, 0),
        }
        resp, d = observe(accounts, actions[endpoint], *watchers)
        total_new = sum(len(c.new) for c in d.values())

        assert resp.status_code == 400 or total_new == 0, (
            f"零金额产生了 {total_new} 条流水：" + "；".join(str(c) for c in d.values())
        )

    @pytest.mark.xfail(strict=True, reason="G3（P0）：3 位小数让账户永久 500，只有 initializeDB 能救")
    def test_three_decimals_corrupts_account(self, bank, accounts):
        """写路径不校验精度，读路径 `setScale(2)` 无 RoundingMode → 之后每次读都抛异常。"""
        resp, d = observe(accounts, lambda: bank.deposit(SMALL, "0.001"), SMALL)
        c = d[SMALL]

        assert resp.status_code == 400 or not c.corrupted, (
            f"0.001 存款返回 {resp.status_code}，之后账户读不到（余额 {fmt(c.after_bal)}）"
        )

    def test_huge_amount_has_no_upper_bound(self, bank, accounts):
        """G4 的事实固化：系统对金额没有上限校验。

        这条断言的是**现状**而不是「正确行为」—— 哪天加了风控，
        这里会变红，届时应改成「超限被拒」的预期。
        """
        resp, d = observe(accounts, lambda: bank.deposit(SMALL, 999_999_999), SMALL)

        assert resp.status_code == 200, resp.brief()
        assert d[SMALL].d_bal == Decimal("999999999"), f"{d[SMALL]}"

    @pytest.mark.parametrize("endpoint", ["withdraw", "transfer"])
    @pytest.mark.xfail(strict=True, reason="G5：允许无限透支，零余额可转出 100")
    def test_overdraft_from_zero_balance(self, bank, accounts, endpoint):
        actions = {
            "withdraw": lambda: bank.withdraw(ZERO, 100),
            "transfer": lambda: bank.transfer(ZERO, RICH, 100),
        }
        resp, d = observe(accounts, actions[endpoint], ZERO)

        assert resp.status_code == 400 or d[ZERO].d_bal == ZERO_D, (
            f"零余额账户透支成功：{d[ZERO]}"
        )


# --------------------------------------------------------------------------
# 五、转账的原子性与特化场景
# --------------------------------------------------------------------------
@pytest.mark.consistency
class TestTransferAtomicity:
    def test_transfer_to_missing_account_rolls_back(self, bank, accounts):
        """唯一的好消息：AOP 事务有效，扣款被撤销。

        这条是正向结论，也是「不需要再测原子性」的依据 ——
        该测的是事务管不到的东西：并发和幂等。
        """
        resp, d = observe(accounts, lambda: bank.transfer(FUNDED, MISSING, 100), FUNDED)

        assert resp.status_code == 400, resp.brief()
        assert d[FUNDED].d_bal == ZERO_D, f"未回滚：付款方 {d[FUNDED]}"
        assert d[FUNDED].new == [], f"未回滚：新增 {len(d[FUNDED].new)} 条流水"

    def test_transfer_from_missing_account_rolls_back(self, bank, accounts):
        resp, d = observe(accounts, lambda: bank.transfer(MISSING, RICH, 100), RICH)

        assert resp.status_code == 400, resp.brief()
        assert d[RICH].d_bal == ZERO_D, f"未回滚：收款方 {d[RICH]}"

    @pytest.mark.xfail(strict=True, reason="G7：自己转自己生成 2 条净零流水")
    def test_transfer_to_self_creates_no_transaction(self, bank, accounts):
        resp, d = observe(accounts, lambda: bank.transfer(SMALL, SMALL, 100), SMALL)
        c = d[SMALL]

        assert resp.status_code == 400 or c.new == [], (
            f"自己转自己：余额 {fmt(c.d_bal)}、新增 {len(c.new)} 条流水 "
            f"{[(t['type'], t['amount']) for t in c.new]}"
        )

    @pytest.mark.xfail(strict=True, reason="G10：跨客户转账无授权校验（契约空白，先问预期）")
    def test_transfer_cross_customer_is_rejected(self, bank, accounts):
        """客户 12212 向客户 12323 转账。当前放行且记账。"""
        resp, d = observe(accounts, lambda: bank.transfer(FUNDED, CROSS, 100), FUNDED, CROSS)

        assert resp.status_code == 403 or d[FUNDED].d_bal == ZERO_D, (
            f"跨客户转账未被拒绝：付款方 {fmt(d[FUNDED].d_bal)}、收款方 {fmt(d[CROSS].d_bal)}"
        )

    @pytest.mark.xfail(strict=True, reason="G3（P0）：双方账户同时被打坏")
    def test_three_decimals_corrupts_both_accounts(self, bank, accounts):
        resp, d = observe(accounts, lambda: bank.transfer(FUNDED, RICH, "0.001"), FUNDED, RICH)

        assert not (d[FUNDED].corrupted or d[RICH].corrupted), (
            f"0.001 转账后读不到：付款方 {fmt(d[FUNDED].after_bal)}、"
            f"收款方 {fmt(d[RICH].after_bal)}"
        )


# --------------------------------------------------------------------------
# 六、幂等：串行重放，与并发是两回事
# --------------------------------------------------------------------------
@pytest.mark.consistency
class TestIdempotency:
    """同一请求重放 N 次，净效果应当等于执行 1 次。

    这里衡量的是**串行**重放（做了两次），不是并发（两次只算一次）——
    方向相反，根因也不同（缺幂等键 vs 缺锁），绝不能合并成一个缺陷。

    POST 在 HTTP 语义里不承诺幂等，所以这是**业务要求**而非协议要求。
    判定它算不算缺陷，看的是「客户端超时后能否安全重试」：
    该接口的响应没有交易号、没有流水 ID，事后无从证明哪笔是重复的 → 不能。
    """

    @pytest.mark.xfail(strict=True, reason="G6：无幂等键，同参数转账两次扣两次")
    def test_transfer_repeated_deducts_once(self, bank, accounts):
        before = state(accounts, FUNDED)
        bank.transfer(FUNDED, RICH, 100)
        bank.transfer(FUNDED, RICH, 100)
        c = delta(before, state(accounts, FUNDED), FUNDED)

        assert c.d_bal == -HUNDRED, (
            f"同参数转账两次共扣 {fmt(c.d_bal)}、新增 {len(c.new)} 条流水"
        )

    @pytest.mark.xfail(strict=True, reason="G6：存款同样无幂等，连发两次加两次")
    def test_deposit_repeated_adds_once(self, bank, accounts):
        before = state(accounts, SMALL)
        bank.deposit(SMALL, 100)
        bank.deposit(SMALL, 100)
        c = delta(before, state(accounts, SMALL), SMALL)

        assert c.d_bal == HUNDRED, f"同参数存款两次共加 {fmt(c.d_bal)}"


# --------------------------------------------------------------------------
# 七、并发：事务保证原子性，但不保证并发正确性
# --------------------------------------------------------------------------
def fire_concurrently(url: str, params: dict, n: int) -> list:
    """n 个线程各自独立建连，在 Barrier 处对齐后同时发出 POST。

    不能用 HttpClient / requests.Session：Session 不是线程安全的，
    多线程抢同一个连接池会在**客户端**排队加锁，
    测到的就变成自己的锁，而不是服务端的竞态。
    """
    barrier = threading.Barrier(n)
    codes: list = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=30)
        resp = requests.post(url, params=params, timeout=30)
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return codes


@pytest.mark.concurrency
class TestConcurrency:
    """默认不跑（`-m "not concurrency"`）。

    付款方用 FUNDED（1100.00）而不是 SMALL（10.45）：10 × 10 元全程不透支，
    `ΔA = -100` 之外没有第二种解释。探测阶段用的是 SMALL，三组都在透支，
    「余额不足」在理论上是一个潜在的解释项。
    """

    N = 10
    AMOUNT = 10

    def _run(self, bank, accounts, base_url):
        return observe(
            accounts,
            lambda: fire_concurrently(
                f"{base_url}/transfer",
                {"fromAccountId": FUNDED, "toAccountId": RICH, "amount": self.AMOUNT},
                self.N,
            ),
            FUNDED,
            RICH,
        )

    # strict=False：竞态丢失量随机（同场景两次跑得 -50 / -40），
    # 万一某次没丢，XPASS 不该算作「用例坏了」。
    @pytest.mark.xfail(strict=False, reason="丢失更新：10 并发只扣了一部分，钱凭空多出")
    def test_concurrent_transfer_loses_update(self, bank, accounts, base_url):
        codes, d = self._run(bank, accounts, base_url)
        expect = Decimal(-self.AMOUNT * self.N)

        assert all(c == 200 for c in codes), f"并发中有请求失败：{codes}"
        assert d[FUNDED].d_bal == expect, (
            f"应扣 {fmt(expect)}，实扣 {fmt(d[FUNDED].d_bal)}；"
            f"收款方 {fmt(d[RICH].d_bal)}；"
            f"凭空多出 {fmt(-(d[FUNDED].d_bal + d[RICH].d_bal))}"
        )

    @pytest.mark.xfail(strict=False, reason="流水条数正确但余额错 —— 只比条数的对账会全部通过")
    def test_concurrent_tx_count_correct_but_balance_lost(self, bank, accounts, base_url):
        """这条是整个并发组里最有价值的论点。

        流水 10 条全对（金额、方向都对），只有余额错。
        所以一致性校验必须比对「余额变动量 = 流水金额之和」，
        只比条数会全部通过 —— 和查询模块「count vs ids 两条独立路径」同一个思想。
        """
        _, d = self._run(bank, accounts, base_url)

        assert len(d[FUNDED].new) == self.N, f"付款方流水应为 {self.N} 条，实际 {len(d[FUNDED].new)}"
        assert len(d[RICH].new) == self.N, f"收款方流水应为 {self.N} 条，实际 {len(d[RICH].new)}"
        assert d[FUNDED].tx_sum == Decimal(self.AMOUNT * self.N)
        assert d[FUNDED].d_bal == Decimal(-self.AMOUNT * self.N), (
            f"流水合计 {fmt(d[FUNDED].tx_sum)} 与余额变动 {fmt(d[FUNDED].d_bal)} 不一致"
        )
