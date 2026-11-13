# 执行提供方与等级管理

本体提供通用执行提供方契约、注册表、生命周期管理、会话代次、CLI、HTTP 管理通道
与 WebUI 执行级别页面。平台引擎由独立适配器实现，管理元数据不代表已安装执行引擎。

| 项目 | 当前状态 |
| --- | --- |
| 等级定义、能力与回执元数据 | 已实现 |
| 同账号排空切换、超时与失败处理 | 已实现，用隔离提供方和 HTTP 测试验证 |
| 演示与显式 HTTP profile | 已接入管理器 |
| L0 本地入口 | 通用界面驱动描述与不可用占位，由独立实现提供能力 |
| L1 本地入口 | 通用附着扩展描述与不可用占位，由独立实现提供能力 |
| L2 本地入口 | 通用完整客户端描述与不可用占位，由独立实现提供能力 |
| L3 本地入口 | 通用协议核心描述与不可用占位，由独立实现提供能力 |

查看或选择元数据不会加载原生组件、附着客户端或执行键鼠输入。
外部 HTTP 服务的等级来自配置声明，不等于 Heredity 已实现或验证其机制。

## 目录和查看

```text
src/heredity/providers/
  contracts.py       不可变等级、机制、动作和回执描述
  placeholders.py    四个独立的未就绪本地入口
  manager.py         单账号动作所有权、排空、代次与切换
  registry.py        演示、外部 HTTP 与显式注册表
```

```powershell
.\.venv\Scripts\python.exe run.py providers list
.\.venv\Scripts\python.exe run.py providers list --registry examples/providers.demo.json
```

查看命令仅构建元数据，不连接接口、不激活提供方。
输出 `inspection_only=true`；输出中的 `ready` 表示候选已经配置，可尝试连接，
不是账号已登录。账号就绪由运行时的 `state=ready` 和身份核验共同表示。

`execution_level=null` 保留给演示或未声明机制的 profile。Demo 不属于 L0。
动作、内存访问和会话归属分别描述，不混成一个等级数值。

## 演示切换

示例注册表绑定账号 `10000`，包含演示提供方 A / B：

```powershell
$env:HEREDITY_EXECUTION_ADMIN_TOKEN = "replace-with-a-distinct-admin-token"
.\.venv\Scripts\python.exe run.py serve `
  --providers examples/providers.demo.json --provider demo --sse
```

在管理 API 所在终端设置同一管理凭据：

```powershell
$env:HEREDITY_WEBUI_PASSWORD = "replace-with-your-local-test-password"
$env:HEREDITY_WEBUI_EXECUTION_TOKEN = "replace-with-a-distinct-admin-token"
.\.venv\Scripts\python.exe run.py webui
```

两个终端的环境变量不会自动共享，管理凭据值需一致。
WebUI 页面为 `http://127.0.0.1:5178/execution`，也可从侧栏“执行级别”进入。
没有配置独立管理通道时，页面仍可查看，但切换按钮禁用。
UI 启动说明见 `webui.md`；示例配置不表示当前机器正在运行测试服务。

切换不修改注册表的 `default_provider`；重启时仍按显式启动配置选择。
所有演示提供方共享同账号的持久 ID 分配器，切换不会重放消息或重复分配旧 ID。
Demo 与真实 profile 不允许混入同一注册表或运行时，状态目录也分别保存。

## 外部服务声明

`examples/providers.external.example.json` 与
`examples/onebot-upstream.example.json` 展示公开 OneBot HTTP 接口的接入格式。
账号、端口和凭据均须按实际情况替换，示例不证明本机存在相应服务。

注册表 `schema_version=1`，必须指定 `self_id`、`default_provider`、`providers`。
条目类型只支持 `demo` 和 `http_json`；没有任意 Python 模块或二进制自动加载入口。
每个外部 profile 都必须绑定同一账号，含身份确认动作，并满足原 HTTP profile 校验。

