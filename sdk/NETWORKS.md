# Heredity 本体网络接口与模块 SDK

网络接口是 Heredity 本体管理的资源，独立于各个平台插件。
网络声明与分配从 Python SDK `1.1.0` 起提供；`1.2.0` 增加同请求多接口与热撤销，
`1.3.0` 增加已释放请求的重新申请与按保存选择自动恢复，插件握手 API 仍为 `1.0`。
插件负责平台登录与通信；宿主负责创建网络端点、鉴权、接口启停、模块绑定和消息路由。
一个模块不需要提供插件 Manifest、登录平台或实现插件心跳，就可以使用已配置的接口。

## 接口身份

每个接口由宿主生成一个唯一、随机的 **16 位小写十六进制 ID**，例如：

```text
8d7af96cb31240e5
```

ID 是资源标识，不是鉴权令牌。接口配置保存后，其 ID 不因修改显示名称、重启程序、
启停或变更端口而随机变化。删除接口后旧 ID 不会被分配给其他接口。
模块须获取使用该接口所需的宿主授权；单凭知道 ID 不能绕过网络端点的 access token。

宿主的模块服务满足 SDK 中的 `NetworkInterfaces` 协议：

```python
from nyxion_heredity_sdk.networks import NetworkInterfaces

def attach(interfaces: NetworkInterfaces):
    return interfaces.bind_module(
        module_id="example.bot",
        interface_id="8d7af96cb31240e5",
    )
```

接口配置管理不是模块 SDK 的职责。模块拿到的是一个受限定的 `ModuleBinding`，
不会拿到全局注册表、其他接口、平台会话密钥或宿主数据库。
同进程模块属于可信代码，这个 Python 对象边界不构成恶意代码权限沙箱。

## 模块能力声明与接口分配

正常模块通过宿主提供的 `NetworkInterfaceDeclarations` 服务声明需求。
`NetworkInterfaceAllocator` 是其中可等待分配、绑定授权和释放授权的结构协议；
前者另外提供快速返回的 `declare_interface()`。这些服务都不是平台插件接口。
支持 SDK 1.2 多接口的服务另外满足 `NetworkInterfaceMultiAllocator`，增加
`request_interfaces()` 与 `release_binding()`，原有单接口结构协议保持兼容。
SDK 1.3 的 `NetworkInterfaceLifecycle` 在此基础上增加 `reapply_interface()`。
宿主内部的具体实现位于 `heredity.network_allocator`，SDK 本身只提供标准库类型。

```python
from nyxion_heredity_sdk import (
    NetworkInterfaceMultiAllocator,
    NetworkInterfaceGrant,
    NetworkInterfaceRequirement,
)

def attach(networks: NetworkInterfaceMultiAllocator, module_id: str):
    requirement = NetworkInterfaceRequirement(
        module_id=module_id,
        transport_types=("http_server", "websocket_server"),
        directions=("call", "receive"),
        account="example:12345678",
        purpose="Account message processing",
        max_instances=1,
        max_interfaces=16,
    )
    declaration = networks.declare_interface(requirement)
    if declaration["state"] != "granted":
        return declaration
    grants = (NetworkInterfaceGrant(**item) for item in declaration["grants"])
    return tuple(networks.bind_grant(module_id, grant) for grant in grants)
```

`example:12345678` 是格式示例；实际使用时应填写插件声明的真实平台与账号，
不能把已有账号的前缀改成示例前缀。

`declare_interface()` 立即返回声明状态，不等待网络连接，也不自行选择已有接口。相同模块的相同
pending、granted 或 denied 需求重复声明会返回同一请求，避免启动循环反复堆积请求。
宿主在“网络接口”页面显示待分配需求，由用户选择一个或多个已经配置的接口或拒绝请求。
请求状态为 pending、granted、denied、released、revoked 或 expired；
其中 revoked、expired、released 后，模块可以明确重新声明，生成新的 pending 请求。
快照的 `grants` 只包含仍有效的独立授权，兼容字段 `grant` 为第一项，空集合时为 `None`。
一个接口只在同一请求中出现一次，每个授权都有自己的 `grant_id`、代次和过期时间。

