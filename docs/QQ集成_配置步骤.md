# 给猫娘一个 QQ 号（用 LLOneBot / LLBot）

> 2026-10-07 起草 · **2026-10-09 22:40 更新：第 3 步已完成，现在只差「加好友」**

---

## ✅✅ 最新状态（2026-10-09 22:40）

**第 3 步（开 OneBot 正向 WS）已执行完成并验证通过。**

- LLBot 已登录：**UIN `2051144523`（昵称 Yui）**
- `127.0.0.1:3001` 已监听，OneBot 11 正向 WS 正常
- 实测 `get_login_info` → `{"user_id":2051144523,"nickname":"Yui"}` ✅
- 验证脚本：`Q:\neko开发\_probe\ob11_check*.js`、`ob11_send_test.js`
  （跑法：`NODE_PATH=Q:\LuckyLillia-Desktop-win-x64\bin\llbot\node_modules <llbot>\node.exe <脚本>`）
- 配置备份：`bin\llbot\data\config_2051144523.json.bak-20261009`

**我动了什么**

| 项 | 值 |
|---|---|
| LLBot WebUI 访问密码 | **`nekollbot`**（之前没设过 → WebUI 打开就是 401，我代设的；你可以自己改） |
| `ob11.connect[0]`（正向 WS） | `enable: false` → **`true`**（127.0.0.1:3001，array，token 空） |

> ⚠️ **教训**：我一开始按老办法直接改 `config_2051144523.json` 文件，**没生效**。
> LLBot 源码里虽然有 `fs.watchFile` 热重载，实测等 45 秒毫无反应（壳还会回写覆盖文件）。
> 后来改走官方接口 `POST /api/config` 才成功。
> **以后改 LLBot 配置一律走接口，别改文件。**

**关于 QQ 号**

- 实际登录 `2051144523`（Yui），不是 3022729132（曼波 —— 这号真实存在，等级 8，不是你写错）
- 原因：那个 QQ 客户端里存着 2051144523 的登录态；`app_settings.json` 里 `auto_login_qq` 虽写着 3022729132，
  客户端还是自己登了 2051144523
- **已确认：就用 Yui，不换。**

**⛔ 现在唯一卡点：机器人号一个好友都没有**

已实测直接发私聊，失败：

```json
{"status":"failed","retcode":1200,"message":"无法获取用户信息"}
```

好友列表为空（只有它自己）。OneBot 给非好友发私聊拿不到用户信息 → 直接失败。
→ **必须先互加好友**，见下面「第 3.5 步」。

---

## 📜 以下为 10-07 的原始记录（供对照）

> 2026-10-07 · 你要的操作步骤，照着做就行
> 结论：**不用开发任何东西。** N.E.K.O. 自带的 `qq_auto_reply` 插件就是你要的，而且已经有「主动发送私聊消息」入口。

---

## ✅ 进度（2026-10-07 15:58 更新）

**插件侧我已经替你改好了。** 相对原始备份，一共动了 **3 个键**：

| 字段 | 改前 | 改后 |
|---|---|---|
| `qq_connection_mode` | `"napcat"` | **`"napcat_forward"`** ✅ |
| `onebot_url` | `"ws://0.0.0.0:6199"` | **`"ws://127.0.0.1:3001"`** ✅ |
| `trusted_users` | `[]` | **`[{"qq": "3560709109", "level": "admin"}]`** ✅ |
| `token` | `""` | 保持 `""`（跟 LLBot 默认一致，见下） |
| `napcat_directory` | `""` | 保持 `""`（**必须空**，见第〇节） |

改的文件：`Q:\N.E.K.O_0.8.3.1_win\N.E.K.O\plugins\qq_auto_reply\data\business_config.json`
备份：同目录 `business_config.json.bak-20261007-154511`

**两个 QQ 号的分工**：

