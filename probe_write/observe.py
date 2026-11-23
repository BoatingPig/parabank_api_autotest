"""观测层 —— 只回答「现在账户是什么状态」，不判断对错。

为什么单独一层
--------------
写接口的响应体几乎没有信息量（一句 `Successfully transferred $100 ...`），
真正的结果全在「余额」和「流水」这两处。所以"怎么把状态读出来"必须是独立能力，
而不是散落在 runner 的每条 case 里 —— 否则「精度 case 之后余额读不到」这种事
会在十几个地方各处理一遍，且处理方式不一致。

约定：读不到 = None，不抛异常。
    账户被精度问题打成 500 时，``balance()`` 返回 None 而不是崩，
    这样 runner 能把它记成「观测失败」这条事实，而不是中断整轮探测。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from api.http_client import HttpClient


def _json_or_none(client: HttpClient, path: str) -> Optional[Any]:
    resp = client.get(path)
    if resp.status_code != 200 or not resp.is_json:
        return None
    # 写接口的 Content-Type 声明 JSON 但内容是裸字符串，json 解析会失败 → 这里也会拿到 None
    return resp.json


def balance(client: HttpClient, account_id: int) -> Optional[Decimal]:
    """账户余额；读不到（不存在 / 500 损坏 / 非 JSON）返回 None。"""
    data = _json_or_none(client, f"/accounts/{account_id}")
    if not isinstance(data, dict) or "balance" not in data:
        return None
    return Decimal(str(data["balance"]))


def transactions(client: HttpClient, account_id: int) -> list[dict]:
    """账户全部流水；读不到返回空列表。"""
    data = _json_or_none(client, f"/accounts/{account_id}/transactions")
    return data if isinstance(data, list) else []


def snapshot(client: HttpClient, account_ids: tuple[int, ...]) -> dict[int, dict]:
    """一次性抓取多个账户的状态。"""
    return {
        aid: {"bal": balance(client, aid), "txs": transactions(client, aid)}
        for aid in dict.fromkeys(account_ids)  # 去重且保序
    }


def delta(before: dict[int, dict], after: dict[int, dict], account_id: int) -> dict:
    """算单个账户的变动：余额差 + 新增流水。

    新增流水用 **id 集合差** 求，而不是「条数差」——
    条数差无法回答「新增的是哪几条」，也就无法核对金额与借贷方向。
    """
    b, a = before[account_id], after[account_id]
    d_bal = None if (b["bal"] is None or a["bal"] is None) else (a["bal"] - b["bal"])
    old_ids = {t["id"] for t in b["txs"]}
    new_txs = [t for t in a["txs"] if t["id"] not in old_ids]
    return {
        "d_bal": d_bal,
        "new": new_txs,
        # 写之前能读、写之后读不到 —— 账户被写坏了
        "corrupted": b["bal"] is not None and a["bal"] is None,
    }


def fmt_money(value: Optional[Decimal]) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def fmt_signed(value: Optional[Decimal]) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}"


def fmt_new_tx(new_txs: list[dict]) -> str:
    """形如 ``1条(Debit,100.00)`` / ``2条(Credit,100.00|Debit,100.00)``。"""
    if not new_txs:
        return "0"
    parts = [f"{t['type']},{Decimal(str(t['amount'])):.2f}" for t in new_txs]
    return f"{len(new_txs)}条({'|'.join(parts)})"
