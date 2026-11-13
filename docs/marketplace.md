# Heredity 插件市场

市场属于 Heredity 本体。它负责目录获取、用户确认、文件校验和安装登记，
不把下载代码导入 Python 宿主，也不覆盖仓库里的嵌入插件源码。
可执行插件交给独立进程中的 Aegis WebAssembly 运行时。

## 官方目录

固定索引地址：

```text
https://raw.githubusercontent.com/NyXion-EcoWarehouse/Nyxion-Heredity-Registry/main/plugins.json
```

目录顶层包含 `plugins` 数组，可选 `registry_version` 和 `last_updated`。
每个插件的字段为：

```json
{
  "id": "example.adapter",
  "name": "Example Adapter",
  "description": "A minimal WebAssembly adapter",
  "version": "1.0.0",
  "author": {"name": "Example", "github": "https://github.com/example"},
  "permissions": ["bus.call", "bus.subscribe"],
  "download_url": "https://example.com/releases/plugin.zip",
  "hash_sha256": "<64 lowercase hexadecimal characters>",
  "status": "active",
  "security_advisory": "",
  "min_heredity_version": "0.3.0"
}
```

`security_advisory` 可省略或为 null，均表示空说明。`license`、`repo_url`、
`tags` 是可选展示元数据。`author.name` 必需，`author.github`、`author.url` 可选。
当前只接受三段数字稳定版本，不猜测未知预发行版本的兼容关系。
插件 ID 必须是小写命名空间标识，同一目录中重复 ID 的所有条目均不可安装。
无效条目显示在 `invalid_entries`，不会使整个页面报错，也不会由宿主补造 ID。

允许的权限是 `fs.read`、`fs.write`、`net.connect`、`bus.call`、
`bus.publish`、`bus.subscribe`。批准权限只给运行时能力上限；
文件目录、网络目的地和模块总线命名空间仍由宿主限定。

2026 年 11 月 13 日只读核对官方地址得到 HTTP 200。当前唯一的示例条目缺少
`id`，下载地址仍为 `https://github.com/.../plugin.wasm` 占位地址，
摘要为空文件 SHA256，最低 Heredity 版本为 `1.0.0`。
该条目不能作为可安装插件，市场会明确报告无效条目。

## 刷新与安装任务

启动刷新和手动刷新均进入有界后台队列，不阻塞 UI 或平台插件控制循环。
最多等待 32 个任务，保留最近 128 个任务结果。重复中的刷新或同插件安装合并为现有任务。
目录上限 2 MiB、最多 1000 条目；下载包上限 32 MiB，网络预算默认 12 秒。
HTTPS 重定向最多 5 次，每一跳均重新验证协议和公开目的地。

获取成功的目录保存到宿主状态目录下 `market/catalog.json`。
网络或格式错误保留上次有效目录并标记 `stale`，缓存仅供展示。
安装必须先成功刷新，再确认当前版本、SHA256 和完整权限集合。
`banned` 无论是否强制确认均拒绝下载；`warning` 需要显式 `force_warning=true`。
最低宿主版本不满足时在下载前拒绝，加载本机安装记录时也再次检查。

市场服务接口：

```python
market = Marketplace(
    state_dir,
    host_version="0.3.0",
    on_install=load_verified_module,
    sandbox_info=runtime_information,
)

snapshot = market.snapshot()
refresh_job = market.refresh()
install_job = market.install(
    "example.adapter",
    expected_version="1.0.0",
    expected_sha256=approved_sha256,
    confirmed_permissions=["bus.call", "bus.subscribe"],
    force_warning=False,
)
job = market.job(install_job["job_id"])
```

快照字段：

| 字段 | 含义 |
| --- | --- |
| `index_url` | 固定官方索引地址 |
| `last_attempt_at` / `last_success_at` | 最近尝试与成功获取的 Unix 时间 |
| `refreshing` / `stale` | 当前刷新及缓存状态 |
| `plugins` | 通过校验的目录条目，附 `installed` 版本摘要 |
| `invalid_entries` | 被拒绝条目的位置、可见名称和固定错误 |
| `installed_errors` | 本机已安装包的逐项校验或禁止启动错误 |
| `jobs` | 有界任务结果 |
| `sandbox` | 宿主提供的运行时状态 |
| `error` | 有界 `code`、`message`，或 null |

任务对象包含 `job_id`、`kind`、`plugin_id`、`state`、
`created_at`、`updated_at`、`result`、`error`。
状态为 queued、running、completed、failed 或 cancelled。
公开结果不包含本机绝对路径，下载 URL 展示时删除 query，异常不暴露签名 URL 或凭据。

## 下载与文件校验