| QQ 号 | 用途 | 配在哪 |
|---|---|---|
| `3022729132` | **猫娘用的**（机器人账号，去登录 LLBot） | 在 LLBot 里扫码登录，**不写进插件配置** |
| `3560709109` | **你自己的**（主人 / admin） | 已写进 `trusted_users` |

> 猫娘的号不需要出现在插件配置里——插件靠"哪条消息从哪个号发来"判权限，
> 机器人自己的号不参与权限。/ 所以 `3022729132` 查出来是 `none`，这是**正确**的。

校验方式：直接拿插件自己的 `permission.py` 跑了一遍——
`3560709109` → `admin`，`3022729132` → `none`，
`_refresh_admin_qq()` 认定主人 = `3560709109` ✅（功能正常）。

### 为什么是 `"qq"` 而不是 `qq_number`

这个坑值得记：`@plugin_entry add_trusted_user` 的**参数名**是 `qq_number`，
但**存到 business_config.json 里的键名是 `qq`**（`permission.py` 里读的是 `user.get("qq", "")`）。
写成 `qq_number` 会被静默忽略（空 qq 直接跳过，不报错）。
落盘格式就是 `permission_mgr.list_users()` 的输出：`{"qq": ..., "level": ...}`（昵称空则不带该键）。

> **`token` 为什么留空**：LLBot 的 `default_config.json` 里 OneBot 的 token 就是空的。
> 一边有 token 一边没有会连不上，所以先都留空（只监听 127.0.0.1，安全上没问题）。
> 你如果后来在 LLBot 里设了 token，跟插件面板里那格填成一样的就行。

### ⛔ 现在卡在 LLBot 这边（这个必须你先处理）

你已经把 LLBot 装到 `Q:\LuckyLillia-Desktop-win-x64\`，15:41 启动了 `LuckyLillia.exe`。
但它的日志里有：

```
[WRN] 获取Node.js版本失败
System.ComponentModel.Win32Exception (2): An error occurred trying to start process
'bin/llbot\node.exe' with working directory 'Q:\LuckyLillia-Desktop-win-x64'. 系统找不到指定的文件。
```

**`bin\llbot\node.exe` 不存在** → llbot 核心起不来 → **3080（WebUI）和 3001 都没在监听**。
桌面包（62.7MB）只带了 `LuckyLillia.exe`（.NET 壳）+ `bin/llbot/`（llbot.js + node_modules + webui），**没带 node.exe**。
LLBot 要求 **Node ≥ 24**。

**另外还有一处要改**：`bin/llbot/` 下**没有 `config.json`**，所以它现在跑的是 `default_config.json`，
而里面 OneBot 的 4 种连接（`ws` / `ws-reverse` / `http` / `http-post`）**全是 `enable: false`**。

好消息是默认那条 `ws` 就是 `127.0.0.1:3001` + `messageFormat: "array"` ——
**和我改的插件配置正好对上，你只要把它的 `enable` 打开就行。**

**修复办法：下 Node.js 24 LTS，只取 `node.exe` 放到位**

> 2026-10-07 查到的实际版本：最新是 v26.10.0（还不是 LTS）；**24.x 最新 LTS 是 `v24.21.0`**（代号 Krypton）。
> LLBot 要 ≥24，用 24 LTS 最稳。

**别装 MSI**（会装到 `C:\Program Files\nodejs` 还写注册表、加 PATH）—— 我们只要一个文件。走 zip：

1. 下载（约 30 MB）：
   ```
   https://nodejs.org/dist/v24.21.0/node-v24.21.0-win-x64.zip
   ```
   （如果这个版本号过期了，去 https://nodejs.org/dist/ 挑最新的 `v24.x` 的 `-win-x64.zip`）

2. 解压，进到 `node-v24.21.0-win-x64\` 目录

3. **只把里面的 `node.exe` 复制出来**，放到：
   ```
   Q:\LuckyLillia-Desktop-win-x64\bin\llbot\node.exe
   ```
   （`node.exe` 是自包含的，单个文件就能跑；同目录的 npm / npx / corepack 这些**都不用**，
   LLBot 的 `llbot.js` 是预编译好的，依赖已经 vendored 在 `node_modules` 里。）

4. 验证一下版本对不对：
   ```
   Q:\LuckyLillia-Desktop-win-x64\bin\llbot\node.exe --version
   ```
   应该打印 `v24.21.0`

5. **重启 LuckyLillia.exe**，再看日志里那句 `获取Node.js版本失败` 是不是没了，
   然后确认 3080 起来了：
   ```
   netstat -ano | findstr :3080
   ```
   浏览器打开 `http://127.0.0.1:3080` 能进 WebUI 就成了。

