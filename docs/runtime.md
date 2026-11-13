# 账号运行时与事件接入

Heredity 的通用账号运行时负责身份校验、消息规范化、持久化和事件分发。
平台私有包的解析由独立适配器实现；提供方必须先把事件转换成下述明确契约。
内存中的端点候选不会自动变成提供方。

## 启动与状态

```powershell
$env:HEREDITY_ACCESS_TOKEN = "consumer-example-token"
$env:HEREDITY_PROVIDER_TOKEN = "provider-example-token"
$env:HEREDITY_EVENT_SECRET = "event-signing-example-secret"
.\.venv\Scripts\python.exe run.py serve --demo --sse
```

上述值仅为示例。消费者令牌与提供方令牌必须不同。
未设置 `HEREDITY_PROVIDER_TOKEN` 时，事件写入入口关闭。
`--provider-token-env`、`--event-secret-env` 可指定其他环境变量名。

每个运行实例只绑定一个 `self_id`。不同账号应使用各自的实例、监听端口和令牌。
默认数据库位于 `.heredity/<demo|profile>/<self_id>.sqlite3`，
演示数据与真实接口配置使用不同目录。`--state-dir` 改变根目录；
`--memory-state` 使用临时内存数据库。

启动时通过 `get_login_info` 校验账号；所有非本地后端动作前重新检查身份。
身份不匹配会阻止动作，不会切换到其他账号或演示后端。
检查和动作是两个调用；真实提供方仍必须在服务端固定账号，才能避免外部账号切换竞态。

`get_status` 保留 `online`、`good`，并返回状态、身份确认和接收链路信息：

- `identity_verified` 表示登录资料与配置账号匹配。
- `online` 还要求后端处于在线状态。
- `receive_healthy` 初始为 `null`，收到有效提供方事件后才有接收证据。
- `good` 只有在在线、接收链路已确认健康且提供方没有明确报告故障时为 `true`。
- `provider_good` 保留提供方明确报告的健康状态，不会把 `false` 升级为健康。

空闲不会自动导致接收失败。适配器自行维护其平台会话和鉴权心跳。
上次 HTTP 请求成功不能单独证明上游消息送达或客户端渲染。
`get_runtime_info` 是 Heredity 扩展，提供运行模式和本地能力信息。

## 消息与本地历史

支持 CQ 字符串和消息段数组；`auto_escape=true` 将 CQ 外形保留为普通文本。
所有段完整校验通过后才会调用后端。`text` 和 `reply` 有通用字段校验；
扩展段及其平台字段保留给提供方处理，不意味着已实现对应的上游能力。
当前无法无损表示成 CQ 的嵌套参数会明确拒绝。

用户和群 ID 必须为正整数；消息 ID 为非零有符号 32 位整数，负数有效。
本地 ID 不被当作上游原始序号，不替代平台的真实发送回执。
演示后端使用持久、账号独立的计数器，重启后不重用旧 ID。

`get_msg` 优先读取当前账号的本地记录。被撤回的记录保留在数据库中，
但普通读取和历史列表不再返回它们。后端离线时，本地历史仍可查询：

```json
{
  "action": "get_local_history",
  "params": {
    "message_type": "private",
    "user_id": 10001,
    "limit": 20
  }
}
```

群历史使用 `message_type: "group"` 与 `group_id`。
可提供 `before_message_id`，按本地入库顺序向前分页；`limit` 范围为 1–1000。
这是 Heredity 的本地历史扩展，不声称等同于上游服务器历史。
真实模式的 `delete_msg` 必须先由提供方确认成功，才写本地撤回标记；
本地标记本身不能替代上游撤回。演示模式只修改演示状态。

发送已经完成而本地入库失败时返回 `retcode: 1501`，并携带：

```json
{"details":{"action_completed":true,"message_id":-123}}
```

提供方返回畸形发送结果时使用 `1502` 和
`{"action_outcome":"unknown","retry_safe":false}`。两种情况都不能自动重发。
运行时不会重试发送或在重启后重放写操作。

