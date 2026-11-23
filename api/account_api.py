"""账户查询，主要给断言用（billpay 本身只返回一句结果，对错必须回查）。"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .http_client import HttpClient, Resp


class AccountApi:
    def __init__(self, client: HttpClient):
        self.client = client

    def get(self, account_id) -> Resp:
        return self.client.get(f"/accounts/{account_id}")

    def balance(self, account_id) -> Optional[Decimal]:
        """返回 Decimal 便于精确比较（浮点 0.1+0.2 != 0.3 会害死人）。"""
        resp = self.get(account_id)
        if resp.status_code != 200 or not resp.is_json:
            return None
        return Decimal(str(resp.json["balance"]))

    def transactions(self, account_id) -> list:
        resp = self.client.get(f"/accounts/{account_id}/transactions")
        if resp.status_code != 200 or not resp.is_json:
            return []
        return resp.json

    def snapshot(self, account_id) -> tuple[Optional[Decimal], int]:
        """余额 + 流水条数，用来做『增量对账』。"""
        return self.balance(account_id), len(self.transactions(account_id))
