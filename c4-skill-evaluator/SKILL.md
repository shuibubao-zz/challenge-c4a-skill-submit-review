---
name: c4-skill-evaluator
description: >
  C4 技能提交自动评审器。给定一个装着 C4 技能提交的本地文件夹，自动识别每位作者、
  核查 5 项必交物完整性（Skill 说明文档 / 可执行内容 / Demo / 教学说明 / AI 生成日志）、
  按 C4 四条件（可复用 Reusable、可执行 Executable、可验证 Verifiable、IO 明确 Clear I/O）
  逐条取证评分，输出 Markdown 评审报告 + Excel 详表 + JSON 结果，并给出缺口与改进建议。
  关键特性：每条判定都带**证据锚点**（文件名 + 行号），「语法校验 / .skill 包结构 / Demo 是否真实产物」
  三项是**真实执行**而非关键词匹配；内置反堆砌（重复关键词不累计加分）与负信号硬地板
  （出现硬编码本机路径/密钥/占位符即封顶）；自带 `--self-test`（16 项，含无 PyYAML
  解析回归与结构形态用例）与带人工标注的黄金测试集（测误判率）。
  Use whenever the user says "评审 C4 提交", "检查技能提交完整性", "evaluate C4 submissions",
  "check C4 completeness", "review skill submissions", "C4评审报告", "给这批技能打分",
  or provides a folder path and asks to evaluate/review the skill files inside.
  Also trigger when the user mentions C4/C4A challenge together with evaluation, grading or review.
version: 1.0
---

# C4 技能提交自动评审器

## 一句话定位

**输入**：一个装着 C4 技能提交的本地文件夹（微信群里收上来的、命名的或乱命名的都行）
**输出**：一份「谁交了、缺什么、质量几档、为什么」的评审报告（Markdown + Excel + JSON）

它像一个助教：不只点名（谁没交），还按评分标准批改（质量几档），并且**每条判定都能指出
证据在第几行**——这样被评审的人可以当场核对，评审者也不用背"我觉得"的黑锅。

---

## 一、为什么需要它

C4 是"每个人交一个技能"的挑战。真人评审的问题是：
- 数量一大，完整性核查会漏（谁少交了一个文件）；
- 质量评分靠印象，不同人给的分不可比；
- 被评审者不服气时，评审者拿不出证据。

本评审器把这三点都变成**可复核的机械动作**：
1. 完整性 = 5 个槽位逐一匹配（文件名 + 内容信号双通道）；
2. 质量 = 4 个条件 × 每条件 5–6 个检测项，**命中即取证**；
3. 每个检测项的输出都带 `文件名:行号`（或"未发现：xxx"），可当场核对。

---

## 二、运行方式

```bash
# 0) 自检（先看这个，16 项全 PASS 才说明环境没问题）
python scripts/c4_evaluator.py selftest

# 1) 评审一个文件夹
python scripts/c4_evaluator.py evaluate "C:/path/to/C4_submissions" \
    --rubric references/c4_rubric_v2.yaml \
    --out ./评审输出 \
    --json ./评审输出/result.json

# 2) 跑黄金测试集（测评审器自身的误判率）
python scripts/golden_test.py --out ./evidence/golden_report.txt
```

**环境**：Python 3.9+，**零强制依赖**——有 PyYAML 就优先用 PyYAML 解析评分标准，没有则降级到内置迷你解析器（文本抽取自实现）。两条路径的一致性由自检担保。
`openpyxl` 可选——装了才输出 Excel 详表，没装只输出 Markdown/JSON，不报错。

**输出物**：

| 文件 | 内容 |
|---|---|
| `C4评审报告.md` | 班级总览 + 每位作者的完整性表 / 质量表 / 综合分 / 改进建议 |
| `C4评审详表.xlsx` | 同上，表格化（需 openpyxl） |
| `--json` 指定路径 | 机器可读结果，便于二次处理 |

---

## 三、评分标准（机器可读，可改）

标准写在 `references/c4_rubric_v2.yaml`，**不是硬编码在代码里**——改标准不改代码。

### 完整性（5 槽位）

Skill 说明文档 / 可执行内容 / Demo / 教学说明 / AI 生成日志。
每槽位两条通道：文件名模式（`*skill*` 等）+ 内容信号（如"使用场景""输入""输出"）。
只有文件名没有内容时仍判 ✅，但会用 `filename` 标注判定方式，便于人工复核。