## 提供方事件入口

`POST /_heredity/events` 使用单独的提供方 Bearer 令牌，只接受 JSON。
普通 OneBot 消费者令牌和查询参数不能授权该入口。
CLI 创建的托管运行时还要求当前 `X-Heredity-Session-Epoch`，
用于拒绝旧来源的迟到事件；提供方生命周期和切换见 [`providers.md`](providers.md)。
以下是合成的演示输入：

```powershell
$runtimeHeaders = @{}
if ($env:HEREDITY_ACCESS_TOKEN) {
    $runtimeHeaders.Authorization = "Bearer $env:HEREDITY_ACCESS_TOKEN"
}
$runtimeInfo = Invoke-RestMethod http://127.0.0.1:5700/get_runtime_info -Headers $runtimeHeaders
$headers = @{
    Authorization = "Bearer $env:HEREDITY_PROVIDER_TOKEN"
    "X-Heredity-Session-Epoch" = $runtimeInfo.data.session_epoch
}
$eventBody = @{
    time = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    self_id = 10000
    post_type = "message"
    message_type = "private"
    message_id = -123
    user_id = 10001
    message = "hello"
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:5700/_heredity/events `
    -Method Post -Headers $headers -ContentType application/json -Body $eventBody
```

当前支持：

- 私聊与群聊消息：`post_type=message`，以及明确提供的 `message_sent` 扩展。
- 好友/群撤回：`notice_type=friend_recall|group_recall`。
- 好友/入群请求：`request_type=friend|group`，群请求含 `sub_type=add|invite`。
- 明确的生命周期和状态心跳。

未知类型、缺少必要字段、账号不符均明确失败，不会猜成成功。
群消息需要 `group_id`；撤回需要原消息 ID，群撤回另需 `operator_id`。
私聊自发事件若 `user_id=self_id`，必须额外提供 `target_id` 指明会话对端，
否则拒绝，避免把历史错误索引到自己。

默认按消息身份或规范化事件身份去重。提供方也可通过
`X-Heredity-Event-ID` 提供不超过 512 字符的稳定事件 ID。
事件、对应消息记录或撤回墓碑在同一 SQLite 事务提交后才广播。
撤回早于消息到达时，迟到消息只留存记录，不恢复普通可见性，也不再次广播为新消息。

返回数据含 `persisted`、`duplicate`、`sequence`，迟到消息还含 `suppressed`。
`sequence` 是本地事件序号，不是上游包序号或消息 ID。
被抑制的事件没有进入在线重放窗口，不能把它的序号作为 SSE 游标。

## HTTP 上报与 SSE

`--event-url` 启用正向 HTTP 上报，包括有效的提供方事件、生命周期和心跳。
`HEREDITY_EVENT_SECRET` 非空时，按实际发送字节计算 HMAC-SHA1，
添加 `X-Signature: sha1=<digest>`；接收端应校验原始请求体。
`X-Self-ID` 标明绑定账号。快速操作响应尚未实现。

`--sse` 启用 `GET /_heredity/events`，使用消费者认证。
这是 Heredity 扩展，不是 OneBot v11 的标准传输。
不带游标时只接收新事件；`?after=0` 请求当前完整保留窗口，
也可用 `Last-Event-ID` 从已接收的序号继续，后者优先于查询参数。
事件帧含 `id` 与 JSON `data`；没有每个订阅者的无界队列。

默认在线窗口保留 256 条。游标过期或不属于保留窗口返回 HTTP 409，
超前/无效游标返回 400；连接中落后太多会收到 `gap` 帧并断开。
重启后的在线窗口不包含此前持久事件。SQLite 历史保留，但当前不会自动补发。
HTTP 投递失败为尽力而为，记录失败或窗口缺口，不自动重复业务动作。
尚未实现可靠投递队列、持久投递游标或完整生产级连接资源配额。

数据库含消息正文、请求标识和事件记录，未加密；项目不会自动删除历史。
这些本地状态文件已加入 Git 忽略规则，不能把它们当成可公开的测试夹具。
