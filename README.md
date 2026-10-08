# 自主创业智能体仿真系统（startup_mvp）

毕业设计项目：一个**离散 24 小时 tick 驱动的自主创业智能体（AIE）仿真系统**，模拟一个 AI 智能体从发现商业机会、在沙箱中完成交付、到收款结算的完整生命周期，并以安全边界和记账守恒为核心设计目标。

## 核心闭环

```
机会发现 (Scanner) → 规则过滤 (Filter) → 预检 (Preflight) → 模型决策 (Model)
    → 沙箱执行 (Sandbox) → 人工审批门禁 (Approval Gate) → PR 交付 (Delivery)
    → 维护者合并 → 支付结算 (Settlement) → 对账 (Reconcile)
```

收入只能来自工具结算（`poll_waiting_event` 幂等去重），交付 ≠ 结算（MERGED → DELIVERED + payment_waiting，零收入）。

## 安全边界（pytest Gate 门禁驱动）

- **记账防作弊**：防凭空收入、防重放结算、防复活 DEAD 机会、金额严格等于 observed_revenue、全生命周期守恒恒等式
- **人工审批硬门禁**：不可变工件（canonical 哈希）、审批卡按操作一次性消费、五锁防护（外部写锁 / TOCTOU 篡改检测 / 目标锁 / 范围锁 / 重放防护）
- **沙箱防逃逸**：`--memory-swap=512m` 封堵宿主机 swap 击穿、exit 137 / OOM 熔断、exit 0 才 commit、UID 1000 全程
- **故障域隔离**：模型超时重试上限、单任务硬预算熔断、崩溃重启零二次扣费、外内对账
- **协作拓扑守卫**：基线漂移守卫（GEN_1_INVALIDATED 吸收态）、冲突守卫（Index-First 三重真值探针）、不可解冲突熔断（UNRESOLVABLE_HALT + 事故黑匣子）
- **合规边界**：Upwork MCP 审计（只读发现 + Draft 起草自治，对外提交与金融动作人工断点）

## 目录结构

| 路径 | 说明 |
|---|---|
| `controller.py` | 24h 离散仿真控制器（tick 推进、状态机、记账唯一入口） |
| `tools/` | 工具层：GitHub 适配器/扫描器/PR 发布器、支付适配器、沙箱交付、审批门禁、漂移/冲突守卫、商业评估器、Upwork 客户端等 |
| `tools/opportunity_sources/` | 机会来源抽象层（Algora 等，16 字段统一契约） |
| `tests/` | 28+ 测试套件（安全边界、PR 生命周期、结算流、真实远程执行、冲突/漂移、商业 Pilot 等） |
| `sandbox_manager.py` | Docker 沙箱管理（在 Linux 服务器原生环境执行） |
| `DEBT_LOG.md` | 技术债台账 |
| `data/` | 审计报告与种子数据 |

## 环境

- Python 3.10，pytest
- Docker 沙箱运行于外部 Linux 服务器（本地不运行 Docker）

```bash
python -m pytest tests/
```

## 开发方式

按 **Gate 门禁**推进：每个子阶段先落对抗测试套件（安全边界、回放回测、故障注入），全部通过后封板进入下一阶段。里程碑记录（Gate 1 → 4.5-B4-4 全链路实证，含跨库 Fork 拓扑、多代评审闭环、真实冲突消解与熔断）见各工具模块内 docstring 与 `DEBT_LOG.md`。
