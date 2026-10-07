---
name: c4-skill-evaluator
description: >
  Scan a local folder containing Elite20 C4 skill submissions from a WeChat group,
  identify each submission's author, check deliverable completeness (5 required files),
  evaluate skill quality against the C4 four-criteria rubric (Reusable, Executable,
  Verifiable, Clear I/O), and generate a structured evaluation report with per-author
  scores, gap analysis, and improvement suggestions.
  Use whenever the user says "evaluate C4 submissions", "check C4 completeness",
  "review skill submissions", "评审C4提交", "检查技能提交", "C4评审报告",
  or provides a folder path and asks to evaluate/review the skill files inside.
  Also trigger when the user mentions C4 challenge together with evaluation or review.
---

# C4 Skill Submission Evaluator

## Purpose

Given a local folder path containing C4 skill submissions (typically downloaded from
a WeChat group), this skill automatically evaluates each submission for completeness
and quality. Think of it as an automated teaching assistant who not only checks if
homework was submitted, but also grades it against a rubric and writes feedback.

## Prerequisites

### Runtime
- Python 3 with `openpyxl`, `pypdf`, `python-pptx`, `pyyaml` available
- (Optional for Level 3) Access to an LLM API for content-level quality assessment

### Input
- A local folder path containing C4 submission files
- Files should follow Elite20 naming convention: `姓名拼音_C4_内容描述.扩展名`

## Workflow

### Step 1 — Scan & Identify Authors

Scan the target folder (recursively) and collect all files matching C4 patterns.

**Author extraction chain** (try in order):
1. Filename prefix before first `_C4_` → author name
2. Subfolder name (if files are grouped by author)
3. File metadata (PDF author, DOCX properties)
4. Mark as `Unknown` and flag for manual review

**Output:** `Dict[author_name, List[file_info]]`

### Step 2 — Completeness Check (5 Required Files)

For each author, check against C4's five required deliverables:

```yaml
required_files:
  skill_doc:
    label: "Skill 说明文档"
    signals:
      filename: ["skill说明", "skill_doc", "skill-doc"]
      content: ["使用场景", "输入", "输出", "解决什么问题"]
    extensions: [".md", ".pdf", ".docx"]

  executable:
    label: "可执行内容"
    signals:
      filename: ["技能", "skill"]
      content: ["```", "def ", "class ", "import "]
    extensions: [".skill", ".py", ".md"]

  demo:
    label: "Demo"
    signals:
      filename: ["demo", "演示", "截图"]
    extensions: [".mp4", ".mov", ".png", ".jpg", ".gif", ".webm"]

  teaching_doc:
    label: "教学说明"
    signals:
      filename: ["教学说明", "tutorial", "teaching", "上手指南"]
      content: ["上手", "常见坑", "步骤", "安装", "注意事项"]
    extensions: [".md", ".pdf", ".docx"]

  ai_log:
    label: "AI 日志"
    signals:
      filename: ["AI日志", "AI_log", "ai-log"]
      content: ["使用的 AI", "prompt", "迭代次数", "AI 工具"]
    extensions: [".md", ".pdf", ".docx"]
