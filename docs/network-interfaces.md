# Heredity 本体网络接口

网络接口由 Heredity 本体创建和维护，不是平台插件功能。平台插件提供账号与
平台通信能力；接口提供对机器人应用及内部模块的网络接入和独立路由。

## 创建和配置

在侧边栏“网络接口”（`/connections`）点击“新建接口”，选择：

| 类型 | 行为 |
| --- | --- |
| HTTP 服务端 | 监听指定地址、端口和路径，接收 OneBot 动作，可提供 SSE 事件流 |
| HTTP 客户端 | 向指定 HTTP(S) 地址投递事件，也可通过模块接口调用远端 OneBot |
| 正向 WebSocket | 监听 WS 地址，处理动作和分发事件 |
| 反向 WebSocket | 连接指定 WS(S) 地址，处理对端动作、事件及关联响应 |

新宿主的接口目录为空，不默认监听 OneBot HTTP。先由用户创建接口，再分配给模块。
接口支持独立名称、账号绑定、鉴权、心跳及相应传输配置。创建默认停止，明确启动后
才绑定端口或建立远端连接。监听端口 `0` 表示由系统分配，实际端点会显示在列表。
运行中也可直接保存配置，宿主安全重建该接口传输并恢复原启用状态，接口 ID 不变。
“重试”重建传输并刷新当前适配器状态，不重发任何业务动作。
网络接口启停不会重启平台插件或其平台会话。

同一接口只选择一个平台账号，不会广播发送动作。账号留空时仅在有唯一在线账号时
自动选中；多个在线账号未明确绑定时，业务请求返回账号未选定。客户端接口也可独立
连接远端 OneBot，不要求在本机加载平台插件。

旧版自动生成的默认 HTTP 接口在升级时移除，用户创建的其他接口保留。
全部接口使用相同生命周期规则；停止后可以删除，没有不可删除的主接口。
管理 UI 通过本体内部服务读取账号状态，不依赖某个网络接口。

## 唯一 ID 与持久化

ID 由宿主生成，格式为 **16 位小写十六进制**，例如 `8d7af96cb31240e5`。
模块不自行分配 ID，也不通过改名获得另一个 ID。创建时检查碰撞；
删除后的 ID 保留墓碑，不再分配给其他接口。

配置位于 `<宿主状态目录>/network-interfaces.json`，包括接口名称、类型、
绑定账号、设置、稳定 ID 和启用意图。重启恢复这些资源及启用的网络连接，
不重发业务请求。平台登录是否自动恢复由对应插件的自动登录开关决定。
鉴权与签名配置属于敏感本地设置：文件以私有创建权限写入，
Windows 仍受宿主账号目录权限约束。接口在创建时随机生成令牌，不接受自定义或清空。
HTTP 事件签名使用同一个接口令牌，无须另外配置签名密钥。
常规 GET 和接口目录只返回是否配置；已登录的管理用户可通过凭据路由查看、复制令牌，
或明确重新生成。重新生成会撤销旧令牌与旧模块授权，不修改接口 ID。

接口 ID 是资源标识，不是 access token。网络消费者仍按接口配置鉴权；
同进程模块属于宿主信任的代码，不通过 ID 获得操作系统权限沙箱。

## 模块接入

模块依赖 SDK 的网络声明服务，不导入本体或平台实现。宿主注入限定的服务对象：

```python
from nyxion_heredity_sdk import NetworkInterfaceGrant, NetworkInterfaceRequirement

requirement = NetworkInterfaceRequirement(
    module_id=networks.module_id,
    transport_types=("http_server", "websocket_server"),
    account="example:12345678",
    directions=("call", "receive"),
    purpose="个人账号接入",
)
request = networks.declare_interface(requirement)
# 本体页面选择已创建的接口后，再次查询声明即可取得授权。
if request["state"] == "granted":
    grants = [NetworkInterfaceGrant(**value) for value in request["grants"]]
    bindings = [networks.bind_grant(networks.module_id, grant) for grant in grants]
```

`example:` 仅表示示例平台命名空间；实际配置须使用插件声明的真实平台前缀，
不能将已有账号或接口的前缀改成示例值。

同一请求可分配多个接口，每个接口拥有独立授权和句柄。页面可以热修改整组选项、
撤销其中一个或撤销全部，再对同一请求重新分配。保存时保留未变接口的授权身份和订阅，
只撤销移除项；撤销全部使请求回到待分配状态。已释放的模块请求不能复活，
需要模块重新声明；已拒绝的请求可以由管理用户明确重新分配。
SDK 的 `request_interfaces()` 返回全部授权；旧 `request_interface()` 保留首项兼容行为。
模块选择一个句柄调用动作，多分配不会自动广播或重复执行动作。

## 模块重启与重新申请

模块首次声明时默认开启“自动启动”，尚未选择接口时只显示待分配。
用户保存分配后，本体记住该模块与账号的接口选择；模块再次启动、重启或明确重新申请时，
会异步核验并尝试恢复这些接口，必要时启动对应监听或客户端连接。
关闭自动启动后仍保留选择，但下一次启动只重新声明，不自动分配和启动。
恢复不会选择替代接口、跨账号借用接口或创建端口；已删除、类型变化、账号不符等情况
会明确显示为待处理，不能伪造接入成功。

