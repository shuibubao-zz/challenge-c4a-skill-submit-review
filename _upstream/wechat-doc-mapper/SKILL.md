---
name: wechat-doc-mapper
description: >
  Scan a WeChat-synced local folder (or any attachment directory) to inventory all documents,
  identify each file's author and challenge assignment via naming convention (Author_ChallengeID_Part.ext),
  extract title, infer purpose, and group files into per-author deliverable bundles mapped to
  Elite20 challenges. Outputs a Markdown summary table (Author × Challenge) plus a downloadable
  Excel (.xlsx) file with gap analysis per author.
  Use whenever the user says "scan WeChat folder", "map documents to challenges",
  "inventory group attachments", "classify WeChat files", "文档分类", "微信文件整理",
  "挑战映射", "附件清单", or provides a folder path and asks to catalog/classify the files inside.
  Also trigger when the user mentions Elite20 challenges together with a folder of documents,
  or asks "what documents do we have for each challenge". Works with any mix of PDF, Word, PPT,
  LaTeX, Markdown, images, archives, and plain text files.
---

# WeChat Document → Challenge Mapper

## Purpose

Given a local folder path containing group discussion attachments (typically synced from WeChat),
this skill identifies each file's author, groups files into per-author deliverable bundles,
then maps each bundle to the relevant Elite20 challenge(s). Think of it as a teaching assistant
who collects everyone's homework from a shared dropbox — first sorting by student name,
then checking which assignment each piece belongs to, and finally reporting who's missing what.

## Prerequisites

### Runtime
- Python 3 with `openpyxl`, `pypdf`, `python-pptx`, `pandas` available
- The xlsx skill at `/mnt/skills/public/xlsx/SKILL.md` (for Excel output formatting)
- The file-reading skill at `/mnt/skills/public/file-reading/SKILL.md` (for format dispatch)

### File Naming Convention (MUST be communicated to group BEFORE collection)

For the skill to reliably identify **who** submitted **what** for **which challenge**,
files in the WeChat folder should follow this naming pattern:

```
<Author>_<ChallengeID>_<Part>.<ext>
```

**Fields:**
- `Author` — the submitter's name (English or pinyin, no spaces; use CamelCase or dash)
  e.g. `ZhangWei`, `Li-Ming`, `WangXiao`
- `ChallengeID` — `C1`, `C2`, `C3`, etc.
- `Part` — a short descriptor of what this file is within the challenge deliverable bundle
  e.g. `syllabus-original`, `翻译方法`, `paper`, `references`, `reflection`

**Examples:**
```
ZhangWei_C1_syllabus-original.pdf
ZhangWei_C1_syllabus-translated.docx
ZhangWei_C1_翻译方法.md
LiMing_C2_paper.tex
LiMing_C2_references.bib
WangXiao_C3_interaction-log.md
WangXiao_C3_reflection.md
```

**Subfolder variant** (also supported):
```
ZhangWei/
├── C1_syllabus-original.pdf
├── C1_syllabus-translated.docx
└── C1_翻译方法.md
LiMing/
├── C2_paper.tex
└── C2_references.bib
```

When filenames do NOT follow the convention, the skill falls back to:
1. First lines / header of the document (look for author name or student ID)
2. File metadata (PDF author field, DOCX core_properties.author)
3. Parent folder name (if files are grouped by author in subfolders)
4. Mark as `Unknown Author` and flag for manual review

## Challenge Registry

The skill maps documents against these Elite20 challenges. The registry is defined in
`references/challenges.yaml` — read it at startup. If the user provides additional or
different challenges, update the registry before running.

### Current Challenges

| ID | Name (CN) | Name (EN) | Key Artifacts |
|----|-----------|-----------|---------------|
| C1 | 课程资料获取与翻译 | Course Materials Acquisition & Translation | syllabus, slides, readings, translations, method.md |
| C2 | AI for Math 论文 | AI for Math Paper | paper.tex, paper.pdf, references.bib, summary.md |
| C3 | 群内高质量参与 | High-Quality Group Participation | interaction_log.md, reflection.md, discussion posts |

## Workflow

### Step 0 — Get the folder path

Ask the user for the folder path if not already provided. Accept absolute or `~`-relative paths.
Validate the path exists and is a directory.

```bash
# Expand ~ and verify
FOLDER="<user-provided-path>"
ls -la "$FOLDER" | head -20
```