```

**Output:** Per-author completeness matrix (✅ / ❌ for each of 5 files)

### Step 3 — Quality Evaluation (4 Criteria)

For each author's submission that passes completeness check, evaluate against
C4's four quality criteria.

> ⚠️ **THIS IS THE PART YOU NEED TO DESIGN.**
> The signals below are starter suggestions. Your innovation goes here.

```
┌─────────────────────────────────────────────────────────┐
│  CRITERIA          │  WHAT TO CHECK                      │
├─────────────────────────────────────────────────────────┤
│  Reusable          │  - No hardcoded absolute paths      │
│  (可复用)          │  - Has installation instructions    │
│                    │  - Environment requirements listed  │
│                    │  - Works without author's machine   │
├─────────────────────────────────────────────────────────┤
│  Executable        │  - Contains runnable code/prompt    │
│  (可执行)          │  - .skill structure is valid        │
│                    │  - No syntax errors (if code)       │
│                    │  - Has YAML frontmatter (if SKILL)  │
├─────────────────────────────────────────────────────────┤
│  Verifiable        │  - Has test cases or examples       │
│  (可验证)          │  - Expected output is defined       │
│                    │  - Demo shows real results           │
│                    │  - Success/failure criteria clear    │
├─────────────────────────────────────────────────────────┤
│  Clear I/O         │  - "输入___，输出___" present       │
│  (IO 明确)         │  - Input types/formats specified    │
│                    │  - Output types/formats specified    │
│                    │  - One-liner summary exists          │
└─────────────────────────────────────────────────────────┘
```

**Scoring:** Each criterion → ✅ (meets) / ⚠️ (partial) / ❌ (missing)

### Step 4 — Generate Report

Output a Markdown evaluation report with:

1. **班级总览** — submission count, completeness rate, quality distribution
2. **作者详情** — per-author: completeness matrix + quality scores + feedback
3. **排名表** — sorted by composite score
4. **改进建议** — per-author actionable next steps
5. (Optional) Excel file with full data

## Report Template

```markdown
# C4 提交自动评审报告

生成时间：{timestamp}
扫描路径：{folder_path}
识别提交：{n_authors} 位作者，{n_files} 个文件

## 一、班级总览

| 指标 | 数值 |
|------|------|
| 总提交人数 | {n} |
| 完整提交（5/5） | {n_complete} |
| 部分提交 | {n_partial} |
| 平均质量分 | {avg_score}/4.0 |

## 二、作者详情

### {Author 1}

**完整性检查：**

| 文件 | 状态 | 匹配文件 |
|------|------|----------|
| Skill 说明 | ✅ | Author1_C4_skill说明.md |
| 可执行内容 | ✅ | Author1_C4_my-skill.skill |
| Demo | ❌ | — |
| 教学说明 | ⚠️ | Author1_C4_readme.md (可能匹配) |
| AI 日志 | ✅ | Author1_C4_AI日志.md |

**质量评审：**

| 条件 | 评级 | 依据 |
|------|------|------|
| 可复用 | ✅ | 有安装说明，无绝对路径，环境要求明确 |
| 可执行 | ✅ | .skill 包结构完整，YAML frontmatter 有效 |
| 可验证 | ⚠️ | 有示例但缺少预期输出定义 |
| IO 明确 | ✅ | "输入一个文件夹路径，输出分类报告" |

**改进建议：**
1. 补充 demo 截图或视频
2. 在 skill 说明中增加预期输出的具体格式说明

---

（重复每个作者...）

## 三、排名

| 排名 | 作者 | 完整性 | 质量分 | 综合分 |
|------|------|--------|--------|--------|
| 1 | ... | 5/5 | 3.5/4 | ... |

## 四、全班改进建议

- 最常见缺失：{most_missing_file}
- 最弱维度：{weakest_criterion}
- 建议下次提交前用此评审技能自检
```

## Edge Cases

| 情况 | 处理 |
|------|------|
| 空文件夹 | 报告"未找到 C4 相关文件"，列出扫描路径和匹配规则 |
| 文件名不规范 | 回退到内容检测，标记为"需人工确认" |
| 超大文件（>50MB） | 跳过内容分析，仅做文件名/类型匹配 |
| 二进制文件（.mp4 等） | 仅做文件名匹配，不做内容分析 |
| 非 C4 文件混在其中 | 过滤后在报告中单独列出"非 C4 文件" |
| 同一作者多个版本 | 识别 `_v2`、`_v3`，报告最新版本，追踪迭代历史 |
| 编码问题（中文文件名） | 使用 UTF-8 解码，GBK 回退 |

## What You Need to Build

This SKILL.md is a **starter scaffold**, not a finished skill. You need to:

1. **Implement the scanning logic** (adapt from wechat-doc-mapper)
2. **Implement the completeness checker** (the signal definitions above are a start)
3. **Design and implement the quality evaluator** (this is YOUR innovation)
4. **Write the report generator** (template above is a start, customize it)
5. **Test on real C4 submissions** and iterate

The architecture choice (pure Python script vs. LLM-assisted vs. hybrid) is yours.
Document your choice and reasoning in your 方案设计.md.
