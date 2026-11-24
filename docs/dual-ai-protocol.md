# 双 AI 协作协议 v4

目标：按工作部件难度分工而不是按比例凑工作量，减少跨模型交接长度和重复
输入，让实现、指导、接管和验收依据同一份可核查状态继续工作。
跨模型交接的是压缩指令、显式决定与证据，不要求模型交换隐藏推理过程。

## 工作部件与档次

一次任务由以下工作部件组成，难度不同：

| 部件 | 说明 | 主要承担者 |
|---|---|---|
| `read` / `locate` / `search` | 读取、定位、检索 | 执行 AI |
| `normalize` | 上下文压缩、有效信息规范化 | 执行 AI |
| `execute` | 修改文件、运行命令 | 按档次：L1 主 AI，其余执行 AI |
| `monitor` | 运行监控、测试验证 | 执行 AI（全主档除外） |
| `think` / `design` | 思考决策、方案设计、疑难诊断 | 主 AI |
| `report` | 输出汇报 | 执行 AI 起草，主 AI 汇总 |

档次从难到易：

| 档次 | 名称 | 分工 | 调用形态 |
|---|---|---|---|
| `L0` | 全主档 · 最高 | 全部由主 AI 完成 | 1 次调用 |
| `L1` | 主控重档 | 主 AI 规划、思考、设计、主要执行、验收；执行 AI 只读取/定位/检索/监控 | 规划 + 执行 + 验收 |
| `L2` | 中高档 · 主从配合（默认） | 执行 AI 压缩上下文 → 主 AI 给简洁指令 → 执行 AI 执行监控、可询问一次 → 主 AI 验收 | 压缩（仅长上下文）+ 规划 + 执行 + 验收 |
| `L3` | 低档 · 从主导 | 主 AI 只下发契约；执行 AI 自主执行监控；全部通过时跳过验收，失败时主 AI 只读审查 | 契约 + 执行（+ 失败审查） |
| `L4` | 全从档 · 最低 | 全部由执行 AI 完成 | 1 次调用 |

角色由任务的 `ops` 决定，`role` 字段只是规划参考，宿主会按档次重新分配。
工作点 `points` 仍保留，但只用于界面展示，不参与分配。

## 调度和会话

1. `L0` / `L4` 只调用一个角色，独立实现及验证。
2. 其余档次由主 AI 只读规划或下发契约，生成稳定任务 ID、`ops` 和必须保留的
   `constraints`。保持任务顺序，连续同角色项合并调用，不跨角色重排。
3. 历史超过 7000 字符且该档次允许时，先由执行 AI 只读压缩为
   `{summary, constraints, refs, risks}`；主 AI 首次接手只读压缩结果，
   原文始终保留在 `input.json`。摘要落盘为 `context-brief.json`，主 AI
   只在缺项、冲突或需要具体证据时定位原文。约束、接口和风险不按字符或条数截断；
   摘要结构无效或不比原文短时退回原始文件引用。短上下文不产生额外压缩调用。
4. 每个角色独立保存会话 ID。Claude 使用 `--resume`，Codex 使用 `exec resume`。
   会话按角色、来源、模型、思考档位、项目和权限配置隔离。
5. 首次接手读取原始输入、状态卡和适用的上轮快照；已有有效摘要时，主 AI
   读取摘要并按需核实原文。续接只发送当前任务和状态索引。
   大状态卡改用完整文件引用及有效 JSON 索引。
6. 执行 AI 可输出一行压缩询问后停止：
   `DUAL_ASK: {"task":"T1","problem":"…","need":"…","tried":["…"]}`。
   主 AI 收到询问、任务和约束的压缩摘要，只输出一行
   `DUAL_GUIDE: {"decision":"…","next":["…"],"avoid":["…"],"done_when":"…"}`；
   执行 AI 原地续做。询问次数由档次限制（L2 一次，L3 两次）。
7. 第一项失败即暂停后续任务。主 AI 读取完整失败报告、代码现场及已保留决定后
   接管；成功前缀不重做。
8. 验收由档次和界面「复查」开关共同决定。复查开启（默认）时：`L1` / `L2`
   始终验收；`L3` 全部通过时跳过验收以省一次调用，失败时只读审查；
   `L0` / `L4` 不单独验收。复查关闭时，所有档次都不再调用独立的主 AI
   复查；执行者自检、失败接管和逐项结果判定不变，失败、漏项或取消仍然
   不会报告为成功。规划提示不会安排与调度器验收重复的独立验收任务。
