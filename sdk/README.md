# NyXion Heredity SDK 1.5.0

本目录是插件边界的规范与示例。公开 Python 包为
`nyxion_heredity_sdk`，只依赖 Python 标准库，不导入宿主 `heredity`、
平台协议、原生签名库或 WebUI。具有独立 wheel 与自包含源码发行包的构建流程，
尚未向包仓库发布。

SDK 握手 API 保持 `1.0`，Python 包版本为 `1.5.0`。本次发行将消息扩展段的
平台语义校验交回适配器，宿主和平台插件要求 SDK `>=1.5,<2`。通用消息边界继续
校验有界 JSON、消息段结构、文本和回复 ID，透明保留其他消息段的数据。
现有 Aegis WebAssembly 包声明、严格 Schema、能力调用 ABI，以及运行中模块
重新申请接口与按保存选择自动恢复的服务继续兼容。
可信 Python 平台插件仍使用同进程 `embedded` 模式；Aegis 市场模块使用独立
Wasmtime Worker，经有界 IPC 调用限定能力。远程插件和其他语言 SDK 尚未实现。
两种运行模式使用不同 Manifest；Aegis 不接受下载的 Python 或原生二进制执行入口。
见 [`AEGIS.md`](AEGIS.md)。

## 1. 职责与依赖方向

宿主提供 UI、OneBot 协议端点、动作路由、插件状态、账号会话与持久事件入口。
平台插件维护自身登录、平台会话、平台网络协议及平台版本兼容。

插件只依赖本 SDK 的公开接口，不能导入宿主运行时、宿主数据库或内部锁。
宿主通过 `PluginContext` 提供限定到当前插件实例的回调与私有状态目录。
平台插件可以使用自己的工作线程、原生 Worker 和内部缓存；这些均是插件内部细节。

本体网络接口另有独立的模块 SDK：模块按随机 16 位接口 ID 绑定、调用、发布和接收，
声明所需传输、操作方向和账号，由宿主分配一个或多个已配置接口并发放独立授权。
它不需要插件 Manifest，也不参与平台登录握手；网络接口能力属于宿主服务，
不能放进插件的接口配置或 Manifest。见 [`NETWORKS.md`](NETWORKS.md)。
平台插件可通过可选的 `PluginContext.networks` 使用相同的限定服务，
不会获得端口配置、token 或其他模块的授权。
模块每次启动都可快速声明需求；宿主按模块与账号保存自动连接开关和明确选择的接口 ID。
关闭或重启模块会撤销当次请求与旧句柄，后续运行按保存选择发放新的授权。

同进程插件并非安全沙箱。类型与边界校验能够发现协议错误，不能限制恶意 Python
代码访问文件、网络或宿主进程，也不能隔离原生代码崩溃。
使用原生组件的平台插件属于这类可信嵌入代码，其状态不会标记为 Aegis 隔离。
Aegis 的包类型 `SandboxModuleManifest` 可独立导入，不依赖 Wasmtime；
实际虚拟机、独立进程、权限 Broker 与越权终止由宿主实现。

## 2. Manifest

`PluginManifest.from_dict()` 接受以下完整、严格的必需字段集合，另允许可选的
`ui_pages`；缺失、重复能力、未知字段、非法标识、未知执行模式和反向兼容区间会被拒绝。

```json
{
  "schema_version": 1,
  "plugin_id": "example.offline",
  "name": "Offline SDK example",
  "version": "1.0.0",
  "platform": "example",
  "execution": "embedded",
  "api_major": 1,
  "api_minor": 0,
  "host_api_min_minor": 0,
  "host_api_max_minor": 0,
  "actions": ["get_login_info", "echo"],
  "capabilities": ["synthetic"],
  "config_schema": {
    "type": "object",
    "properties": {
      "self_id": {"type": "string", "minLength": 1, "maxLength": 32}
    },
    "required": ["self_id"],
    "additionalProperties": false
  }
}
```