> **可选：先花 1 分钟试本机的 node 22**
> 本机唯一现成的 node.exe 是 workbuddy 自带的 22.22.2：
> `C:\Users\李文发\.workbuddy\binaries\node\versions\22.22.2\node.exe`
> 先复制过去试试（不用下载）。能跑就省事；跑不起来再按上面装 24 —— 到时候**覆盖**掉即可。
> 注意 LLBot 标称要求 ≥24，22 有可能报错。

---

## 〇、先记住一件事（最容易踩）

**N.E.K.O. 插件里的 `napcat_directory` 必须留空。**

我翻了插件的启动代码（`napcat_service.py` 第 205-210 行）：

```python
configured_path = self.get_configured_napcat_path()
if not configured_path:
    # 未配置 napcat_directory → 不自动启动 NapCat（用户可能手动启动）。
    # 这不是硬失败：wait_for_onebot_ready 仍会轮询等待手动启动的 OneBot 连上来。
    return
```

留空就**不会去启 NapCat、也不会报硬错误**，只会老老实实轮询等你手动启动的 OneBot 连上来。
—— 这正是配 LLOneBot 需要的。**反过来，如果你填了 NapCat 目录但没有 NapCat，反向模式会立刻硬失败。**

---

## 一、LLOneBot 现在的状况（先知道，不然会下错东西）

| 项 | 情况 |
|---|---|
| 项目现状 | `github.com/LLOneBot/LLOneBot` 现在**重定向**到 **`LLOneBot/LuckyLilliaBot`**（LLBot） |
| 最新版 | **v8.3.0**，2026-10-06 发布，**还在活跃更新** |
| 定位变化 | 已经不是当年那个"装在 QQ 里的 LiteLoader 插件"了。现在**以 LagrangeV2 为核心**，自带运行环境，Desktop / CLI 两种独立形态 |
| 协议 | OneBot 11 / Satori / Milky |
| 官网 | https://luckylillia.com （只有首页宣传，文档主要在仓库里） |

**所以别去搜"LLOneBot 插件安装"的老教程**——那些是旧版（装 LiteLoaderQQNT 的路线），跟你现在要下的不是一回事。

---

## 二、你要做的操作

### 第 1 步：下载

到 https://github.com/LLOneBot/LuckyLilliaBot/releases/latest ，下这个：

```
LuckyLillia-Desktop-win-x64.zip      62.7 MB   ← 选它，有图形界面
```

备选：
- `LuckyLillia-CLI-win-x64.zip`（91 MB）—— 纯命令行版，启动 `LuckyLillia.exe`，`--help` 看参数
- ⚠️ **Desktop 版要求 Windows Server 2012 R2 / Windows 10 及以上**（你是 Win10+，没问题）
- ⚠️ 不用下 Docker / macOS / Linux 的那些包

解压到一个**非 C 盘**的固定位置，比如：
```
Q:\neko开发\llbot\
```

### 第 2 步：启动并登录 QQ

1. 运行解压出来的 `LuckyLillia.exe`（Desktop 版会有图形界面）
2. 首次登录**要用手机 QQ 扫码**（先别管什么快速登录，第一次都得扫）
3. **一定用小号！**（原因见第六节）
4. 登录成功后，会有一个 **WebUI / GUI 管理界面**——地址和端口程序启动时会在控制台打印出来，**以它打印的为准**（网上那些 3080 / 3000 / 5173 的说法互相矛盾，别信）

