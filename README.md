# agent-jev-approval

使用 TypeSafe Jev 为 Coding Agent 提供统一、保守的自动审批能力。

第一阶段只适配 Codex `PermissionRequest` Hook。Jev 只有在 deterministic hard rules 通过、多个风险概率都满足阈值、且风险分类 confidence 足够高时才会自动批准；其他情况全部保留 Codex 原生用户审批流程。

## 架构

```text
Codex PermissionRequest stdin
          │
          ▼
adapters/codex.py       Codex 协议 → ApprovalRequest
          │
          ▼
approval.py             hard rules → Jev → threshold decision
       ┌──┴──┐
       │     │
    ALLOW  FALLBACK_TO_USER
       │     │
       ▼     └── 空 stdout，Codex 显示原生审批
Codex allow JSON
```

核心模型和流程不依赖 Codex。未来可添加 `adapters/claude_code.py` 或其他 Agent Adapter，复用 `models.py`、`policy.py` 和 `approval.py`。

## 安装

需要 Python 3.10 或更高版本：

```powershell
py -m pip install -e ".[test]"
```

安装后可使用：

```powershell
agent-jev-approval codex
```

## TypeSafe 配置

设置 API key：

```powershell
$env:TYPESAFE_API_KEY = "..."
```

官方 SDK 默认读取 `TYPESAFE_API_KEY`，模型默认为 `jev-latest`。如需覆盖模型或服务地址，可设置：

```powershell
$env:TYPESAFE_DEFAULT_MODEL = "jev-latest"
$env:TYPESAFE_BASE_URL = "https://api.typesafe.ai"
```

MVP 为审批流程使用短超时（1.5 秒），并关闭 SDK 重试；API key 缺失、超时或 API 错误都会回退用户。

## Codex 接入

将 [`examples/codex-hooks.json`](examples/codex-hooks.json) 的内容合并到用户或项目 Hook 配置：

- 用户级：`~/.codex/hooks.json`
- 项目级：`<repo>/.codex/hooks.json`

Windows 使用 `commandWindows` 字段。安装命令不在 PATH 时，可将命令改为：

```text
py -m agent_jev_approval.cli codex
```

Codex 会要求审查非托管 Hook。使用 Codex 的 `/hooks` 查看并信任当前 Hook 定义；修改 Hook 文件后需要重新审查。

## 审批流程

1. Codex 通过 stdin 发送 `PermissionRequest` JSON。
2. Codex Adapter 提取 `tool_name`、`tool_input`、`cwd` 和会话上下文。
3. deterministic hard rules 先检查明显危险操作。
4. 通过后，TypeSafe Jev 同时评估多个问题：
   - 是否适合无人值守执行
   - 是否破坏性或不可逆
   - 是否涉及 credential、secret 或敏感数据
   - 是否扩大用户原始操作范围
   - 是否影响当前任务之外的资源
   - 风险分类：只读、可逆变更、敏感/外部、破坏性
5. 程序读取多个 Jev probability、风险分类概率和 confidence，按阈值决定。
6. 只有 `ALLOW` 会写入 Codex allow JSON；所有其他路径 stdout 为空，让 Codex 继续原生用户审批。

MVP 不主动返回 `deny`。

## Hard rules

以下类型默认不自动批准：

- `sudo`、`doas`
- `rm -rf`、递归/批量删除、`find ... -delete`、`Remove-Item -Recurse`
- `git push --force`、`git reset --hard`、`git clean -fdx`
- `kubectl delete`
- 权限修改：`chmod`、`chown`、`icacls`、`Set-Acl`
- 系统级配置修改：`systemctl`、注册表、Windows Service、`launchctl`
- credential/secret 写入或登录操作
- 其他明显不可逆操作

规则使用命令 token、选项和参数进行轻量判断，不把普通文本中的 `rm -rf` 当作命令。它不是完整 shell parser；无法确定的操作仍会交给 Jev，并继续经过保守阈值判断。

## 默认阈值

默认 `PolicyConfig` 为：

```text
min_safe_probability = 0.90
max_risk_probability = 0.10
min_safe_band_probability = 0.85
min_confidence = 0.85
provider_timeout_seconds = 1.5
```

只有以下条件全部满足才会 `ALLOW`：

- `safe_to_auto_approve >= 0.90`
- 每个风险 Noul `<= 0.10`
- `risk_band` 为 `read_only` 或 `reversible_change`
- 选中的安全风险分类概率 `>= 0.85`
- 风险分类 confidence `>= 0.85`
- 没有 hard rule 命中

库调用方可以传入自定义 `PolicyConfig`：

```python
from agent_jev_approval.approval import evaluate_approval
from agent_jev_approval.policy import PolicyConfig

result = evaluate_approval(
    request,
    provider,
    policy=PolicyConfig(min_safe_probability=0.95),
)
```

降低阈值会扩大自动执行范围，应先使用本地样本评估；Hook CLI 默认使用保守配置。

## 安全模型

- 任意异常、超时、API error、缺少 Jev answer 或 malformed response 都是 `FALLBACK_TO_USER`。
- hard rule 在 Jev 调用前执行，高风险命令不会发送给 provider。
- stdout 只可能包含官方 Codex allow JSON 或为空。
- stderr 只输出固定 reason code，不输出 API key、token、credential、secret、完整命令或完整参数。
- SDK 的 `typesafe_sdk` body logging 会被关闭。
- 规范化后的审批请求会发送给 TypeSafe 进行评估；部署方应根据自身数据策略决定是否允许工具参数出站。

## 本地测试

```powershell
py -m pytest -q
```

测试不依赖真实 TypeSafe API key，使用 fake provider 和本地 HTTP stub 覆盖：

- 安全只读操作自动批准
- 普通低风险操作交由 Jev 判断
- hard rule 回退
- Jev 风险高或 confidence 不足回退
- TypeSafe timeout/API error/malformed response 回退
- Codex allow JSON 和空 stdout fallback

也可以手动模拟 Codex 输入：

```powershell
@'{
  "hook_event_name": "PermissionRequest",
  "tool_name": "Bash",
  "tool_input": { "command": "git status" },
  "cwd": "D:\\work",
  "session_id": "demo-session",
  "turn_id": "demo-turn",
  "permission_mode": "default"
}'@ | agent-jev-approval codex
```

没有可用 API key 时，预期 stdout 为空，stderr 只显示 fallback reason。

## 新增 Agent Adapter

新增 Adapter 只需要：

1. 解析目标 Agent 的审批事件。
2. 构造 `ApprovalRequest(agent, action, arguments, cwd, context)`。
3. 调用 `evaluate_approval`。
4. 仅将 `ALLOW` 映射到目标 Agent 的批准协议；其他结果映射为目标 Agent 的“未做决定/继续用户审批”。

不要在新 Adapter 中复制 hard rules、TypeSafe 调用或阈值逻辑。

## 官方协议参考

- [Codex Hooks 文档](https://developers.openai.com/zh-Hans/docs/hooks)
- [Codex PermissionRequest 输出 Schema](https://github.com/openai/codex/blob/main/codex-rs/hooks/schema/generated/permission-request.command.output.schema.json)
- [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python)
- [TypeSafe Python SDK 源码](https://github.com/typesafe-ai/typesafe-sdk-python)