`plugin_id` 是稳定的发行身份，`platform` 是平台命名空间；它们不代表进程 ID。
插件实例 ID 由宿主在每次装载时新建。平台账号 ID 在 SDK 中采用字符串；
SDK 本身不把账号限制为数值；当前宿主将在线会话直接投影为 OneBot v11 账号，
因此该宿主的在线 `self_id` 必须是规范的正整数字符串，且不超过有符号 64 位范围。
平台原生身份不是数值时，适配器必须自行维护稳定且明确的 OneBot 身份映射；
宿主不会猜造平台账号，也不替适配器推导映射。

同一 major 才能协商。插件所需 `api_minor` 不得大于宿主 minor，且宿主 minor
必须落在插件明确声明的闭区间内。插件版本与 SDK API 版本分别演进。
平台插件加载器只执行 `embedded`；其他值必须报错，不能静默降级。
Aegis 市场模块走独立包加载器，不复用或扩展此平台 Manifest 的 `execution` 字段。

### 宿主侧边栏页面

插件可在元数据中声明最多 8 个宿主渲染的子页面：

```json
{
  "ui_pages": [
    {"id": "accounts", "title": "平台账号", "view": "accounts", "icon": "users"}
  ]
}
```

Python 类型为 `PluginUIPage`（兼容别名 `UIPage`）：

```python
from nyxion_heredity_sdk import PluginUIPage

page = PluginUIPage("accounts", "平台账号", view="accounts", icon="users")
```

`id` 是 1..64 字符的小写单路由片段，只允许英文字母、数字及片段间连字符，
同一插件下不可重复。`title` 是 1..80 字符的可见单行标题。当前只实现
`view="accounts"` 与 `icon="users"`，表示宿主提供的账号实例管理界面。
插件不提供任意 URL、路径、HTML、脚本或可执行 UI 资源。宿主派生本地路径：
`/plugins/{plugin_id}/{page.id}`，验证元数据后将入口注册到侧边栏。
页面声明不得触发插件装载或平台登录。

`ui_pages` 缺省为空，空集合的 `to_dict()` 不新增字段，因此原有 13 字段的
Manifest 与离线示例保持兼容。这是 SDK 1.0 的可选元数据扩展，不改变
握手和动作的语义；未知页面视图仍须拒绝。这表示新版读取器接受旧声明，
不表示旧版严格读取器能够接受新增字段。使用页面声明时，宿主和 SDK
必须成套更新到包含此扩展的版本。

### 同插件多账号实例

`plugin_id` 标识插件发行，不能用不同账号修改它。一个发行可由宿主创建多个
有稳定 `instance_key` 的配置实例，每次启动仍使用新的运行时 `instance_id`。
插件工厂必须返回独立对象；不得用模块级单例共享账号、密钥、连接或登录 Challenge。
首版一个运行实例绑定一个账号，多个账号使用多个独立实例，不共享 `session_epoch`。

配置实例的增删、显示名称、持久化目录和 OneBot 当前账号选择由宿主管理。
每个实例通过自己的 `PluginContext` 接收专属状态目录与回调；插件只在该目录下
存储账号状态。宿主必须按实例限定回调和 Challenge 路由，停止一个账号不能撤销
同插件的其他账号。账号动作所有权仍按平台与账号唯一绑定，创建多个实例不允许
绕过相同账号的所有权检查。多账号 OneBot 路由必须显式选择账号，不能广播发送。

宿主管理 API 与 SDK 平台接口分开。首版账号页通过已鉴权的管理通道使用：

```text
GET    /api/heredity/plugins/{plugin_id}/instances
POST   /api/heredity/plugins/{plugin_id}/instances
       {"instance_key": "work-account", "display_name": "工作账号"}
POST   /api/heredity/plugins/{plugin_id}/instances/{instance_key}/start
       {"config": {"self_id": "10001"}}
POST   /api/heredity/plugins/{plugin_id}/instances/{instance_key}/stop
       {}
GET    /api/heredity/plugins/{plugin_id}/instances/{instance_key}/challenge
DELETE /api/heredity/plugins/{plugin_id}/instances/{instance_key}
```

创建配置实例只保存逻辑 key 和显示名称，不导入插件、不登录，也不保存启动配置中的
凭据。删除要求实例已确认停止，账号历史与插件私有恢复数据不随配置条目删除。
账号页不能将某一实例的二维码用于另一个实例，也不能在切换实例时复用旧会话 epoch。
这些管理路由不是插件自行提供的 Web 服务，不改变插件只依赖公开 SDK 的边界。