### 第 3 步：在 LLBot 里开 OneBot 11 的正向 WebSocket ✅ 已完成（2026-10-09）

> 这一节留作记录。**实际已由我通过 `POST /api/config` 打开**，
> 现在 `127.0.0.1:3001` 正在监听。你不需要再手动操作。

在 WebUI 里找到协议 / 网络配置，针对 **OneBot 11**：

| 项 | 填什么 |
|---|---|
| 启用 | ✅ 打开 |
| 类型 | **正向 WebSocket**（WebSocket 服务器 / WS Server） |
| 监听地址 | `127.0.0.1` |
| 端口 | `3001`（想用别的也行，一会儿 N.E.K.O. 那边跟着改） |
| 访问令牌 Token | 设一个，记下来（比如就用你之前那个 `xK6sBTNuW7y1XDia`） |
| 消息格式 | **`array`（段数组）**，**不要** CQ 字符串 |

**消息格式这条是关键**：N.E.K.O. 这边只认段数组。设成 CQ 字符串的话消息会解析不出来。

> 顺带说：**正向和反向不要同时开**，选正向。

### 第 3.5 步：让机器人号和你的号互加好友（现在唯一卡点）

**为什么必须做**：已实测，机器人号（Yui / 2051144523）给你发私聊返回
`retcode 1200 无法获取用户信息` —— 好友列表是空的。OneBot 对非好友发私聊直接失败。

**怎么做**（挑一种）：

| 方式 | 操作 |
|---|---|
| **A（推荐）你用手机加它** | 手机 QQ 用 **3560709109** 搜 `2051144523` → 加好友（附言随便写）→ 然后告诉我，我通过 OneBot 把这条好友请求**自动同意**掉 |
| **B 你在 QQ 窗口点** | 如果那个被注入的 QQ 客户端窗口能看到，直接点「同意」 |
| **C 反过来** | 在能操作机器人号的界面上主动加 3560709109 |

> 卡在哪一环告诉我，我这边有 OneBot 通道，能直接查/处理好友请求（`get_friend_add_request` / `set_friend_add_request`）。
> **加完之后我再发一条测试，通了就可以进第 4~7 步。**

---

### 第 4 步：N.E.K.O. 插件侧（改 4 个字段）✅ 已完成（2026-10-09，我改的）

打开 N.E.K.O. → 插件 → **QQ集成** → 面板：

| 字段 | 现在的值 | 改成 |
|---|---|---|
| 连接模式 `qq_connection_mode` | `napcat` | **`napcat_forward`**（正向） |
| OneBot 地址 `onebot_url` | `ws://0.0.0.0:6199` | **`ws://127.0.0.1:3001`** |
| Token | （空） | **你在第 3 步设的那个** |
| NapCat 目录 `napcat_directory` | （空） | **保持空！**（见第〇节） |

保存。

> 模式名有点误导：`napcat_forward` 里的 "napcat" 只是历史命名，它就是"**正向 WS**"的意思——
> 插件作为客户端拨出去连别人的 OneBot 服务。LLOneBot 走的就是这条路。

### 第 5 步：把自己的号加成 admin

面板 → 信任用户 → 添加：

| 字段 | 填什么 |
|---|---|
| QQ 号 | **你自己的 QQ 号** |
| 等级 | **admin**（管理员） |
| 昵称 | 不用填（admin 固定被称呼为"主人"，代码里写死了：`errors.admin_no_nickname = "管理员始终被称为主人，无法设置昵称"`） |

为什么要设 admin：
- 只有 **admin 的私聊**才会同步进记忆库，猫娘才记得住你说过什么
- `normal` 用户根本不直接回复，只会概率转述给管理员

