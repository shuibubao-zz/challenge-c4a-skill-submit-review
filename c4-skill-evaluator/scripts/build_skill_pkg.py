#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_skill_pkg.py — 打包 c4-skill-evaluator.skill 并防漂移。

铁律（P0 事故教训，不可删）：
    **改过 rubric / 代码之后必须重新打包，打包后必须重跑自检与黄金集。**
    否则仓库内与 .skill 内是两份会各自漂移的 rubric——
    v4 的 P0（rubric 加行内注释 → 无 PyYAML 环境崩溃）正是"改了库内、没同步包内"造成的。

用法:
    python build_skill_pkg.py            # 重新打包（覆盖 C4A 根目录的 .skill）
    python build_skill_pkg.py --check    # 只校验：包内文件与仓库当前文件是否一致
"""

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
PKG_PATH = SKILL_DIR.parent / "c4-skill-evaluator.skill"
EXCLUDE = {".DS_Store", "Thumbs.db", "__pycache__"}


def collect_files() -> list[Path]:
    files = sorted(p for p in SKILL_DIR.rglob("*")
                   if p.is_file() and not (set(p.parts) & EXCLUDE)
                   and p.name not in EXCLUDE and "__pycache__" not in p.parts)
    return files


def build() -> int:
    files = collect_files()
    with zipfile.ZipFile(PKG_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(SKILL_DIR).as_posix())
    print("已打包 %d 个文件 → %s（%d 字节）"
          % (len(files), PKG_PATH, PKG_PATH.stat().st_size))
    print("\n⚠️ 别忘了验收三连：")
    print("  1) python scripts/c4_evaluator.py selftest        # 16/16")
    print("  2) python scripts/golden_test.py                  # 与 evidence/ 留档一致")
    print("  3) 解包到另一目录再跑一遍（见 evidence/skill安装验证.txt）")
    return 0


def check() -> int:
    if not PKG_PATH.exists():
        print("ERROR: 找不到 %s，先打包" % PKG_PATH, file=sys.stderr)
        return 1
    files = collect_files()
    with zipfile.ZipFile(PKG_PATH) as z:
        zipped = {n: z.read(n) for n in z.namelist()}
    local = {f.relative_to(SKILL_DIR).as_posix(): f.read_bytes() for f in files}
    drift = []
    for n in sorted(set(local) | set(zipped)):
        if n not in zipped:
            drift.append("仓库新增/包内缺失: %s" % n)
        elif n not in local:
            drift.append("包内多余/仓库已删: %s" % n)
        elif hashlib.sha256(local[n]).hexdigest() != hashlib.sha256(zipped[n]).hexdigest():
            drift.append("内容漂移: %s" % n)
    if drift:
        print("检测到 %d 处漂移（改完 rubric/代码后没重新打包？）：" % len(drift))
        for d in drift:
            print("  -", d)
        print("→ 运行 python scripts/build_skill_pkg.py 重新打包")
        return 1
    print("OK：包内 %d 个文件与仓库完全一致" % len(local))
    return 0


def main():
    ap = argparse.ArgumentParser(description="打包/校验 c4-skill-evaluator.skill")
    ap.add_argument("--check", action="store_true", help="只校验不打包")
    args = ap.parse_args()
    sys.exit(check() if args.check else build())


if __name__ == "__main__":
    main()