只接受 HTTPS URL，禁止内嵌用户名密码、私网、回环、链路本地及云元数据地址。
DNS 解析结果必须全部为公开地址；实际 TLS 连接固定使用已验证的地址，
仍按原域名执行 SNI 和证书验证，不使用环境代理。
固定官方索引使用独立传输：只有完整 URL 与上文常量完全一致时，
额外接受本机 DNS 代理使用的 `198.18.0.0/15` 假地址，仍按原域名强制验证 TLS 证书和 SNI，
并禁止索引重定向。该例外不适用于包下载、其他路径、查询参数或主机，
回环、私网和元数据地址始终拒绝。
GitHub 发布资源的 HTTPS 重定向和签名 query 可用于真实下载；
这些 query 不进入公开快照或错误信息。

文件流式写入随机临时文件，同时计算 SHA256，校验通过前不展开或加载。
不匹配时清除临时文件，固定返回：

```text
安全警告：插件文件哈希校验失败，可能被篡改！
```

目录摘要用于识别用户确认的文件，不等同于发布者签名。
本地登记与重新加载也会对包和提取出的 WASM 分别重新计算摘要。

## 包格式

允许裸 WASM 和 ZIP 两种格式。ZIP 只能含两个普通文件：

```text
manifest.json
module.wasm
```

不接受额外脚本、目录、重复成员、绝对路径、路径遍历、符号链接、加密成员和异常压缩比。
Manifest 上限 32 KiB，WASM 上限 16 MiB。Manifest 使用精确字段集合：

```json
{
  "schema_version": 1,
  "id": "example.adapter",
  "name": "Example Adapter",
  "version": "1.0.0",
  "sdk_api": {"major": 1, "minor": 0},
  "min_heredity_version": "0.3.0",
  "permissions": ["bus.call", "bus.subscribe"],
  "module": "module.wasm"
}
```

ID、名称、版本、最低宿主版本和权限必须与已确认目录一致。
裸 WASM 没有独立 Manifest，宿主从已确认目录生成同结构的安装元数据；
这只说明目录摘要绑定了文件字节，不声称已验证模块内部的身份。
运行时仍须编译并通过握手、能力及内存边界检查。

安装路径为：

```text
<state_dir>/market/plugins/<plugin_id>/<version>/
    manifest.json
    module.wasm
    package.bin
```

采用新目录原子发布，相同 ID 与版本的目录不会被覆盖。
安装登记位于 `market/installed.json`，记录经过校验的 Manifest、批准权限及两个摘要。
每条批准记录还由宿主单独保存的 `market/approval.key` 计算 HMAC。
仅改登记文件的权限、Manifest 或摘要不能扩大已批准能力；该密钥不交给插件或浏览器。
这不是发布者签名，也不能防护已能同时修改宿主密钥和全部文件的宿主权限入侵。
重新加载时再次解析原始包，把原 ZIP Manifest、WASM 字节与已批准记录、提取副本逐项核对。
`installed_records()` 是只供宿主使用的可信入口，验证文件后提供绝对路径给 Aegis，
这些路径不会进入浏览器 API。当前有效目录将条目标为 banned 时禁止再次加载。
坏包按条目报告到 `installed_errors`，其余已验证包仍可加载。
`installed_record(plugin_id)` 用于启动、重启前的单项重验，失败时给出固定错误。
目录中的重复身份即使一条为 active、另一条为 banned，也会阻止已有同 ID 包加载；
该禁止集合会随目录缓存持久化，不能通过重启丢掉禁止记录。

宿主可传入无参数的 `on_refresh` 回调，在目录成功更新后同步检查运行中的模块。
该回调在市场锁外执行，用于停止新目录禁止的模块。回调失败不丢弃已获得的有效目录，
而是报告独立的状态同步错误。

`on_install(record)` 可自动启动已确认插件，返回 `{"started": true}` 才记录为已启动。
沙箱加载失败仍保留校验通过的安装文件，任务结果为 `installed_not_started`；
不会把“文件已下载”描述成“插件已运行”。

## Aegis ABI v1

宿主能力导入：

```text
nyxion_aegis.host_call(i32 ptr, i32 len, i32 outptr, i32 outcap) -> i32
```

请求是 JSON 对象 `{"capability": "...", "operation": "...", "params": {...}}`。
返回值为写入输出缓冲区的 JSON 字节数；越权请求由 Aegis 终止工作进程。

模块导出：

```text
memory
aegis_alloc(i32 size) -> i32
aegis_call(i32 ptr, i32 len) -> i64
```

`aegis_call` 返回值高 32 位为响应地址，低 32 位为响应长度。
调用 JSON 为 `{"method": "...", "params": {...}}`。
首次握手方法是 `handshake`，参数包括 `api_major`、`api_minor` 和 `module_id`，
回复必须回显 API `1.0` 和 Manifest 的插件 ID。宿主从 ID 派生模块命名空间，
包不能声明其他模块的路径。
