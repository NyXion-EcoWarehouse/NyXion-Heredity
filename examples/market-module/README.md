# NyXion Example Module

这是一个最小 Aegis WebAssembly 占位模块，用于上传到内部插件目录和验证安装流程。
它不申请任何权限，不会访问文件、网络或外部服务。启动完成 ABI 握手，
后续调用返回固定的运行状态 JSON，可用于验证安装、启动、停止和重启。
它只验证模块生命周期，不会伪造已登录账号。

重新生成包：

```powershell
.\.venv\Scripts\python.exe scripts\Build-ExampleModule.py
```

输出的安装 ZIP 只包含 `manifest.json` 和 `module.wasm`。
构建脚本同时产生 `plugins.json` 和 SHA-256。索引默认使用计划的 GitHub Release
下载地址；上传实际包后，确保 `download_url` 与真实地址一致。
修改下载地址不影响已生成 ZIP 的 SHA-256；修改 ZIP 内容则必须重新生成摘要。
