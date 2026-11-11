# QQ集成（qqbetter_auto_reply）

让 N.E.K.O. 接入 QQ，成为一个有自己"见闻"和"记忆"的 AI 伙伴。通过 OneBot v11（正向 / 反向 WebSocket，兼容 NapCat / LLOneBot / go-cqhttp / Lagrange 等任意 OneBot 后端）或 QQ 官方开放平台接入，支持多群注意力竞争、疲劳系统、动态回复缓冲、主动搭话、B 站见闻、文件内容读取、多模态输出、图库与表情包、识图、换头像等能力。

## 功能一览

- **双通道接入**：OneBot v11 泛用连接（正向 / 反向 WS）+ QQ 官方开放平台，二选一即可。
- **注意力与疲劳**：多群注意力竞争、疲劳系统，避免刷屏和过度打扰。
- **动态回复缓冲**：把连续消息合并成一个回复窗口，按消息量自适应等待，解决"回答顺序错乱、半天不回"的问题。
- **主动搭话**：根据聊天节奏在合适的时机主动开口，不是只会被动回话。开口前会先刷一批新见闻，手里有真素材再说话；刷不动、超时就沿用旧素材，没素材就老实问一句，不许编。
- **定时搭话**：在引导里配「几点、让她做什么」（如早上说早安、晚上说晚安），到点由模型照任务自由发挥生成一句私聊发给你。每条每天只发一次，没连上 QQ 时宽限窗内自动重试、过期不补发。
- **私聊↔群 记忆打通**：引导里可开，两个独立开关。开「最近对话」——你在群里说话时，她带得上私聊刚聊过的几句；开「长期记忆」——她长期记住你的事也带得进群。两者都只认插件权限名册里的 admin（你自己），跟 QQ 群里的群主/群管理员身份无关；提示词硬性要求不主动展开、不说细节、不把私事讲给群里其他人。关掉就等于没发生过，不会删除已经记住的东西。
- **B 站见闻**：定时去 B 站刷视频（抓标题、弹幕、字幕），形成自己的"见闻"，聊天时自然地提起来；别人发来 B 站链接时也能打开看内容再回话。
- **断线对账**：断线重连后自动和 QQ 服务端历史对账，把断网、休眠、协议端掉线期间漏掉的消息补回，不再"断网就丢消息"。
- **文件与图片**：读取收到的文件内容（文本 / VLM 图片描述），多模态输出。
- **图库与表情包**：收到的图 / 表情包连描述一起入库、按内容去重、可按关键词检索；QQ 表情索引（商城表情零成本 mface 指针、收藏表情按 md5 认一次永久复用）。
- **识图服务**：套用本体主模型识图，双键缓存跨重启复用，省钱不重复花识别费。
- **一键换头像**：居中裁方、缩到 640，换完自动反查确认落地，失败自动重试。

## 环境要求

- N.E.K.O. 宿主（SDK `>=0.1.0,<0.2.0` 推荐，`>=0.1.0,<0.3.0` 支持）。
- 一个 OneBot v11 后端（NapCat / LLOneBot / go-cqhttp / Lagrange 等），或 QQ 官方开放平台应用。
- 本插件**无第三方 Python 依赖**，全部依赖宿主环境自带的库。

## 安装与配置

1. 从插件市场安装本插件。
2. 在插件管理器中启动它。
3. 打开插件面板（Hello QQ），按界面引导配置 OneBot v11 的 WebSocket 地址，或 QQ 开放平台的 AppID / Secret。
4. 连接成功后即可在群聊 / 私聊中互动。

## Development

插件源码及其 Git 仓库直接位于：

```text
src/qqbetter_auto_reply/
```

发布到插件市场时，请使用以下 GitHub 仓库名：

```text
n.e.k.o_plugin_qqbetter_auto_reply
```

Python 运行时依赖声明在 `pyproject.toml` 中，并在打包时同步到 `vendor/`。
生成的 `vendor/` 不提交；本地构建和 CI 会在发布检查前重新生成它。

## Market release / Market 发布

发布 `plugin.toml` 中声明的版本。默认会推送 Git tag、等待标准 GitHub Release，然后通知插件市场。

```bash
uv run neko-plugin publish qqbetter_auto_reply
```

如需仅执行一部分：

```bash
uv run neko-plugin publish github qqbetter_auto_reply
uv run neko-plugin publish market https://github.com/<owner>/n.e.k.o_plugin_qqbetter_auto_reply/releases/tag/v0.8.3
```

生成的 `.github/workflows/release.yml` 会构建并上传插件包；Market 会独立验证该 Release 后再发布。

## Entry

```toml
entry = "plugin.plugins.qqbetter_auto_reply:QQAutoReplyPlugin"
```