`NetworkInterfaceRequirement` 的字段：

| 字段 | 含义 |
| --- | --- |
| `module_id` | 稳定、最长 128 字符的小写模块标识 |
| `transport_types` | 可接受的接口类型，非空且不重复 |
| `directions` | 可使用的 `call`、`publish`、`receive` 操作 |
| `account` | 可选的 `platform:self_id` 精确账号要求 |
| `purpose` | 最长 256 字符的可见单行需求说明 |
| `max_instances` | 每个接口授权允许的同时绑定句柄数，范围 1..64 |
| `max_interfaces` | 单个请求允许的接口数量，范围 1..64，默认 16 |

标准接口类型为 `http_server`、`http_client`、`websocket_server` 和
`websocket_client`，公开常量为 `NETWORK_TRANSPORT_TYPES`。
`NETWORK_DIRECTIONS` 列出上述三个操作方向。没有声明的操作会在进入接口前
抛出 `NetworkAllocationDenied`，而不是只在分配页面做提示。

宿主检查所选接口的传输类型与账号要求。不同账号的接口不能借用；用户明确分配
一个尚未绑定账号的接口时，宿主可以将该接口绑定到所需账号，且必须核对其配置
仍与用户选择时一致。停止状态的接口也可分配，分配本身不隐式启动它。
模块不能选择端口、修改接口配置、自行生成接口 ID、获取 access token，
也不能要求宿主在没有配置的情况下自动创建端点。

`request_interfaces(requirement, timeout=30)` 适用于允许等待的工作线程，
在同一声明至少有一个有效授权时返回 `tuple[NetworkInterfaceGrant, ...]`。
它返回当前授权快照，不自动为所有接口执行业务动作；模块必须按具体 ID 明确调用一个句柄。
原有 `request_interface()` 只返回第一项。等待时间必须处于 `(0, 120]` 秒。
超时抛出 `DeadlineExceeded`，已存在的 pending 请求仍保留供用户处理。
宿主拒绝分配、超过分配或句柄限额时使用 `NetworkAllocationDenied`。
平台插件的 `start()`、`ping()` 等控制入口应使用快速声明，不能等待用户分配。

Grant 记录请求 ID、模块 ID、接口 ID、接口代次、传输类型、账号、
可选过期时间与本次发放的 `grant_id`。`grant_id` 每次重新发放都不同，防止
拒绝后再分配相同接口时，旧授权重新生效。Grant 不含 token，也不是网络鉴权凭据。
`bind_grant(module_id, grant)` 必须核验宿主仍持有完全相同的授权及对应配置版本，
不得接受模块自行修改后的副本。宿主给模块的限定服务也只接受当前模块的请求，
不能声明、绑定或释放其他模块的资源。

```python
grants = networks.request_interfaces(requirement, timeout=5)
grant = next(item for item in grants if item.interface_id == configured_interface_id)
binding = networks.bind_grant(requirement.module_id, grant)
try:
    message = binding.receive(timeout=1)
finally:
    binding.close()
    networks.release_binding(grant.request_id, grant.interface_id)
```

`release_binding(request_id, interface_id)` 只撤销该请求中指定接口的授权并关闭其派生句柄；
同一请求的其他接口以及其他模块的句柄继续有效。撤销最后一个授权后，请求恢复 pending，
模块和管理员可以在同一请求上重新分配。重复撤销一个未分配接口无副作用。
`release_interface()` 释放整个请求，状态变为 released，重复释放无副作用。
released 请求不能通过管理员重新分配、拒绝或撤销操作恢复；模块须明确声明新请求。
这些操作不停止或删除共享接口，也不影响平台账号。接口首次参与模块分配后由宿主按有效授权
控制平台账号路由。撤销最后一个授权会停止该接口接收的账号动作与发出的账号事件，
监听端口仍可保持运行；重新明确分配后才恢复路由。同接口还有其他请求的有效授权时，
它们继续工作。从未参与模块分配的手工接口保留独立路由。重新授权不会重放撤销前或
未授权期间的事件，也不会重新执行之前的业务动作。普通接口启停保留授权与稳定 ID，
停止期间业务调用仍会明确失败。修改配置、重试重建、轮换 token、删除接口、
授权到期使对应接口的旧授权失效，其余接口保持有效。仍有授权时请求保持 granted 并提供错误说明；
全部失效后为 revoked 或 expired。宿主关闭则撤销全部授权。
旧句柄抛出 `NetworkBindingClosed`，不能自动迁移到新的配置或账号。
已捕获执行目标的在途动作仍按原目标完成，不会自动切换接口或重发。

