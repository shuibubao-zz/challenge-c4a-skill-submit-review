#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
c4_evaluator.py — C4 技能提交自动评审器（Level 1–4）

用法:
    python c4_evaluator.py evaluate <FOLDER> [--out DIR] [--rubric PATH] [--json PATH] [--no-excel]
    python c4_evaluator.py selftest                 # 内置一致性断言
    python c4_evaluator.py golden  <GOLDEN_DIR>     # 对黄金测试集测误判率

设计要点（详见 卢怡然_C4A_方案设计.md）：
  · 零强制依赖可运行：有 PyYAML 优先用 PyYAML，没有则降级到内置迷你解析器；
    openpyxl 同为可选增强，缺失时自动降级
  · 规则确定型（可复现、可测试、可审计），并预留 LLM 深审接口
  · 每个判定都必须带证据（文件名 + 行号），无证据不计数
  · 检测项布尔封顶：重复关键词不累加（防堆砌）
  · 负信号硬地板：硬编码路径/密钥直接封顶，关键词法最容易漏的一类
  · 区分「确实缺失」与「机器查不到」（cannot_verify）
"""

from __future__ import annotations

import argparse
import json
import os
import py_compile
import re
import statistics
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_RUBRIC = SKILL_DIR / "references" / "c4_rubric_v2.yaml"

TEXT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".sh", ".js", ".ts", ".csv", ".log"}
ARCHIVE_EXT = {".skill", ".zip"}
MEDIA_EXT = {".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp3", ".m4a", ".wav"}
BINARY_EXT = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}

MAX_TEXT_BYTES = 400_000      # 单文件最多读 400KB，防止超大文件拖慢
EMPTY_FILE_BYTES = 1          # < 1 字节视为空文件

# 作者识别：`姓名_C4_内容` —— 基座正则只允许 [A-Za-z-]，中文名会全部落入 Unknown
# v2 改造：允许 CJK、点、空格下划线
NAMING_RE = re.compile(r"^(?P<author>.+?)_(?P<cid>C4[A-Z]?|c4[a-z]?)_(?P<part>.+)$")
VERSION_RE = re.compile(r"(?:^|[_\-])(v\d+)(?:[_\-.]|$)", re.IGNORECASE)
C4_MARK_RE = re.compile(r"(?:^|[^A-Za-z0-9])C4[A-Z]?(?:[^A-Za-z0-9]|$)")

# 技能包内部的常规子目录：它们**不是作者名**（v3 修复）
# 旧逻辑：父目录名直接当作者 → 评审单个技能包时会把 scripts/ references/ 当成三个作者
SKILL_SUBDIRS = {
    "scripts", "references", "assets", "docs", "examples", "example",
    "src", "tests", "test", "data", "evidence", "demo", "demos", "lib", "bin",
}
# 单技能包根目录标记：命中即认为「整个文件夹 = 一份提交」
SKILL_ROOT_MARKERS = ("SKILL.md", "skill.md", "SKILL.MD")


# --------------------------------------------------------------------------
# YAML 加载（优先 PyYAML，缺失时用内置迷你解析器）
# --------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return _mini_yaml(text)


def _strip_inline_comment(line: str) -> str:
    """去掉行内注释，保留引号字符串里的 `#`。

    规则与 YAML 一致：`#` 只有在行首或**前一个字符是空白**、且不在引号内时才是注释起点。
    P0 事故教训（v4）：`min_content_hits: 2   # R1：...` 曾把注释一起当值解析，
    导致 int('2 # R1…') 崩溃 —— rubric 里写行内注释是合法操作，解析器必须扛得住。
    """
    out = []
    q = None
    prev = ""
    for ch in line:
        if q:
            out.append(ch)
            if ch == q:
                q = None
            prev = ch
            continue
        if ch == "#" and (prev == "" or prev.isspace()):
            break
        if ch in "\"'":
            q = ch
        out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _mini_yaml(text: str) -> dict:
    """极简 YAML 子集解析器：映射 / 列表 / 内联列表 / 引号字符串 / 注释。

    只为在无 PyYAML 环境下仍能读取 references/c4_rubric_v2.yaml。
    不支持：块标量 | > 、锚点、多行折叠。
    """
    root: dict = {}
    stack = [(-1, root)]          # (indent, container)
    # 先统一剥离行内注释（引号感知），后续所有分支拿到的都是干净行
    lines = [_strip_inline_comment(l) for l in text.splitlines()]
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if line.startswith("- "):
            item_src = line[2:].strip()
            if not isinstance(container, list):
                continue
            if ":" in item_src and not item_src.startswith(('"', "'", "[")):
                # 列表项是一个映射：收集后续同缩进（更深）的键
                mapping: dict = {}
                k, v = _split_kv(item_src)
                mapping[k] = _scalar(v)
                sub_indent = indent + 2
                while i < len(lines):
                    nxt = lines[i]
                    if not nxt.strip() or nxt.lstrip().startswith("#"):
                        i += 1
                        continue
                    nind = len(nxt) - len(nxt.lstrip(" "))
                    if nind < sub_indent:
                        break
                    if nind > sub_indent:
                        i += 1
                        continue
                    s = nxt.strip()
                    if s.startswith("- "):
                        i += 1
                        continue
                    k2, v2 = _split_kv(s)
                    mapping[k2] = _scalar(v2)
                    i += 1
                container.append(mapping)
            else:
                container.append(_scalar(item_src))
            continue

        if ":" not in line:
            continue
        key, val = _split_kv(line)
        if val == "":
            # 可能是下一行的列表（缩进更深）
            nxt_indent = None
            for j in range(i, len(lines)):
                if lines[j].strip() and not lines[j].lstrip().startswith("#"):
                    nxt_indent = len(lines[j]) - len(lines[j].lstrip(" "))
                    break
            if nxt_indent is not None and nxt_indent > indent and lines[j].strip().startswith("- "):
                container[key] = []
                stack.append((indent, container[key]))
            else:
                container[key] = {}
                stack.append((indent, container[key]))
        else:
            container[key] = _scalar(val)
    return root


def _split_kv(s: str):
    if s.startswith(('"', "'")):
        q = s[0]
        end = s.find(q, 1)
        if end != -1:
            rest = s[end + 1:].lstrip()
            if rest.startswith(":"):
                return _scalar(s[: end + 1]), rest[1:].strip()
    idx = s.find(":")
    if idx == -1:
        return s, ""
    return s[:idx].strip(), s[idx + 1:].strip()


def _scalar(v: str):
    v = v.strip()
    if not v:
        return ""
    # 兜底（P0 教训）：即使行级注释剥离漏了什么，解析前再剥一次行内注释。
    # 注意必须用引号感知的剥离 —— 正则值里的 `#`（如 "^#\s*..."）不是注释。
    if "#" in v:
        v = _strip_inline_comment(v).strip()
        if not v:
            return ""
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1]
        out, buf, q, esc = [], "", None, False
        for ch in inner:
            if esc:
                buf += ch
                esc = False
                continue
            if ch == "\\" and q:
                buf += ch          # 引号内的转义（如 \"）原样保留，交给 _unescape
                esc = True
                continue
            if q:
                if ch == q:
                    q = None
                buf += ch
            elif ch in "\"'":
                q = ch
                buf += ch
            elif ch == ",":
                if buf.strip():
                    out.append(_scalar(buf.strip()))
                buf = ""
            else:
                buf += ch
        if buf.strip():
            out.append(_scalar(buf.strip()))
        # 递归 _scalar 已对每个元素做过转义还原，这里不能再来一遍（双重还原曾把
        # `\\n` 错变成真换行、`\\\\` 错变成单个反斜杠，与 PyYAML 不一致）
        return out
    if v.startswith(('"', "'")) and v.endswith(('"', "'")) and len(v) >= 2:
        return _unescape(v[1:-1])
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if v in ("null", "~", "None"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return _unescape(v)


def _unescape(s: str) -> str:
    """顺序转义还原（与 PyYAML 双引号标量行为对齐：\n \t \\ \' \"）。

    v4 修复：旧实现只处理引号与反斜杠，"---\\nname:" 里的 \\n 不会被还原成换行，
    与 PyYAML 结果不一致 —— 强化后的自检（深度比对）抓住了它。
    """
    if "\\" not in s:
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt in ("\\", '"', "'"):
                out.append(nxt)
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Step 1 — 采集与作者识别
# --------------------------------------------------------------------------

def scan_folder(folder: Path) -> list[dict]:
    items = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file():
            continue
        if f.name.startswith(".") or f.name.startswith("__"):
            continue      # 跳过隐藏文件与 __expected__.json 之类的标注文件
        try:
            stat = f.stat()
        except OSError:
            continue
        rel = str(f.relative_to(folder)).replace("\\", "/")
        ext = f.suffix.lower()
        items.append({
            "rel": rel,
            "name": f.stem,
            "fullname": f.name,
            "ext": ext,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
            "abs": str(f),
            "parent": f.parent.name,
        })
    return items


def is_c4_related(item: dict, folder: Path) -> bool:
    """判断文件是否属于 C4 提交。"""
    name = item["fullname"]
    if NAMING_RE.match(item["name"]):
        return True
    if C4_MARK_RE.search(name):
        return True
    # 子文件夹按作者分组（文件夹名不是 C4 本身）
    top = item["rel"].split("/")[0] if "/" in item["rel"] else ""
    if top and C4_MARK_RE.search(top):
        return True
    return False


def identify_author(item: dict, root_name: str = "") -> tuple[str, str]:
    """返回 (author, method)。基座只认 ASCII 姓名，v2 支持中文 + 三级回退。"""
    m = NAMING_RE.match(item["name"])
    if m:
        return m.group("author").strip(), "filename"
    # 回退 1：父文件夹名（按作者分目录的常见做法）
    # 安全约束 A：父文件夹若就是扫描根目录，说明是「所有人文件平铺在一个群里」，
    #             此时用文件夹名会把全班合并成一个作者 —— 必须放弃该回退。
    # 安全约束 B（v3 修复）：父文件夹若是技能包的常规子目录（scripts/references/...），
    #             它不是作者名 —— 改为取再上一层（技能包目录名）。
    parts = item["rel"].split("/")
    parent = item["parent"]
    if parent and parent != root_name and parent.lower() not in ("c4", "c4a", "submissions", "提交"):
        if parent.lower() in SKILL_SUBDIRS and len(parts) >= 3:
            grand = parts[-3]
            if grand and grand.lower() not in SKILL_SUBDIRS:
                return grand, "parent_folder(跳过技能子目录)"
            # 形如 技能包/scripts/x.py：parts[-3] 就是包目录，已处理
            return root_name, "parent_folder(技能包根目录)"
        return parent, "parent_folder"
    # 回退 2：文档首行里的 作者/Author/姓名
    if item["ext"] in TEXT_EXT:
        head = read_text(Path(item["abs"]), limit=4000)
        m2 = re.search(r"(?:作者|Author|姓名|Name)\s*[:：]\s*([^\s，,。\n]{1,20})", head)
        if m2:
            return m2.group(1).strip(), "content_header"
    return "Unknown", "unresolved"


def _confidence(method: str) -> str:
    """作者识别方式的置信度（v3：新增单技能包两档，不再一律标 low）。"""
    if method in ("filename", "filename(单技能包内)"):
        return "high"
    if method == "single_skill_root":
        return "mid（来自技能包名，建议人工确认）"
    if method.startswith("parent_folder"):
        return "mid"
    return "low（需人工确认）"


def detect_single_skill(folder: Path) -> str | None:
    """判断「这个文件夹本身是不是一个技能包」。

    命中条件：根目录有 SKILL.md（大小写不敏感）或有 *.skill。
    返回这份提交的作者/提交名（优先 SKILL.md frontmatter 的 author / name，否则用目录名）；
    不是单技能包则返回 None（交给常规的作者分组逻辑）。

    为什么需要它：评审目标常常是**一个个技能包目录**而不是"全班文件平铺的文件夹"。
    旧逻辑按父目录名分组，会把 `scripts/` `references/` 当成三个作者 —— 报告直接不可信。
    """
    root_skill = next((folder / m for m in SKILL_ROOT_MARKERS if (folder / m).is_file()), None)
    has_pkg = any(folder.glob("*.skill"))
    if not (root_skill or has_pkg):
        return None

    label = None
    if root_skill is not None:
        try:
            head = root_skill.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            head = ""
        m = re.search(r"^author\s*[:：]\s*(.+)$", head, re.MULTILINE)
        if m:
            label = m.group(1).strip().strip("\"'")
        if not label:
            m = re.search(r"^name\s*[:：]\s*(.+)$", head, re.MULTILINE)
            if m:
                label = m.group(1).strip().strip("\"'")
    return label or folder.name


def detect_version(item: dict) -> str:
    m = VERSION_RE.search(item["name"])
    return m.group(1).lower() if m else "v1"


# --------------------------------------------------------------------------
# 文本读取
# --------------------------------------------------------------------------

def read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except Exception:
        return ""


def load_corpus(files: list[dict]) -> dict[str, str]:
    """读取所有可读文本，返回 {rel: content}。"""
    corpus = {}
    for f in files:
        if f["size"] < EMPTY_FILE_BYTES:
            continue
        if f["ext"] in TEXT_EXT:
            corpus[f["rel"]] = read_text(Path(f["abs"]))
        elif f["ext"] in ARCHIVE_EXT:
            txt = read_skill_member(Path(f["abs"]), "SKILL.md")
            if txt:
                corpus[f["rel"] + "::SKILL.md"] = txt
    return corpus


def read_skill_member(path: Path, member_hint: str) -> str:
    """从 .skill/.zip 中读取 SKILL.md（真实结构校验，非关键词）。"""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            cand = [n for n in names if n.endswith(member_hint)]
            if not cand:
                return ""
            cand.sort(key=len)
            return z.read(cand[0]).decode("utf-8", errors="replace")
    except Exception:
        return ""


def find_evidence(corpus: dict[str, str], terms, use_regex: bool = False):
    """在语料中查找任一条目，返回 (命中与否, '文件:行号') 证据。"""
    for term in terms or []:
        try:
            pat = re.compile(term, re.IGNORECASE) if use_regex else None
        except re.error:
            pat = None
        for rel, content in corpus.items():
            if use_regex:
                if pat is None:
                    continue
                m = pat.search(content)
                if m:
                    line = content.count("\n", 0, m.start()) + 1
                    return True, "%s:%d" % (rel, line)
            else:
                idx = content.lower().find(term.lower())
                if idx != -1:
                    line = content.count("\n", 0, idx) + 1
                    return True, "%s:%d" % (rel, line)
    return False, None


# --------------------------------------------------------------------------
# Step 2 — 完整性检查
# --------------------------------------------------------------------------

def check_completeness(files: list[dict], corpus: dict[str, str], rubric: dict) -> dict:
    spec = rubric["completeness"]
    result = {}
    for slot, cfg in spec.items():
        pats = cfg.get("filename_patterns") or []
        exts = set(e.lower() for e in (cfg.get("preferred_ext") or []))
        min_hits = int(cfg.get("min_content_hits") or 0)
        signals = cfg.get("content_signals") or []

        matched, method, evidence = None, None, None
        # ① 文件名命中（且文件非空）
        for f in files:
            if f["size"] < EMPTY_FILE_BYTES:
                continue
            low = f["fullname"].lower()
            if any(p.lower() in low for p in pats):
                matched, method = f, "filename"
                evidence = f["rel"]
                break
        # ② 扩展名命中（如 demo 的 .png/.mp4）
        if matched is None:
            for f in files:
                if f["size"] < EMPTY_FILE_BYTES:
                    continue
                if f["ext"] in exts and any(p.lower() in f["fullname"].lower() for p in pats):
                    matched, method = f, "filename+ext"
                    evidence = f["rel"]
                    break
        # ③ 内容信号（需满足 min_content_hits 个不同信号）
        if matched is None and signals:
            best = (0, None)
            for rel, content in corpus.items():
                hits = sum(1 for s in signals if s.lower() in content.lower())
                if hits > best[0]:
                    best = (hits, rel)
            if best[0] >= max(1, min_hits) and best[1]:
                matched = next((f for f in files if f["rel"] == best[1].split("::")[0]), None)
                method = "content(%d signals)" % best[0]
                evidence = best[1]

        empty_hit = matched is not None and matched["size"] < EMPTY_FILE_BYTES
        result[slot] = {
            "label": cfg["label_cn"],
            "present": matched is not None and not empty_hit,
            "file": matched["rel"] if matched else None,
            "method": method,
            "evidence": evidence,
            "note": ("文件为 0 字节，视为未提交" if empty_hit else None),
        }
    return result


# --------------------------------------------------------------------------
# Step 3 — 质量评审（四条件）
# --------------------------------------------------------------------------

def real_check_syntax(files: list[dict]) -> tuple[bool | None, str | None]:
    """真实语法校验：.py 走 py_compile。无 .py 文件 → n/a(None)。"""
    pyfiles = [f for f in files if f["ext"] == ".py" and f["size"] >= EMPTY_FILE_BYTES]
    if not pyfiles:
        return None, None
    ok_all, first_ok, first_bad = True, None, None
    for f in pyfiles:
        try:
            py_compile.compile(f["abs"], cfile=tempfile.mktemp(suffix=".pyc"), doraise=True)
            if first_ok is None:
                first_ok = f["rel"]
        except Exception as e:
            ok_all = False
            if first_bad is None:
                first_bad = "%s (%s)" % (f["rel"], type(e).__name__)
    if ok_all:
        return True, "py_compile 通过：%s" % first_ok
    return False, "语法错误：%s" % first_bad


def real_check_skill_pkg(files: list[dict]) -> tuple[bool | None, str | None]:
    """.skill 包真实结构校验。无 .skill/.zip → n/a(None)。"""
    pkgs = [f for f in files if f["ext"] in ARCHIVE_EXT and f["size"] >= EMPTY_FILE_BYTES]
    if not pkgs:
        return None, None
    reports = []
    ok = True
    for f in pkgs:
        try:
            with zipfile.ZipFile(f["abs"]) as z:
                bad = z.testzip()
                names = z.namelist()
                has_skill = any(n.endswith("SKILL.md") for n in names)
                if bad is not None:
                    ok = False
                    reports.append("%s: 损坏成员 %s" % (f["rel"], bad))
                elif not has_skill:
                    ok = False
                    reports.append("%s: 缺少 SKILL.md" % f["rel"])
                else:
                    reports.append("%s: OK（%d 个成员，含 SKILL.md）" % (f["rel"], len(names)))
        except Exception as e:
            ok = False
            reports.append("%s: 无法解析（%s）" % (f["rel"], type(e).__name__))
    return ok, "; ".join(reports)


def real_check_demo(files: list[dict]) -> tuple[bool | None, str | None]:
    """Demo 真实性：存在但为空/过小 → False；不存在 → n/a（完整性已判罚，避免双重扣分）。"""
    demos = [f for f in files if f["ext"] in MEDIA_EXT]
    if not demos:
        return None, None
    bad = [f["rel"] for f in demos if f["size"] < 1024]
    if bad:
        return False, "疑似占位：%s" % ", ".join(bad)
    return True, "%d 个媒体文件，最小 %d 字节" % (len(demos), min(f["size"] for f in demos))


def has_skill_pkg(files: list[dict]) -> bool:
    return any(f["ext"] in ARCHIVE_EXT or f["fullname"].lower() == "skill.md" for f in files)


REAL_CHECKS = {
    "syntax_ok": real_check_syntax,
    "skill_pkg_valid": real_check_skill_pkg,
    "demo_real": real_check_demo,
}


def grade_ratio(passed: int, applicable: int, thresholds: dict) -> str:
    if applicable == 0:
        return "⚠️"          # 无可判项 → 不给满分也不给零分，标注需人工
    r = passed / applicable
    if r >= float(thresholds.get("pass", 0.75)):
        return "✅"
    if r >= float(thresholds.get("partial", 0.40)):
        return "⚠️"
    return "❌"


def apply_negative_cap(grade: str, corpus: dict[str, str], negatives: list,
                       details: list | None = None) -> tuple[str, list]:
    """负信号硬地板：命中则把评级压到 cap 对应的等级（只降不升）。

    两种触发源：
      · pattern —— 在语料中正则命中（如硬编码路径 / 明文密钥）
      · real_ref —— 引用某个「真实校验」检测项的结果（如 py_compile 失败）
    real_ref 是 v2 的关键补强：代码跑不起来时，无论其它项多漂亮，可执行都必须是 ❌。
    """
    order = {"❌": 0, "⚠️": 1, "✅": 2}
    flags = []
    by_id = {d["id"]: d for d in (details or [])}
    for neg in negatives or []:
        cap = float(neg.get("cap", 0.5))
        cap_grade = "✅" if cap >= 1.0 else ("⚠️" if cap >= 0.5 else "❌")
        ref = neg.get("real_ref")
        if ref:
            d = by_id.get(ref)
            if d and d.get("state") == "fail":
                if order[cap_grade] < order[grade]:
                    grade = cap_grade
                flags.append({"id": neg["id"], "severity": neg.get("severity"),
                              "evidence": d.get("evidence"), "cap": cap, "source": "real:" + ref})
            continue
        pat = neg.get("pattern")
        if not pat:
            continue
        hit, ev = find_evidence(corpus, [pat], use_regex=True)
        if hit:
            if order[cap_grade] < order[grade]:
                grade = cap_grade
            flags.append({"id": neg["id"], "severity": neg.get("severity"), "evidence": ev,
                          "cap": cap, "source": "pattern"})
    return grade, flags


def evaluate_quality(files: list[dict], corpus: dict[str, str], rubric: dict,
                     completeness: dict | None = None) -> dict:
    spec = rubric["quality"]
    thresholds = rubric["grading"]["thresholds"]
    out = {}
    for crit, cfg in spec.items():
        items, details = [], []
        for it in cfg.get("check_items") or []:
            cid = it.get("id")
            # 仅在存在 .skill 包 / SKILL.md 时才适用的检测项，否则记 n/a
            if it.get("requires_skill_pkg") and not has_skill_pkg(files):
                details.append({"id": cid, "label": it["label"], "state": "n/a",
                                "evidence": "无 .skill 包或 SKILL.md，该项不适用"})
                continue
            if it.get("real"):
                fn = REAL_CHECKS.get(cid)
                if fn is None:
                    continue
                val, ev = fn(files)
                if val is None:
                    details.append({"id": cid, "label": it["label"], "state": "n/a",
                                    "evidence": "无适用文件"})
                    continue
                items.append(1 if val else 0)
                details.append({"id": cid, "label": it["label"],
                                "state": "pass" if val else "fail", "evidence": ev})
                continue

            if "none_of_regex" in it or "none_of" in it:
                if "none_of_regex" in it:
                    hit, ev = find_evidence(corpus, it["none_of_regex"], use_regex=True)
                    terms = it["none_of_regex"]
                else:
                    hit, ev = find_evidence(corpus, it["none_of"])
                    terms = it["none_of"]
                val = not hit
                ev = ("未发现：" + ", ".join(terms)[:60]) if val else ("命中禁词 @ " + str(ev))
            else:
                # any_of 与 any_of_regex 取并集：任一命中即算命中（补召回）
                hit, ev = find_evidence(corpus, it.get("any_of") or [])
                if not hit and it.get("any_of_regex"):
                    hit, ev = find_evidence(corpus, it["any_of_regex"], use_regex=True)
                val = hit
            items.append(1 if val else 0)
            details.append({"id": cid, "label": it["label"],
                            "state": "pass" if val else "fail", "evidence": ev})

        passed = sum(items)
        applicable = len(items)
        grade = grade_ratio(passed, applicable, thresholds)
        neg_flags = []
        if not corpus:
            grade = "⚠️"      # 无可读文本语料 → 机器无法评审，不给零分
            details.append({"id": "no_corpus", "label": "无可读文本语料", "state": "n/a",
                            "evidence": "该作者未提交任何可读文本文件"})
        else:
            grade, neg_flags = apply_negative_cap(grade, corpus, cfg.get("negative") or [], details)

        # 跨维度耦合：连可执行内容都没有，就谈不上「别人拿过去能复用」
        if (crit == "reusable" and completeness
                and not completeness.get("executable_content", {}).get("present")
                and grade == "✅"):
            grade = "⚠️"
            neg_flags.append({"id": "no_executable_content", "severity": "cap_warn",
                              "evidence": "完整性检查未发现可执行内容", "cap": 0.5,
                              "source": "cross-criterion"})

        out[crit] = {
            "label_cn": cfg["label_cn"],
            "weight": cfg.get("weight", 0.25),
            "grade": grade,
            "passed": passed,
            "applicable": applicable,
            "details": details,
            "negative_flags": neg_flags,
        }
    return out


# --------------------------------------------------------------------------
# 反作弊信号
# --------------------------------------------------------------------------

def detect_gaming(corpus: dict[str, str], rubric: dict) -> list[str]:
    warns = []
    thr = int((rubric.get("anti_gaming") or {}).get("stuffing", {}).get("threshold", 12))
    counts = Counter()
    for rel, content in corpus.items():
        low = content.lower()
        for kw in ("输入", "输出", "安装", "测试", "示例", "步骤", "input", "output", "test"):
            c = low.count(kw.lower())
            if c >= thr:
                counts[kw] += c
    for kw, c in counts.items():
        warns.append("疑似关键词堆砌：「%s」在单文件中出现 %d 次（阈值 %d）—— 检测项为布尔封顶，堆砌不加分"
                     % (kw, c, thr))
    # 内容重复
    sim = float((rubric.get("anti_gaming") or {}).get("duplicate_content", {}).get("threshold", 0.9))
    rels = list(corpus.keys())
    for i in range(len(rels)):
        for j in range(i + 1, len(rels)):
            a, b = corpus[rels[i]], corpus[rels[j]]
            if not a or not b:
                continue
            n = min(len(a), len(b))
            if n < 200:
                continue
            same = sum(1 for x, y in zip(a[:n], b[:n]) if x == y)
            if same / n >= sim:
                warns.append("疑似一稿多投：%s 与 %s 前 %d 字符相似度 %.2f" % (rels[i], rels[j], n, same / n))
    return warns


# --------------------------------------------------------------------------
# 综合评分
# --------------------------------------------------------------------------

def composite(completeness: dict, quality: dict, rubric: dict) -> dict:
    g = rubric["grading"]
    score_map = g["score_map"]
    n_present = sum(1 for v in completeness.values() if v["present"])
    c_score = n_present / max(1, len(completeness))
    q_score = sum(score_map[v["grade"]] * v["weight"] for v in quality.values())
    w_c = float(g["composite"]["w_completeness"])
    w_q = float(g["composite"]["w_quality"])
    total = c_score * w_c * 100 + q_score * w_q * 100
    return {
        "n_present": n_present,
        "n_total": len(completeness),
        "completeness_score": round(c_score, 4),
        "quality_score": round(q_score, 4),
        "total": round(total, 1),
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def evaluate_folder(folder: Path, rubric_path: Path = DEFAULT_RUBRIC) -> dict:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise SystemExit("ERROR: '%s' 不是目录" % folder)
    rubric = load_yaml(Path(rubric_path))

    items = scan_folder(folder)
    c4_files = [it for it in items if is_c4_related(it, folder)]
    non_c4 = [it for it in items if it not in c4_files]
    fallback_all = False
    if not c4_files and items:
        # 边界：整个文件夹里没有任何 _C4_ 标记（命名完全不规范）
        # → 不静默返回空报告，而是整体视为一份提交并明确标注，交由人工确认
        c4_files, non_c4, fallback_all = items, [], True

    # 作者分组
    root_name = folder.name
    authors: dict[str, list[dict]] = defaultdict(list)
    methods: dict[str, Counter] = defaultdict(Counter)

    # v3 新增：单技能包模式 —— 根目录含 SKILL.md（或 *.skill）时，
    # 整个文件夹就是**一份**提交，不能按子目录拆成 scripts / references 多个"作者"。
    single_label = detect_single_skill(folder)
    if single_label:
        # 文件名里若明确写了「姓名_C4_xxx」，作者以文件名为准（人工标注/黄金集都依赖它）；
        # 单技能包的包名只在**文件名解析不出作者**时才作为标签。
        name_authors = set()
        for it in c4_files:
            _m = NAMING_RE.match(it["name"])
            if _m:
                name_authors.add(_m.group("author").strip())
        from_name = (len(name_authors) == 1)
        label = name_authors.pop() if from_name else single_label
        method = "filename(单技能包内)" if from_name else "single_skill_root"
        for it in c4_files:
            it["author"] = label
            it["version"] = detect_version(it)
            authors[label].append(it)
            methods[label][method] += 1
    else:
        for it in c4_files:
            a, m = identify_author(it, root_name)
            it["author"] = a
            it["version"] = detect_version(it)
            authors[a].append(it)
            methods[a][m] += 1

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "folder": str(folder),
        "rubric_version": rubric.get("version"),
        "n_files_total": len(items),
        "n_files_c4": len(c4_files),
        "n_files_non_c4": len(non_c4),
        "non_c4_files": [x["rel"] for x in non_c4][:30],
        "fallback_all_files": fallback_all,
        "authors": [],
    }

    for author in sorted(authors.keys()):
        files = authors[author]
        corpus = load_corpus(files)
        comp = check_completeness(files, corpus, rubric)
        qual = evaluate_quality(files, corpus, rubric, comp)
        comp_score = composite(comp, qual, rubric)
        versions = sorted({f["version"] for f in files})
        report["authors"].append({
            "author": author,
            "id_method": methods[author].most_common(1)[0][0],
            "id_confidence": (_confidence(methods[author].most_common(1)[0][0])),
            "n_files": len(files),
            "versions": versions,
            "files": [{"rel": f["rel"], "ext": f["ext"], "size": f["size"], "version": f["version"]}
                      for f in files],
            "completeness": comp,
            "quality": qual,
            "score": comp_score,
            "gaming_warnings": detect_gaming(corpus, rubric),
            "suggestions": build_suggestions(comp, qual, rubric),
        })

    report["authors"].sort(key=lambda a: -a["score"]["total"])

    # 班级级统计
    if report["authors"]:
        report["class_stats"] = {
            "n_authors": len(report["authors"]),
            "n_complete_5": sum(1 for a in report["authors"] if a["score"]["n_present"] == 5),
            "n_partial": sum(1 for a in report["authors"]
                             if 3 <= a["score"]["n_present"] < 5),
            "n_insufficient": sum(1 for a in report["authors"] if a["score"]["n_present"] < 3),
            "avg_total": round(statistics.mean(a["score"]["total"] for a in report["authors"]), 1),
            "most_missing": missing_counter(report, rubric),
            "weakest_criterion": weakest(report),
        }
    else:
        report["class_stats"] = {"n_authors": 0}
    return report


def missing_counter(report: dict, rubric: dict) -> list[list]:
    c = Counter()
    for a in report["authors"]:
        for slot, v in a["completeness"].items():
            if not v["present"]:
                c[rubric["completeness"][slot]["label_cn"]] += 1
    return [[k, v] for k, v in c.most_common()]


def weakest(report: dict) -> str | None:
    c = Counter()
    tot = Counter()
    for a in report["authors"]:
        for crit, v in a["quality"].items():
            tot[v["label_cn"]] += 1
            if v["grade"] != "✅":
                c[v["label_cn"]] += 1
    if not tot:
        return None
    return max(tot.keys(), key=lambda k: c[k] / tot[k])


def build_suggestions(comp: dict, qual: dict, rubric: dict) -> list[str]:
    s = []
    for slot, v in comp.items():
        if not v["present"]:
            s.append("补齐 **%s**%s" % (v["label"], "（检测到 0 字节文件，有文件名不等于有内容）"
                                        if v.get("note") else ""))
    prio = {"❌": 0, "⚠️": 1, "✅": 2}
    for crit, v in sorted(qual.items(), key=lambda kv: prio[kv[1]["grade"]]):
        if v["grade"] == "✅":
            continue
        failed = [d for d in v["details"] if d["state"] == "fail"]
        if failed:
            names = "、".join(d["label"] for d in failed[:3])
            s.append("**%s**（%s）：建议补 %s" % (v["label_cn"], v["grade"], names))
        for nf in v.get("negative_flags") or []:
            s.append("**%s** 触发负信号 `%s`（证据 %s）——需人工确认是否为示例命令"
                     % (v["label_cn"], nf["id"], nf["evidence"]))
    if not s:
        s.append("五项齐全、四条件全 ✅ —— 建议补充：真实使用者反馈 / 跨环境复现记录")
    return s


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------

MARK = {"✅": "✅", "⚠️": "⚠️", "❌": "❌"}


def render_markdown(report: dict, rubric: dict) -> str:
    L = []
    cs = report.get("class_stats", {})
    L.append("# C4 提交自动评审报告")
    L.append("")
    L.append("生成时间：%s ｜ 评审器版本：c4-skill-evaluator v1.0 ｜ 评分标准：c4_rubric_v%d"
             % (report["generated_at"], report.get("rubric_version", 2)))
    L.append("")
    L.append("扫描路径：`%s` ｜ 识别提交：**%d 位作者 / %d 个 C4 文件**（非 C4 文件 %d 个）"
             % (report["folder"], cs.get("n_authors", 0), report["n_files_c4"], report["n_files_non_c4"]))
    L.append("")
    L.append("## 一、班级总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append("| 总提交人数 | %d |" % cs.get("n_authors", 0))
    L.append("| 完整提交（5/5） | %d |" % cs.get("n_complete_5", 0))
    L.append("| 部分提交（3–4/5） | %d |" % cs.get("n_partial", 0))
    L.append("| 严重缺失（<3/5） | %d |" % cs.get("n_insufficient", 0))
    L.append("| 平均综合分 | %.1f / 100 |" % cs.get("avg_total", 0))
    if cs.get("most_missing"):
        L.append("| 最常见缺失 | %s |" % "、".join("%s(×%d)" % (k, v) for k, v in cs["most_missing"][:3]))
    if cs.get("weakest_criterion"):
        L.append("| 最弱维度 | %s |" % cs["weakest_criterion"])
    L.append("")
    L.append("## 二、作者详情")
    L.append("")
    for i, a in enumerate(report["authors"], 1):
        L.append("### %d. %s" % (i, a["author"]))
        L.append("")
        L.append("识别方式：`%s`（置信度 %s） ｜ 文件数 %d ｜ 版本 %s"
                 % (a["id_method"], a["id_confidence"], a["n_files"], "/".join(a["versions"])))
        L.append("")
        L.append("**完整性检查 %d/5**" % a["score"]["n_present"])
        L.append("")
        L.append("| 必须文件 | 状态 | 匹配文件 | 判定方式 |")
        L.append("|---|---|---|---|")
        for slot, v in a["completeness"].items():
            L.append("| %s | %s | %s | %s |" % (
                v["label"], "✅" if v["present"] else "❌",
                ("`%s`" % v["file"]) if v["file"] else "—",
                v["method"] or "—"))
            if v.get("note"):
                L.append("| ↳ 备注 | | %s | |" % v["note"])
        L.append("")
        L.append("**质量评审（四条件）**")
        L.append("")
        L.append("| 条件 | 评级 | 检测项 | 依据 |")
        L.append("|---|---|---|---|")
        for crit, v in a["quality"].items():
            ev = "；".join(d["evidence"] for d in v["details"] if d["state"] == "pass" and d["evidence"])[:180]
            L.append("| %s | %s | %d/%d | %s |" % (
                v["label_cn"], v["grade"], v["passed"], v["applicable"], ev or "—"))
        L.append("")
        L.append("**综合分：%.1f / 100**（完整性 %.2f × 0.4 + 质量 %.2f × 0.6）"
                 % (a["score"]["total"], a["score"]["completeness_score"], a["score"]["quality_score"]))
        L.append("")
        if a["gaming_warnings"]:
            L.append("**反作弊信号：**")
            for w in a["gaming_warnings"]:
                L.append("- %s" % w)
            L.append("")
        L.append("**改进建议：**")
        for j, s in enumerate(a["suggestions"], 1):
            L.append("%d. %s" % (j, s))
        L.append("")
        L.append("---")
        L.append("")
    L.append("## 三、排名")
    L.append("")
    L.append("| 排名 | 作者 | 完整性 | 可复用 | 可执行 | 可验证 | IO明确 | 综合分 |")
    L.append("|---:|---|---|---|---|---|---|---:|")
    for i, a in enumerate(report["authors"], 1):
        q = a["quality"]
        L.append("| %d | %s | %d/5 | %s | %s | %s | %s | **%.1f** |" % (
            i, a["author"], a["score"]["n_present"],
            q["reusable"]["grade"], q["executable"]["grade"],
            q["verifiable"]["grade"], q["clear_io"]["grade"], a["score"]["total"]))
    L.append("")
    L.append("## 四、机器查不到的事（必须人工过一遍）")
    L.append("")
    for c in (rubric.get("cannot_verify") or []):
        L.append("- **%s**：%s" % (c["id"], c["desc"]))
    L.append("")
    L.append("> 本节是评审器的边界声明：规则只能查结构与信号，**「这个技能到底有没有用」需要人判断**。")
    L.append("> 任何 ✅ 都不代表「这个技能好」，只代表「它在该检测项上有可引用的证据」。")
    return "\n".join(L)


def write_excel(report: dict, out_path: Path) -> bool:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        return False
    wb = Workbook()
    hf = Font(name="Arial", bold=True, size=11, color="FFFFFF")
    fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    cf = Font(name="Arial", size=10)
    bd = Border(*[Side(style="thin")] * 4)

    ws = wb.active
    ws.title = "Ranking"
    heads = ["排名", "作者", "识别方式", "文件数", "完整性", "可复用", "可执行", "可验证", "IO明确", "综合分", "主要缺失"]
    for c, h in enumerate(heads, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font, cell.fill, cell.border = hf, fill, bd
    for i, a in enumerate(report["authors"], 2):
        miss = "、".join(v["label"] for v in a["completeness"].values() if not v["present"]) or "无"
        row = [i - 1, a["author"], a["id_method"], a["n_files"],
               "%d/5" % a["score"]["n_present"],
               a["quality"]["reusable"]["grade"], a["quality"]["executable"]["grade"],
               a["quality"]["verifiable"]["grade"], a["quality"]["clear_io"]["grade"],
               a["score"]["total"], miss]
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font, cell.border = cf, bd
    for c in range(1, len(heads) + 1):
        ws.column_dimensions[ws.cell(row=1, column=c).column_letter].width = 16
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("Detail")
    heads2 = ["作者", "维度", "检测项", "结果", "证据"]
    for c, h in enumerate(heads2, 1):
        cell = ws2.cell(row=1, column=c, value=h)
        cell.font, cell.fill, cell.border = hf, fill, bd
    r = 2
    for a in report["authors"]:
        for crit, v in a["quality"].items():
            for d in v["details"]:
                for c, val in enumerate([a["author"], v["label_cn"], d["label"], d["state"], d["evidence"] or "—"], 1):
                    cell = ws2.cell(row=r, column=c, value=val)
                    cell.font, cell.border = cf, bd
                r += 1
    for c in range(1, len(heads2) + 1):
        ws2.column_dimensions[ws2.cell(row=1, column=c).column_letter].width = 30
    ws2.freeze_panes = "A2"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return True


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def cmd_selftest(rubric_path: Path) -> int:
    rubric = load_yaml(rubric_path)
    cases = []

    def case(name, fn):
        try:
            fn()
            cases.append((name, True, ""))
        except AssertionError as e:
            cases.append((name, False, str(e)))

    def t_rubric_loaded():
        assert "completeness" in rubric and "quality" in rubric, "评分标准未加载完整"
        assert len(rubric["completeness"]) == 5, "完整性必须有 5 项"

    def t_mini_yaml():
        src = Path(rubric_path).read_text(encoding="utf-8")
        mini = _mini_yaml(src)
        try:
            import yaml  # type: ignore
        except ImportError:
            # 无 PyYAML：本断言若继续跑就退化为「mini 和 mini 比」的恒真空转。
            # 真检查交给 t_mini_yaml_key_fields_numeric（关键字段必须可数值化）。
            return
        ref = yaml.safe_load(src)
        assert mini == ref, "迷你 YAML 解析器与 PyYAML 深度比对不一致"

    def t_mini_yaml_key_fields_numeric():
        """P0 回归（v4）：无 PyYAML 环境下，mini 解析器必须完整解析本 rubric
        且关键字段可直接数值化 —— 行内注释曾让 int('2 # R1…') 崩溃。"""
        src = Path(rubric_path).read_text(encoding="utf-8")
        mini = _mini_yaml(src)
        assert mini["completeness"]["skill_doc"]["min_content_hits"] == 2, \
            "min_content_hits 未数值化（行内注释剥离失效）"
        assert mini["completeness"]["ai_log"]["min_content_hits"] == 2
        assert mini["grading"]["thresholds"]["pass"] == 0.75, "pass 阈值未数值化"
        assert mini["grading"]["thresholds"]["partial"] == 0.40, "partial 阈值未数值化"
        assert mini["quality"]["reusable"]["weight"] == 0.25
        caps = [n.get("cap") for n in mini["quality"]["reusable"].get("negative") or []]
        assert caps and all(isinstance(c, (int, float)) for c in caps), "negative.cap 未数值化"
        # 用 mini 解析结果跑主流程 = 无 PyYAML 环境的真实路径（曾在此崩溃）
        comp = check_completeness([], {}, mini)
        assert len(comp) == 5, "mini 解析的 rubric 无法驱动主流程"

    def t_struct_single_skill_pkg():
        """结构形态用例（v4 补强）：带子目录的技能包。
        回归背景：scripts/ references/ 曾被父目录回退当成两位作者。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "SKILL.md").write_text(
                "---\nname: demo-skill\nauthor: 张三\n---\n正文\n", encoding="utf-8")
            (d / "scripts").mkdir()
            (d / "scripts" / "run.py").write_text("print('x')\n", encoding="utf-8")
            (d / "references").mkdir()
            (d / "references" / "r.yaml").write_text("a: 1\n", encoding="utf-8")
            (d / "张三_C4_skill说明.md").write_text(
                "# 技能说明\n使用场景：x\n输入文件夹，输出报告\n", encoding="utf-8")
            rep = evaluate_folder(d, rubric_path)
            assert rep["class_stats"]["n_authors"] == 1, \
                "单技能包被拆成多个作者（scripts/references 当成了人）"
            assert rep["authors"][0]["author"] == "张三", "文件名作者未优先于包名"

    def t_struct_flat_dir():
        """结构形态用例（v4 补强）：扁平目录（无 SKILL.md / .skill）
        不得误触发单技能包模式，作者仍按文件名分组。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "张三_C4_skill说明.md").write_text("# 技能\n使用场景：x\n", encoding="utf-8")
            (d / "李四_C4_AI日志.md").write_text("AI：Claude\n", encoding="utf-8")
            assert detect_single_skill(d) is None, "扁平目录被误判为单技能包"
            rep = evaluate_folder(d, rubric_path)
            authors = {a["author"] for a in rep["authors"]}
            assert {"张三", "李四"} <= authors, "扁平目录作者分组错误：%s" % authors

    def t_author_cjk():
        m = NAMING_RE.match("卢怡然_C4_skill说明")
        assert m and m.group("author") == "卢怡然", "中文作者名识别失败（基座正则只认 ASCII）"

    def t_author_ascii():
        m = NAMING_RE.match("ZhangWei_C4_demo")
        assert m and m.group("author") == "ZhangWei", "英文作者名识别失败"

    def t_version():
        assert detect_version({"name": "A_C4_x_v2"}) == "v2", "版本追踪失败"

    def t_scoring_monotone():
        assert grade_ratio(4, 4, {"pass": 0.75, "partial": 0.4}) == "✅"
        assert grade_ratio(2, 4, {"pass": 0.75, "partial": 0.4}) == "⚠️"
        assert grade_ratio(0, 4, {"pass": 0.75, "partial": 0.4}) == "❌"

    def t_negative_cap():
        corpus = {"a.md": 'api_key = "ABCDEFGH12345678"\n安装说明\n输入 输出'}
        g, flags = apply_negative_cap("✅", corpus, [
            {"id": "hard_secret", "pattern": r"(?:api[_-]?key)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{8,}[\"']", "cap": 0.0}])
        assert g == "❌" and flags, "硬密钥未触发负信号硬地板"

    def t_empty_file():
        files = [{"rel": "a.md", "fullname": "a.md", "name": "a", "ext": ".md", "size": 0, "parent": ""}]
        comp = check_completeness(files, {}, rubric)
        assert not any(v["present"] for v in comp.values()), "0 字节文件不应计为已提交"

    def t_real_syntax_ok():
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ok.py"
            p.write_text("def f():\n    return 1\n", encoding="utf-8")
            ok, ev = real_check_syntax([{"abs": str(p), "rel": "ok.py", "ext": ".py", "size": 20}])
            assert ok is True, "合法 .py 未通过语法校验"

    def t_real_syntax_fail():
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.py"
            p.write_text("def f(:\n", encoding="utf-8")
            ok, ev = real_check_syntax([{"abs": str(p), "rel": "bad.py", "ext": ".py", "size": 10}])
            assert ok is False, "语法错误文件未被检出"

    def t_skill_pkg():
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "x.skill"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("x/SKILL.md", "---\nname: x\n---\nbody")
            ok, ev = real_check_skill_pkg([{"abs": str(z), "rel": "x.skill", "ext": ".skill", "size": 100}])
            assert ok is True, "合法 .skill 包未通过结构校验"

    def t_no_double_count():
        corpus = {"a.md": "安装\n" * 200}
        hit, _ = find_evidence(corpus, ["安装"])
        assert hit, "基础检索失效"
        q = {"reusable": {"label_cn": "可复用", "weight": 0.25, "check_items": [
            {"id": "install_instructions", "label": "安装", "any_of": ["安装"]}]}}
        fake = {"quality": q, "grading": {"thresholds": {"pass": 0.75, "partial": 0.4}}}
        out = evaluate_quality([], corpus, fake)
        assert out["reusable"]["passed"] == 1 and out["reusable"]["applicable"] == 1, \
            "重复关键词被累计加分（未布尔封顶）"

    def t_end_to_end():
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "张三_C4_skill说明.md").write_text(
                "# 技能\n使用场景：评审\n输入一个文件夹，输出报告\n安装：pip install x\n环境要求 Python 3\n"
                "自检：python x.py --self-test\n预期：输出 Markdown\n示例：输入 A 得到 B\n边界：空文件夹\n", encoding="utf-8")
            rep = evaluate_folder(d, rubric_path)
            assert rep["class_stats"]["n_authors"] == 1, "端到端流程未识别出作者"
            assert rep["authors"][0]["author"] == "张三", "作者名错误"

    for name, fn in [
        ("评分标准加载完整（5 项完整性 + 4 条件）", t_rubric_loaded),
        ("迷你 YAML 解析器与 PyYAML 深度一致（有 PyYAML 时）", t_mini_yaml),
        ("迷你解析器无 PyYAML 时关键字段可数值化（P0 回归）", t_mini_yaml_key_fields_numeric),
        ("中文作者名识别（基座正则的缺陷）", t_author_cjk),
        ("英文作者名识别", t_author_ascii),
        ("版本追踪 _v2", t_version),
        ("评级阈值单调（比例制）", t_scoring_monotone),
        ("负信号硬地板：硬编码密钥封顶 ❌", t_negative_cap),
        ("0 字节文件视为未提交", t_empty_file),
        ("真实校验：合法 .py 通过 py_compile", t_real_syntax_ok),
        ("真实校验：语法错误 .py 被检出", t_real_syntax_fail),
        ("真实校验：.skill 包结构校验", t_skill_pkg),
        ("反堆砌：重复关键词不累计加分", t_no_double_count),
        ("端到端：单作者全流程", t_end_to_end),
        ("结构形态：带子目录的单技能包不被拆成多作者", t_struct_single_skill_pkg),
        ("结构形态：扁平目录不误触发单技能包", t_struct_flat_dir),
    ]:
        case(name, fn)

    print("c4-skill-evaluator 自检（%d 项）" % len(cases))
    for name, ok, err in cases:
        print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, ("" if ok else "  → " + err)))
    n_ok = sum(1 for _, ok, _ in cases if ok)
    print("\n%d/%d 通过" % (n_ok, len(cases)))
    return 0 if n_ok == len(cases) else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="C4 技能提交自动评审器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("evaluate", help="评审一个文件夹")
    e.add_argument("folder")
    e.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    e.add_argument("--out", default=None, help="报告输出目录")
    e.add_argument("--json", default=None, help="JSON 结果输出路径")
    e.add_argument("--no-excel", action="store_true")

    s = sub.add_parser("selftest", help="内置自检")
    s.add_argument("--rubric", default=str(DEFAULT_RUBRIC))

    args = ap.parse_args()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(Path(args.rubric)))

    folder = Path(args.folder)
    rubric = load_yaml(Path(args.rubric))
    report = evaluate_folder(folder, Path(args.rubric))
    md = render_markdown(report, rubric)

    out_dir = Path(args.out) if args.out else folder
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "C4评审报告.md"
    md_path.write_text(md, encoding="utf-8", newline="\n")
    print("Markdown 报告 → %s" % md_path)

    if not args.no_excel:
        xlsx = out_dir / "C4评审详表.xlsx"
        if write_excel(report, xlsx):
            print("Excel 详表   → %s" % xlsx)
        else:
            print("Excel 详表   → 跳过（未安装 openpyxl，属可选增强）")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print("JSON 结果    → %s" % args.json)
    print("\n识别 %d 位作者，平均综合分 %.1f"
          % (report["class_stats"]["n_authors"], report["class_stats"].get("avg_total", 0)))


if __name__ == "__main__":
    main()