### Step 1 — Identify author and build deliverable bundles

This is the FIRST analytical step. Before classifying content, determine WHO submitted each file.
Think of it as sorting mail — you look at the return address before opening the envelope.

**1a. Parse the filename convention:**

```python
import re

NAMING_PATTERN = re.compile(
    r'^(?P<author>[A-Za-z\-]+)_(?P<challenge>C\d+)_(?P<part>.+)$'
)

def parse_filename(stem: str) -> dict:
    """Try to extract author + challenge + part from naming convention."""
    m = NAMING_PATTERN.match(stem)
    if m:
        return {
            'author': m.group('author'),
            'challenge_hint': m.group('challenge'),
            'part': m.group('part'),
            'convention_match': True,
        }
    return {'author': None, 'challenge_hint': None, 'part': stem, 'convention_match': False}
```

**1b. Fallback author extraction** (when filenames don't follow convention):

| Source | Extraction Method |
|--------|-------------------|
| Parent folder name | If files sit inside `ZhangWei/` or `张伟/`, use folder name as author |
| PDF metadata | `pypdf` → `reader.metadata.author` |
| DOCX metadata | `python-docx` → `doc.core_properties.author` |
| Document header | First 5 lines — look for patterns like `Author:`, `作者：`, `姓名：`, `Name:` |
| WeChat sender prefix | Some WeChat exports prepend sender name — check for `[Name]` prefix |

**1c. Group into bundles:**

After author identification, group files into `{author → {challenge → [files]}}` bundles.
This transforms a flat file list into a structured deliverable map.

```python
from collections import defaultdict

bundles = defaultdict(lambda: defaultdict(list))
unknown_author_files = []

for item in inventory:
    parsed = parse_filename(item['name'])
    author = parsed['author']

    # Fallback chain if convention didn't match
    if not author:
        author = extract_author_from_metadata(item)  # PDF/DOCX metadata
    if not author:
        author = extract_author_from_folder(item)     # parent folder name
    if not author:
        author = extract_author_from_header(item)     # first lines of document
    if not author:
        unknown_author_files.append(item)
        author = 'Unknown'

    item['author'] = author
    item['challenge_hint'] = parsed.get('challenge_hint')
    bundles[author][parsed.get('challenge_hint', 'unassigned')].append(item)
```

### Step 2 — Inventory all files

Recursively list every file in the folder. Capture: relative path, extension, file size, modification date.

```bash
find "$FOLDER" -type f -not -name '.*' | sort
```

Build a Python data structure:

```python
import os, json
from pathlib import Path
from datetime import datetime

folder = Path("<FOLDER>").expanduser()
inventory = []
for f in sorted(folder.rglob("*")):
    if f.is_file() and not f.name.startswith('.'):
        stat = f.stat()
        inventory.append({
            "path": str(f.relative_to(folder)),
            "name": f.stem,
            "ext": f.suffix.lower(),
            "size_kb": round(stat.st_size / 1024, 1),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()[:10],
        })
print(json.dumps(inventory, indent=2, ensure_ascii=False))
```

### Step 3 — Extract title and infer purpose per file

For each file, use the appropriate reader to extract a title or heading. The dispatch logic
mirrors the file-reading skill:

| Extension | Title Extraction Method |
|-----------|------------------------|
| `.pdf` | `pypdf` → first page text → first line or metadata `/Title` |
| `.docx` | `pandoc -t plain` → first heading line; or `python-docx` core_properties.title |
| `.pptx` | `python-pptx` → slide 1 title shape text |
| `.tex` | grep for `\title{...}` |
| `.md`, `.txt` | first `#` heading or first non-empty line |
| `.xlsx`, `.csv` | sheet name + first row header |
| `.zip`, `.tar.gz` | list contents, summarize |
| `.png`, `.jpg` | filename-based; note as image |
| other | use filename as title |

**Purpose inference** — After extracting the title, infer the document's purpose by considering:

1. **Filename keywords**: "syllabus", "翻译", "paper", "论文", "reflection", "讨论", "method", etc.
2. **Content keywords** (from first ~500 chars): look for domain signals
3. **File type signal**: .tex → likely paper; .bib → references; slides → course material
4. **Subfolder context**: folder names like `/original/`, `/translated/` are strong signals

Produce a one-line purpose string in Chinese + English, e.g.:
`"课程大纲原文 (Original course syllabus)"`

### Step 4 — Map to challenges

For each document, score it against each challenge using a keyword + artifact-schema match:

```
Challenge C1 signals: syllabus, slides, readings, 课程, 翻译, translation, original, method,
                      prompt, workflow, 资料, assignment, /original/, /translated/
Challenge C2 signals: paper, 论文, LaTeX, .tex, .bib, theorem, proof, AI4Math, math,
                      verification, generation, semantic, summary
Challenge C3 signals: discussion, 讨论, reflection, 反思, interaction, 提问, 回答, log,
                      participation, 群, chat, 观点, question, answer
```

A document may map to **multiple** challenges (e.g., a reflection on the translation process
touches both C1 and C3). Assign a primary challenge and optional secondary challenges.

If the filename convention provided a `challenge_hint` (e.g., `C1` from `ZhangWei_C1_syllabus.pdf`),
use it as the primary assignment and cross-validate with keyword scoring. Flag any mismatch
(e.g., file named `_C1_` but content signals C2) for manual review.

If a document matches no challenge, mark it as `C0 — Uncategorized`.

### Step 5 — Generate outputs

#### 5a. Markdown table (in chat)

Present a table with these columns:

```
| # | Author | File Name | Format | Title / 标题 | Inferred Purpose / 推断用途 | Challenge | Notes |
```

Group rows by **Author first, then by Challenge** for readability. This makes it easy to see
each person's complete deliverable bundle at a glance. Include a summary matrix at the bottom:

```
| Author    | C1 | C2 | C3 | Uncategorized |
|-----------|----|----|----|----|
| ZhangWei  | 3  | —  | —  | 1  |
| LiMing    | —  | 2  | —  | —  |
| WangXiao  | —  | —  | 2  | —  |
| Unknown   | —  | —  | —  | 1  |
```

#### 5b. Excel file

Generate a professional `.xlsx` with:

- **Sheet 1: Document Inventory** — full table with columns:
  Author, File Name, Extension, Size (KB), Modified Date, Extracted Title, Inferred Purpose,
  Primary Challenge, Secondary Challenge(s), Convention Match (✓/✗), Notes
- **Sheet 2: Author × Challenge Matrix** — pivot showing per-author, per-challenge document counts
  and which artifact-schema slots are filled vs. missing for each author's bundle
- **Sheet 3: Challenge Coverage** — aggregate view across all authors showing
  which artifact-schema slots are filled vs. missing overall
- **Sheet 4: Challenge Reference** — the challenge definitions for context

Use the xlsx skill formatting conventions: Arial font, header row with bold + colored fill,
auto-fitted column widths, freeze top row.

Save to `/mnt/user-data/outputs/elite20_doc_map.xlsx`.

### Step 6 — Gap analysis (per author)

After mapping, report which challenge artifact-schema slots are **not yet covered**,
broken down by author. This tells each person exactly what they still need to submit.

For each author, check their bundle against each challenge they're contributing to:

- C1: Is there an `/original/` set? A `/translated/` set? A `method.md`?
- C2: Is there a `.tex`? A `.bib`? A `summary.md`?
- C3: Is there an `interaction_log.md`? A `reflection.md`?

Present gaps as actionable items: "Challenge C2 缺少 references.bib — 需要补充参考文献。"

## Edge Cases

- **Files not following naming convention**: Fall back to metadata/header/folder extraction chain. Flag as `convention_match: False` in output.
- **Multiple authors in one document**: If a document lists co-authors, assign to the first author and note others in the Notes column.
- **Author name variants**: Same person may appear as `ZhangWei`, `Zhang-Wei`, `张伟`. The skill does NOT auto-merge variants — flag potential duplicates for manual review.
- **Empty folder**: Report "No files found" and exit gracefully.
- **Nested subfolders**: Recurse, but use subfolder names as context signals (especially for author identification).
- **Duplicate filenames**: Include relative path to disambiguate.
- **Non-UTF8 filenames**: Use `errors='surrogateescape'` when reading paths.
- **Very large files (>50MB)**: Note size but skip content extraction; classify by name/ext only.
- **Binary/unknown files**: Mark as `unknown` format, classify by name only.
- **Convention mismatch**: File named `_C1_` but content signals C2 → flag for review, trust content over filename.

## Extensibility

The challenge registry lives in `references/challenges.yaml`. To add Challenges 4–7 later,
simply append entries to that file. The mapping logic uses the registry dynamically —
no code changes needed in the main workflow.