管理员可在模块运行时通过宿主 `assign_many(request_id, interface_ids)` 提交新的目标集合。
宿主先完整校验所有选择，再原子替换授权集合；未改变的接口保留原有授权和订阅，
移除的接口只关闭该请求下的句柄，新增接口独立发放授权，不要求停止模块或账号。
原有 `assign(request_id, interface_id)` 等于提交只含一个接口的新集合。
`unassign(request_id, interface_id)` 撤销指定接口，省略接口 ID 撤销全部并回到 pending。
denied 请求可由管理员明确重新分配；released 请求不可重新分配。

多接口账号绑定会先完成全量静态校验，再逐个使用配置代次检查认领尚未绑定的账号。
授权集合只有在全部认领和最终检查通过后才替换。如果并发修改或持久化故障发生于认领途中，
已经保存的部分账号绑定可能保留；宿主会明确报告需检查配置，不擅自回滚并覆盖用户的新修改。
这时原有有效授权集合不被此次分配替换。

分配记录和授权只在当前宿主进程内有效，重启后模块重新声明。宿主每次停止模块时
释放该模块的请求并撤销此前发放的限定服务，旧工作线程不能重新请求下一次运行的授权。
平台插件可以通过可选的 `PluginContext.networks` 使用同一限定服务；
这不把网络配置变成 Manifest 字段，也不把接口生命周期交给平台插件。
平台登录和网络分配状态分别报告，未分配接口不等于平台登录失败。

## 自动恢复与重新申请

模块应在每次启动时使用当次 `PluginContext.networks` 快速声明接口需求。
宿主为稳定的 `module_id` 与 `account` 保存用户明确选择的接口 ID，自动连接默认开启。
第一次声明没有保存选择时仍为 pending；宿主不会猜选现有接口、自动创建接口或切换账号。
修改需求的可见 `purpose` 不会丢掉选择；传输、方向和容量变化仍会按当前需求重新验证。
不同账号的选择分别保存，不能用旧账号接口恢复另一个账号。

用户在界面分配接口会保存 ID 列表，分配动作本身不隐式启动接口。下次模块启动、
明确重新申请或开启自动连接时，宿主在有界后台队列中重新验证这些 ID、发放新授权并启动接口。
`declare_interface()`、插件心跳和模块停止不等待网络启动。
停止或替换模块会取消旧任务、撤销其新发放的授权；已开始的底层网络连接可能完成，
但结果不能重新取得被撤销的账号路由，也不会继续启动后续接口。
默认在启动各接口前检查同一任务的 15 秒预算；已经开始的连接仍遵循传输自身的超时，
不能由 Python 线程强制中断。启动失败、接口丢失、账号或传输不兼容、超过容量时，
请求回到 pending 并给出有界错误，保留已知选择供用户检查，不自动尝试其他接口。
这不是消息重试机制，恢复过程不会发送或重放业务动作。

默认队列最多 1024 个任务，每个请求同时只调度一个恢复任务；重复启动声明不会新增申请。
关闭自动连接会取消后续自动启动，保留当前已经有效的授权与保存的选择。
管理员 `unassign()` 或模块 `release_binding()` 删除对应的保存选择；
撤销全部以及拒绝请求会清空该账号的保存列表，避免重启后恢复用户刚取消的接口。
`release_interface()` 只结束本次请求，`release_module()` 和宿主关闭也保留重启意图；
需要取消未来连接时应撤销选择或关闭自动连接。

