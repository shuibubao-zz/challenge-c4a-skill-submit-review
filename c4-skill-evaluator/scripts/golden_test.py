#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
golden_test.py — 用带人工标注的黄金测试集测量评审器的误判率。

为什么必须做这件事：
  rubric 对「评审器质量」的正面信号是「评分标准清晰 / 有测试用例 / 误判率低」。
  前两条文档里写写就能声称，第三条**必须实测**——否则「我的评审器很准」只是一句自述。

用法:
    python golden_test.py [--golden DIR] [--out FILE]

输出：每个维度的准确率、过判率（判得比人松）、漏判率（判得比人严）、
      严重误判率（✅ 与 ❌ 直接颠倒）。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c4_evaluator import evaluate_folder, load_yaml, DEFAULT_RUBRIC  # noqa: E402

GOLDEN = Path(__file__).resolve().parent.parent / "references" / "golden_set"
ORDER = {"❌": 0, "⚠️": 1, "✅": 2}


def run(golden_dir: Path, rubric_path: Path) -> dict:
    rows = []
    for fx in sorted(p for p in golden_dir.iterdir() if p.is_dir()):
        exp_path = fx / "__expected__.json"
        if not exp_path.exists():
            continue
        exp = json.loads(exp_path.read_text(encoding="utf-8"))
        rep = evaluate_folder(fx, rubric_path)
        got_author = rep["authors"][0]["author"] if rep["authors"] else None
        got = {
            "author": got_author,
            "id_method": rep["authors"][0]["id_method"] if rep["authors"] else None,
            "completeness": {k: v["present"] for k, v in rep["authors"][0]["completeness"].items()}
            if rep["authors"] else {},
            "quality": {k: v["grade"] for k, v in rep["authors"][0]["quality"].items()}
            if rep["authors"] else {},
            "score": rep["authors"][0]["score"]["total"] if rep["authors"] else 0,
        }
        rows.append({"fixture": fx.name, "expected": exp, "got": got})

    # ---- 完整性：逐槽位二分类 ----
    comp_tp = comp_fp = comp_fn = comp_tn = 0
    comp_errors = []
    for r in rows:
        for slot, exp_v in r["expected"]["completeness"].items():
            got_v = bool(r["got"]["completeness"].get(slot))
            if exp_v and got_v:
                comp_tp += 1
            elif not exp_v and got_v:
                comp_fp += 1
                comp_errors.append((r["fixture"], slot, "应缺但判为有"))
            elif exp_v and not got_v:
                comp_fn += 1
                comp_errors.append((r["fixture"], slot, "应有但判为缺"))
            else:
                comp_tn += 1

    # ---- 质量：四条件评级 ----
    crit_stats = {}
    q_errors = []
    for r in rows:
        for crit, exp_g in r["expected"]["quality"].items():
            got_g = r["got"]["quality"].get(crit, "?")
            st = crit_stats.setdefault(
                crit, {"n": 0, "exact": 0, "within1": 0, "over": 0, "under": 0, "severe": 0})
            st["n"] += 1
            if got_g == exp_g:
                st["exact"] += 1
                st["within1"] += 1
            else:
                if abs(ORDER.get(got_g, 1) - ORDER.get(exp_g, 1)) <= 1:
                    st["within1"] += 1
                if ORDER.get(got_g, 1) > ORDER.get(exp_g, 1):
                    st["over"] += 1
                    q_errors.append((r["fixture"], crit, "过判", exp_g, got_g))
                else:
                    st["under"] += 1
                    q_errors.append((r["fixture"], crit, "漏判", exp_g, got_g))
                if {got_g, exp_g} == {"✅", "❌"}:
                    st["severe"] += 1

    # ---- 作者识别 ----
    author_stats = {"n": 0, "ok": 0, "errors": []}
    for r in rows:
        author_stats["n"] += 1
        if r["got"]["author"] == r["expected"]["author"]:
            author_stats["ok"] += 1
        else:
            author_stats["errors"].append((r["fixture"], r["expected"]["author"], r["got"]["author"]))

    n_comp = comp_tp + comp_fp + comp_fn + comp_tn
    return {
        "rows": rows,
        "completeness": {
            "n": n_comp, "tp": comp_tp, "fp": comp_fp, "fn": comp_fn, "tn": comp_tn,
            "accuracy": (comp_tp + comp_tn) / n_comp if n_comp else 0,
            "false_positive_rate": comp_fp / (comp_fp + comp_tn) if (comp_fp + comp_tn) else 0,
            "false_negative_rate": comp_fn / (comp_fn + comp_tp) if (comp_fn + comp_tp) else 0,
            "errors": comp_errors,
        },
        "quality": {k: {**v, "accuracy": v["exact"] / v["n"] if v["n"] else 0,
                        "within1_rate": v["within1"] / v["n"] if v["n"] else 0}
                    for k, v in crit_stats.items()},
        "quality_errors": q_errors,
        "author": author_stats,
    }