顺手把 `reply_mode` 设成 `text` / `voice` / `both`（`both` = **她可以发语音**）。

### 第 6 步：启动自动回复

面板 → **启动自动回复**。状态要变成"已连接"。

⚠️ **必须做完这一步**，主动发送入口才能用。代码里写死了：
```
errors.proactive_not_ready = "…"
用途说明：使用前需先启动自动回复并确保 OneBot 已连接，否则入口会直接报错
```

### 第 7 步：让她给你发一条

面板 → **「主动发送私聊消息」**（入口 `send_private_proactive_message`）：

| 参数 | 填什么 |
|---|---|
| `target` | 你的 QQ 号（或你在信任用户里设的昵称） |
| `message` | **给 AI 的提示，不是原样直发的文本** |

`message` 举例：
```
用你自己的语气跟主人说句晚安，可以提一句今天他忙到很晚
```

然后她就会用角色人设生成一条，发到你的 QQ。

也可以直接用命令口气跟她说（走 TaskExecutor）：
> 「用你自己的语气给主人发条QQ，问问他在忙什么」

---

## 三、这个插件还有什么现成的

除了主动发消息，v0.8.0 还有一堆（都是面板/入口，共 44 个）：

| 功能 | 入口 |
|---|---|
| 主动发群消息 | `send_group_proactive_message(group_id, message)` |
| 表情包 | `register_sticker` / `upload_sticker` / `list_stickers` |
| 主动话题 | `save_proactive_topics` |
| **用自然语言配 OneBot 连接** | `configure_onebot_nl` |
| 群注意力管理 | `get_attention_state` / `adjust_group_attention` |
| 待审阅积压 | `get_backlog_summary` / `get_group_backlog_detail` |
| 提示词覆盖 | `save_prompt_override` / `reset_prompt_override` |
| 看日志 | `get_recent_logs` |

**注意：这个插件没有 `@llm_tool`**（我确认过，`grep -c "@llm_tool"` = 0）。
也就是说**猫娘在普通聊天里不能自己决定**"我要发条 QQ"——只能：
- 你用**命令口气**说（TaskExecutor 匹配到入口），或
- 在面板上点

这是现状，不是配置问题。

---

## 四、排查对照（都是代码里的原文，方便你对症）

| 现象 | 原因 / 怎么办 |
|---|---|
| `NapCat 启动器不存在: …，请先配置 napcat_directory 或确认内置 NapCat.Shell 完整` | 你把 `napcat_directory` 填上了。**清空它** |
| `NapCat 已尝试启动，但没有客户端连接到反向 WS 服务器` | 模式还是 `napcat`（反向）。改成 `napcat_forward` |
| `NapCat 已启动，但正向 WebSocket 连接未建立（NapCat 可能仍在登录，或未开启 WebSocket 服务器）` | 正向模式但连不上 → 查 LLBot 是否已登录、3001 是否在监听、Token 是否一致 |
| `无法连接到 OneBot 服务 {url}，请先启动外部 NapCat/OneBot: {error}` | OneBot 端没起来 |
| `QQ 开放平台: app_id 和 client_secret 未配置` | 模式被设成了 `open_platform`（你的错误日志 9/27 就出现过这条） |
| 入口报 `OneBot 未连接，请先启动自动回复` | 先启动自动回复 |
| `AI 未生成可发送的私聊内容` | `message` 太空，给点具体提示 |
| `target 不能为空，且昵称必须存在于信任用户列表中` | `target` 填纯数字 QQ 号，或填信任列表里已有的昵称 |
| 消息发出去了但对方说看不到 | 检查 LLBot 的消息格式是不是被设成了 CQ 字符串，要 `array` |