宿主将策略保存在其状态目录的 `network-module-policies.json`。文件只包含模块 ID、账号、
自动连接开关和接口 ID，不包含 access token、Grant、登录材料或消息正文。
请求 ID、grant_id、代次、订阅句柄和当前 scope 始终只属于本次运行，不能从文件恢复。

```python
from nyxion_heredity_sdk import NetworkInterfaceLifecycle

def request_again(networks: NetworkInterfaceLifecycle, old_request_id: str):
    return networks.reapply_interface(old_request_id)
```

模块仍运行时，重新申请已 released、denied、revoked 或 expired 的请求会创建新的请求 ID。
已经有同模块与账号的当前 pending/granted 请求时，返回当前请求，避免重复堆积。
当前 pending 的重新申请可明确再次调度失败的自动恢复；不会在后台无限自动重试。
已停止的 scope 不能重新申请；宿主侧返回 `MODULE_INACTIVE`，SDK 的旧 scope 抛
`NetworkBindingClosed`。旧 released 请求仍不能被 `assign()` 直接复活，旧 grant 永远无效。

请求快照新增 `auto_start`、`saved_interface_ids`、`module_active`、`can_reapply`、
`superseded_by` 和 `restoring`。同一模块与账号的新请求取代旧请求时，
旧请求保存其 `superseded_by` 关联。默认目录只显示当前请求；
`catalog(include_history=True)` 才包含终结历史。历史记录不会增加可用权限。
宿主管理接口为：

```text
GET  /api/heredity/network-requests?include_history=true
POST /api/heredity/network-requests/{request_id}/auto-start
     {"enabled": true}
POST /api/heredity/network-requests/{request_id}/reapply
     {}
```

管理请求仍需宿主 UI 鉴权。自动连接开关和接口选择属于宿主服务，
模块不能通过 SDK 修改持久策略或获取端点凭据。

## 使用已绑定接口

```python
from nyxion_heredity_sdk import thaw_json
from nyxion_heredity_sdk.networks import NetworkBindingClosed, NetworkMessageGap

binding = interfaces.bind_module("example.bot", "8d7af96cb31240e5")
try:
    result = binding.call("get_status", {}, timeout=5)
    ordinary_result = thaw_json(result)

    message = binding.receive(timeout=1)
    if message is not None:
        print(message.interface_id, message.kind, message.id)
        # payload 为不可变、已验证的 JSON 值。
        # 仅在外部集成确有需要时转回可变 dict/list。
        ordinary_payload = thaw_json(message.payload)
except NetworkMessageGap:
    # 此订阅已超出缓存，不能假装没有漏消息。显式关闭并重新绑定。
    binding.close()
    raise
except NetworkBindingClosed:
    # 端点已删除、运行代次已撤销，或宿主关闭。
    raise
finally:
    binding.close()
```

`call()` 只执行一次，不广播给同接口的观察者，不因失败切换到另一个接口，
也不重试发送动作。调用目标取决于接口类型：可能是宿主绑定的平台账号，也可能是
接口明确配置的远程 OneBot 地址。返回值保留宿主回调的 JSON 结构并冻结；
它不将未知发送结果转换为成功。

`timeout` 是有限的 `(0, 120]` 秒，默认 30 秒。宿主收到剩余预算并负责将其应用到
具体传输。嵌入模式下这是合作式时限；它不能强制中断不合作的 Python 函数。
已开始的动作不因订阅关闭而自动取消或重发，实际发送结果仍需正常的完成证据判断。

## 发布接口事件

```python
binding.publish_event({
    "time": 1700000000,
    "self_id": 12345678,
    "post_type": "meta_event",
    "meta_event_type": "heartbeat",
    "status": {"online": True, "good": True},
    "interval": 15000,
})
```

宿主把这一个事件交给该接口的出站传输。具体支持范围由接口决定；停用接口或不能发布
事件的接口应明确失败，不能悄悄借用另一个端点。模块发布事件不代表真实平台接收了消息，
不生成平台发送回执，也不直接进入平台插件。

## 接收接口消息