9. 同一界面的下一轮请求可复用配置兼容的各角色会话，并携带新请求、增量上下文和上轮快照。
   句柄只由当前对话在内存中传递，磁盘快照不自动恢复会话；角色、项目、模型、思考档位、
   权限、CLI 或服务商/账户变化时隔离。新建或恢复其他对话、取消和调度异常清除关联。
   最新用户请求优先；已在兼容会话内的历史不反复压缩，完整原文仍保存。

## 版本化任务状态与交接

两个角色使用同一份持久化任务状态（`state.json` 的 `task_state`），宿主确定性
生成，不从模型回答猜测：

```yaml
task_id: T1
revision: 7                 # 单调递增；每次计划、记录、切换、未确认操作都更新
goal: 用户最终目标
goal_source: current_request   # previous.json 与当前输入冲突时以最新请求为准
constraints: [...]
completed:
  - id: T1
    status: verified         # verified | provisional | incomplete
    summary: ...
    evidence_ref: call-003.json
decisions:
  - task_id: T1
    decision: ...
    basis: call-003.json
    status: confirmed        # confirmed | provisional
artifacts:
  - path: VERSION
    version: 1c34f88707b5    # sha256 前 12 位；大文件为前缀+大小
    locator: VERSION:1
    accessible: true
verification:
  - task_id: T1
    check: ...
    status: verified
    evidence_ref: call-003.json#verification[0]
pending:
  - id: T2
    status: blocked          # blocked | incomplete | unconfirmed | pending_user_action
    reason: ...
    needs_check: true        # 未确认操作必须先核对再继续
next_action: 执行任务 T2：...
resume_point: 任务 2/3（T2）
```

- 只有带实际验证证据的通过记录才标 `verified`；通过但缺证据为 `provisional`。
- 未执行、失败、`open_issues`、用户保留操作和未确认操作都进入 `pending`，
  不会因为顶层 `passed` 而被当作完成。
- 产物记录路径、版本/摘要和定位，但引用不代表模型已读取内容；文件缺失时
  `accessible: false`，接手方按 `segment_refs` 或路径重新核实。
- 旧协议（v2/v3）没有 `task_state` 时写为 `status: unknown`，不补造成完成结论。

交接方式：

1. 首次交接或角色尚未确认任何 revision 时，发送必要完整状态。
2. 之后按该角色已确认的 revision 只发送增量；目标以 `goal_ref` 引用，避免
   重复注入原文。角色未确认、确认版本超前或历史被裁剪时回退完整状态。
3. 状态卡由状态确定性序列化生成，不额外调用模型写摘要；只有长上下文的
   压缩步骤会调用执行 AI，且摘要结构不完整或没有变短时退回文件引用。
4. 交接 token 预算由 `DualRunThread(handoff_token_budget=...)` 配置，默认
   1200。计数使用 `heuristic-ascii/4+cjk/1.5` 估算，只用于裁剪，不是计费
   token。超预算时依次裁剪切换明细、历史、证据正文和重复报告；目标、约束、
   关键决策、pending 和 `next_action` 始终保留。核心仍超预算时写
   `budget_exceeded: true` 和 `segment_refs`，保持完整 JSON，不截断。
5. 每个角色的公共规则、报告协议放在稳定前缀，不含时间、路径、任务 ID 或
   另一模型输出；动态阶段、状态和任务包放在后部。序列化顺序固定。
6. 切换只发生在规划/契约落盘、任务结果返回、产物保存、验收开始等边界。
   日志记录切换原因、边界、已知缓存状态、交接大小和重新读取成本；费用
   未知时保持 `null`，不据此声称节省。工具未返回、写入未确认或运行中不
   进行常规切换。故障恢复先核对 `unconfirmed_operations`，不自动重放。
7. 不传递或重放另一个模型的内部思维链；只传结论、决策依据、证据位置、
   失败尝试和恢复条件。供应商协议字段按原接口处理。

## 执行报告

最后一个非空行是 `DUAL_RESULT: {JSON}`。顶层包含 `status`、`summary` 和非空
`verification` 字符串数组。状态只有 `passed`、`blocked`、`failed`。
执行任务包还必须有 `tasks` 数组，按包内顺序逐项提供：

