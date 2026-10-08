# UPWORK_MCP_CAPABILITY_REPORT

> Commercial Pilot Phase 1.5：Upwork 官方 MCP Server 协议、Tools 目录、OAuth 与自动化条款审计。
> 审计性质：纯调研与只读能力发现，零商业副作用（未提交 Proposal、未发消息、未调用
> Offer/Contract 接口、未触发 Connects 扣费、未接入生产凭据、无后台守护进程）。
> 证据来源：官方文档公开页（upwork.com/ai/mcp、Upwork Support 55446516654611）、
> 公开社区工具观测（Glama connector 目录、uphunt.io 实测、GBTI Network 分析）、
> 端点实测（GET https://mcp.upwork.com/mcp -> HTTP 401，OAuth 保护）。

## 一、协议、认证与 Tool 清单盘点

### 1.1 OAuth 2.1 规范
| 项 | 事实 |
|---|---|
| 授权模式 | OAuth 2.1 + 动态客户端注册（Dynamic Client Registration）+ PKCE |
| 认证方式 | 浏览器登录完成授权，**无 API Key** |
| Scopes | **非细粒度**：连接即授予全量 scopes（无法只开搜索、关消息/合同/财务） |
| Token 刷新 | 自动刷新；可随时在 Upwork 账号设置中吊销 |
| 动态注册约束 | 按精确 Host 白名单（实测 claude.ai / cursor.com / chatgpt.com / loopback 通过，未登记 Host 得 invalid_redirect_uri） |
| 端点实测 | `GET https://mcp.upwork.com/mcp` → HTTP **401**（未授权探测，符合 OAuth 保护预期） |

### 1.2 Tool 清单（Freelancer 侧，官方 `upwork__` 前缀）
- 仪表盘：`get_freelancer_dashboard`（邀请/报价/消息/匹配/Connects 余额一览）
- 发现：`find_jobs` / `list_jobs` / `search_jobs`（过滤：jobType/minBudget/sort/minScore/categories）、`get_job_posting`、`get_rate_insights`
- Draft-Confirm 流：`confirm_draft` / `get_draft` / `update_draft` / 附件上传三件套（start/confirm/get_status/store）
- 邀请/报价：`list_offers` / `manage_offers`（accept/decline/request changes——**绑定接受在 upwork.com 完成**）
- 合同与交付：`list_contracts` / `update_contract` / `end_contract` / `manage_milestones`（提交里程碑工作）
- 账户/资料：`get_account` / `update_account`（可用性、技能、教育、作品集等）
- 消息：`get_messages` / `send_message`
- 元工具：`get_tool_help` / `set_tool_mode` / `set_tool_permission`
- 目录规模：官方公布 **46 tools / 142 actions / ~410 参数**；公开观测到 33 个 `upwork__` 工具。
- **关键缺口**：官方工具目录**无 submit_proposal / accept_offer 直达工具**——提案走
  Draft→Confirm 两步；无轮询工具（polling 仅存在于第三方 `upwork_` 单下划线服务器）。

### 1.3 生产 Marketplace 与 AI Agent Playground 边界
- 生产 Marketplace MCP 承载**真实交易**（Connects 真实扣费、合同真实生效）；
- 官方存在供 AI Agent 无偿测试的 Playground 环境，接口形态与生产一致但**无真实经济后果**；
- 两者的条款边界：Playground 测试不消耗 Connects、不产生合同，生产接入须走完整
  Draft-Confirm 与站点确认链。

## 二、商业全流程八问逐项取证

**A. AIE 可以完全自主读取什么数据？**
Job 详情（标题/描述/预算区间/技能/评分过滤结果）、报价与邀请列表、消息内容、合同列表、
Connects 余额、费率洞察（get_rate_insights）、账户资料。均为只读类（Terms 第一层
Read Actions，无需确认）。

**B. AIE 可以独立起草什么？**
提案（Draft 状态，`manage_proposals create` + `get_proposal_context`，可基于个人资料
与历史工作接地起草）、消息草稿、资料更新草稿。全部仅产生 Draft，不产生外部效果。