配置使用 `validate_config(manifest, config)`：

- 支持单一 `type`：object、array、string、integer、number、boolean、null。
- 支持 properties、required、布尔 additionalProperties、enum、minimum、
  maximum、minLength、maxLength、items、minItems、maxItems。
- 接受 title、description、default、examples、writeOnly、readOnly、
  deprecated 和 `$schema` 注解；default 不会自动写入配置。
- 未实现的关键字（如 `$ref`、pattern、anyOf）明确拒绝，不能忽略验证约束。
- `writeOnly` 是 UI/日志处理标记，不是加密或权限保证。宿主仍须避免输出配置值。

## 3. 公开接口

```python
class Plugin(Protocol):
    manifest: PluginManifest

    def handshake(self, hello: HostHello) -> PluginHello: ...
    def start(self, context: PluginContext, config: Mapping) -> None: ...
    def ping(self, heartbeat: Heartbeat) -> Pong: ...
    def call(self, request: ActionRequest) -> ActionResult: ...
    def stop(self, reason: str) -> None: ...
```

加载顺序：

1. 校验 Manifest、API 范围及配置。
2. `handshake()` 绑定本次实例与随机 nonce；核验 reply 后才调用 `start()`。
3. `start()` 初始化资源。需要登录时发布会话状态与 Challenge，随后尽快返回。
4. 宿主持续发起 `ping()`，按需调用 `call()`；登录与平台网络工作由插件自行调度。
5. 停止接单、撤销旧会话，调用 `stop()`，待插件有期限释放资源。

每个插件实例只接受一次握手和一次 start；stop 应幂等。
插件不能在导入模块、读取 Manifest 或握手时自动登录平台。
`ping()` 必须快速返回，不等待平台网络和长业务锁。平台在线状态与插件活性分别报告。
`call()` 不得在宿主全局锁下执行；同一账号需要保持顺序的动作由插件自行串行化。
宿主可能并发调用 ping、call、stop，插件须保护自身状态并及时响应关闭。

### 握手和常驻心跳

```python
HostHello(instance_id, nonce, api_major=1, api_minor=0)
PluginHello(instance_id, nonce, plugin_id, api_major=1, api_minor=0)
Heartbeat(instance_id, nonce, sequence, deadline)
Pong(instance_id, nonce, sequence)
```

插件回复必须原样关联 instance_id、nonce 和 sequence。每次心跳可使用新的探测 nonce，
不要求它等于首次握手的 nonce。nonce 用于检测陈旧回复，
并非对同进程恶意插件的身份认证。心跳 sequence 单调递增，过期或重复探测不得当作
当前健康证据。`deadline` 是当前进程的 `time.monotonic()` 秒数。

宿主使用 `validate_handshake()` 与 `validate_pong()`；API 不兼容、回复错实例、
错 nonce、错序号、超时均应撤销该实例的可用状态。SDK 不提供自动业务重试。
同进程 Python 代码不能被安全强制中断，永久阻塞的插件需要更强的运行隔离或重启，
不能把一个超时返回描述成已经终止插件执行。

### 账号会话

```python
SessionIdentity(platform, self_id, session_epoch)
SessionUpdate(session, state, reason="")
context.update_session(update)
```

状态集合：starting、login_required、authenticating、online、offline、failed、stopped。
Python 公开常量 `SESSION_STATES` 是该集合；管理 UI 必须包括 `starting`。
online 必须携带非空 SessionIdentity。实例存活不等于账号在线。
每次登录、断线后的重新绑定和插件替换均需要新 session_epoch。
平台、账号、epoch 任一不同，即属于不同会话；旧事件或旧动作不能通过当前路由。
同一账号的动作只能有一个执行所有者，事件订阅不能变成动作广播。

宿主须校验 `SessionIdentity.platform == manifest.platform`，并保留实例绑定。
SDK 中的声明不替代平台登录验证；插件必须先得到真实平台身份才能宣布 online。

### 登录交互

```python
Challenge(challenge_id, kind, title, payload, expires_at=None)
context.publish_challenge(challenge)
```

