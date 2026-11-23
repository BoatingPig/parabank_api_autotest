"""并发探测 —— 单独一个脚本，不进 runner 的单发循环。

为什么不合并
------------
runner 的每条 case 是「一个请求」，而并发是「N 个请求同时到达」。
两者的执行结构不同（线程 + 屏障），而且并发必须**串行地一组一组跑**，
每组之间要 reset —— 混进单发循环会让「一条用例 = 一次请求」这个前提失效。

为什么不用 HttpClient
---------------------
``HttpClient`` 内部持有一个 ``requests.Session``，而 Session 不是线程安全的。
并发场景每线程独立建连，才测得到服务端的竞态而不是客户端的锁。
"""
from __future__ import annotations

import csv
import sys
import threading
from decimal import Decimal
from pathlib import Path

import requests

from api.http_client import HttpClient

from .cases import (BASE_URL, CONCURRENCY_FROM, CONCURRENCY_SETTINGS,
                    CONCURRENCY_TO)
from .observe import balance, delta, fmt_signed, snapshot

OUT_DIR = Path(__file__).parent / "out"
OUT_CSV = OUT_DIR / "concurrency.csv"

COLUMNS = [
    "tag", "threads", "amount", "expect_deduct", "d_from", "d_to",
    "conserve", "tx_from", "tx_to", "http_ok", "flag",
]


def _fire(n: int, amount: int, barrier: threading.Barrier) -> tuple[list[int], list[str]]:
    """n 个线程在屏障处对齐后同时发请求，返回状态码与异常名。"""
    codes: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=30)  # 尽量让请求同时到达
            resp = requests.post(
                f"{BASE_URL}/transfer",
                params={"fromAccountId": CONCURRENCY_FROM,
                        "toAccountId": CONCURRENCY_TO,
                        "amount": amount},
                headers={"Accept": "application/json"},
                timeout=60,
            )
            with lock:
                codes.append(resp.status_code)
        except Exception as exc:  # 连接池耗尽 / 超时
            with lock:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return codes, errors


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    http = HttpClient(BASE_URL)   # 只用于 reset 与观测（单线程）
    rows = []

    print(f"并发明细: {CONCURRENCY_FROM} → {CONCURRENCY_TO}，每笔金额见下\n")
    for n, amount, tag in CONCURRENCY_SETTINGS:
        http.post("/initializeDB")
        watch = (CONCURRENCY_FROM, CONCURRENCY_TO)
        before = snapshot(http, watch)

        barrier = threading.Barrier(n)
        codes, errors = _fire(n, amount, barrier)

        after = snapshot(http, watch)
        d1 = delta(before, after, CONCURRENCY_FROM)
        d2 = delta(before, after, CONCURRENCY_TO)
        expect = Decimal(-amount * n)

        conserve = "是" if d1["d_bal"] is not None and d2["d_bal"] is not None \
            and (d1["d_bal"] + d2["d_bal"]) == 0 else "否"
        flags = []
        if conserve == "否":
            flags.append("守恒破坏")
        if d1["d_bal"] is not None and d1["d_bal"] != expect:
            flags.append("扣款不到位")
        if errors:
            flags.append(f"异常:{','.join(sorted(set(errors)))}")
        if any(c != 200 for c in codes):
            flags.append("非200")

        print(f"{tag:<16} 期望扣 {expect:>8.2f}  实际 {fmt_signed(d1['d_bal']):>9} "
              f"收款 {fmt_signed(d2['d_bal']):>7}  守恒={conserve:<2} "
              f"流水 {len(d1['new'])}/{len(d2['new'])}  {'|'.join(flags)}")

        if conserve == "否" and d1["d_bal"] is not None and d2["d_bal"] is not None:
            # ΔA + ΔC > 0 即总额凭空变多：收款方足额到账，付款方少扣了
            gap = d1["d_bal"] + d2["d_bal"]
            print(f"{'':16} └─ 凭空多出 {gap:.2f} 元："
                  f"收款方足额到账，付款方只扣了 {-d1['d_bal']:.2f}")

        rows.append({
            "tag": tag,
            "threads": n,
            "amount": amount,
            "expect_deduct": f"{expect:.2f}",
            "d_from": fmt_signed(d1["d_bal"]),
            "d_to": fmt_signed(d2["d_bal"]),
            "conserve": conserve,
            "tx_from": len(d1["new"]),
            "tx_to": len(d2["new"]),
            "http_ok": sum(1 for c in codes if c == 200),
            "flag": "|".join(flags),
        })

    http.post("/initializeDB")
    rest = all(balance(http, aid) is not None for aid in (12345, 12456, 12567))
    print(f"\n收尾 reset 完成，核心账户可读 = {rest}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"已写入 {OUT_CSV}（{len(rows)} 行）")

    http.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