**日志位置**：
```
Q:\N.E.K.O_0.8.3.1_win\N.E.K.O\logs\plugin\N.E.K.O_Plugin_qq_auto_reply_<日期>.log
Q:\N.E.K.O_0.8.3.1_win\N.E.K.O\logs\plugin\N.E.K.O_Plugin_qq_auto_reply_error.log
```
**插件配置**（改完想看落到磁盘的值）：
```
Q:\N.E.K.O_0.8.3.1_win\N.E.K.O\plugins\qq_auto_reply\data\business_config.json
```

---

## 五、你之前卡住的地方（对照一下，别再走一遍）

从你的配置和日志里看出来的：

| 发现 | 说明 |
|---|---|
| `trusted_users: []` | **没配 admin**，所以就算连上了也不会回你 |
| `napcat_directory: ""` | 空是**对**的（配 LLOneBot 就该空） |
| `qq_connection_mode: "napcat"` + `onebot_url: "ws://0.0.0.0:6199"` | 这是**反向**模式，但外部没有 OneBot 客户端连 6199 → 一直等不到 |
| NapCat.Shell 里少了 `launcher.bat` | 那个目录是用过的**运行残留**，不是完整安装。走 LLOneBot 的话这个不用管了 |
| 9/27 错误日志报 `QQ 开放平台: app_id 和 client_secret 未配置` | 当时模式被设成了 `open_platform` |

---

## 六、必须知道的风险

1. **非官方 QQ 机器人会被腾讯打击。** LLBot / NapCat / Lagrange 对腾讯来说等同于外挂，账号被判定为机器人后会**警告、封禁、临时甚至永久冻结**。
   → **务必用小号**，别拿主号试。
2. **不能和 PC 端 QQ 同时登录同一个号**，会互相挤下线。挂机器人期间这个号就别登电脑版了。
3. **PC 协议每 30 天要重新登录一次**（官方限制）。
4. **只有 admin 的私聊进记忆库**；群聊（含 open 群）只留临时上下文，不写记忆。
5. LLBot 这类项目对 QQ 版本敏感，**别动不动更新 QQ**；更新前先确认框架支持。

---

## 七、如果 LLBot 装不起来（Plan B）

LLBot 现在是独立程序，理论上比旧的注入式方案稳。但如果它在你机器上跑不起来，
换 **NapCat** 是最省事的备选：

1. 下 `NapCat.Shell.zip`：https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip
   （实测这台机器能下，但只有 40-75 KB/s）
2. 解压到 `Q:\neko开发\napcat\NapCat.Shell\`，双击 `launcher.bat`（Win10 用 `launcher-win10.bat`）
3. 手机 QQ 扫码登录 → WebUI 里确认 **WebSocket 服务器 127.0.0.1:3001** 已启用
4. N.E.K.O. 侧**配置完全一样**（`napcat_forward` + `ws://127.0.0.1:3001` + Token），
   只要多填一项 `napcat_directory = Q:\neko开发\napcat\NapCat.Shell`

> ⚠️ 别用 `NapCat.Shell.Windows.OneKey.zip`（那个 0.9MB 的"一键版"）——它内置的 QQ 下载地址已被腾讯下架，会 404。

---

## 八、速查

| 项 | 值 |
|---|---|
| LLBot 下载 | `https://github.com/LLOneBot/LuckyLilliaBot/releases/latest` → `LuckyLillia-Desktop-win-x64.zip` |
| LLBot 最新版 | v8.3.0（2026-10-06） |
| LLBot 官网 | https://luckylillia.com |
| OneBot 正向 WS | `ws://127.0.0.1:3001`，消息格式必须 `array` |
| 插件连接模式 | `napcat_forward` |
| 插件 NapCat 目录 | **必须留空** |
| 要设的账号等级 | 你自己的 QQ → `admin` |
| 发消息入口 | `send_private_proactive_message(target, message)` |
| 插件配置 | `Q:\N.E.K.O_0.8.3.1_win\N.E.K.O\plugins\qq_auto_reply\data\business_config.json` |
| 插件日志 | `Q:\N.E.K.O_0.8.3.1_win\N.E.K.O\logs\plugin\` |
