# 架构问题清单（Debt Log）

> 记录已发现、尚未完全修复的架构级问题。每条必须给出发现证据与修复计划。

## DEBT-001：pycache 字节码污染已签名提交（发现于 Gate 4.5-B2，2026-10-01）

- **现象**：交付流水线在 `rm -rf __pycache__` 之后运行 pytest，pytest 重新生成
  `__pycache__/*.pyc`，随后 `git add -A` 将二进制字节码一并提交；真实推送
  Cc-mug-up/aie-b2-sandbox PR #4 的提交中即含 3 个 .pyc 文件。
- **证据**：`git diff HEAD^ HEAD` 输出含 `Binary files ... __pycache__/calculator.cpython-310.pyc differ`；
  审批卡 DIFF HASH 13c1c14a… 覆盖了这些二进制头。
- **影响**：污染仓库历史；使 diff 与工件哈希包含非源码二进制噪声。
- **修复计划**（后续流水线强制）：
  1. 所有交付/预检 exec 注入环境变量 `PYTHONDONTWRITEBYTECODE=1`；
  2. 提交前强制写 `.gitignore`（至少含 `__pycache__/`、`*.pyc`）；
  3. 提交前 `git add` 白名单化（仅源码文件），不再使用裸 `git add -A`。
- **状态**：OPEN（b2_rehearsal_driver.py 已加入强制项；SandboxDeliveryPipeline 为冻结工件，待后续门禁统一加固）。

## DEBT-006：git apply 按 hunk 声明计数静默截断（发现于 Gate 4.5-B4-2，2026-10-02）

- **现象**：B3-3 Gen-2 补丁的测试 hunk 声明 `+11,8` 而实际新增 9 行，git apply 未报
  corrupt patch，而是按声明数截断、静默丢弃尾部 3 行——导致 B3-3 合并进主干的
  81689316 实际缺失 `test_multiply_rejects_non_numeric`。五重对账只验 SHA 不验内容，
  此差异未被发现。
- **处置**：本 Gate 起补丁 hunk 计数一律人工复核（逐文件 wc -l 实测行号）；B4-2
  非平凡演练已按真实行布局重建补丁。
- **状态**：OPEN（长期项：交付流水线应增加"审批卡 declared diff 与提交实际 diff
  的规范化逐字比对"内容级对账，而非仅 SHA）。

## DEBT-005：git apply 拒绝"文件首行无上下文 hunk"（发现于 Gate 4.5-B3-3，2026-10-02）

- **现象**：git 2.34.1 的 `git apply` 对 `@@ -1,1 +1,1 @@`（文件首行、无前后上下文）
  的单行变更 hunk 拒绝匹配（字节级一致仍报 "patch does not apply"），
  而 classic `patch -p1` 对同一补丁正常匹配。
- **处置**：补丁统一为"变更行附带上下文行"的 hunk 结构（已验证通过）。
- **状态**：OPEN（规避措施生效；根源疑为 git 2.34 模糊匹配边界条件）。

## DEBT-004：changed_files 声明值与 Git 物理事实不一致（发现于 Gate 4.5-B3-1，2026-10-01）

- **现象**：freeze_artifact 的 changed_files 为调用方手动入参；B3 第一轮提交实际包含
  `.gitignore`，但 V1 审批卡只声明了 calculator.py——双重 Scope Lock 累积检查据此报警。
- **整改（已就地消除）**：freeze_artifact 彻底废除 changed_files 手动入参，改为从
  diff_content（Git 真实输出）解析提取（`_derive_changed_files`）；B3 累积检查的文件
  基准改为 `git diff --name-only base..commit_1` 的物理文件集合，断言
  files(base..commit_2) ⊆ files(base..commit_1)。Git 真实对象为唯一真理源。
- **状态**：RESOLVED（2026-10-01，裁决就地修正；A3/B2 调用点已同步更新）。

## DEBT-003：单账号靶场的评审触发适配（发现于 Gate 4.5-B3-1，2026-10-01）

- **现象**：GitHub 平台硬性禁止 PR 作者对自己发起 "Request changes"（选项置灰），
  单账号靶场（Token 与网页登录同为 Cc-mug-up）中原生
  `review.state == "CHANGES_REQUESTED"` 物理上不可能生成。
- **处置**：ReviewNormalizer 增加显式指令前缀规则——COMMENTED 评审正文以
  `[CHANGES_REQUESTED]` 开头时视同生效（trigger=EXPLICIT_PREFIX）。
  这是字面指令标记，非自然语言启发式；生产多账号环境下原生 state 仍为最高优先级。
- **状态**：OPEN（单账号靶场长期适配；记录以便未来审计区分 EXPLICIT_PREFIX 与 NATIVE_STATE 触发源）。

## DEBT-002：canonical() 对尾换行敏感（发现于 Gate 4.5-B2，2026-10-01）

- **现象**：`canonical()` 保留文本尾部的 `\n`（`split("\n")` 的末位空元素），
  `splitlines()` 则吞掉尾换行——两种提取方式对同一 diff 产出不同哈希。
  执行器首版用 splitlines 提取，导致现场重算哈希与签名失配（熔断正确起效）。
- **证据**：65351793…（splitlines 提取）≠ f26d8e30…（split("\n") 提取），
  同一工作区同一提交。
- **影响**：任何"先冻结、后校验"的环节，双方提取路径必须字节级一致。
- **修复计划**：新代码统一约定"diff 文本必须用 `split("\n")` 保留尾换行后交
  canonical()"；远期（需重新签发所有在途工件时）可将 canonical 改为尾换行幂等。
- **状态**：OPEN（已规避；冻结语义未改动，避免既有签名失效）。