恢复在本体后台执行，插件的启动和握手不等待网络连接。模块停止、取消自动启动或重新
修改选择后，旧恢复任务不能发布新的有效授权；业务动作和消息不因恢复失败而重发。
用户单项或全部撤销、拒绝分配时，会同步取消对应记忆选择，避免重启后自行恢复。
模块停止及宿主正常退出只释放运行授权，保留用户的自动启动策略和保存的选择。
策略位于 `<宿主状态目录>/network-module-policies.json`；网络令牌仍由接口配置单独管理。

旧释放记录可在模块仍活跃时点击“重新申请”。本体会返回已有的当前请求或生成新请求，
旧请求和旧授权不会复活。模块已停止时需要先启动模块，再重新申请。
默认目录只展示每个模块与账号的当前请求，已被后续请求替代的记录收进历史，
避免同一模块的“已释放”和“待分配”同时占据主列表。

模块可以通过 `publish_event()` 将 OneBot 事件发布到指定接口的出站传输。
它不伪造平台发送回执，也不自动写入平台插件的账号事件入口。
指定账号的接口拒绝其他账号的事件。

`receive()` 观察四种消息：动作 `action`、响应 `response`、事件 `event` 和状态 `state`。
同接口多个模块各有读取位置，不会互相抢消息；不同接口的消息不混用。
仅观察动作不会再次执行业务。停用时模块仍可观察停止状态，动作和发布失败；
删除接口、修改配置、重试或轮换令牌只撤销该接口的旧授权与绑定，
同一请求分配的其他接口继续工作。可在原请求上重新分配。
普通启停保留授权；停止时动作和发布失败。释放模块不停止共享接口。
平台插件通过 `PluginContext.networks` 使用相同声明服务，网络配置不进入平台登录 schema。

参与过分配的接口受授权路由控制：最后一个授权撤销后，监听仍运行，
但不再向账号转发动作或事件，也不回退到唯一在线账号。重新分配后热恢复，
不补发无授权期间积压的事件。该受控状态会持久化；重启后等待模块的新授权。
从未参与分配的独立接口仍按用户配置的账号工作。

SDK 提供不可变消息对象和有界内存队列。溢出抛出 `NetworkMessageGap`，
不能把漏消息当作正常读取。此订阅当前为进程内功能，不声明远程模块注册服务。
完整字段、代次、容量和超时语义见 [`../sdk/NETWORKS.md`](../sdk/NETWORKS.md)。

## 管理 API

以下路由需要管理 UI 登录，所有写操作校验跨站来源：

```text
GET    /api/heredity/network-interfaces
POST   /api/heredity/network-interfaces
GET    /api/heredity/network-interfaces/{interface_id}
PUT    /api/heredity/network-interfaces/{interface_id}
POST   /api/heredity/network-interfaces/{interface_id}/start
POST   /api/heredity/network-interfaces/{interface_id}/stop
POST   /api/heredity/network-interfaces/{interface_id}/retry
GET    /api/heredity/network-interfaces/{interface_id}/credentials
POST   /api/heredity/network-interfaces/{interface_id}/regenerate-token
DELETE /api/heredity/network-interfaces/{interface_id}
GET    /api/heredity/network-requests
POST   /api/heredity/network-requests/{request_id}/assign
POST   /api/heredity/network-requests/{request_id}/unassign
POST   /api/heredity/network-requests/{request_id}/deny
POST   /api/heredity/network-requests/{request_id}/auto-start
POST   /api/heredity/network-requests/{request_id}/reapply
```

`GET /api/heredity/network-requests?include_history=true` 包含历史记录；
`auto-start` 接受 `{"enabled": true}` 或 `{"enabled": false}`，
`reapply` 接受空对象并返回当前有效请求。

创建示例（端口、账号使用实际配置）：

```json
{
  "name": "工作机器人",
  "type": "http_server",
  "enabled": false,
  "account": "example:12345678",
  "settings": {
    "host": "127.0.0.1",
    "port": 5702,
    "path": "/onebot",
    "heartbeat_seconds": 15,
    "sse_enabled": true
  }
}
```

返回快照包含生成的 `interface_id`，以后用此 ID 修改、启停或绑定模块。
模块分配请求体为 `{"interface_ids": ["8d7af96cb31240e5", "9d7af96cb31240e5"]}`，
表示替换当前选择集；空数组撤销全部。单接口旧格式 `{"interface_id": "8d7af96cb31240e5"}`
仍可使用，表示替换为这一项。
`unassign` 使用 `{"interface_id": "8d7af96cb31240e5"}` 撤销指定项，
空对象撤销全部；`deny` 使用空对象。
整组选项先验证兼容性，失败不替换原授权集合；多接口账号绑定如遇并发变更或磁盘失败，
已成功的账号配置可能保留，API 会返回明确错误，需检查后再次分配。
`access_token` 和 `event_secret` 不允许在创建、更新中提交。
WebSocket 动作通过 echo 关联，
断线重连只恢复连接，不重放原动作。HTTP 不自动重试或跟随重定向。

本体宿主不再提供旧单监听热配置。独立的历史 CLI 传输不属于本体接口资源 API。