def render(res: dict, rubric: dict) -> str:
    L = []
    c = res["completeness"]
    q = res["quality"]
    n_q = sum(v["n"] for v in q.values())
    q_exact = sum(v["exact"] for v in q.values())
    q_within = sum(v["within1"] for v in q.values())
    q_severe = sum(v["severe"] for v in q.values())
    q_over = sum(v["over"] for v in q.values())
    q_under = sum(v["under"] for v in q.values())

    L.append("# 评审器准确率实测报告（黄金测试集）")
    L.append("")
    L.append("测试集：%d 个带人工标注的 fixture ｜ 评分标准 v%s ｜ 评审器 c4-skill-evaluator v1.0"
             % (len(res["rows"]), rubric.get("version")))
    L.append("")
    L.append("> **标注声明**：ground truth 由交付者本人按「读完内容后人工判读」给出，"
             "**先写标签再看评审器输出**，不用输出反推标签（否则是循环论证）。"
             "标注者为单一人，非独立评审团——这是本测试的可信度上限，如实写明。")
    L.append("")
    L.append("## 一、总体")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append("| 完整性判定（槽位级，n=%d） | **%.1f%%** 准确 |" % (c["n"], c["accuracy"] * 100))
    L.append("| ↳ 误报（应缺判为有） | %d 例，误报率 %.1f%% |" % (c["fp"], c["false_positive_rate"] * 100))
    L.append("| ↳ 漏报（应有判为缺） | %d 例，漏报率 %.1f%% |" % (c["fn"], c["false_negative_rate"] * 100))
    L.append("| 质量评级（条件级，n=%d） | **精确匹配 %.1f%% ｜ 相差≤1级 %.1f%%** |"
             % (n_q, q_exact / n_q * 100 if n_q else 0, q_within / n_q * 100 if n_q else 0))
    L.append("| ↳ 过判（判得比人松） | %d 例 |" % q_over)
    L.append("| ↳ 漏判（判得比人严） | %d 例 |" % q_under)
    L.append("| ↳ **严重误判（✅ ↔ ❌ 颠倒）** | **%d 例，占 %.1f%%** |"
             % (q_severe, q_severe / n_q * 100 if n_q else 0))
    L.append("| 作者识别 | %d/%d 正确 |" % (res["author"]["ok"], res["author"]["n"]))
    L.append("")
    L.append("## 二、分维度准确率")
    L.append("")
    L.append("| 条件 | 样本 | 精确匹配 | ≤1级 | 过判 | 漏判 | 严重误判 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for crit, v in q.items():
        L.append("| %s | %d | %d (%.0f%%) | %d (%.0f%%) | %d | %d | %d |" % (
            crit, v["n"], v["exact"], v["accuracy"] * 100, v["within1"],
            v["within1_rate"] * 100, v["over"], v["under"], v["severe"]))
    L.append("")
    L.append("## 三、逐 fixture 对照")
    L.append("")
    L.append("| fixture | 人工完整性 | 机器完整性 | 人工四条件 | 机器四条件 | 作者 |")
    L.append("|---|---|---|---|---|---|")
    for r in res["rows"]:
        e, g = r["expected"], r["got"]
        L.append("| %s | %d/5 | %d/5 | %s | %s | %s |" % (
            r["fixture"],
            sum(1 for x in e["completeness"].values() if x),
            sum(1 for x in g["completeness"].values() if x),
            "".join(e["quality"][k] for k in ("reusable", "executable", "verifiable", "clear_io")),
            "".join(g["quality"].get(k, "?") for k in ("reusable", "executable", "verifiable", "clear_io")),
            ("%s→%s" % (e["author"], g["author"])) if e["author"] != g["author"] else g["author"]))
    L.append("")
    L.append("（四条件顺序：可复用 / 可执行 / 可验证 / IO明确）")
    L.append("")
    L.append("## 四、差异明细")
    L.append("")
    if c["errors"]:
        L.append("**完整性差异：**")
        for fx, slot, kind in c["errors"]:
            L.append("- `%s` / %s：%s" % (fx, slot, kind))
    else:
        L.append("**完整性差异：无**")
    L.append("")
    if res["quality_errors"]:
        L.append("**质量差异：**")
        for fx, crit, kind, exp_g, got_g in res["quality_errors"]:
            L.append("- `%s` / %s：%s（人工 %s，机器 %s）" % (fx, crit, kind, exp_g, got_g))
    else:
        L.append("**质量差异：无**")
    L.append("")
    if res["author"]["errors"]:
        L.append("**作者识别差异：**")
        for fx, exp_a, got_a in res["author"]["errors"]:
            L.append("- `%s`：人工 %s，机器 %s" % (fx, exp_a, got_a))
    L.append("")
    L.append("## 五、这份数字意味着什么")
    L.append("")
    L.append("- **精确匹配率不是越高越好**：本测试集故意包含边界样本，"
             "若达到 100% 反而说明测试集太简单（或标签是照着输出填的）。")
    L.append("- **真正要压到 0 的是「严重误判」**：把 ❌ 判成 ✅ 会让不合格提交蒙混过关，"
             "把 ✅ 判成 ❌ 会冤枉合格提交，这两类代价远高于差一级。")
    L.append("- **残留差异全部列出**，不做选择性汇报——见第四节。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(GOLDEN))
    ap.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    rubric = load_yaml(Path(args.rubric))
    res = run(Path(args.golden), Path(args.rubric))
    md = render(res, rubric)
    print(md)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8", newline="\n")
        print("\n已写入 %s" % args.out, file=sys.stderr)

    q = res["quality"]
    n_q = sum(v["n"] for v in q.values())
    severe = sum(v["severe"] for v in q.values())
    ok = res["completeness"]["accuracy"] >= 0.9 and (severe / n_q if n_q else 1) <= 0.1
    print("\n门槛：完整性准确率 ≥90%% 且 严重误判率 ≤10%% → %s"
          % ("通过" if ok else "未通过"), file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
