"""探测执行器 —— reset → 快照 → 发送 → 快照 → 记录。

它只做「采集」，不做「判定」
------------------------------
判定（这是缺陷吗）留给契约表。runner 里出现的 flag 全是**机械事实**：
    账户损坏 / 5XX / 非 200 / 守恒破坏 / 余额未动但有流水
没有一条依赖「我认为应该怎样」。这条分界和流水查询那套的
``expect.py``（语义预期）与 ``runner.py``（事实采集）是同一原则。

为什么 reset 在每条**之前**
---------------------------
1. 每条用例的起点必须一致，否则后一条读到的是前一条的残留；
2. 前一条如果是破坏性的（精度），下一条开始前能自动恢复；
3. 末尾再 reset 一次，保证交付时环境干净（不把脏库留给后面的用例）。

为什么破坏性用例排最后
----------------------
``amount=0.001`` 会让账户余额带上三位小数，此后**任何**读该账户的接口都返回 500。
如果夹在中间跑，后面十几条都会失败，而且报错看起来像「接口挂了」——
这类失败最难排查，因为它把「一个 case 的影响」伪装成「系统的整体故障」。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from api.http_client import HttpClient

from .cases import BASE_URL, CASES, Case
from .observe import (balance, delta, fmt_money, fmt_new_tx, fmt_signed,
                      snapshot)

OUT_DIR = Path(__file__).parent / "out"
OUT_CSV = OUT_DIR / "write_matrix.csv"

COLUMNS = [
    "case_id", "group", "endpoint", "params", "repeat",
    "status", "content_type", "body", "before_bal",
    "d_from", "d_to", "new_tx", "conserve", "flag", "note",
]


def _ordered(cases: list[Case]) -> list[Case]:
    """破坏性用例排到最后，组内保持声明顺序。"""
    return [c for c in cases if not c.destructive] + [c for c in cases if c.destructive]


def _send(client: HttpClient, case: Case) -> list:
    return [client.post(case.endpoint, params=case.params) for _ in range(case.repeat)]


def _run_one(client: HttpClient, case: Case) -> dict:
    # ① 干净起点（顺带把上一条的残留清掉）
    client.post("/initializeDB")
    # ② 操作前快照
    before = snapshot(client, case.watch)
    # ③ 发送（repeat 次，全部记下来）
    resps = _send(client, case)
    # ④ 操作后快照
    after = snapshot(client, case.watch)

    statuses = [r.status_code for r in resps]
    last = resps[-1]

    # ⑤ 算每个账户的变动
    rows = {aid: delta(before, after, aid) for aid in dict.fromkeys(case.watch)}
    ordered_aids = list(dict.fromkeys(case.watch))
    d1 = rows[ordered_aids[0]]
    d2 = rows[ordered_aids[1]] if len(ordered_aids) > 1 else None

    # ⑥ 守恒（只在双账户时有意义）
    if d2 is None:
        conserve = "-"
    elif d1["d_bal"] is None or d2["d_bal"] is None:
        conserve = "N/A"
    else:
        conserve = "是" if (d1["d_bal"] + d2["d_bal"]) == 0 else "否"

    # ⑦ flag：全部是机械判据，不含任何语义预期
    flags = []
    if d1["corrupted"] or (d2 and d2["corrupted"]):
        flags.append("账户损坏")
    if any(s >= 500 for s in statuses):
        flags.append("5XX")
    elif any(s != 200 for s in statuses):
        flags.append(f"HTTP{statuses[0]}")
    if conserve == "否":
        flags.append("守恒破坏")
    if d1["d_bal"] == 0 and d1["new"]:
        flags.append("余额未动但有流水")

    before_desc = " ".join(f"{aid}={fmt_money(before[aid]['bal'])}" for aid in ordered_aids)
    new_desc = " ".join(f"{aid}:{fmt_new_tx(rows[aid]['new'])}" for aid in ordered_aids)

    return {
        "case_id": case.cid,
        "group": case.group,
        "endpoint": case.endpoint,
        "params": str(case.params),
        "repeat": case.repeat,
        "status": ",".join(str(s) for s in statuses),
        "content_type": last.content_type or "-",
        "body": last.text.replace("\r", " ").replace("\n", " ").strip()[:80],
        "before_bal": before_desc,
        "d_from": fmt_signed(d1["d_bal"]),
        "d_to": fmt_signed(d2["d_bal"]) if d2 else "-",
        "new_tx": new_desc,
        "conserve": conserve,
        "flag": "|".join(flags),
        "note": case.note,
    }


def _recover(client: HttpClient, case: Case) -> bool:
    """破坏性用例跑完立刻重置，并验证账户恢复可读。"""
    client.post("/initializeDB")
    return all(balance(client, aid) is not None for aid in case.watch)


def _print_row(row: dict) -> None:
    status = row["status"]
    mark = "!!" if row["flag"] else "  "
    print(
        f"{mark} {row['case_id']:<3} {row['group']:<10} {status:<7} "
        f"{row['d_from']:>10} {row['d_to']:>10}  {row['new_tx']:<28} "
        f"守恒={row['conserve']:<2} {row['flag']}"
    )


def main() -> int:
    try:  # Windows 控制台默认 GBK，中文会乱码
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    client = HttpClient(BASE_URL)
    rows = []
    orphans = []  # 被写坏、需要 reset 才能恢复的账户

    print(f"探测目标: {BASE_URL}")
    print(f"用例总数: {len(CASES)}（破坏性 {sum(c.destructive for c in CASES)} 条，排在最后）\n")

    for case in _ordered(CASES):
        row = _run_one(client, case)
        rows.append(row)
        _print_row(row)

        if case.destructive:
            ok = _recover(client, case)
            orphans.append((case.cid, ok))
            print(f"     └─ 破坏性用例：reset 后账户可读 = {ok}"
                  f"{'（只有 reset 能恢复）' if ok else '（reset 也救不回来！）'}")

    # 收尾：把环境恢复干净
    client.post("/initializeDB")
    rest_ok = all(balance(client, aid) is not None for aid in (12345, 12456, 12567))
    print(f"\n收尾 reset 完成，核心账户可读 = {rest_ok}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    # ---- 摘要（只做统计，不下结论）------------------------------------
    print(f"\n已写入 {OUT_CSV}（{len(rows)} 行）")
    # 注意：repeat>1 的 case 状态是 "200,200"，不能用 == "200" 判等，否则会漏计
    ok200 = sum(1 for r in rows if "200" in r["status"].split(","))
    s5xx = sum(1 for r in rows if "5XX" in r["flag"])
    s4xx = sum(1 for r in rows if r["status"].startswith("4"))
    print(f"状态码分布: 200×{ok200}  4xx×{s4xx}  5xx×{s5xx}")
    for label in ("守恒破坏", "余额未动但有流水", "账户损坏"):
        hit = [r["case_id"] for r in rows if label in r["flag"]]
        print(f"{label:<14} {len(hit):>2} 条  {', '.join(hit) if hit else '-'}")
    if orphans:
        print(f"写坏账户的用例  {len(orphans)} 条  "
              f"{', '.join(c for c, _ in orphans)}（均已 reset 恢复）")

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
