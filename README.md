<div align="center">
  <img
    src="https://raw.githubusercontent.com/NyXion-EcoWarehouse/.github/main/assets/Heredity%E9%81%97%E4%BC%A0logo.png"
    width="150"
    alt="Heredity Logo"
  />

  <h1>
    <a href="https://github.com/NyXion-EcoWarehouse">NyXion Ecosystem</a>
    /
    NyXion Heredity
  </h1>

  <p><b>『黄昏所在地。』</b></p>

  <p>
    连接 NyXion 生态与外围 IM 的平台中立通讯桥梁
    <br />
    A Platform-Neutral IM Bridge for the NyXion Ecosystem
  </p>

  <p>
    <img src="https://img.shields.io/badge/Status-Alpha-orange" alt="Status: Alpha" />
    <img src="https://img.shields.io/badge/Protocol-OneBot_11-blue" alt="Protocol: OneBot 11" />
    <img src="https://img.shields.io/badge/Architecture-Plugin_Host-blueviolet" alt="Architecture: Plugin Host" />
    <img src="https://img.shields.io/badge/License-MIT-success" alt="License: MIT" />
  </p>
</div>

---

# NyXion Heredity

> 『黄昏所在地。』

**NyXion Heredity** 是 NyXion 生态系统中的外围通讯基础设施，为 **NyXion Gestalt** 及其他上层应用提供连接外部 IM（第三方通讯平台）的底层桥梁。

项目以 **OneBot 11** 为主要对外接口，通过平台中立的宿主、消息总线和独立插件体系，将不同平台的通讯能力接入统一的应用生态。社区可以独立开发、更新和维护平台适配器。

当前版本：**Alpha Build 26.11.13 · Core Revision 1**，Python 包版本为 `26.11.13.post1`。

## ✨ 核心特性

- **OneBot 11 接口**：提供消息事件与动作调用接口，支持 HTTP、正向和反向 WebSocket；具体消息类型与平台能力由适配器实现。
- **平台中立的运行框架**：本体不内置特定平台的登录流程、私有通讯协议或签名实现，平台连接通过独立插件提供。
- **内嵌插件宿主**：通过公开 SDK 接入适配器，统一管理插件发现、配置、握手、启停和运行状态。
- **多账号与接口管理**：账号实例拥有独立配置和状态，通过 `platform:self_id` 区分身份，网络接口与账号分配分别管理。
- **树状命名空间总线**：通过受控的命名空间组织模块与能力调用，为事件处理、接口分配和插件协作提供基础。
- **本地管理 WebUI**：集中查看账号、插件、接口、模块和运行状态，完成配置与调试。
- **面向开发者的扩展能力**：通过 SDK、事件机制和能力接口组织消息处理与模块调用，为自定义工作流提供接入基础。

## 🚀 快速启动

需要 **Python 3.11 或更新版本**。请从本仓库的 [Releases](https://github.com/NyXion-EcoWarehouse/NyXion-Heredity/releases) 下载本体运行分发包，完整解压后启动：

- **Windows**：双击 `Win.cmd`，或在 PowerShell 中执行 `.\Win.cmd`。
- **Linux**：在终端执行 `bash Linux.sh`。

启动脚本会自动创建或复用本目录的 `.venv`，安装本体、SDK 和运行依赖，然后启动应用。首次安装通常需要联网。

默认管理地址为 `http://127.0.0.1:5099`，实际地址以启动输出为准。默认生成的管理密码保存在 `.heredity/host/ui-password.txt`，自定义密码配置除外。

需要调整端口时，可将 `.env.example` 复制为 `.env`，设置 `HEREDITY_UI_PORT`。新安装默认不创建账号实例或 OneBot 监听接口，请在管理页面中配置。

## 🔌 平台适配与扩展

平台适配器作为独立 Python 包安装到本体使用的同一个虚拟环境中。安装完成后重新启动本体，再通过管理页面配置和启用相应实例。

本体负责 UI、插件生命周期、账号路由、网络接口与通用能力管理；适配器负责平台认证、通讯协议、消息能力及其本地依赖。

支持的平台、消息类型和动作范围，以所安装适配器的说明及实际能力为准。安装成功或本地握手正常，不代表平台账号已经登录。

开发者应通过公开 SDK 接入，避免依赖本体内部实现。接口契约和发布要求见 [插件架构](docs/plugin-architecture.md) 与 [SDK 文档](sdk/README.md)。

## 🛡️ 安全与生态边界

NyXion Heredity 将平台实现与本体分离，并区分不同扩展的执行边界：

- **内嵌 Python 适配器**属于可信代码，运行于宿主进程中，具有该进程的权限。通过 SDK 加载并不意味着获得沙箱隔离。
- **Aegis WebAssembly 模块**运行于独立的 Wasmtime 工作进程中，文件、网络和总线访问通过宿主授予的能力接口完成。
- **权限与接口分配**由宿主管理；模块声明权限不等于已经获得授权，账号选择也不等于接口授权。

`.env`、`.heredity/`、设备文件、会话缓存、令牌、日志和二维码属于本地私有数据。不要将这些内容或 `.venv/` 随源码、问题反馈或发行包公开。

使用者应自行评估第三方插件的来源、权限、兼容性与账号风险，并遵守适用规则及第三方平台的使用要求。本项目不保证第三方平台协议长期可用，也不为独立插件的安全性提供隐含保证。

## 📚 开发文档

- [整体架构](docs/architecture.md)
- [插件架构与接入边界](docs/plugin-architecture.md)
- [SDK 开发文档](sdk/README.md)
- [网络接口与账号分配](docs/network-interfaces.md)
- [Aegis 模块与权限模型](sdk/AEGIS.md)

项目持续开发中，目前为 **Alpha 测试阶段**，功能和接口可能调整。欢迎提交问题、改进文档，以及开发独立的平台适配器和扩展模块。

## 💖 为 Nyxion 的星火添一把柴

如果你愿意的话，可以点击下方展开赞助码，请开发者喝杯咖啡。开发不易，Vibe烧钱。
亦或者...来帮我测试？

<details>
<summary>👉 点击这里展开</summary>
<br>
<div align="center">
  <table style="border-collapse: collapse; border: none;">
    <tr>
      <td align="center" style="padding: 15px; border: none;">
        <img src="https://github.com/NyXion-EcoWarehouse/.github/raw/main/assets/%E6%94%AF%E4%BB%98%E5%AE%9D%E8%B5%9E%E8%B5%8F%E7%A0%81.png" width="280" alt="支付宝">
        <br><br>
        <b>支付宝 (Alipay)</b>
      </td>
      <td align="center" style="padding: 15px; border: none;">
        <img src="https://github.com/NyXion-EcoWarehouse/.github/raw/main/assets/%E5%BE%AE%E4%BF%A1%E8%B5%9E%E8%B5%8F%E7%A0%81.jpg" width="460" alt="微信">
        <br><br>
        <b>微信 (WeChat)</b>
              </td>
      <td align="center" style="padding: 15px; border: none;">
        <img src="https://github.com/NyXion-EcoWarehouse/.github/raw/main/assets/Heredity%E7%BE%A4.jpg" width="360" alt="QQ群">
        <br><br>
      </td>
    </tr>
  </table>
</div>
</details>

## 📄 许可证

本项目采用 [MIT License](LICENSE)。

项目地址：https://github.com/NyXion-EcoWarehouse/NyXion-Heredity