### 质量（4 条件 × 5–6 检测项）

| 条件 | 关注 | 检测项举例 |
|---|---|---|
| 可复用 Reusable | 换台机器还能用吗 | 有安装说明、声明依赖、**无硬编码本机绝对路径**、**无硬编码密钥**、有泛化说明 |
| 可执行 Executable | 真的能跑吗 | 含代码块、含 YAML frontmatter、有明确入口、**py_compile 真实语法校验**、**.skill 包结构真实校验**、**无 TODO/YOUR_ 占位** |
| 可验证 Verifiable | 怎么证明它对 | 有测试/自检、定义预期输出、有输入→输出样例、**Demo 是真实产物（非空非占位）**、结果可度量 |
| IO 明确 Clear I/O | 输入输出说清了吗 | 「输入X，输出Y」一句话、输入类型/格式、输出类型/格式、一句话定位、边界与异常 |

评级：命中比例 → ✅ / ⚠️ / ❌（阈值见 YAML `grading.thresholds`）。
**综合分 = 完整性 × 0.4 + 质量 × 0.6**。

### 三条防作弊规则（重要）

1. **反堆砌**：同一个关键词反复出现**不累计**加分（`anti_gaming.duplicate_content`）。
2. **负信号硬地板**：出现硬编码本机路径 / 明文密钥 / `TODO`/`YOUR_XXX` 占位符时，
   对应条件**封顶**，无论正面信号命中多少（`apply_negative_cap`）。
3. **真实校验项**（`real: true`）：语法校验、.skill 包结构、Demo 真实性这三项
   **真的去执行**（`py_compile` / 读 zip / 查文件字节数），缺证据即判 fail——
   不接受"文档里写了有测试"就算有测试。

---

## 四、设计取舍（为什么这么做）

| 取舍 | 选择 | 理由 |
|---|---|---|
| 评分标准放哪 | YAML 外置 | 标准会变；改标准不该动代码 |
| 判定要不要给证据 | 必须给（`文件:行号`） | 评审的核心价值是可复核，不是打分 |
| 关键词匹配 vs 真实执行 | 混合：能用真实执行的三项一律真实执行 | 纯关键词会被"写了但其实跑不起来"骗过 |
| 依赖 | 零强制（有 PyYAML 用 PyYAML，没有用内置解析器） | 评审器自己都装不上，就别评别人 |
| 分数 | 完整性 0.4 + 质量 0.6 | 质量权重更高，但也拦住"写得漂亮却少交文件" |

---

## 五、已知边界（别把它当裁判）

- **它测的是"可机械核验的部分"**：文件齐不齐、有没有硬编码、能不能过语法、说明写没写清。
  它**不评价**技能本身是否有用、思路是否新颖、教学效果好不好——那需要人。
- **作者识别是三级回退**：文件名 `姓名_C4_*` → 目录名 → `unresolved`。
  识别不出的会标 `confidence: low`，报告里明写"需人工确认"，不猜。
- **误判率有实测数字**：见 `evidence/golden_test_v3.txt`——
  完整性 97.5%、质量精确匹配 59.4%、相差≤1 级 96.9%、**严重误判 3.1%**。
  ⚠️ 精确匹配不到 60% 是**故意**的：测试集里放了边界样本，
  若达到 100% 说明测试集太简单或标签是照输出填的（循环论证）。

---

## 六、文件清单

| 路径 | 作用 |
|---|---|
| `SKILL.md` | 本文件 |
| `scripts/c4_evaluator.py` | 评审引擎（扫描→识别→完整性→质量→综合→报告），含 16 项 `--self-test` |
| `scripts/build_golden_set.py` | 生成带人工标注的黄金测试集（8 个 fixture，各含 `__expected__.json`） |
| `scripts/golden_test.py` | 用黄金测试集测评审器自身的误判率，输出完整对照 |
| `scripts/build_skill_pkg.py` | 打包 .skill 并防漂移校验（`--check`）；**改 rubric/代码后必须重打包** |
| `references/c4_rubric_v2.yaml` | 机器可读评分标准 v2（可改，不改代码） |
| `references/golden_set/` | 黄金测试集 fixture（人工 ground truth） |
| `教学说明.md` | 给使用者的分步教学 |
| `拿来说明.md` | 拿了谁的、改了什么、没拿什么 |
