"""流水查询 6 个接口的封装。

只负责拼 URL 和发请求，不解释结果 —— 判定全部留在用例里。

两个细节：
1. **所有路径参数都 quote**。`month` 是中文（`11月`）、日期是 `MM-dd-yyyy`、
   注入载荷带单引号和空格，直接拼进 URL 会让 requests 的编码行为变得不确定。
   显式 `safe=""` 也能保证空值仍然产生 `//`，从而复现「双斜杠 → 404」这个路由行为。
2. **不在这里断言**。同一个接口在正常/异常场景下的期望完全不同，
   封装层一旦开始判断，用例就没法表达「这里本来就该失败」。
"""
from __future__ import annotations

from urllib.parse import quote

from .http_client import HttpClient, Resp


def _q(value) -> str:
    return quote(str(value), safe="")


class TransactionApi:
    def __init__(self, client: HttpClient):
        self.client = client

    # ---- 6 个接口 ----
    def by_id(self, transaction_id) -> Resp:
        return self.client.get(f"/transactions/{_q(transaction_id)}")

    def list_by_account(self, account_id) -> Resp:
        return self.client.get(f"/accounts/{_q(account_id)}/transactions")

    def by_amount(self, account_id, amount) -> Resp:
        return self.client.get(f"/accounts/{_q(account_id)}/transactions/amount/{_q(amount)}")

    def by_month_type(self, account_id, month, transaction_type) -> Resp:
        return self.client.get(
            f"/accounts/{_q(account_id)}/transactions/month/{_q(month)}/type/{_q(transaction_type)}"
        )

    def by_date_range(self, account_id, from_date, to_date) -> Resp:
        return self.client.get(
            f"/accounts/{_q(account_id)}/transactions/fromDate/{_q(from_date)}/toDate/{_q(to_date)}"
        )

    def on_date(self, account_id, date) -> Resp:
        return self.client.get(f"/accounts/{_q(account_id)}/transactions/onDate/{_q(date)}")

    # ---- 断言辅助 ----
    @staticmethod
    def ids(resp: Resp) -> set:
        """结果集 ID。响应不是数组（400/404/500）时返回空集，避免用例里到处判类型。"""
        data = resp.json
        return {t["id"] for t in data} if isinstance(data, list) else set()

    @staticmethod
    def rows(resp: Resp) -> list:
        data = resp.json
        return data if isinstance(data, list) else []
