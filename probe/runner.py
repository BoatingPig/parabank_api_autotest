"""从 render 生成的 case 中发送请求，记录报文特征，落成 CSV。

只做四件事，不要再往里加东西：
1. 拼 URL      —— BASE_URL + case["url"]（参数替换与编码已在 render 阶段完成）
2. 发请求      —— GET，不跟随重定向
3. 抽 7 个字段  —— status / content_type / result_count / result_ids / body_len / elapsed_ms / error_snippet
4. 落 CSV      —— utf-8-sig，Excel 双击不乱码

运行（必须在 autotest 目录下，否则 probe 这个包名找不到）：
    cd D:\\parabank\\autotest
    python -m probe.runner
    python -m probe.runner --dry-run        # 只打印 URL，不发请求
    python -m probe.runner --timeout 5 --out out\\try.csv

设计取舍（都有原因，别删注释）：
- allow_redirects=False：ParaBank 的 index/overview 有 302，默认跟随会把真实状态码吃掉
- content.decode(...) 而不是 resp.text：500 错误页的 charset 声明不可靠，text 会乱码或抛异常
- "<transaction>" 带右尖括号：不带会误匹配外层 <transactions>，条数直接算错
- error_snippet 用 " ".join(body.split())：压掉换行，否则一条记录会被 CSV 拆成多行
- result_ids 存完整 ID 列表，不是只存条数：条数相同不代表结果相同。
  amount=300 和 amount=0 都返回 1 条，但记录分别是 12145 / 13699。
  判定"参数有没有生效"必须比对集合是否相等，不能比对大小。
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys
import time

import requests

from probe.config import BASE_URL
from probe.render import gen_case

OUT_DIR = pathlib.Path(__file__).parent / "out"
DEFAULT_OUT = OUT_DIR / "matrix.csv"

FIELDS = [
    "case_id",
    "endpoint",
    "param",
    "value",
    "is_baseline",
    "url",
    "status",
    "content_type",
    "result_count",
    "result_ids",
    "body_len",
    "elapsed_ms",
    "error_snippet",
]

# 只取流水 ID。transaction 的 XML 里 <id> 就是流水主键，findall 的顺序与响应一致
# （SQL 是 ORDER BY date, id，顺序稳定）。错误响应（4xx/5xx）没有 <id>，返回空串。
TRANSACTION_ID_RE = re.compile(r"<id>(\d+)</id>")

# 三条已知答案。runner 自己写错时，这里必须先红，否则 106 条结果全是垃圾
# 第 4 项校验首个流水 ID：验证 result_ids 的提取没有错位（None = 不校验具体值，"" = 必须为空）
SANITY = [
    ("/services/bank/transactions/12145", 200, 1, "12145"),
    ("/services/bank/accounts/12345/transactions", 200, 7, None),
    ("/services/bank/transactions/abc", 404, 0, ""),
]


def probe_one(case, session, timeout):
    """发一条请求，返回一行记录。"""
    url = BASE_URL + case["url"]

    started = time.perf_counter()
    resp = session.get(url, timeout=timeout, allow_redirects=False)
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    body = resp.content.decode("utf-8", "replace")
    return {
        **case,
        "status": resp.status_code,
        "content_type": (resp.headers.get("Content-Type") or "").split(";")[0].strip(),
        "result_count": body.count("<transaction>"),
        "result_ids": ",".join(TRANSACTION_ID_RE.findall(body)),
        "body_len": len(resp.content),
        "elapsed_ms": elapsed_ms,
        "error_snippet": " ".join(body.split())[:200] if resp.status_code >= 400 else "",
    }


def sanity_check(session, timeout):
    """用三条已知答案验证 runner 本身。顺带把连接建好，后面 106 条的 elapsed 不含建连开销。"""
    print("[sanity] 三条已知答案自检")
    all_passed = True
    for path, want_status, want_count, want_first_id in SANITY:
        case = {
            "case_id": "sanity",
            "endpoint": "-",
            "param": "-",
            "value": "-",
            "is_baseline": False,
            "url": path,
        }
        row = probe_one(case, session, timeout)
        ids = [x for x in row["result_ids"].split(",") if x]

        # want_first_id 为 None 表示这条只校验状态码和条数
        id_ok = want_first_id is None or (ids[0] if ids else "") == want_first_id
        passed = row["status"] == want_status and row["result_count"] == want_count and id_ok
        all_passed = all_passed and passed
        mark = "OK  " if passed else "FAIL"
        print(
            f"  [{mark}] {path:46} "
            f"-> {row['status']} / {row['result_count']} 条 / ids[{len(ids)}] "
            f"(期望 {want_status} / {want_count}"
            + (f" / 首条 {want_first_id})" if want_first_id is not None else ")")
        )
    return all_passed


def run_all(cases, session, timeout):
    rows = []
    for index, case in enumerate(cases, start=1):
        rows.append(probe_one({**case, "case_id": f"C{index:03d}"}, session, timeout))
        if index % 10 == 0 or index == len(cases):
            print(f"  进度 {index}/{len(cases)}")
    return rows


def write_csv(rows, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows, out_path):
    status_count = {}
    for row in rows:
        status_count[row["status"]] = status_count.get(row["status"], 0) + 1

    server_errors = [r for r in rows if r["status"] == 500]
    empty_200 = [r for r in rows if r["status"] == 200 and r["result_count"] == 0]

    # result_ids 和 result_count 是两条独立路径算出来的，必须互相吻合。
    # 不符说明 id 正则与当前 XML 结构对不上，后面所有集合比较都会失效。
    mismatched = [
        r
        for r in rows
        if len([x for x in r["result_ids"].split(",") if x]) != r["result_count"]
    ]

    print(f"\n[完成] {len(rows)} 条 -> {out_path}")
    print("  状态码分布   : " + ", ".join(f"{code}×{n}" for code, n in sorted(status_count.items())))
    print(f"  500 服务器错误: {len(server_errors)} 条")
    print(f"  200 但 0 条   : {len(empty_200)} 条（要先分辨是正常空结果还是静默失败）")
    print(
        f"  ids 与条数不符: {len(mismatched)} 条"
        + ("  <- runner 解析有问题，先修这里再看结论" if mismatched else "")
    )

    if mismatched:
        print("\n  ids 提取异常清单（id 正则与 XML 结构可能已不匹配）:")
        for row in mismatched[:5]:
            print(
                f"    {row['case_id']} {row['endpoint']:26} {row['param']}={row['value']!r} "
                f"count={row['result_count']} ids={row['result_ids'][:40]!r}"
            )

    if server_errors:
        print("\n  500 清单（优先看这些，值很可能已经进了 SQL 层）:")
        for row in server_errors:
            print(f"    {row['endpoint']:26} {row['param']}={row['value']!r}")


def main():
    parser = argparse.ArgumentParser(description="ParaBank 接口契约探测器 v1")
    parser.add_argument("--timeout", type=int, default=10, help="单条请求超时秒数")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="CSV 输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印 URL，不发请求")
    args = parser.parse_args()

    cases = list(gen_case())

    if args.dry_run:
        for index, case in enumerate(cases, start=1):
            print(
                f"C{index:03d} {case['endpoint']:26} {case['param']:13} "
                f"{case['value']!r:20} {case['url']}"
            )
        print(f"\n共 {len(cases)} 条（未发请求）")
        return 0

    try:
        with requests.Session() as session:
            # 显式固定基线头，不依赖 requests 版本的默认值
            session.headers.update({"Accept": "*/*"})

            if not sanity_check(session, args.timeout):
                print(f"\n[FAIL] 自检未通过，先修 runner，别去看这 {len(cases)} 条结果。")
                print("       如果三条全错，先确认数据库已初始化：")
                print(f"       curl -X POST {BASE_URL}/services/bank/initializeDB")
                return 1

            rows = run_all(cases, session, args.timeout)
    except requests.RequestException as exc:
        print(f"[FAIL] 请求异常：{exc}")
        print(f"       确认 ParaBank 在跑：{BASE_URL}")
        return 2

    out_path = pathlib.Path(args.out)
    write_csv(rows, out_path)
    print_summary(rows, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
