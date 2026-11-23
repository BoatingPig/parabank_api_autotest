"""`POST /billpay` 接口测试。

组织方式：按「测试目的」分组，而不是按「接口」堆在一起 ——
正常路径 → 金额边界 → 参数校验 → 请求体校验 → 一致性 → 契约。

标记说明：
    正常情况下应当通过
    xfail(strict=True) = 已确认的缺陷。期望行为写在用例里，当前实现不满足。
    一旦开发修好，这些用例会变成 XPASS 并让流水线变红，提醒你来移除标记。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from data.payee_data import ACCOUNT_CHECKING, ACCOUNT_RICH, payee

pytestmark = pytest.mark.billpay


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _ids(transactions: list) -> set:
    return {t["id"] for t in transactions}


def _new_tx(accounts, account_id, before_ids) -> list:
    """取本次新增的流水。不依赖返回顺序，避免被排序变化坑到。"""
    return [t for t in accounts.transactions(account_id) if t["id"] not in before_ids]


# --------------------------------------------------------------------------
# 一、正常业务路径
# --------------------------------------------------------------------------
@pytest.mark.happy
class TestHappyPath:
    def test_pay_returns_bill_pay_result(self, bill_pay):
        """正常支付：200 + BillPayResult 三个字段回显正确。"""
        resp = bill_pay.pay(ACCOUNT_CHECKING, "1.00", payee())

        assert resp.status_code == 200, resp.brief()
        assert resp.is_json, f"期望 JSON，实际 {resp.content_type}"
        body = resp.json
        assert body["payeeName"] == "Test Payee"
        assert Decimal(str(body["amount"])) == Decimal("1.00")
        assert body["accountId"] == ACCOUNT_CHECKING

    def test_pay_deducts_balance_exactly(self, bill_pay, accounts):
        """核心断言：余额精确减少支付金额。

        这个接口只返回一句结果，不看余额根本判断不了对错 —— 这是「写操作必须回查」的典型。
        """
        before = accounts.balance(ACCOUNT_CHECKING)

        resp = bill_pay.pay(ACCOUNT_CHECKING, "1.00", payee())
        assert resp.status_code == 200, resp.brief()

        after = accounts.balance(ACCOUNT_CHECKING)
        assert after == before - Decimal("1.00"), f"余额不符：{before} -> {after}"

    def test_pay_appends_one_debit_transaction(self, bill_pay, accounts):
        """新增恰好 1 条流水，类型 Debit，描述带收款人名字。"""
        before_ids = _ids(accounts.transactions(ACCOUNT_CHECKING))

        resp = bill_pay.pay(ACCOUNT_CHECKING, "2.50", payee(name="Alice"))
        assert resp.status_code == 200, resp.brief()

        new = _new_tx(accounts, ACCOUNT_CHECKING, before_ids)
        assert len(new) == 1, f"应新增 1 条流水，实际 {len(new)} 条"
        assert new[0]["type"] == "Debit"
        assert Decimal(str(new[0]["amount"])) == Decimal("2.50")
        assert new[0]["description"] == "Bill Payment to Alice"

    @pytest.mark.parametrize("amount,expected", [("0.01", "10.44"), ("0.10", "10.35"), ("10.45", "0.00")])
    def test_pay_boundary_valid_amounts(self, bill_pay, accounts, amount, expected):
        """合法金额边界：最小分、常规、刚好全额。数据驱动。"""
        resp = bill_pay.pay(ACCOUNT_CHECKING, amount, payee())
        assert resp.status_code == 200, resp.brief()
        assert accounts.balance(ACCOUNT_CHECKING) == Decimal(expected)

    def test_payee_name_with_chinese(self, bill_pay, accounts):
        """中文收款人不应乱码（编码问题在中文环境里是高频缺陷）。"""
        before_ids = _ids(accounts.transactions(ACCOUNT_CHECKING))

        resp = bill_pay.pay(ACCOUNT_CHECKING, "1.00", payee(name="张三"))
        assert resp.status_code == 200, resp.brief()
        assert resp.json["payeeName"] == "张三"

        new = _new_tx(accounts, ACCOUNT_CHECKING, before_ids)
        assert new and new[0]["description"] == "Bill Payment to 张三"

    def test_pay_from_another_customer_account(self, bill_pay, accounts):
        """跨账户支付：另一个客户(parasoft)的账户也能付，验证不是只对 john 可用。"""
        before = accounts.balance(ACCOUNT_RICH)
        resp = bill_pay.pay(ACCOUNT_RICH, "1.00", payee())
        assert resp.status_code == 200, resp.brief()
        assert accounts.balance(ACCOUNT_RICH) == before - Decimal("1.00")


# --------------------------------------------------------------------------
# 二、金额边界（缺陷集中区）
# --------------------------------------------------------------------------
@pytest.mark.boundary
class TestAmountBoundary:
    @pytest.mark.xfail(strict=True, reason="BUG-01 零元支付未拦截：期望 400，实际 200 且生成 0 元流水")
    def test_zero_amount_rejected(self, bill_pay):
        resp = bill_pay.pay(ACCOUNT_CHECKING, "0.00", payee())
        assert resp.status_code == 400, f"0 元支付应被拒绝，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-02 负数支付会给账户加钱：期望 400，实际 200 且余额增加")
    def test_negative_amount_rejected(self, bill_pay, accounts):
        before = accounts.balance(ACCOUNT_CHECKING)
        resp = bill_pay.pay(ACCOUNT_CHECKING, "-50.00", payee())
        after = accounts.balance(ACCOUNT_CHECKING)

        assert resp.status_code == 400, (
            f"负数金额应被拒绝，实际 {resp.brief()}，且余额 {before} -> {after}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-03 不校验余额：期望 400（参数 minimumBalance=100），实际 200 且余额变为 -99999988.55",
    )
    def test_amount_exceeds_balance_rejected(self, bill_pay, accounts):
        resp = bill_pay.pay(ACCOUNT_CHECKING, "99999999", payee())
        assert resp.status_code == 400, (
            f"支付额远超余额应被拒绝，实际 {resp.brief()}，余额={accounts.balance(ACCOUNT_CHECKING)}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-04【高危】三位小数支付后账户永久 500：setScale(2) 未指定舍入模式抛 ArithmeticException",
    )
    def test_three_decimal_amount_rejected(self, bill_pay, accounts):
        """金额精度应为 2 位。传 0.001 应返回 400，而不是把账户写坏。"""
        resp = bill_pay.pay(ACCOUNT_CHECKING, "0.001", payee())
        assert resp.status_code == 400, f"三位小数应被拒绝，实际 {resp.brief()}"

        # 更严重的后果：此后该账户连查询都失败
        assert accounts.balance(ACCOUNT_CHECKING) is not None, "支付后账户已无法读取（余额接口 500）"

    @pytest.mark.xfail(strict=True, reason="BUG-05 非数字金额返回 404（应为 400），状态码语义错误")
    def test_non_numeric_amount_returns_400(self, bill_pay):
        resp = bill_pay.pay(ACCOUNT_CHECKING, "abc", payee())
        assert resp.status_code == 400, f"非法金额应返回 400，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-06 接受科学计数法 1e3 并扣除 1000 元，且响应里回显 1E+3")
    def test_scientific_notation_amount_rejected(self, bill_pay, accounts):
        before = accounts.balance(ACCOUNT_CHECKING)
        resp = bill_pay.pay(ACCOUNT_CHECKING, "1e3", payee())
        after = accounts.balance(ACCOUNT_CHECKING)
        assert resp.status_code == 400, f"科学计数法应被拒绝，实际 {resp.brief()}，余额 {before} -> {after}"


# --------------------------------------------------------------------------
# 三、必填参数校验
# --------------------------------------------------------------------------
@pytest.mark.validation
class TestRequiredParams:
    @pytest.mark.xfail(strict=True, reason="BUG-07 缺 accountId 返回 500 + HTML（应 400 + JSON）")
    def test_missing_account_id(self, bill_pay):
        resp = bill_pay.pay_raw(query="amount=1.00", body=payee())
        assert resp.status_code == 400, f"缺必填参数应 400，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-08 缺 amount 返回 500 + HTML（应 400 + JSON）")
    def test_missing_amount(self, bill_pay):
        resp = bill_pay.pay_raw(query=f"accountId={ACCOUNT_CHECKING}", body=payee())
        assert resp.status_code == 400, f"缺必填参数应 400，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-09 账户不存在返回 500（应 404）")
    def test_unknown_account_returns_404(self, bill_pay):
        resp = bill_pay.pay(99999999, "1.00", payee())
        assert resp.status_code == 404, f"账户不存在应 404，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-10 accountId 非数字返回 404（应 400）")
    def test_invalid_account_id_format(self, bill_pay):
        resp = bill_pay.pay("abc", "1.00", payee())
        assert resp.status_code == 400, f"参数类型错误应 400，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-11 负账户号返回 500（应 400）")
    def test_negative_account_id(self, bill_pay):
        resp = bill_pay.pay(-1, "1.00", payee())
        assert resp.status_code in (400, 404), f"非法账户号应 400/404，实际 {resp.brief()}"


# --------------------------------------------------------------------------
# 四、请求体校验
# --------------------------------------------------------------------------
@pytest.mark.validation
class TestPayeeBody:
    def test_no_body_no_content_type_returns_415(self, bill_pay):
        """既没有 body 也没有 Content-Type → 415。这一条实现合理，作为对照组保留。"""
        resp = bill_pay.pay_raw(
            query=f"accountId={ACCOUNT_CHECKING}&amount=1.00", send_body=False, content_type=None
        )
        assert resp.status_code == 415, resp.brief()

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-19 声明了 Content-Type: json 但 body 为空，返回 500（应 400/415）",
    )
    def test_empty_body_with_json_content_type(self, bill_pay):
        """带了 JSON 头却没有 body，这是最容易发生的客户端手误，不该 500。"""
        resp = bill_pay.pay_raw(
            query=f"accountId={ACCOUNT_CHECKING}&amount=1.00", send_body=False, content_type="application/json"
        )
        assert resp.status_code in (400, 415), f"空 body 应 400/415，实际 {resp.brief()}"

    def test_wrong_content_type_returns_415(self, bill_pay):
        """用 form 表单提交而非 JSON，实现正确返回 415。"""
        resp = bill_pay.pay_raw(
            query=f"accountId={ACCOUNT_CHECKING}&amount=1.00",
            body="accountId=1&amount=1",
            content_type="application/x-www-form-urlencoded",
        )
        assert resp.status_code == 415, resp.brief()

    @pytest.mark.xfail(strict=True, reason="BUG-12 空 body {} 支付成功，流水写成 'Bill Payment to null'（脏数据）")
    def test_empty_payee_rejected(self, bill_pay, accounts):
        before_ids = _ids(accounts.transactions(ACCOUNT_CHECKING))
        resp = bill_pay.pay_raw(query=f"accountId={ACCOUNT_CHECKING}&amount=1.00", body={})
        new = _new_tx(accounts, ACCOUNT_CHECKING, before_ids)

        assert resp.status_code == 400, (
            f"空收款人应被拒绝，实际 {resp.brief()}，且写入流水 {[t['description'] for t in new]}"
        )

    @pytest.mark.xfail(strict=True, reason="BUG-13 缺 payee.name 仍支付成功，payeeName 回显 null")
    def test_missing_payee_name_rejected(self, bill_pay):
        body = payee()
        body.pop("name")
        resp = bill_pay.pay_raw(query=f"accountId={ACCOUNT_CHECKING}&amount=1.00", body=body)
        assert resp.status_code == 400, f"缺收款人姓名应 400，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-14 缺 payee.address 仍支付成功（address 为嵌套必填对象）")
    def test_missing_payee_address_rejected(self, bill_pay):
        body = payee()
        body.pop("address")
        resp = bill_pay.pay_raw(query=f"accountId={ACCOUNT_CHECKING}&amount=1.00", body=body)
        assert resp.status_code == 400, f"缺收款人地址应 400，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-15 多余字段导致 500（应忽略未知字段或返回 400），反序列化未做容错")
    def test_unknown_field_does_not_crash(self, bill_pay):
        body = payee()
        body["hacker"] = "1"
        resp = bill_pay.pay_raw(query=f"accountId={ACCOUNT_CHECKING}&amount=1.00", body=body)
        assert resp.status_code in (200, 400), f"未知字段不应导致 500，实际 {resp.brief()}"

    @pytest.mark.xfail(strict=True, reason="BUG-16 非法 JSON 返回 500（应 400），未捕获反序列化异常")
    def test_malformed_json_returns_400(self, bill_pay):
        resp = bill_pay.pay_raw(query=f"accountId={ACCOUNT_CHECKING}&amount=1.00", body="{not json")
        assert resp.status_code == 400, f"非法 JSON 应 400，实际 {resp.brief()}"


# --------------------------------------------------------------------------
# 五、幂等与一致性
# --------------------------------------------------------------------------
@pytest.mark.consistency
class TestIdempotencyAndConsistency:
    @pytest.mark.xfail(strict=True, reason="BUG-17 重复提交无幂等/防重，连续两次扣两次款")
    def test_duplicate_submit_not_double_charged(self, bill_pay, accounts):
        before = accounts.balance(ACCOUNT_CHECKING)
        for _ in range(2):
            bill_pay.pay(ACCOUNT_CHECKING, "1.00", payee())
        after = accounts.balance(ACCOUNT_CHECKING)

        assert after >= before - Decimal("1.00"), f"重复提交被扣两次：{before} -> {after}"

    def test_response_amount_equals_actual_deduction(self, bill_pay, accounts):
        """响应里的 amount 必须等于实际扣款额 —— 对不上就是「界面显示与实际不一致」。"""
        before = accounts.balance(ACCOUNT_CHECKING)
        resp = bill_pay.pay(ACCOUNT_CHECKING, "3.33", payee())
        after = accounts.balance(ACCOUNT_CHECKING)

        assert resp.status_code == 200, resp.brief()
        deducted = before - after
        assert deducted == Decimal(str(resp.json["amount"])), f"扣款 {deducted} != 响应 {resp.json['amount']}"


# --------------------------------------------------------------------------
# 六、契约
# --------------------------------------------------------------------------
@pytest.mark.contract
class TestContract:
    def test_success_response_schema(self, bill_pay):
        """成功响应字段集合与类型符合 openapi.yaml 的 BillPayResult。"""
        body = bill_pay.pay(ACCOUNT_CHECKING, "1.00", payee()).json

        assert set(body) == {"payeeName", "amount", "accountId"}, f"字段不符: {set(body)}"
        assert isinstance(body["payeeName"], str)
        assert isinstance(body["amount"], (int, float))
        assert isinstance(body["accountId"], int)

    @pytest.mark.xfail(strict=True, reason="BUG-18 错误响应是 HTML 错误页而非 JSON（契约未定义错误 schema，实现也不统一）")
    def test_error_response_is_json(self, bill_pay):
        """错误响应应当是机器可解析的 JSON，而不是一坨 HTML。"""
        resp = bill_pay.pay(99999999, "1.00", payee())
        assert resp.is_json, f"错误响应应为 JSON，实际 {resp.content_type}：{resp.brief()}"