`receive(timeout=0)` 是非阻塞读取，队列为空时返回 `None`。正数等待时间最多 120 秒。
同一个接口可以绑定多个观察模块，同一个模块也可建立多个独立订阅；
各自拥有读取位置，彼此不会抢走消息。

每条 `NetworkMessage` 包含：

| 字段 | 含义 |
| --- | --- |
| `id` | 该次宿主观察消息的唯一 ID |
| `interface_id` | 来源接口的稳定 16 位 ID |
| `kind` | `action`、`event`、`response` 或 `state` |
| `payload` | 不可变且有大小、深度和类型约束的 JSON 对象 |
| `timestamp` | Unix 时间，单位为秒 |

载荷约定：

- `action`：`{"action": "...", "params": {...}, "echo": ...}`，echo 可缺省。
- `event`：对应 OneBot 事件对象，保留其 `self_id`、`post_type` 等字段。
- `response`：OneBot 返回对象，不改变成功码、消息身份或错误语义。
- `state`：接口状态对象；它不是平台账号在线证明。

观察消息和执行业务动作是两回事。订阅一个动作消息不会再执行该动作；宿主仍只选择
一个执行目标。观察模块不能因为看到同一动作又发布一次来假装完成确认。
`NetworkMessage` 的 ID 也不等于平台消息 ID，不能用来替代真实发送回执。

## 容量与关闭

默认每订阅最多保留 256 条消息，宿主可在 1..4096 范围内配置。
本体总订阅数最多 1024。宿主在发出消息时只冻结一次载荷，多个观察者共享不可变对象，
没有为每个观察模块强制做 JSON 序列化或复制完整消息正文。

订阅溢出，或观察消息无法在 SDK 的大小、深度等限制内保留时，
宿主会撤下该订阅的缓存并记录缺口。观察失败不会改写已执行动作的响应或中断事件投递。
后续读取、调用或发布会抛
`NetworkMessageGap`，其中包含接口 ID 和丢失条数，不会静默略过旧消息。
模块需显式处理缺口并重新绑定；SDK 当前不承诺跨进程重启的订阅重放。
平台事件持久化与外部消费者交付确认属于对应宿主／传输机制，不能用此内存队列代替。

接口普通启停可以发布 `state`，绑定的稳定 ID 不变；停用时调用失败，但模块可继续观察状态。
删除、修改配置或显式重试重建时宿主会撤销绑定代次，旧句柄不能悄悄转到新实例。
`ModuleBinding.generation` 标识当前宿主内的运行代次，不是磁盘配置版本或身份凭据。
`close()` 幂等，关闭一个订阅不停止接口，也不关闭其他模块或平台账号。

## 宿主实现约定

内置 `NetworkModuleBus` 属于本体，其构造只接受显式宿主回调：

```python
from heredity.network_bus import NetworkModuleBus

bus = NetworkModuleBus(
    exists=lambda interface_id: interface_id in configured_interfaces,
    dispatch=dispatch_one_interface,
    publish=publish_one_interface,
)

binding = bus.bind("example.bot", "8d7af96cb31240e5")
bus.emit("8d7af96cb31240e5", "state", {"state": "running"})
```

回调签名分别为：

```text
exists(interface_id) -> bool
dispatch(interface_id, action, immutable_params, timeout_seconds) -> JSON
publish(interface_id, immutable_event) -> JSON | None
```

外部 I/O 不在总线全局订阅锁内执行。接口管理器应在操作开始时捕获唯一目标，
避免接口配置变化把在途动作转移到另一个端点或账号。
`bus.bindings(interface_id)` 返回模块 ID 与订阅数量，不返回句柄、载荷或鉴权值。
`bus.invalidate(interface_id)` 撤销旧运行代次并唤醒等待者；
`bus.close()` 关闭全部观察订阅，但不负责关闭接口管理器本身的网络资源。

本 SDK 只有进程内对象服务，本身不实现 HTTP/WebSocket 传输，也没有远程模块 RPC
或插件网络配置协议。实际传输能力以宿主接口目录为准。