**C. AIE 最终能直接向外提交什么？**
经 `confirm_draft` 显式确认后：提案提交（Connects 在确认时扣费）、消息发送、
资料更新、附件上传。**无**直达的 accept_offer / submit_contract 工具。

**D. 哪些操作在协议层/流程层必须有人类显式确认？**
- 协议层：一切写动作走 Draft-Confirm 双步（"approve all" 不算确认，须逐工具设置）；
- 流程层：提案提交、消息发送（且 AI 生成内容强制携带 AI 披露标签）。

**E. 哪些业务环节不暴露 API，必须引导用户去 upwork.com 网页操作？**
合同生效（Accept Offer）、里程碑资金注入、托管释放（Release Escrow）等**绑定金融
动作全部在 upwork.com 站点完成**——MCP 只读取/更新状态，无对应执行接口。

**F. Connects 在哪个具体 Tool 调用时扣除？**
`confirm_draft`（对提案类草稿执行确认）时扣除；仅创建 Draft 不扣费；
Playground 环境不扣费。

**G. 三个真实确认边界：**
- **合同生效（Accept Offer）**：MCP 仅提供 `manage_offers` 意图表达，绑定接受在
  upwork.com 站点完成（合同在站点同意后才开始）；
- **里程碑提交（Submit Work）**：`manage_milestones` 可提交里程碑工作（Draft-Confirm），
  资金划转不在此完成；
- **资金释放（Release Escrow）**：无 MCP 接口，站点操作。

**H. 监管约束与报备边界（Terms of Use 关键条款）：**
- Section 4.1：仅允许"具体、有记录、用户定向的任务"访问，**禁止枚举或持续监测
  Upwork 内容语料**——定周期轮询实时 Job Feed 属越界；
- Section 5.8：禁止"纯自动化决策"（产生法律或类似效力）；
- Section 5.2：禁止 UI 自动化绕行；
- Section 6.6：凭据下的一切行为由用户负责；
- Section 7.2：禁止多账号扇出与 IP 轮换；
- AI 评分过滤持久化：排序输入（Job Success Score / Top Rated 等具体权重）**未公开**，
  不得据此构建自动化择优决策；多 Tool 工作流须遵循逐 Tool 确认，无"一键全自动"模式。

## 三、AIE 合法自治边界与人工断点（能力比对矩阵）

| 能力 | 官方 MCP 支持 | 人工断点 | AIE 边界定性 |
|---|---|---|---|
| 搜索/读取 Job 与资料 | ✅ 只读 | 无 | 全自治 |
| 起草提案/消息 | ✅ Draft | 无（起草阶段） | 全自治 |
| 提交提案 | ✅ confirm_draft | **确认时人工 + Connects 扣费** | 半自治 |
| 发送消息 | ✅ confirm_draft | **人工确认 + AI 披露标签** | 半自治 |
| 接受 Offer / 合同生效 | ❌ 无直达工具 | **站点人工** | 禁止自治 |
| 里程碑提交 | ✅ manage_milestones | Draft-Confirm | 半自治 |
| Escrow 释放 | ❌ 无接口 | **站点人工** | 禁止自治 |
| 实时 Job Feed 轮询 | ❌ 无工具且条款禁止 | — | 禁止自治 |
| AI 择优自动化决策 | ❌ 条款禁止纯自动决策 | 决策须人工 | 禁止自治 |

**审计结论**：AIE 的合法自治域 = **只读发现 + Draft 起草**；一切对外提交与金融动作
必须在人工确认断点或 upwork.com 站点完成；定周期轮询与自动择优决策被条款明确禁止。

## 四、原始 Schema 节选证据

- `GET https://mcp.upwork.com/mcp` → HTTP 401（OAuth 保护，未授权探测拒绝——端点存活证据）
- 公开目录节选（Glama 观测）：`upwork__find_jobs`, `upwork__confirm_draft`,
  `upwork__get_freelancer_dashboard`, `upwork__manage_offers`, `upwork__list_contracts`,
  `upwork__send_message`, `upwork__get_tool_help`（共 33 公开 / 46 官方声明）
- Terms 分层证据：Read Actions（无确认）/ Draft-Confirm Actions（逐项确认）/
  Binding Actions（站点认证）
