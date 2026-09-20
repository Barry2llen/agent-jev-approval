# agent-jev-approval

**一个由 TypeSafe Jev 驱动、保守且可审计的 Coding Agent 自动审批工具。**

[English](README.md)

`agent-jev-approval` 将 Agent 的审批请求转换为统一模型，先执行确定性的安全规则，再使用 TypeSafe Jev 评估；只有所有安全门槛都通过时才自动批准。任何不确定情况都会交回 Agent 原生的用户审批流程。

项目刻意保持小而清晰。第一阶段只接入 Codex `PermissionRequest` Hook，核心审批模型不依赖 Codex，未来可以增加其他 Adapter，而不引入插件框架。

## 项目状态

当前仓库是早期 MVP：

- 支持的 Agent：Codex `PermissionRequest` Hook
- 支持的 Provider：官方 Python SDK 调用 TypeSafe Jev
- 自动决策：只返回 `ALLOW`
- 回退行为：保留 Agent 原生用户审批
- 明确不在范围内：主动 `deny`、Claude Code、其他 Agent 和动态插件加载

## 设计原则

- **失败时交给用户。** malformed input、hard rule 命中、低置信度、Provider 错误和超时都不会变成自动批准。
- **由代码拥有最终决策。** Jev 返回 typed probability，程序结合多个概率和阈值决定，而不是信任单一的模型 verdict。
- **边界小而清晰。** Adapter 负责把 Agent 协议转换为 `ApprovalRequest`，Provider 负责 typed assessment，审批引擎负责策略。
- **默认可审计。** 高影响操作在 Provider 调用前就会被排除出自动批准路径。

## 架构

```text
Codex PermissionRequest（stdin）
          │
          ▼
adapters/codex.py       Agent 事件 → ApprovalRequest
          │
          ▼
approval.py             hard rules → Jev → threshold decision
       ┌──┴──┐
       │     │
    ALLOW  FALLBACK_TO_USER
       │     │
       ▼     └── 空 stdout → 原生用户审批
Codex allow JSON
```

公共 seam 保持很小：

- `ApprovalRequest`：统一的 Agent、操作、参数、工作目录和上下文。
- `ApprovalProvider`：一个 `assess(request)` 方法，返回 typed Jev 信号。
- `evaluate_approval(...)`：确定性策略加 Provider 结果，返回 `ALLOW` 或 `FALLBACK_TO_USER`。

## 安装

需要 Python 3.10 或更高版本。

在项目 checkout 中安装：

```powershell
py -m pip install .
```

开发和测试环境：

```powershell
py -m pip install -e ".[test]"
```

安装后的命令：

```powershell
agent-jev-approval codex
```

## 配置 TypeSafe

在 Codex 使用的环境中设置 API key：

```powershell
$env:TYPESAFE_API_KEY = "..."
```

官方 SDK 默认读取 `TYPESAFE_API_KEY`，模型默认为 `jev-latest`。也可以使用 SDK 支持的配置：

```powershell
$env:TYPESAFE_DEFAULT_MODEL = "jev-latest"
$env:TYPESAFE_BASE_URL = "https://api.typesafe.ai"
```

Hook 使用 1.5 秒短超时并关闭 SDK 重试。缺少 key、超时、连接失败、API error 或 response 无法解析时，都会回退用户。

## 接入 Codex

将 [`examples/codex-hooks.json`](examples/codex-hooks.json) 合并到 Codex 生效的 Hook 配置层：

- 用户级：`~/.codex/hooks.json`
- 仓库级：`<repo>/.codex/hooks.json`

示例包含 Windows 的 `commandWindows`。如果 console script 不在 `PATH` 中，可以改用：

```text
py -m agent_jev_approval.cli codex
```

Codex 可能会要求审查非托管 Hook。使用 `/hooks` 检查并信任精确的 Hook 定义；修改命令或配置后需要重新审查。

## 决策流程

1. Codex 通过 stdin 发送一个 `PermissionRequest` JSON 对象。
2. Codex Adapter 读取 `tool_name`、`tool_input`、`cwd` 和会话上下文。
3. deterministic hard rules 在任何 Jev 调用前检查明显危险的操作。
4. TypeSafe Jev 在一次请求中评估多个独立信号：
   - 是否适合无人值守执行；
   - 是否具有破坏性或不可逆性；
   - 是否涉及 credential、secret、token 或敏感数据；
   - 是否扩大了用户原始操作范围；
   - 是否影响当前任务或工作区之外的资源；
   - 风险分类：`read_only`、`reversible_change`、`sensitive_or_external` 或 `destructive`。
5. 程序对所有必需 probability 和风险分类 confidence 应用阈值。
6. 只有 `ALLOW` 会写入 Codex 的结构化 allow response；其他所有路径都不写 stdout，让 Codex 继续原生用户审批。

