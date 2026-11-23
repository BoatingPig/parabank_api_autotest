"""把 matrix.csv 压成一份可执行的缺陷清单。

六步，顺序不能反
----------------
1. 载入 + 派生字段    ids 转 set、status 转 int、kind 从 config 反查
2. 前置校验          baseline 是否可用、count 与 ids 是否自洽
                     —— 这步不过，后面所有结论都是沙上建塔
3. 组内纵向比较      每个变体与「同组 baseline」比结果集，得到集合关系
4. 逐条判定          用 expect.py 的语义预期，把关系映射成缺陷类型
5. 横向检查          等价组是否分裂、同一值在不同接口是否一致
6. 落 findings.csv   一条缺陷一行，evidence 要能直接贴进 bug 单

为什么判定不查数据库
--------------------
数据库只知道「数据是什么」，不知道「参数该不该生效」。
month=Nov 返回了全量 7 条，库里确实有 7 条,两者"一致" —— 但参数被静默忽略了。
所以参照系是 baseline + 等价组 + 语义预期，不是 DB。

数据一致性对账（接口 vs SQL）是另一层测试，要处理时区（接口比库早一天）
和精度（300.00 vs 300.0000），不在本脚本职责内。

为什么区分「超集」和「全等」
----------------------------
全等 = 条件压根没加上，返回了全部（month=Nov）。
超集 = 条件加上了但方向错了，返回比基线更多（fromDate=2026-11-11 解析成公元 179 年，
       把下界拉到公元前，于是包含全部流水）。
只盯「变少」的缺陷会漏掉后者，而后者往往更严重。

运行
----
    cd D:\\parabank\\autotest
    python -m probe.analyze
    python -m probe.analyze --matrix probe\\out\\matrix.csv --out probe\\out\\findings.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import unicodedata

from probe.config import ENDPOINTS
from probe.expect import EQUIV_GROUPS, INJECTION_VALUES, expect_for

OUT_DIR = pathlib.Path(__file__).parent / "out"
DEFAULT_MATRIX = OUT_DIR / "matrix.csv"
DEFAULT_OUT = OUT_DIR / "findings.csv"

FINDINGS_FIELDS = [
    "finding_id",
    "severity",
    "finding",
    "case_id",
    "endpoint",
    "param",
    "value",
    "expected",
    "actual",
    "evidence",
]

# 严重级：P0 必修（安全/崩溃）、P1 契约缺陷、P2 需确认、P3 提示
SEVERITY = {
    "BASELINE_BROKEN": "P0",
    "SERVER_ERROR": "P0",
    "INJECTION": "P0",
    "SUPERSET": "P1",
    "CROSS_OVERLAP": "P1",
    "SILENT_IGNORE": "P1",
    "INVALID_ACCEPTED": "P1",
    "DISJOINT_RESULT": "P1",
    "UNEXPECTED_REJECT": "P2",
    "NOOP_BROKEN": "P2",
    "UNEXPECTED_DATA": "P2",
    "UNEXPECTED_RESULT": "P2",
    "UNKNOWN_STATUS": "P2",
    "EQUIV_MISMATCH": "P2",
    "CROSS_API_INCONSISTENT": "P2",
    "DATA_INCONSISTENT": "P2",
    "EMPTY_RESULT": "P3",
    "NO_EXPECTATION": "P3",
}

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

REL_CN = {
    "noop": "全等",
    "subset": "真子集",
    "superset": "超集",
    "empty": "空",
    "cross": "交叉",
    "disjoint": "无关",
    "nobase": "无基线",
}

# 预期语义 → 可接受的「结果集关系」（仅对 200 有效；reject 对应 4xx，单独判）
ACCEPT_REL = {
    "noop": {"noop"},
    "filter": {"subset"},
    "empty": {"empty"},
    "key": {"noop", "subset", "empty", "disjoint"},
    "reject": set(),
}

# (endpoint, param) -> kind，反查自 config，避免和 config 各写一份
KIND = {(e["name"], p["name"]): p["kind"] for e in ENDPOINTS for p in e["params"]}


# ======================================================================
# Step 1  载入 + 派生字段
# ======================================================================
def ids_of(cell: str) -> set[str]:
    return {x for x in cell.split(",") if x}


def load(matrix_path: pathlib.Path) -> list[dict]:
    rows = []
    with open(matrix_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            # CSV 读出来全是字符串，这里一次性转掉。
            # 不转就会踩 "200" != 200 恒为真的坑（实测把 94 条变体全判成了「拒绝」）
            r["ids"] = ids_of(r["result_ids"])
            r["status_i"] = int(r["status"])
            r["count_i"] = int(r["result_count"])
            r["is_base"] = r["is_baseline"] == "True"
            r["kind"] = KIND.get((r["endpoint"], r["param"]), "?")
            r["cls"] = (
                "5xx" if r["status_i"] >= 500
                else "4xx" if r["status_i"] >= 400
                else "3xx" if r["status_i"] >= 300
                else "2xx"
            )
            rows.append(r)
    return rows


def group_by(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """一个「组」= 同一接口 + 同一参数位。组内恰好一条 baseline。"""
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for r in rows:
        groups[(r["endpoint"], r["param"])].append(r)
    return groups


# ======================================================================
# Step 3  集合关系（纵向比较的核心）
# ======================================================================
def relate(ids: set[str], base: set[str]) -> str:
    """变体结果集与基线结果集的关系。

    顺序有讲究：先判空，因为空集是任何集合的子集，不先判会被归成「真子集」，
    把「过滤掉全部」和「过滤掉一部分」混为一谈 —— 这两者的缺陷性质完全不同。
    """
    if not base:
        return "nobase"
    if not ids:
        return "empty"
    if ids == base:
        return "noop"
    if ids < base:
        return "subset"
    if ids > base:
        return "superset"
    if ids & base:
        return "cross"
    return "disjoint"


# ======================================================================
# 缺陷记录的构造
# ======================================================================
def mk(row: dict, name: str, expected: str, evidence: str, actual: str | None = None) -> dict:
    return {
        "severity": SEVERITY[name],
        "finding": name,
        "case_id": row["case_id"],
        "endpoint": row["endpoint"],
        "param": row["param"],
        "value": row["value"],
        "expected": expected,
        "actual": actual if actual is not None else f'{row["status"]} / {row["count_i"]} 条',
        "evidence": evidence,
    }


# ======================================================================
# Step 2  前置校验
# ======================================================================
def precheck(rows: list[dict], groups: dict) -> list[dict]:
    """两项自洽性检查。任何一项不过，后面的判定都要打折看。"""
    out = []

    # 2.1 result_count 与 result_ids 必须一致。
    #     两者走的是两条独立路径（数标签 vs 正则抓 id），不一致说明 runner 的解析坏了
    for r in rows:
        if r["status_i"] == 200 and len(r["ids"]) != r["count_i"]:
            out.append(mk(
                r, "DATA_INCONSISTENT",
                "result_count == len(result_ids)",
                f'result_count={r["count_i"]}，但抓到 {len(r["ids"])} 个 id —— runner 的解析逻辑有问题',
            ))

    # 2.2 baseline 必须 200 且非空，否则整组变体失去参照系
    for (ep, param), members in groups.items():
        base = next((r for r in members if r["is_base"]), None)
        if base is None:
            raise SystemExit(f"[致命] 组 {ep} × {param} 没有 baseline 行，矩阵不完整")
        if base["status_i"] != 200 or base["count_i"] == 0:
            out.append(mk(
                base, "BASELINE_BROKEN",
                "baseline 返回 200 且非空",
                f'基线 value={base["value"]!r} 只拿到 {base["status"]}/{base["count_i"]} 条，'
                f'该组 {len(members) - 1} 条变体全部失去对照 —— 判定不可信',
            ))
    return out


# ======================================================================
# Step 4  逐条判定
# ======================================================================
def judge_one(row: dict, base: dict) -> dict | None:
    allowed = expect_for(row["param"], row["kind"], row["value"])
    if allowed is None:
        return mk(
            row, "NO_EXPECTATION", "-",
            f'expect.py 里查不到 (param={row["param"]}, kind={row["kind"]}, value={row["value"]!r})，'
            f'本条未判定 —— 缺失预期必须显式暴露，不能当通过',
        )

    rel = relate(row["ids"], base["ids"])
    exp = " | ".join(allowed) + f'（基线 {base["count_i"]} 条）'

    # 基线坏了就没法比较 —— BASELINE_BROKEN 已经报过根因，这里不再逐条制造噪音
    if rel == "nobase":
        return None

    # --- 4.1 500 先判：任何用户输入都不该让服务崩，不需要参考预期 ---
    if row["status_i"] == 500:
        # HTML 错误页的正文是通用模板，没有信息量，别塞进 evidence 里占版面
        snippet = "" if "html" in row["content_type"] else f'；响应片段: {row["error_snippet"][:110]}'
        return mk(
            row, "SERVER_ERROR", exp,
            f'500 不是任何输入的合法结果；content_type={row["content_type"]}，'
            f'body {row["body_len"]} 字节{snippet} —— 根因去 catalina 日志找 bad SQL grammar',
        )

    # --- 4.2 3xx：说明 runner 的 allow_redirects 漏了，真实状态码被吃掉了 ---
    if row["cls"] == "3xx":
        return mk(row, "UNKNOWN_STATUS", exp, "出现 3xx，检查 runner 是否关掉了自动跟随重定向")

    # --- 4.3 4xx：预期 reject 就是对的，否则报「该收没收」 ---
    if row["cls"] == "4xx":
        if "reject" in allowed:
            return None
        return mk(row, "UNEXPECTED_REJECT", exp, f'该值应被正常处理，却返回 {row["status"]}')

    if row["status_i"] != 200:
        return mk(row, "UNKNOWN_STATUS", exp, f'非预期状态码 {row["status"]}')

    # ---------------- 以下全部是 200 ----------------

    # 4.4 注入：载荷 + 结果集异常 → 越过过滤条件
    if row["value"] in INJECTION_VALUES and rel in ("superset", "disjoint", "cross"):
        return mk(
            row, "INJECTION", exp,
            f'注入载荷返回 200，结果集{REL_CN[rel]}于基线（{len(row["ids"])} 条 vs 基线 {len(base["ids"])} 条），'
            f'account_id 过滤被绕过',
        )

    # 4.5 超集：条件生效了但方向错，返回比基线更多
    if rel == "superset":
        return mk(
            row, "SUPERSET", exp,
            f'返回 {len(row["ids"])} 条，比基线多 {len(row["ids"]) - len(base["ids"])} 条 —— '
            f'过滤条件被削弱（不是没生效，是生效错了方向）',
        )

    # 4.6 交叉：有重叠但不互相包含，典型是条件串错了字段
    if rel == "cross":
        return mk(
            row, "CROSS_OVERLAP", exp,
            f'与基线部分重叠但互不包含（交集 {len(row["ids"] & base["ids"])} 条）',
        )

    # 4.7 落在预期允许的关系里 → 通过
    accept = set().union(*(ACCEPT_REL[a] for a in allowed))
    if rel in accept:
        return None

    # 4.8 不接受，按主预期给出具体归因（名字要指向根因，不写笼统的「异常」）
    primary = allowed[0]

    if primary == "reject":
        return mk(
            row, "INVALID_ACCEPTED", exp,
            f'非法值返回 200，结果{REL_CN[rel]}（{len(row["ids"])} 条）—— 该拒未拒',
        )

    if primary == "noop":
        return mk(
            row, "NOOP_BROKEN", exp,
            f'All 的语义是「不过滤」，结果必须等于基线，实际{REL_CN[rel]}'
            f'（{len(row["ids"])} 条 vs {len(base["ids"])} 条）',
        )

    if primary == "filter":
        if rel == "noop":
            return mk(
                row, "SILENT_IGNORE", exp,
                f'结果与基线逐条相同（{len(row["ids"])}/{len(base["ids"])} 条）—— '
                f'过滤条件根本没加进查询，但接口返回 200',
            )
        if rel == "empty":
            return mk(row, "EMPTY_RESULT", exp, "过滤后为空，需确认是合法空结果还是参数未被识别")
        if rel == "disjoint":
            return mk(row, "DISJOINT_RESULT", exp, "结果与基线零交集，疑似条件字段串错")

    if primary == "empty":
        return mk(row, "UNEXPECTED_DATA", exp, f'预期空结果，实际返回 {len(row["ids"])} 条')

    return mk(row, "UNEXPECTED_RESULT", exp, f'结果{REL_CN[rel]}，不在可接受集合 {sorted(accept)} 内')


def judge(rows: list[dict], groups: dict) -> list[dict]:
    out = []
    for members in groups.values():
        base = next(r for r in members if r["is_base"])
        for r in members:
            if r["is_base"]:
                continue
            f = judge_one(r, base)
            if f:
                out.append(f)
    return out


# ======================================================================
# Step 5  横向检查
# ======================================================================
def check_equiv(rows: list[dict]) -> list[dict]:
    """等价组：语义相同、写法不同，结果必须一致。

    这是唯一不依赖「预期」的检查 —— 同一个语义给出两种结果，必然至少一个错，
    与 PRD 怎么写无关，所以证据最硬。
    """
    index: dict[tuple[str, str], dict[str, dict]] = collections.defaultdict(dict)
    for r in rows:
        index[(r["endpoint"], r["param"])][r["value"]] = r

    out = []
    for g in EQUIV_GROUPS:
        scope = index.get((g["endpoint"], g["param"]), {})
        got = [(v, scope[v]) for v in g["values"] if v in scope]
        if len(got) < 2:
            continue
        sigs = {(r["cls"], tuple(sorted(r["ids"]))) for _, r in got}
        if len(sigs) == 1:
            continue
        detail = "; ".join(f'{v!r}={r["status"]}/{r["count_i"]}条' for v, r in got)
        out.append({
            "severity": SEVERITY["EQUIV_MISMATCH"],
            "finding": "EQUIV_MISMATCH",
            "case_id": ",".join(r["case_id"] for _, r in got),
            "endpoint": g["endpoint"],
            "param": g["param"],
            "value": " | ".join(v for v, _ in got),
            "expected": "同一语义必须给同一结果",
            "actual": f'{len(sigs)} 种不同结果',
            "evidence": f'{g["name"]}：{detail}',
        })
    return out


def check_cross_api(rows: list[dict]) -> list[dict]:
    """同一个值（同一 kind）在不同接口上是否被同等对待。

    这是「单看每个都对，横着摆才矛盾」的那类缺陷。空值不参与：
    空值走的是路由不匹配（双斜杠），不反映业务处理差异。
    """
    buckets: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r["is_base"] or r["value"] == "":
            continue
        buckets[(r["kind"], r["value"])].append(r)

    out = []
    for (kind, value), members in buckets.items():
        if len({r["endpoint"] for r in members}) < 2:
            continue
        classes = collections.Counter(r["cls"] for r in members)
        if len(classes) < 2:
            continue
        detail = "; ".join(
            f'{r["endpoint"]}={r["status"]}/{r["count_i"]}条'
            for r in sorted(members, key=lambda x: x["case_id"])
        )
        out.append({
            "severity": SEVERITY["CROSS_API_INCONSISTENT"],
            "finding": "CROSS_API_INCONSISTENT",
            "case_id": ",".join(r["case_id"] for r in sorted(members, key=lambda x: x["case_id"])),
            "endpoint": "(多个)",
            "param": "(同 kind)",
            "value": value,
            "expected": f'{kind} 值 {value!r} 在各接口应同等对待',
            "actual": "  ".join(f"{cls}×{n}" for cls, n in sorted(classes.items())),
            "evidence": f'同一语义的值在 {len(members)} 个接口上出现 {len(classes)} 种处理：{detail}',
        })
    return out


# ======================================================================
# Step 6  输出
# ======================================================================
def write_findings(findings: list[dict], out_path: pathlib.Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FINDINGS_FIELDS)
        w.writeheader()
        for i, r in enumerate(findings, 1):
            w.writerow({"finding_id": f"F{i:03d}", **r})


def dwidth(s: str) -> int:
    """显示宽度：中文占 2 列，否则表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - dwidth(s))