执行等级由条目 `execution_level` 显式声明。可另用 `mechanism` 描述
进程内扩展、构包者、事件与回执等独立能力，例如：

```json
{
  "id": "attached-service-local",
  "label": "外部服务（声明 L1）",
  "kind": "http_json",
  "execution_level": "L1",
  "mechanism": {
    "in_process_code": true,
    "business_request_builder": "mixed"
  },
  "profile": "onebot-upstream.example.json"
}
```

机制字段仍受等级一致性校验；L0 不能声明内存写入、进程内代码或原始包事件。
L2/L3 表示实现方拥有会话与主传输，不能声明目标进程注入或外部客户端会话所有权。
`mechanism` 不能覆盖可用状态、来源类型或支持动作列表。
动作能力只来自实际配置的 action map。等级为声明时，
界面始终显示“配置声明 · 未验证”，不会因为账号验证成功就改成“实现确认”。

HTTP profile 只连接显式本地地址；不能指回本实例监听端口形成调用环。
切换外部服务只有在用户明确选择后才尝试登录；目录查看不会进行网络探测。

## 管理 API

| 接口 | 凭据 | 用途 |
| --- | --- | --- |
| `GET /_heredity/providers` | 消费者访问策略 | 等级、提供方和当前会话元数据 |
| `POST /_heredity/providers/switch` | 独立执行管理 Bearer token | 明确切换提供方 |
| `GET /api/heredity/execution` | WebUI 登录会话 | 面板数据与 `canSwitch` |
| `POST /api/heredity/execution/switch` | WebUI 登录会话；由服务端转发管理 token | 面板切换 |

消费者、事件提供方与执行管理 token 必须相互不同。
管理 token 不发送给浏览器，不接受查询参数鉴权。
WebUI 自身只有登录会话；执行管理凭据留在管理 API 服务进程中。

切换请求：

```json
{"provider_id":"demo-secondary","expected_epoch":"当前完整会话代次"}
```

成功响应为 `{state,changed}`。相同且就绪的提供方为无变化操作。
过期 `expected_epoch` 返回 HTTP 409；前端刷新状态但不重试 POST。
未实现或未配置的候选不能被选择，也不会关闭当前可用提供方。

## 会话代次和排空

每次进程启动创建新的不透明 `session_epoch`；脱离旧提供方时再次更新。
切换关闭新动作入口，等待已接收的动作完成，再关闭旧来源并验证新账号。
排空超时且尚未脱离旧来源时保留旧会话；脱离后失败则没有活动来源，
不会自动回退或升级。关闭失败会保留句柄，只能显式重试清理。

生命周期工作有截止时间，超时后迟到的候选不能再激活。
业务动作不自动重发；确认已经发生但结果/持久化异常时，继续保留确认与不可重试详情。

托管运行时的事件入口必须携带：

```text
Authorization: Bearer <事件提供方 token>
X-Heredity-Session-Epoch: <当前完整 session_epoch>
```

代次可从已授权的 `get_runtime_info` 或提供方管理元数据取得；
它是归属标识，不代替认证凭据。
缺失或过期代次返回 409，事件不会写库或广播。
旧库的原始、未托管 `AccountRuntime` 调用仍支持原事件契约；
CLI 创建的运行时已经由管理器托管，使用新契约。

切换结束旧的在线事件窗口和 SSE，再建立新窗口。
持久历史保留；当前没有跨代次自动在线补发。
事件和消息记录中的 `_heredity` 保存 `provider_id` 和 `session_epoch`。

## 消息身份边界

本地历史可跨提供方只读查询，但真实模式不能把已知其他来源的消息 ID
直接交给当前提供方撤回。跨提供方原生标识的别名转换尚未实现，
因此遇到 ID 冲突会保留原记录并明确失败，不按文本或短序号合并。
来源缺失或损坏的旧记录也不会被猜测用于写操作。
演示删除只修改演示数据库，不会执行外部撤回。

这些限制保护现有持久记录，不代表不同适配器的原生标识已经建立映射。