MVP 不会主动返回 `deny`。

## Deterministic hard rules

以下类型永远不会自动批准：

- `sudo`、`doas`；
- 递归或大范围删除，例如 `rm -rf`、`find ... -delete`、`Remove-Item -Recurse`、`rmdir /s`；
- `git push --force`、`git push --force-with-lease`、`git reset --hard`、`git clean -fdx`；
- `kubectl delete`；
- `chmod`、`chown`、`icacls`、`Set-Acl` 等权限修改；
- 修改 `systemctl`、注册表、Windows Service 或 `launchctl` 的系统级操作；
- credential 或 secret 写入、登录操作；
- 其他明显不可逆的操作。

规则使用命令 token、flag 和结构化参数，而不是简单字符串包含。比如 `echo "rm -rf /"` 中的普通文本不会被当作删除命令。它不是完整 shell parser；无法确定的操作仍会交给 Jev，并继续经过保守阈值判断。

## 阈值

默认 `PolicyConfig`：

```text
min_safe_probability = 0.90
max_risk_probability = 0.10
min_safe_band_probability = 0.85
min_confidence = 0.85
provider_timeout_seconds = 1.5
```

只有以下条件全部满足才会自动 `ALLOW`：

- `safe_to_auto_approve >= 0.90`；
- 每个风险 Noul `<= 0.10`；
- `risk_band` 为 `read_only` 或 `reversible_change`；
- 选中的安全风险分类 probability `>= 0.85`；
- 风险分类 confidence `>= 0.85`；
- 没有 hard rule 命中。

库调用方可以传入更严格的策略：

```python
from agent_jev_approval.approval import evaluate_approval
from agent_jev_approval.policy import PolicyConfig

result = evaluate_approval(
    request,
    provider,
    policy=PolicyConfig(min_safe_probability=0.95),
)
```

降低阈值会扩大自动执行范围，应先使用本地数据评估。CLI 默认使用保守配置。

## 安全与隐私

- 任意异常、超时、API error、缺少 answer 或 malformed response 都会变成 `FALLBACK_TO_USER`。
- hard rules 在 Jev 调用前执行，已知高影响命令不会发送给 Provider。
- stdout 只可能包含官方 Codex allow JSON 或为空。
- stderr 只输出稳定的 reason code，不输出 API key、token、credential、secret、完整命令或完整参数。
- Provider 会关闭 TypeSafe SDK 的 body logging。
- 规范化后的审批请求会发送给 TypeSafe 评估。启用前请检查自身数据处理要求；当前 MVP 不做出站 secret redaction。
- Hook 只是防线之一。更强的控制仍应使用最小权限 OS 账号、仓库保护和网络控制。

## 开发与测试

运行完整离线测试：

```powershell
py -m pytest -q
```

测试使用 fake Provider 和本地 HTTP stub，不需要真实 TypeSafe API key，覆盖：

- 安全只读和普通低风险操作；
- 所有 hard-rule 类型；
- Jev 风险高和 confidence 不足；
- timeout、API error、malformed response；
- Codex 输入转换；
- 精确 allow JSON 和空 stdout fallback；
- 使用本地 TypeSafe stub 的真实 CLI 子进程。

手动模拟 Codex 输入：

```powershell
@'
{
  "hook_event_name": "PermissionRequest",
  "tool_name": "Bash",
  "tool_input": { "command": "git status" },
  "cwd": "D:\\work",
  "session_id": "demo-session",
  "turn_id": "demo-turn",
  "permission_mode": "default"
}
'@ | agent-jev-approval codex
```

没有可用 API key 时，预期 stdout 为空，stderr 只显示 fallback reason code。

## 新增 Agent Adapter

在 `src/agent_jev_approval/adapters/` 下新增模块：

1. 解析目标 Agent 的审批事件；
2. 构造 `ApprovalRequest(agent, action, arguments, cwd, context)`；
3. 调用 `evaluate_approval`；
4. 只把 `ALLOW` 映射为目标 Agent 的批准协议，把 fallback 映射为目标 Agent 的原生用户审批。

新 Adapter 不应复制 hard rules、TypeSafe 调用或阈值逻辑。

## 贡献

请保持变更聚焦、易于审查。新增策略应附带回归测试，Adapter 变更应附带协议 fixture，任何改动都不能把 secret 或完整工具参数写入 Hook 日志。

## 官方协议参考

- [Codex Hooks 文档](https://developers.openai.com/zh-Hans/docs/hooks)
- [Codex `PermissionRequest` 输出 Schema](https://github.com/openai/codex/blob/main/codex-rs/hooks/schema/generated/permission-request.command.output.schema.json)
- [TypeSafe Python SDK 文档](https://docs.typesafe.ai/sdk/python)
- [TypeSafe Python SDK 源码](https://github.com/typesafe-ai/typesafe-sdk-python)
