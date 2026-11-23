"""流水查询接口测试（对应 `docs/流水查询接口契约表.md`）。

组织方式与 bill_pay 一致：按测试目的分组，不按接口堆。
    正常路径 → 响应结构 → ID 参数 → 金额语义 → 月份语义 → 类型语义
    → 日期语义 → 横向一致性 → 错误响应契约

一条必须遵守的写法约束：**不要硬编码日期**。

ParaBank 的流水种子数据是相对表达式生成的（`insert.sql:44-60`）：

    CURRENT_DATE - '15' DAY      → 12589
    CURRENT_DATE - '11' DAY      → 13255 / 13477
    CURRENT_DATE - '10' DAY      → 13699
    CURRENT_DATE - '8'  DAY      → 14143
    YEAR(CURRENT_DATE - 1 YEAR)-12-11 → 12145
    YEAR(CURRENT_DATE - 1 YEAR)-12-12 → 12256

每调一次 `initializeDB`，日期就按当天重新算。硬编码 `11-09-2026` 的用例
今天能过、明天就红。所以日期一律从接口返回的 epoch 反推（见 `to_wire_date`），
断言的是「过滤是否生效」这个性质，而不是「某个具体日期返回几条」。

**稳定可硬编码的**：流水 ID（种子写死）、总条数 7、Credit 只有 12145 一条、
12456 零流水、去年 12 月固定有两条。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.transactions

# 接口把 DATE 按 UTC+8 解释，再序列化成 UTC，所以反向还原也得用 UTC+8
CST = timezone(timedelta(hours=8))

ACCOUNT = 12345
ACCOUNT_EMPTY = 12456
CREDIT_ID = 12145
ALL_IDS = {12145, 12256, 12589, 13255, 13477, 13699, 14143}


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def to_wire_date(epoch_ms: int) -> str:
    """epoch 毫秒 → 接口接受的 `MM-dd-yyyy`（按 UTC+8 还原成业务日期）。

    接口返回 `2025-12-10T16:00:00.000Z`，业务上指的是 `2025-12-11`。
    直接用 UTC 日期会差一天，查不到任何东西。
    """
    return datetime.fromtimestamp(epoch_ms / 1000, CST).strftime("%m-%d-%Y")


def month_of(epoch_ms: int) -> int:
    return datetime.fromtimestamp(epoch_ms / 1000, CST).month


@pytest.fixture
def all_txs(tx):
    """当前快照下的全量流水，已按 (date, id) 升序。"""
    resp = tx.list_by_account(ACCOUNT)
    assert resp.status_code == 200, resp.brief()
    return tx.rows(resp)


# --------------------------------------------------------------------------
# 一、正常路径
# --------------------------------------------------------------------------
@pytest.mark.happy
class TestHappyPath:
    def test_list_returns_all_seven(self, tx):
        """基线：账户 12345 恒为 7 条，ID 集合固定。"""
        resp = tx.list_by_account(ACCOUNT)

        assert resp.status_code == 200, resp.brief()
        assert resp.is_json, f"期望 JSON，实际 {resp.content_type}"
        assert tx.ids(resp) == ALL_IDS

    def test_list_sorted_by_date_then_id(self, all_txs):
        """排序契约：ORDER BY date, id —— 不是按 id 排。"""
        keys = [(t["date"], t["id"]) for t in all_txs]
        assert keys == sorted(keys), f"排序不是 (date, id) 升序：{keys}"

    def test_single_transaction_fields(self, tx):
        """单条查询的字段集合与类型。"""
        resp = tx.by_id(CREDIT_ID)

        assert resp.status_code == 200, resp.brief()
        body = resp.json
        assert set(body) == {"id", "accountId", "type", "date", "amount", "description"}
        assert body["id"] == CREDIT_ID
        assert body["accountId"] == ACCOUNT
        assert body["type"] == "Credit"
        assert isinstance(body["date"], int), "date 应是 epoch 毫秒整数"
        assert Decimal(str(body["amount"])) == Decimal("300.00")

    def test_account_without_transactions(self, tx):
        """存在但零流水的账户：200 + 空数组，不是 404。"""
        resp = tx.list_by_account(ACCOUNT_EMPTY)

        assert resp.status_code == 200, resp.brief()
        assert resp.json == []

    def test_amount_exact_match(self, tx):
        """amount 是精确等值匹配，不是范围。"""
        resp = tx.by_amount(ACCOUNT, "300")

        assert resp.status_code == 200, resp.brief()
        assert tx.ids(resp) == {CREDIT_ID}


# --------------------------------------------------------------------------
# 二、响应结构契约
# --------------------------------------------------------------------------
@pytest.mark.contract
class TestResponseStructure:
    def test_single_is_object_not_array(self, tx):
        assert isinstance(tx.by_id(CREDIT_ID).json, dict)

    def test_list_is_array(self, tx):
        assert isinstance(tx.list_by_account(ACCOUNT).json, list)

    def test_json_and_xml_return_same_ids(self, tx):
        """两种内容协商必须指向同一批记录。"""
        as_json = tx.ids(tx.list_by_account(ACCOUNT))

        resp = tx.client.get(f"/accounts/{ACCOUNT}/transactions", headers={"Accept": "application/xml"})
        assert resp.status_code == 200, resp.brief()
        as_xml = {int(i) for i in re.findall(r"<id>(\d+)</id>", resp.text)}

        assert as_json == as_xml, f"JSON {sorted(as_json)} vs XML {sorted(as_xml)}"

    def test_date_is_epoch_milliseconds(self, tx):
        """JSON 的 date 是毫秒时间戳，且与 XML 的 UTC 表示是同一时刻。"""
        body = tx.by_id(CREDIT_ID).json
        assert isinstance(body["date"], int)
        assert 10**12 < body["date"] < 10**13, f"不像毫秒时间戳：{body['date']}"

        xml = tx.client.get(f"/transactions/{CREDIT_ID}", headers={"Accept": "application/xml"})
        m = re.search(r"<date>([^<]+)</date>", xml.text)
        assert m, f"XML 里没有 date 字段：{xml.brief()}"
        parsed = datetime.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

        assert int(parsed.timestamp() * 1000) == body["date"]


# --------------------------------------------------------------------------
# 三、ID 参数（accountId / transactionId）
# --------------------------------------------------------------------------
@pytest.mark.validation
class TestIdParameters:
    def test_account_not_found_returns_400(self, tx):
        """能解析但不存在的账户：400 + text/plain（不是 404）。"""
        resp = tx.list_by_account(0)

        assert resp.status_code == 400, resp.brief()
        assert "Could not find" in resp.text

    @pytest.mark.parametrize("bad", ["abc", "1.5", "2147483648", "1 or 1=1"])
    def test_account_unparseable_returns_404(self, tx, bad):
        """在路由/类型转换层就被挡下：404 + 0 字节，业务代码没执行。"""
        resp = tx.list_by_account(bad)

        assert resp.status_code == 404, resp.brief()
        assert resp.text == "", f"404 不应带 body，实际 {resp.text[:60]!r}"

    def test_transaction_not_found_returns_400(self, tx):
        resp = tx.by_id(0)

        assert resp.status_code == 400, resp.brief()
        assert "Could not find transaction" in resp.text

    @pytest.mark.parametrize("bad", ["abc", "1.5"])
    def test_transaction_unparseable_returns_404(self, tx, bad):
        resp = tx.by_id(bad)

        assert resp.status_code == 404, resp.brief()
        assert resp.text == ""


# --------------------------------------------------------------------------
# 四、金额语义
# --------------------------------------------------------------------------
@pytest.mark.boundary
class TestAmountSemantics:
    def test_zero_matches_zero_amount_transaction(self, tx):
        """amount=0 返回 1 条，不是 0 条 —— 库里真有一条 0.0000 的流水。

        这条极易误判：1 条既可能是「精确命中了零元流水」，也可能是
        「参数被忽略、只返回了第一条」。所以必须断言 ID，不能只断言条数。
        """
        resp = tx.by_amount(ACCOUNT, "0")

        assert resp.status_code == 200, resp.brief()
        assert tx.ids(resp) == {13699}

    @pytest.mark.parametrize("a,b", [("300", "300.00"), ("100", "100.0000")])
    def test_scale_does_not_change_result(self, tx, a, b):
        """数值语义而非字符串匹配：scale 不同但值相同，结果必须一致。"""
        assert tx.ids(tx.by_amount(ACCOUNT, a)) == tx.ids(tx.by_amount(ACCOUNT, b))

    def test_scientific_notation_accepted(self, tx):
        """1e10 被 BigDecimal 接受，是合法输入：200 + 空，不是 404。"""
        resp = tx.by_amount(ACCOUNT, "1e10")

        assert resp.status_code == 200, resp.brief()
        assert resp.json == []

    def test_non_numeric_returns_404(self, tx):
        resp = tx.by_amount(ACCOUNT, "abc")
        assert resp.status_code == 404, resp.brief()


# --------------------------------------------------------------------------
# 五、月份语义
# --------------------------------------------------------------------------
@pytest.mark.validation
class TestMonthParameter:
    def test_all_is_no_filter(self, tx):
        """All 是魔法值：不过滤，结果必须等于全量。"""
        assert tx.ids(tx.by_month_type(ACCOUNT, "All", "All")) == ALL_IDS

    def test_chinese_month_filters_same_month(self, tx, all_txs):
        """用最近一条流水的月份去查：必须查到它，且结果全部属于该月。

        断言的是「过滤生效」这个性质，不是条数 —— 种子日期随当天漂移。
        """
        pick = all_txs[-1]
        month = month_of(pick["date"])

        resp = tx.by_month_type(ACCOUNT, f"{month}月", "All")
        assert resp.status_code == 200, resp.brief()
        assert pick["id"] in tx.ids(resp), f"{month} 月查不到 id={pick['id']}"
        for t in resp.json:
            assert month_of(t["date"]) == month, f"{t['id']} 不属于 {month} 月"

    def test_matches_previous_year_december(self, tx):
        """month 只比 MONTH(date)、忽略年份：去年 12 月的两条必须命中。

        12145/12256 的月份在种子 SQL 里固定为 12，断言不受运行日期影响。
        """
        ids = tx.ids(tx.by_month_type(ACCOUNT, "12月", "All"))

        assert {12145, 12256} <= ids, f"去年 12 月的流水没被匹配到：{sorted(ids)}"

    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.3：month 无法识别时静默返回全量，调用方无法察觉过滤失效",
    )
    def test_english_month_should_not_return_all(self, tx):
        """'Nov' 表达 11 月：认就过滤，不认就报错。唯一不能接受的是静默返回全量。"""
        base = tx.ids(tx.by_month_type(ACCOUNT, "All", "All"))

        resp = tx.by_month_type(ACCOUNT, "Nov", "All")
        assert resp.status_code != 500, resp.brief()
        if resp.status_code == 200:
            assert tx.ids(resp) < base, f"返回了全量 {len(base)} 条，过滤条件被静默丢弃"


# --------------------------------------------------------------------------
# 六、类型语义
# --------------------------------------------------------------------------
@pytest.mark.validation
class TestTypeParameter:
    def test_credit_filters(self, tx):
        assert tx.ids(tx.by_month_type(ACCOUNT, "All", "Credit")) == {CREDIT_ID}

    def test_debit_filters(self, tx):
        assert tx.ids(tx.by_month_type(ACCOUNT, "All", "Debit")) == ALL_IDS - {CREDIT_ID}

    def test_all_is_no_filter(self, tx):
        assert tx.ids(tx.by_month_type(ACCOUNT, "All", "All")) == ALL_IDS

    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.1：type 大小写敏感，CREDIT 直接 500（应 400 或按 Credit 处理）",
    )
    def test_uppercase_should_not_crash(self, tx):
        """非法枚举值不该让服务端崩溃。"""
        resp = tx.by_month_type(ACCOUNT, "All", "CREDIT")

        assert resp.status_code != 500, f"非法枚举值不应导致 500：{resp.brief()}"

    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.2：type 存在 SQL 注入，可绕过 account_id 读出其他账户流水",
    )
    def test_injection_cannot_escape_account(self, tx):
        resp = tx.by_month_type(ACCOUNT, "All", "0' OR '1'='1")

        if resp.status_code == 200:
            leaked = tx.ids(resp) - ALL_IDS
            assert not leaked, f"注入了其他账户的流水 {sorted(leaked)}"
        else:
            assert resp.status_code in (400, 403), resp.brief()


# --------------------------------------------------------------------------
# 七、日期语义
# --------------------------------------------------------------------------
@pytest.mark.boundary
class TestDateParameters:
    def test_on_date_returns_that_day(self, tx, all_txs):
        pick = all_txs[0]
        day = to_wire_date(pick["date"])

        resp = tx.on_date(ACCOUNT, day)
        assert resp.status_code == 200, resp.brief()
        assert pick["id"] in tx.ids(resp), f"{day} 查不到 id={pick['id']}"
        for t in resp.json:
            assert to_wire_date(t["date"]) == day

    def test_on_date_without_data(self, tx):
        """合法但无数据的日期：200 + 空数组。"""
        resp = tx.on_date(ACCOUNT, "01-01-1990")

        assert resp.status_code == 200, resp.brief()
        assert resp.json == []

    def test_range_covering_everything(self, tx, all_txs):
        """从最早到最晚：闭区间，必须覆盖全部 7 条。"""
        lo = to_wire_date(all_txs[0]["date"])
        hi = to_wire_date(all_txs[-1]["date"])

        assert tx.ids(tx.by_date_range(ACCOUNT, lo, hi)) == ALL_IDS

    def test_range_reversed_returns_empty(self, tx, all_txs):
        """from > to：不报错，返回空（区间条件自然不成立）。"""
        lo = to_wire_date(all_txs[0]["date"])
        hi = to_wire_date(all_txs[-1]["date"])

        resp = tx.by_date_range(ACCOUNT, hi, lo)
        assert resp.status_code == 200, resp.brief()
        assert resp.json == []

    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.5：日期未关闭 lenient，非法月日被归一化后参与查询（应 400）",
    )
    def test_invalid_date_rejected(self, tx, all_txs):
        """13 月 45 日不存在，应被拒绝，而不是解析成次年 2 月 14 日。"""
        hi = to_wire_date(all_txs[-1]["date"])

        resp = tx.by_date_range(ACCOUNT, "13-45-2026", hi)
        assert resp.status_code == 400, f"非法日期应 400，实际 {resp.brief()}"

    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.5：ISO 日期被解析成公元 179 年，下界失效后返回超集（应 400）",
    )
    def test_iso_date_rejected(self, tx, all_txs):
        """`2026-11-11` 不是 `MM-dd-yyyy`，应报错而不是静默解析成 179 年。"""
        hi = to_wire_date(all_txs[-1]["date"])

        resp = tx.by_date_range(ACCOUNT, "2026-11-11", hi)
        assert resp.status_code == 400, f"格式错误的日期应 400，实际 {resp.brief()}"


# --------------------------------------------------------------------------
# 八、横向一致性（单看每个都对，横着摆才矛盾）
# --------------------------------------------------------------------------
@pytest.mark.consistency
class TestCrossEndpointConsistency:
    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.4：账户不存在时 5 个接口给出 400 与 200 两种处理",
    )
    def test_account_not_found_consistent(self, tx, all_txs):
        day = to_wire_date(all_txs[0]["date"])

        codes = {
            "/transactions": tx.list_by_account(0).status_code,
            "/amount": tx.by_amount(0, "300").status_code,
            "/month/type": tx.by_month_type(0, "All", "All").status_code,
            "/fromDate/toDate": tx.by_date_range(0, day, day).status_code,
            "/onDate": tx.on_date(0, day).status_code,
        }

        assert len(set(codes.values())) == 1, f"同一错误条件给出多种状态码：{codes}"

    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.3：month 的多种等价写法结果不一致（全量 vs 过滤）",
    )
    def test_month_spellings_consistent(self, tx):
        base = tx.ids(tx.by_month_type(ACCOUNT, "All", "All"))

        for spelling in ("11", "Nov", "nov", "2026-11"):
            resp = tx.by_month_type(ACCOUNT, spelling, "All")
            assert tx.ids(resp) < base, f"month={spelling!r} 返回全量，与中文写法行为不一致"

    @pytest.mark.xfail(
        strict=True,
        reason="契约缺口 6.1：type 大小写行为不一致（Credit=200 / CREDIT=500）",
    )
    def test_type_case_consistent(self, tx):
        lower = tx.by_month_type(ACCOUNT, "All", "Credit")
        upper = tx.by_month_type(ACCOUNT, "All", "CREDIT")

        assert lower.status_code == upper.status_code, (
            f"同一枚举值两种大小写：Credit={lower.status_code} / CREDIT={upper.status_code}"
        )

    def test_amount_forms_consistent(self, tx):
        """`300` 与 `300.00` 是同一个金额，两种写法必须给出同一结果。"""
        assert tx.ids(tx.by_amount(ACCOUNT, "300")) == tx.ids(tx.by_amount(ACCOUNT, "300.00"))


# --------------------------------------------------------------------------
# 九、错误响应契约
# --------------------------------------------------------------------------
@pytest.mark.contract
class TestErrorContract:
    def test_400_is_plain_text(self, tx):
        """业务拒绝 = 400 + text/plain + 人类可读的句子。"""
        resp = tx.list_by_account(0)

        assert resp.status_code == 400, resp.brief()
        assert resp.content_type == "text/plain", f"期望 text/plain，实际 {resp.content_type}"
        assert "Could not find transactions for account #0" in resp.text

    def test_404_has_empty_body(self, tx):
        """路由层拒绝 = 404 + 0 字节，没有 body 也没有 Content-Type。"""
        resp = tx.list_by_account("abc")

        assert resp.status_code == 404, resp.brief()
        assert resp.text == "", f"404 不应带 body，实际 {resp.text[:60]!r}"
        assert resp.content_type == "", f"404 不应带 Content-Type，实际 {resp.content_type}"

    def test_success_is_json(self, tx):
        resp = tx.list_by_account(ACCOUNT)

        assert resp.is_json, f"期望 JSON，实际 {resp.content_type}"
        assert isinstance(resp.json, list)