kind 为 qr、input 或 notice。expires_at 是可选 Unix 时间戳秒数，
与动作的 monotonic deadline 不同。payload 不出现在对象 repr 中。
宿主 UI 只能向获得管理权限的用户展示仍有效的 Challenge。
QR 图像、登录链接和用户输入属于敏感材料，不能进入公共日志或 OneBot 广播。
SDK v1 未规定任意 HTML/JavaScript UI 插入接口；插件不能要求宿主执行网页代码。
Python 公开常量 `CHALLENGE_KINDS` 为 `{"qr", "input", "notice"}`。
`notice` 是合法的结束、错误或提示交互，UI 应显示其文字，不应把它当成无效类型，
也不能保留同一 Challenge 前一次响应中的二维码。`text` 不是 SDK 交互类型。

## 4. 动作与完成证据

```python
ActionRequest(request_id, session, action, params, deadline)
ActionResult(request_id, outcome, data=None, error_code=None, error_message="")
```

宿主只把 manifest 中已声明的 action 发给已绑定的会话。
request_id 在实例内唯一。插件必须在产生平台副作用前检查会话及 deadline，
使用 `request.require_live()` 与 `require_session()`。结果必须回显 request_id，
宿主使用 `validate_action_result()` 校验关联。

| outcome | 含义 |
| --- | --- |
| not_started | 能证明平台操作尚未开始 |
| completed | 已取得平台完成或拒绝的确定证据 |
| unknown | 操作可能已送达，但无法确定结果 |

`error_code is None` 表示成功，必须同时为 completed，且 error_message 为空。
失败的 error_code 是正整数；completed 也可以表示已被平台明确拒绝。
对发送消息等副作用，超时、断线和插件崩溃通常只能报告 unknown，不能改成未发送。
宿主与 SDK 不得因这些故障自动重发。只有可验证的服务器回执才能报告发送成功。
error_message 应为经过脱敏的固定或有界描述，不能携带原生异常堆栈、令牌和请求正文。

SDK `BackendError(message, retcode=1400, *, details=None)` 保留现有 OneBot
集成的业务异常形状；新插件的正式返回边界仍采用 ActionResult。

## 5. 事件确认与背压

```python
EventEnvelope(session, event_id, event)
PublishAck(event_id, persisted, duplicate=False)
ack = context.publish_event(envelope)
```

event_id 是当前插件/账号作用域中的稳定源事件标识，最长 128 个 ASCII 标识字符；
长的原生标识可采用稳定摘要，不能每次重投随机生成。宿主同时按实例和会话校验。
相同 event_id 的不同内容应报冲突，不能悄悄覆盖已经持久化的数据。

只有宿主真正持久化后才能返回 persisted=True。duplicate=True 表示该事件已经
持久化且无需再次发布。插件收到确认前保留自己的恢复依据；宿主提交之后、确认到达
之前崩溃，会发生重复投递，宿主必须幂等去重。SDK 不承诺端到端恰好一次。

队列与资源必须有界。满载时通过明确失败/未确认施加背压，插件不能无限缓冲，
也不能返回虚假 ACK 后丢掉事件。插件重启后的事件重投与消息动作重试是不同语义；
前者可按稳定 event_id 去重，后者不能自动执行。

## 6. 数据所有权和性能

SDK DTO 为不可变 dataclass。params、data、event、配置 schema 和 Challenge
payload 在边界构造时递归验证并冻结为只读 Mapping 与 tuple，防止调用方随后
修改同一字典。禁止自定义对象、循环引用、NaN、无穷值与超出 64 位范围的整数。
深度、节点和 UTF-8 内容预算均有限制；传输另需限制真实编码后的帧长度。

同进程调用直接传递已拥有的类型对象，不要求 JSON 编码或额外 IPC。
`thaw_json()` 只供需要普通可变 dict/list 的旧接口或 JSON 输出边界使用；
调用者不应在每一层反复 thaw/freeze。不要把共享可变字典当作零拷贝优化。
大文件和媒体资源句柄尚未纳入 v1，不应把无限字节串塞入普通事件。

### 消息数据与平台边界

`onebot.normalize_message()` 接受非空字符串或消息段数组，复制并验证普通 JSON
数据，不修改调用者对象。数组中的每个段必须恰好包含 `type` 与对象形式的 `data`；
类型名称必须是合法标识。消息大小上限为 1 MiB，嵌套深度上限为 64 层。
`text` 段要求非空字符串 `data.text`，`reply` 段要求非零有符号 32 位 `data.id`。

