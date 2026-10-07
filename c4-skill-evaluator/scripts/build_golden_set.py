#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_golden_set.py — 生成带人工标注 ground truth 的黄金测试集。

设计原则（重要，关系到结论是否可信）：
  · 每个 fixture 的 __expected__.json 是**人工判读**的结果，先写好再看评审器输出，
    绝不用评审器输出反推标签（那会变成循环论证）。
  · 诚实声明：标注者为交付者本人（单一标注者），非独立评审团；
    标签只覆盖「结构上可判定」的层面，不含「这个技能是否有用」的主观判断。
  · 每个 fixture 覆盖一类边界，故意包含评审器容易误判的情形。
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "references" / "golden_set"

FIXTURES = {}

# ---------------------------------------------------------------------------
# G01 — 齐全且真实：五项齐全，有真实可运行脚本、无硬编码路径、IO 明确
# ---------------------------------------------------------------------------
FIXTURES["G01_complete_good"] = {
    "__expected__": {
        "desc": "五项齐全、内容真实、无硬编码路径与密钥",
        "author": "张三",
        "completeness": {"skill_doc": True, "executable_content": True, "demo": True,
                         "teaching_doc": True, "ai_log": True},
        "quality": {"reusable": "✅", "executable": "✅", "verifiable": "✅", "clear_io": "✅"},
        "note": "人工判读：这是一份可以直接给分的高质量提交",
    },
    "files": {
        "张三_C4_skill说明.md": """# 代码review助手

## 使用场景
给你一段 Python 代码，它找出潜在的空指针与资源泄漏。解决什么问题：人工 review 容易漏看边界。

## 输入 / 输出
输入一个 .py 文件路径，输出一份 Markdown 格式的 review 报告。

## 安装
```bash
pip install -r requirements.txt
```

## 环境要求
Python 3.9+，无需 GPU。依赖见 requirements.txt。

## 泛化
不限于 Python：把解析层换成 tree-sitter 的其它语言 grammar 即可适配 Java/Go。
""",
        "张三_C4_reviewer.py": '''#!/usr/bin/env python3
"""代码 review 助手：找出空指针与资源泄漏。"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(description="code reviewer")
    ap.add_argument("path")
    args = ap.parse_args()
    print("review:", args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
        "张三_C4_demo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000,   # 3KB 占位，非 0 字节
        "张三_C4_教学说明.md": """# 上手指南

## 安装步骤
1. pip install -r requirements.txt
2. python 张三_C4_reviewer.py your_file.py

## 常见坑
- Windows 下请把路径写成相对路径
- 超过 1MB 的文件建议先拆分

## 注意事项
不要把它当成自动修复工具，它只做提示。
""",
        "张三_C4_AI日志.md": """# AI 日志

使用的 AI 工具：Claude Code（cursor 为辅）。

| 轮次 | prompt 要点 | 结果 |
|---|---|---|
| v1 | 帮我写一个代码 review 脚本 | 能跑但漏掉资源泄漏 |
| v2 | 补充 with 语句检测 | 迭代次数 2，覆盖率上升 |
| v3 | 加 argparse 与自检 | 定稿 |
""",
    },
}

# ---------------------------------------------------------------------------
# G02 — 缺两项：只有说明类文件，没有可执行内容与 demo
# ---------------------------------------------------------------------------
FIXTURES["G02_missing_two"] = {
    "__expected__": {
        "desc": "缺「可执行内容」和「Demo」两项（只有文字说明）",
        "author": "李四",
        "completeness": {"skill_doc": True, "executable_content": False, "demo": False,
                         "teaching_doc": True, "ai_log": True},
        "quality": {"reusable": "⚠️", "executable": "❌", "verifiable": "⚠️", "clear_io": "⚠️"},
        "note": "人工判读：说明写得清楚，但没有任何能跑的东西，可执行应为 ❌",
    },
    "files": {
        "李四_C4_skill说明.md": """# 文献速读助手

使用场景：给你一篇 PDF 论文，输出 300 字摘要与三个批判点。解决什么问题：读论文太慢。

输入：PDF 文件路径。输出：Markdown 摘要。

安装：无需安装，把下面的 prompt 复制到任意对话里即可。

环境要求：任意支持长上下文的对话式模型。
""",
        "李四_C4_教学说明.md": """# 教学说明

## 步骤
1. 复制 prompt
2. 粘贴论文全文
3. 读取输出

## 常见坑
- 论文超过 30 页时先拆章节
- 数学公式可能被吞，建议转成文字描述

注意事项：输出仅供参考，关键结论要回原文核对。
""",
        "李四_C4_AI日志.md": """# AI 日志

使用的 AI 工具：ChatGPT。

迭代次数：3。prompt 从「总结这篇论文」优化为「总结并按方法/数据/结论三段批判」。
""",
    },
}

# ---------------------------------------------------------------------------
# G03 — 关键词堆砌：五项齐全但内容空洞，靠重复关键词伪装
#      这是纯关键词法最容易误判成高分的情形（反作弊验证）
# ---------------------------------------------------------------------------
_stuff = ("输入 输出 安装 测试 示例 步骤 " * 30) + "\n" + ("input output install test example " * 30)
FIXTURES["G03_keyword_stuffing"] = {
    "__expected__": {
        "desc": "五项齐全但正文只是关键词重复，没有任何真实内容",
        "author": "王五",
        "completeness": {"skill_doc": True, "executable_content": True, "demo": True,
                         "teaching_doc": True, "ai_log": True},
        "quality": {"reusable": "⚠️", "executable": "❌", "verifiable": "⚠️", "clear_io": "⚠️"},
        "note": ("人工判读：文件都在，但正文是空壳。可执行必须是 ❌（没有任何可运行物），"
                 "其余三项最多 ⚠️；若评审器给出 ✅ 即为严重误判"),
    },
    "files": {
        "王五_C4_skill说明.md": "# 万能助手\n\n" + _stuff,
        "王五_C4_脚本.md": "```\n" + _stuff + "\n```",
        "王五_C4_demo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000,
        "王五_C4_教学说明.md": "# 教学\n\n" + _stuff,
        "王五_C4_AI日志.md": "# AI日志\n\n" + _stuff,
    },
}

# ---------------------------------------------------------------------------
# G04 — 硬编码本机路径 + 硬编码密钥（负信号硬地板验证）
# ---------------------------------------------------------------------------
FIXTURES["G04_hardcoded"] = {
    "__expected__": {
        "desc": "含硬编码绝对路径与明文密钥，其余内容合格",
        "author": "赵六",
        "completeness": {"skill_doc": True, "executable_content": True, "demo": True,
                         "teaching_doc": True, "ai_log": True},
        "quality": {"reusable": "❌", "executable": "✅", "verifiable": "✅", "clear_io": "✅"},
        "note": ("人工判读：可复用必须被压到 ❌（明文密钥 + 绝对路径 = 别人拿过去跑不起来），"
                 "其它三项照常给 ✅。这是纯关键词正向匹配最容易漏掉的一类。"),
    },
    "files": {
        "赵六_C4_skill说明.md": """# 舆情抓取助手

使用场景：抓取指定关键词的舆情。解决什么问题：人工刷太慢。

输入一个关键词，输出 CSV。

## 安装
pip install requests

## 环境要求
Python 3.10

## 泛化
换成任何有开放接口的站点即可。
""",
        "赵六_C4_fetch.py": '''import requests

API = "C:\\\\Users\\\\zhaoliu\\\\data\\\\trend"
api_key = "ABCDEFGH12345678"


def fetch(kw):
    return requests.get(API, params={"q": kw, "key": api_key})


def main():
    print(fetch("ai"))


if __name__ == "__main__":
    main()
''',
        "赵六_C4_demo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000,
        "赵六_C4_教学说明.md": """# 上手

## 步骤
1. 改配置里的路径
2. 运行

## 常见坑
- 路径是写死的，换机器必改
- 密钥不要提交到公开仓库

注意事项：接口有频率限制。
""",
        "赵六_C4_AI日志.md": """# AI 日志
使用的 AI 工具：DeepSeek。迭代次数：2。prompt：帮我写个抓取脚本。
""",
    },
}

# ---------------------------------------------------------------------------
# G05 — 语法错误的可执行文件（真实校验验证）
# ---------------------------------------------------------------------------
FIXTURES["G05_syntax_error"] = {
    "__expected__": {
        "desc": "提交了 .py 但语法错误，跑不起来",
        "author": "孙七",
        "completeness": {"skill_doc": True, "executable_content": True, "demo": False,
                         "teaching_doc": True, "ai_log": False},
        "quality": {"reusable": "⚠️", "executable": "❌", "verifiable": "❌", "clear_io": "⚠️"},
        "note": "人工判读：语法错误 → 可执行必须 ❌；没有 demo 也没有测试 → 可验证 ❌",
    },
    "files": {
        "孙七_C4_skill说明.md": """# 批量重命名助手

使用场景：把一批文件按规则重命名。

输入一个目录，输出重命名后的文件。

安装：无需依赖。环境要求：Python 3。
""",
        "孙七_C4_rename.py": "def rename(path:\n    pass\n",
        "孙七_C4_教学说明.md": """# 步骤
1. 放好目录
2. 运行

常见坑：先备份。注意事项：不可逆。
""",
    },
}

# ---------------------------------------------------------------------------
# G06 — 命名不规范（作者识别回退验证）
# ---------------------------------------------------------------------------
FIXTURES["G06_sloppy_naming"] = {
    "__expected__": {
        "desc": "文件名完全不符合 姓名_C4_内容 规范，考验作者识别回退链",
        "author": "Unknown",
        "completeness": {"skill_doc": True, "executable_content": True, "demo": False,
                         "teaching_doc": False, "ai_log": False},
        "quality": {"reusable": "⚠️", "executable": "⚠️", "verifiable": "⚠️", "clear_io": "⚠️"},
        "note": "人工判读：只有两个文件，且命名不规范；作者应被标为需人工确认",
    },
    "files": {
        "我的技能说明.md": """# 小工具

使用场景：合并 CSV。输入多个 CSV，输出一个。

安装：pip install pandas。环境要求：Python 3.8+。
""",
        "merge.py": '''import sys


def main():
    print("merge", sys.argv[1:])


if __name__ == "__main__":
    main()
''',
    },
}

# ---------------------------------------------------------------------------
# G07 — 五项齐全但全是 0 字节空文件
# ---------------------------------------------------------------------------
FIXTURES["G07_empty_files"] = {
    "__expected__": {
        "desc": "五个文件名都对，但全是 0 字节",
        "author": "周八",
        "completeness": {"skill_doc": False, "executable_content": False, "demo": False,
                         "teaching_doc": False, "ai_log": False},
        "quality": {"reusable": "⚠️", "executable": "⚠️", "verifiable": "⚠️", "clear_io": "⚠️"},
        "note": ("人工判读：有文件名不等于有内容，完整性应为 0/5；"
                 "质量维度在无语料时机器给不出结论，按设计降级为 ⚠️（不是 ❌）"),
    },
    "files": {
        "周八_C4_skill说明.md": "",
        "周八_C4_tool.py": "",
        "周八_C4_demo.png": "",
        "周八_C4_教学说明.md": "",
        "周八_C4_AI日志.md": "",
    },
}

# ---------------------------------------------------------------------------
# G08 — 合法 .skill 包，缺 demo（真实结构校验 + 版本追踪）
# ---------------------------------------------------------------------------
FIXTURES["G08_skill_pkg_v2"] = {
    "__expected__": {
        "desc": "含合法 .skill 包（v2 版本），缺 demo",
        "author": "吴九",
        "completeness": {"skill_doc": True, "executable_content": True, "demo": False,
                         "teaching_doc": True, "ai_log": True},
        "quality": {"reusable": "✅", "executable": "✅", "verifiable": "✅", "clear_io": "✅"},
        "note": "人工判读：.skill 结构完整可安装 → 可执行 ✅；缺 demo 已在完整性扣分，质量不再重复扣",
    },
    "files": {
        "吴九_C4_skill说明_v2.md": """# 会议纪要助手 v2

使用场景：把会议录音转写成结构化纪要。解决什么问题：人工整理耗时长。

输入一段会议文本，输出 Markdown 纪要（含待办与责任人）。

## 安装
把 .skill 包导入即可，无需额外依赖。

## 环境要求
任意支持 skill 的 Agent 环境。

## 泛化
换成访谈、课堂、庭审记录同样适用。
""",
        "吴九_C4_教学说明_v2.md": """# 上手

## 步骤
1. 导入 .skill
2. 粘贴文本
3. 拿纪要

## 常见坑
- 超过 1 小时的建议分段
- 人名识别可能出错

注意事项：涉密会议不要上传。
""",
        "吴九_C4_AI日志_v2.md": """# AI 日志
使用的 AI 工具：Claude。迭代次数：4（v1 结构混乱 → v2 固定三段式）。
""",
    },
    "post": "make_skill_pkg",     # 生成 吴九_C4_meeting-notes.skill
}


def make_skill_pkg(dirpath: Path):
    zpath = dirpath / "吴九_C4_meeting-notes.skill"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("meeting-notes/SKILL.md", """---
name: meeting-notes
description: >
  输入一段会议文本，输出结构化 Markdown 纪要。
---

# Meeting Notes

## 输入 / 输出
输入：会议文本。输出：Markdown 纪要。

## 自检
运行 `python selftest.py`，预期输出 OK。
""")
        z.writestr("meeting-notes/scripts/selftest.py", "print('OK')\n")


def build(out_dir: Path = OUT):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    for name, spec in FIXTURES.items():
        d = out_dir / name
        d.mkdir()
        for fname, content in spec["files"].items():
            p = d / fname
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8", newline="\n")
        if spec.get("post") == "make_skill_pkg":
            make_skill_pkg(d)
        (d / "__expected__.json").write_text(
            json.dumps(spec["__expected__"], ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n")

    print("黄金测试集已生成：%s" % out_dir)
    print("共 %d 个 fixture：" % len(FIXTURES))
    for name, spec in FIXTURES.items():
        print("  %-24s %s" % (name, spec["__expected__"]["desc"]))
    return out_dir


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT)