```json
{
  "status": "passed",
  "summary": "本包完成",
  "verification": ["实际执行的整体验证及结果"],
  "tasks": [
    {
      "id": "T1",
      "status": "passed",
      "summary": "具体修改及结果",
      "changed_files": ["src/example.py"],
      "decisions": ["保留旧字段，以兼容已有调用方"],
      "interfaces": ["example(value: str) -> bool"],
      "verification": ["python3 -m unittest example_test：exit 0"],
      "open_issues": []
    }
  ]
}
```

- 验证说明必须来自实际执行；未执行项写明阻塞原因并返回 blocked。
- 顶层缺少 verification 时，只有完整有效的逐项证据可派生该字段；显式无效证据不能派生。
- 其他仅格式缺项的 passed 报告可只读补报一次，不重跑实施；失败、取消与错误 ID 不进入此捷径。
- DUAL_ASK 可携带 completed 数组，保存已验证的连续成功前缀；续做仅发送剩余任务。
- pending_user_actions 保存用户保留的命令；准备完成与实际发布分别说明，不冒称已执行。
- 缺失、重复、未知或乱序 ID 不会被直接接受；缺少后续报告只保留已验证的成功前缀。
- 有 `open_issues` 的任务不能通过；失败后的成功声明不用于放行后续任务。
- 单任务、接管、端点及无需修复漏项的验收兼容旧版三字段报告。
- 宿主验证报告结构和状态，主 AI 核查代码及工具证据。宿主不会盲目执行模型
  报告里的命令。

## 本地状态与恢复

宿主在 `${XDG_STATE_HOME:-~/.local/state}/claude-console/dual-runs/run-*` 保存：

| 文件 | 内容 |
|---|---|
| `input.json` | 完整用户请求、项目路径及可见对话历史 |
| `context-brief.json` | 可选的上下文摘要，完整保留压缩输出中的约束和引用 |
| `plan.json` | 规范化任务、工作部件、档次、验收要求、约束与角色 |
| `state.json` | 共享任务状态：revision、各角色已确认 revision、目标、约束、已完成/决策/产物/验证/pending、下一步、切换与未确认操作、会话、复查开关、逐项报告引用和测量口径 |
| `call-NNN.json` | 每次实际 CLI 调用的完整输出、错误、会话 ID、模型、服务商、用量、缓存分项、推理 token、交接大小、摘要/重复调用标记和续接标记 |
| `previous.json` | 可选的上轮快照及来源引用；旧协议缺失任务状态时标记 `unknown` |
| `result.json` | 最终状态及统计（档次、复查开关、询问、交接估算、切换次数与原因、重复调用、覆盖率、费用及未知项） |

每轮目录权限 0700、记录文件权限 0600，以临时文件加原子替换写入。
记录保存在项目之外，不混入仓库改动。完整输入可能包含用户提供的敏感内容；
仅本地保留，不写入环境变量或服务商密钥。记录不自动删除。

同一角色的会话失效，只有在明确返回找不到会话、且没有正文或工具操作时才重建
一次，重建会重新读取完整输入和共享状态；失败尝试也计入调用统计。额度不足、
超时、取消、部分执行等情况不会触发自动重放。角色会话复用能保持连续理解，
但不承诺历史输入免费或固定额度节省。提示字符统计包含会话失效后的重建输入，
不包含模型自行读取文件或 CLI 注入的内容，不能等同于计费 tokens。

## 验证

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
python3 -m py_compile dual_ai.py main.py tests/test_dual_ai.py tests/test_dual_protocol.py tests/test_dual_ui.py
```

离线测试包含真正启动模拟 CLI 子进程、临时项目文件写入、会话 ID 往返、合并任务、
上下文预压缩、中途询问与二次指导、低档跳过验收、复查开关（各档次调用序列、
失败接管与取消）、部分失败接管、状态落盘、跨轮交接和 Qt 界面行为；还使用
真实 Qt 事件循环检查切换项目后定位到最近用户提问、快速切换与后台输出隔离、
隐藏对话页恢复定位。不启动真实模型。本机真实 CLI 的参数解析另外用 `--help`
验证，不进行账户调用或扣费。

用量口径、修复证据和对照工具见 [双 AI 开销诊断与验证](dual-ai-performance.md)。