其他类型及其数据作为不透明扩展保留，不限定平台字段、目标格式或扩展内容：

```python
from nyxion_heredity_sdk.onebot import normalize_message

segments = normalize_message([
    {"type": "mention", "data": {"target": "participant-7"}},
    {"type": "extension", "data": {"metadata": {"flags": [True, None, 3]}}},
])
```

平台适配器在执行动作前负责验证自身支持的段、字段与目标身份，并明确拒绝无法执行的
内容。通用 SDK 不把扩展数据当作已经通过平台语义校验。`cq_string()` 可表示平面标量
参数；嵌套对象、数组和无法无损编码的文本附加字段会报错，不会静默丢弃。

## 7. 合规测试与离线示例

`src/nyxion_heredity_sdk/example.py` 是可运行、无网络的平台插件示例。
它只提供合成账号和 echo，不代表真实平台协议实现。

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m nyxion_heredity_sdk.conformance
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_plugin_sdk.py -q
```

合规命令检查严格 Manifest、配置、实例/nonce/API 握手、持续心跳、账号会话、
动作关联、完成状态、事件 ACK 和正常关闭。负面边界另由测试套件覆盖。

只对明确审查过的离线测试工厂使用：

```text
python -m nyxion_heredity_sdk.conformance --plugin module_name:create_offline_fixture
```

指定工厂会导入并执行其代码，不构成沙箱。不要将真实平台登录工厂交给此命令：
通用合规检查不应自动登录用户账号或发送真实消息。
测试成功只证明本次样本的接口行为，不证明第三方插件可信、完整或兼容真实平台。

规范结构位于 `schemas/`；SDK 与宿主测试、插件自己的平台协议测试应分别运行。

## 8. 独立分发构建

仓库中 SDK 的工作源码位于 `src/nyxion_heredity_sdk`，发行元数据位于
`sdk/pyproject.toml`。为避免源码包依赖 `../src`，必须先构建一个独立的暂存源项目：

```powershell
.\.venv\Scripts\python.exe sdk/Build-Sdk.py
```

此命令只调用已安装的 setuptools/wheel，不下载或安装依赖。它在
`build/sdk-distribution/sdk-*/source` 中生成自包含的标准 src-layout 项目，
把标准文档（包括 `AEGIS.md`）、许可证、SDK 源码、py.typed 和三份 schema 一并复制；
随后在相邻 `artifacts` 生成 wheel 与 `.tar.gz` 源码包，打印路径与 SHA256。
它只打包 SDK，不包含宿主、平台插件、原生签名组件和运行状态。
已有同版本产物或显式暂存目录不会被覆盖；每次默认使用新目录。

`--stage-only` 只准备源项目。拿到独立暂存目录或正式源码包后，可以使用标准
PEP 517 工具在其中构建；直接在仓库 `sdk/` 执行构建不包含位于上级的工作源码，
不能替代这个暂存步骤。

验证实际生成的产物：

```powershell
.\.venv\Scripts\python.exe sdk/Check-Distribution.py `
  --wheel <artifact-directory>/nyxion_heredity_sdk-1.5.0-py3-none-any.whl `
  --sdist <artifact-directory>/nyxion_heredity_sdk-1.5.0.tar.gz
```

验证器检查 wheel 包边界和无运行时依赖的元数据，将源码包安全抽到新目录，
从抽出的源码独立重建 wheel，再用 `pip --no-deps --no-index --target`
离线安装。最后通过 `python -I` 只加入该安装目录执行 13 项离线合规检查，
同时确认 Aegis 包声明可解析，无需导入宿主、平台插件、Wasmtime 或 cryptography，
且 typing/schema 资源齐全。生成的产物仅为本地构建，不会上传任何包仓库。

SDK 的运行时只需 Python 3.11+ 标准库；构建时另需声明的 setuptools 和 wheel。
`Check-Distribution.py` 的安全解包使用受支持的 Python tarfile data filter，
需要包含该接口的 Python 发行版。当前验收环境为 Python 3.12。