def print_report(rows, groups, findings, matrix_path, out_path, max_rows) -> None:
    print(f"\n[载入] {matrix_path}")
    print(f"       {len(rows)} 条 case / {len(groups)} 组 / {len({r['endpoint'] for r in rows})} 个接口")

    # ---- 前置校验 ----
    print("\n=== 1. 前置校验 ===")
    broken = [f for f in findings if f["finding"] in ("BASELINE_BROKEN", "DATA_INCONSISTENT")]
    ok_base = len(groups) - sum(1 for f in broken if f["finding"] == "BASELINE_BROKEN")
    print(f"[{'OK' if ok_base == len(groups) else '!!'}] baseline 合法: {ok_base}/{len(groups)} 组")
    print(f"[{'OK' if not any(f['finding'] == 'DATA_INCONSISTENT' for f in broken) else '!!'}] "
          f"result_count 与 result_ids 自洽: "
          f"{sum(1 for f in broken if f['finding'] == 'DATA_INCONSISTENT')} 处不符")
    variants = sum(1 for r in rows if not r["is_base"])
    no_exp = sum(1 for f in findings if f["finding"] == "NO_EXPECTATION")
    print(f"[{'OK' if no_exp == 0 else '!!'}] 语义预期覆盖: {variants - no_exp}/{variants} 条变体"
          f"{'' if no_exp == 0 else f'（{no_exp} 条没有预期，等于没判）'}")
    for f in broken:
        print(f"      {f['severity']} {f['case_id']} {f['evidence']}")

    # ---- 纵向比较 ----
    # 5xx 必须单列：它不是「拒绝」，而是「崩溃」。混进拒绝列会让 500 看起来像正常拦截
    print("\n=== 2. 组内纵向比较（变体 vs 组内基线）===")
    cols = ("4xx", "5xx", "空", "全等", "子集", "超集", "交叉", "无关")
    hdr = ["组（endpoint × param）", "变体", *cols]
    print(pad(hdr[0], 44) + "".join(f"{h:>6}" for h in hdr[1:]))
    print("-" * 98)
    tot = collections.Counter()
    for (ep, param), members in groups.items():
        base = next(r for r in members if r["is_base"])
        c = collections.Counter()
        for r in members:
            if r["is_base"]:
                continue
            if r["status_i"] >= 500:
                c["5xx"] += 1
            elif r["status_i"] >= 400:
                c["4xx"] += 1
            else:
                c[REL_CN[relate(r["ids"], base["ids"])]] += 1
        tot.update(c)
        label = f"{ep} × {param}"
        print(pad(label, 44) + f"{len(members) - 1:>6}" + "".join(f"{c[k]:>6}" for k in cols))
    print("-" * 98)
    print(pad("合计", 44) + f"{sum(tot.values()):>6}" + "".join(f"{tot[k]:>6}" for k in cols))

    # ---- 缺陷清单 ----
    print("\n=== 3. 缺陷清单（按类型，同类只列前 %d 条，全量见 CSV）===" % max_rows)
    grouped = collections.defaultdict(list)
    for f in findings:
        grouped[f["finding"]].append(f)
    for name in sorted(grouped, key=lambda n: (SEVERITY_ORDER[SEVERITY[n]], n)):
        items = grouped[name]
        print(f"\n{SEVERITY[name]} {name}  ×{len(items)}")
        for f in items[:max_rows]:
            val = f["value"] if f["value"] else "(空值)"
            print(f"   {f['case_id']:<22} {f['param']}={val!r:<16} {f['actual']}")
            print(f"   {'':<22} {f['evidence']}")
        if len(items) > max_rows:
            print(f"   ... 其余 {len(items) - max_rows} 条见 CSV")

    # ---- 汇总 ----
    print("\n=== 4. 汇总 ===")
    by_sev = collections.Counter(f["severity"] for f in findings)
    print("   " + "   ".join(f"{s}={by_sev[s]}" for s in ("P0", "P1", "P2", "P3")))
    per_case = {"EQUIV_MISMATCH", "CROSS_API_INCONSISTENT", "BASELINE_BROKEN", "DATA_INCONSISTENT"}
    judged = sum(1 for f in findings if f["finding"] not in per_case)
    print(f"   逐条判定: {variants} 条变体 -> 命中 {judged} 条，通过 {variants - judged} 条")
    print(f"   共 {len(findings)} 条 -> {out_path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把契约矩阵分析成缺陷清单")
    ap.add_argument("--matrix", type=pathlib.Path, default=DEFAULT_MATRIX)
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--max-rows", type=int, default=6, help="每类缺陷在终端最多列几条")
    args = ap.parse_args(argv)

    if not args.matrix.exists():
        print(f"[错误] 找不到矩阵文件 {args.matrix}，先跑 python -m probe.runner")
        return 1

    rows = load(args.matrix)
    groups = group_by(rows)

    findings = []
    findings += precheck(rows, groups)
    findings += judge(rows, groups)
    findings += check_equiv(rows)
    findings += check_cross_api(rows)

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["finding"], f["case_id"]))

    write_findings(findings, args.out)
    print_report(rows, groups, findings, args.matrix, args.out, args.max_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
