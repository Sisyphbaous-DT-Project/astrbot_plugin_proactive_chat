# Fork 维护与版本说明

## 项目身份

当前仓库是 [Sisyphbaous-DT-Project/astrbot_plugin_proactive_chat](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_proactive_chat) 维护的 Fork。

原项目 [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) 由 [DBJD-CR](https://github.com/DBJD-CR) 创建，并由多位社区贡献者共同完善。本 Fork 保留原项目的作者信息、历史贡献记录、版权归属和 AGPL-3.0 许可证，不声称替代原项目的官方发布。

版本血缘如下：

```text
DBJD-CR 原项目 v1.2.3 代码线
  -> Sisyphbaous-DT-Project Fork v1.2.4（Fork 维护起点）
  -> Sisyphbaous-DT-Project Fork v1.3.0（群聊分批配置及相关修复）
  -> Sisyphbaous-DT-Project Fork v1.3.1（装饰发送、稳定性、安全与隐私修复）
```

因此，本文档中的 `v1.2.4`、`v1.3.0` 和 `v1.3.1` 指当前 Fork 的版本，不代表原作者仓库发布了同名版本。

## Fork 的维护目标

本 Fork 延续原项目的主动消息、上下文感知、任务调度、TTS、会话覆写和独立 Web 管理端能力，重点维护以下方向：

1. 与 AstrBot 当前版本以及最低声明版本 `4.8.0` 的真实兼容性。
2. 与 OutputPro 等 `on_decorating_result` 输出增强插件的正确协作。
3. 多平台、多机器人实例和 UMO 别名场景下的准确投递。
4. 坏配置、插件重载、任务取消和部分发送失败时的稳定降级。
5. 本地日志、Web 管理端和配置接口的隐私与安全边界。
6. 不把聊天正文、Prompt、账号标识或异常细节发送到外部遥测服务。

## v1.3.1 与原代码线的主要差异

### 可真实发送的主动消息合成事件

原发送链为了让主动消息经过 AstrBot 装饰钩子，会构造一个合成消息事件。但基础事件的 `send()` 不负责主动平台投递，导致装饰插件提前调用 `event.send()` 时只留下日志或指标，消息并未真正送达。

Fork `v1.3.1` 为主动消息提供专用合成事件：

- `event.send()` 委托到不会再次触发装饰钩子的底层直发边界。
- 事件标记为 LLM 结果并附带 `action_type=proactive`。
- 装饰器的提前分段、末段回填、停止、清空、拦截和临时文件清理均遵循 AstrBot 事件语义。
- 已有分段送达后发生普通装饰异常时，不再重复发送完整原文。

### 真实发送结果

发送结果不再是一个笼统的真假值。内部会记录尝试数、送达数、失败数、停止和拦截状态：

- 至少一个物理分段成功即视为本轮已送达。
- 部分失败不整句重试，避免重复消息。
- 零成功才执行失败补偿。
- 纯拦截不写缓存、历史或未回复次数。
- 平台流水按成功物理分段记录，运行时缓存与 LLM 历史按完整逻辑回复记录一次。

### 统一配置防线

全局配置、群聊批次和会话覆写共享同一个配置处理器：

- Web API 使用严格模式，错误请求返回 400 且不改变旧配置。
- 启动与运行时使用宽容模式，旧坏配置回退安全默认值。
- `context_settings`、`tts_settings`、`segmented_reply_settings`、调度和自动触发等嵌套字段均验证类型与范围。
- AstrBot 4.8 使用普通列表 Schema；支持模板列表的新版 AstrBot 才在内存中升级编辑能力。

### 隐私与 Web 安全

- 删除全部匿名遥测与远程错误上报功能。
- 异常日志只记录固定编号和安全异常类型。
- 日志不输出聊天正文、Prompt、UMO、账号、群号、平台实例、conversation ID 或用户自定义备注。
- Web 管理端修复 `no-auth` 绕过、坏密码配置失效、旧令牌未撤销以及 WebSocket query token 等问题。
- 密码热更新会立即废除旧 HTTP Token 和 WebSocket 连接。

### 生命周期和兼容边界

- 关闭期间禁止新计时器、后台任务、LLM 后续发送和 Web 手动触发继续产生。
- 通知任务取消、TTS 临时文件和平台历史补写均具有独立清理与降级路径。
- `FriendMessage`、`PrivateMessage`、`GroupMessage`、`GuildMessage` 按明确消息类型路由，不根据平台名称猜测会话类型。

## 升级到 v1.3.1

从当前 Fork `v1.3.0` 升级到 `v1.3.1` 不要求重建会话配置，也不新增必须填写的配置项。

请从 [Sisyphbaous-DT-Project Fork 仓库](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_proactive_chat) 获取源码归档，或在版本正式发布后从 [Fork Releases](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_proactive_chat/releases) 获取安装包，并核对 `metadata.yaml` 中的仓库地址与版本号。AstrBot 插件市场中的同名条目可能来自 DBJD-CR 原项目，不能只凭插件名称判断是否包含本 Fork 的修复。

升级后需要注意：

1. `telemetry_config` 已移除，旧配置中的同名字段不再生效。
2. Web 管理端如果设置了密码，旧 Token 会在重载或密码变化后失效，需要重新登录。
3. 错误配置现在可能被 Web API 明确拒绝；请根据返回的安全字段路径修正配置。
4. AstrBot 4.8 的原生插件面板对对象列表编辑能力有限，群聊批次可以通过插件自己的 Web 管理端或配置 JSON 编辑。
5. OutputPro 等装饰插件无需增加专用配置；主动消息会以 LLM 结果进入标准装饰链。

## 兼容性验证范围

`v1.3.1` 发布前验证覆盖：

- 当前 AstrBot 环境的完整插件测试与真实插件管理器加载。
- AstrBot 4.8.0 临时归档环境的导入、配置和核心发送测试。
- OutputPro v2.2.5 风格的提前分段发送与真实时间线测试。
- 私聊、群聊、Guild、多个同类平台实例、平台离线、部分失败、停止、清空和纯拦截。
- Web 管理端密码热更新、旧 Token / WebSocket 失效和坏配置原子拒绝。
- Ruff、编译、Schema、JavaScript、Git diff 与行尾检查。

详细测试数字与逐项修复记录以 [CHANGELOG.md](../CHANGELOG.md) 中的 `v1.3.1` 条目为准。

## 问题反馈与贡献

- 当前 Fork 的问题与改动应在 [Fork 仓库](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_proactive_chat) 跟踪；仓库开放相应功能时，可使用 [Fork Issues](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_proactive_chat/issues) 和 [Fork Pull Requests](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_proactive_chat/pulls)。
- 原项目的历史问题、`v1.2.3` 及更早发布和上游社区信息请前往 [DBJD-CR 原仓库](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat)。
- 涉及 AstrBot 平台能力的问题仍应优先向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 反馈。

## 许可与致谢

本 Fork 继续使用 GNU Affero General Public License v3.0。分发、部署和二次修改时应遵守 [LICENSE](../LICENSE) 中的完整条款。

感谢 DBJD-CR 创建原项目，感谢原项目及当前 Fork 的所有贡献者、测试者和反馈者。Fork 中的维护改动是在原项目已有架构和功能基础上继续演进，并不抹去或替代任何既有贡献。
