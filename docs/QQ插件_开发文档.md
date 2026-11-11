# N.E.K.O. QQ 集成插件 开发文档

> 版本 `0.8.8`　代码 `Q:\neko开发\src\qqbetter_auto_reply`
> 最后核对：2026-11-08（下面的数字都是当天从代码里数出来的，不是估的）

---

## 0. 怎么读这份文档

先说清楚一件事：这个项目里「工具」有三个意思，混在一起会看得云里雾里。本文按这三层分别写。

| 层 | 叫什么 | 数量 | 谁能用 | 在哪节 |
|---|---|---|---|---|
| A | **代码模块**（.py 文件） | 62 个 | 开发者 | 第 8 节 |
| B | **她的工具**（`ToolDefinition`） | 9 个 | 猫娘自己在生成时调用 | 第 9 节 |
| C | **插件入口**（`@plugin_entry`） | 52 个 | 宿主、前端页面、外部脚本 | 第 10 节 |

按目的查：

| 你想干什么 | 去哪节 |
|---|---|
| 搞清这东西在哪、跟上游什么关系、版本号怎么这么乱 | 1、2、3 |
| 想知道一条群消息怎么变成一句回复 | 4、5、6、7 |
| 要改某个文件，先知道它到底管什么 | 8 |
| 想知道她手里有哪些工具、怎么加一个 | 9 |
| 想从外部调它（写脚本、修前端、做自动化） | 10 |
| 调参数 | 11、12 |
| 改提示词、改界面 | 13、14 |
| 部署、打包、发市场 | 15 |
| 它不回消息了 | 16、17 |
| 群聊私聊记忆打通（隐私边界、两个开关、撤销链路） | **19** |
| 接着往下做要注意什么 | 18、附录 |

### 目录

**坐标**　1 仓库与本机路径（含三条版本线）｜2 这是个什么东西｜3 目录结构

**怎么跑起来的**　4 运行架构（启动顺序 / 6 个常驻循环）｜5 一条群消息的旅程｜6 五阶段流水线｜7 横切机制（注意力 / 疲劳 / 缓冲 / 权限 / 记忆打通摘要）

**三层「工具」**　8 代码模块 62 个逐一说明｜9 她的工具 9 个逐一说明｜10 插件入口 52 个全表

**参数与资源**　11 配置 89 项全表｜12 数据文件全表｜13 提示词系统｜14 前端

**动手**　15 开发流程（改→生效→测→打包→发布）｜16 调试手册｜17 踩坑清单（25 条 + 两条通用教训）｜18 已知限制

**专题**　19 群聊私聊记忆打通（0.8.5 / 0.8.6 / 0.8.8 三版的来龙去脉、四个生效条件、十几个文件、两段提示词原文、三处撤销、排查清单）

**附录**　A 名词表｜B 命令速查｜C 版本历史｜D 依赖关系

---

## 1. 仓库与本机路径

这一节是全文档最该先记住的。搞错仓库或目录，后面全是白干。

### 1.1 四个 GitHub 仓库

| 角色 | 地址 | 是什么 |
|---|---|---|
| **猫娘本体**（N.E.K.O.） | https://github.com/Project-N-E-K-O/N.E.K.O | 宿主程序。插件跑在它里面，用它的模型、记忆服务、SDK。`main` 分支 |
| **原 QQ 插件**（社区版，上游） | https://github.com/zhaijiunknow/n.e.k.o_plugin_qq_auto_reply | 本插件的前身，作者 `zhaijiunknow`。**这份文档说的「上游」就是它**。2026-11-07 已到 `v0.14.11-beta.1` |
| **本插件**（这条开发线） | https://github.com/le-le-king/n.e.k.o_plugin_qqbetter_auto_reply | 本文档描述的对象。`main` + 若干 `feat/*` 分支 |
| 历史仓库（已弃用） | https://github.com/le-le-king/n.e.k.o_plugin_qq_auto_reply | 最早用 `qq_auto_reply` 这个 id 建的，市场审核时撞名，改名后留档，别再往里推 |

还有两个不在 GitHub 但必须知道的地址：

- 插件市场：https://market.project-neko.cn （只收 GitHub 仓库，不收 `.neko-plugin` 文件）
- 插件开发文档：https://project-neko.online/zh-CN/plugins/cli

### 1.2 还有一份「原 QQ 插件」没有仓库

本体自带一个内建插件，id 也叫 `qq_auto_reply`，名字也叫「QQ集成」。

- **它没有独立的 GitHub 仓库**，代码固化在 `projectneko_server.exe` 里。
- 但它留了一份完整 Python 源码在本机：`Q:\PerfLogs\steam\steamapps\common\n.e.k.o\resources\bin\plugin\plugins\qq_auto_reply\`（53 个 py，`version = "0.8.0"`，author `N.E.K.O Team`）。
- **本插件就是它的分支**。这也是版本号从 0.8.x 起步的原因 —— 对标的是内建版 0.8.0，不是上游社区版的 0.14.x。

三条线容易搞混，一张表钉住：

| | 内建版 | 本插件 | 上游社区版 |
|---|---|---|---|
| id | `qq_auto_reply` | `qqbetter_auto_reply` | `qq_auto_reply` |
| 版本 | 0.8.0 | **0.8.8** | 0.14.11-beta.1 |
| 代码在哪 | exe 里（本机有源码副本） | 本仓库 | zhaijiunknow 仓库 |
| 能不能改 | **不能**，改了 reload 也没用 | 能，热更新 | 能 |
| 谁在维护 | N.E.K.O Team | 我们 | zhaijiunknow + 社区 |
| 装法 | 随本体 | `.neko-plugin` 导入 / 市场 | 市场 |

**两个「QQ集成」不能同时开**，同时连 3001 会重复回复。这是刻意保留的并存设计（为了能改代码），不是 bug。

### 1.3 本机路径

| 东西 | 路径 | 说明 |
|---|---|---|
| 宿主程序 | `Q:\PerfLogs\steam\steamapps\common\n.e.k.o` | Steam 安装，真正在跑的那个 |
| 数据目录 | `Q:\N.E.K.O_0.8.3.1_win\N.E.K.O` | 有 `plugins.lock.json` 的那层 |
| 插件源码 | `Q:\neko开发\src\qqbetter_auto_reply` | **唯一副本，改这里** |
| 运行实例 | `<数据目录>\.neko-plugin-installations\plugins\qqbetter_auto_reply` | 宿主加载的是这份，由源码同步过来 |
| 插件数据 | `<数据目录>\plugins\qqbetter_auto_reply\data` | 聊天记录、图库、见闻、配置全在这 |
| 日志 | `<数据目录>\logs\plugin\N.E.K.O_Plugin_qqbetter_auto_reply_<日期>.log` | 插件自己的日志在这份里 |
| 打包脚本 | `Q:\neko开发\build_and_install.py` | 见 15.5 |
| 打包产物 | `Q:\neko开发\dist\` | `.neko-plugin` 和分享包 |
| 测试垫片 | `Q:\neko开发\_probe\_guide_testroot\` | 见 15.4 |
| 开发 venv | `Q:\neko开发\.venv` | pytest / ruff / aiohttp |
| 宿主嵌入 Python | `Q:\PerfLogs\steam\steamapps\common\n.e.k.o\resources\bin` | 3.11.13，插件实际跑在这上面 |
| 上游源码副本 | `Q:\neko开发\_upstream\qq_upstream_git` | 从 zhaijiunknow 克隆，提 PR 用的草稿区 |

两个目录必须分清：

- **`resources\bin\plugin\plugins\<id>\`** —— 宿主自带的插件源码副本，**只读参考**。
- **`.neko-plugin-installations\plugins\<id>\`** —— 用户插件（也就是我们自己）的实际加载位置。

> 教训记一笔：曾经在 `resources/bin/projectneko_server.exe` 里搜字符串来判断插件能力，那个结论只对「宿主核心」有效。要读自带插件实现，去上面那个 `plugin\plugins\` 目录。

---

## 2. 这是个什么东西

一句话：把猫娘接进 QQ，让她在群里像个人一样看气氛说话，在私聊里会主动搭话。

### 2.1 两条接入通道

| 通道 | 实现文件 | 触发方式 | 连接方式 |
|---|---|---|---|
| OneBot v11 | `qq_client.py` | 消息推过来就处理 | 正向 WS（主动连 3001）/ 反向 WS（自己开端口等人连） |
| QQ 开放平台 | `qq_open_plat.py` | 只有 @ 机器人才进链路 | HTTP 轮询 + 被动回复 |

上层（注意力、流水线、记忆、工具、B 站、图库）对这两条通道**完全无感知**，差异全部收敛在 `qq_connection.py` 的抽象接口和两个连接器里。

匹配的后端：NapCat / LLBot（LuckyLilliaBot）/ LLOneBot / go-cqhttp / Lagrange，只要说 OneBot v11 就认。

### 2.2 功能面

- 多群注意力竞争：决定「这一句该不该接」，不是每条都回
- 疲劳系统：深夜和连续说话会变懒
- 回复缓冲：攒一攒再说，不是秒回
- 文件内容读取：文本直读，图片走 VLM 描述
- 多模态输出：文字 / 表情包 / 图片 / 语音 / 戳一戳 / 引用
- 本地图库：收图带描述、md5 去重、可检索、一键换头像
- QQ 表情索引：商城表情和收藏表情两条路，认一次永久 0 成本
- B 站见闻：定时刷视频攒素材；别人发链接能真打开看弹幕字幕
- 断线补消息：重连后和 QQ 服务端对账，漏的补回来
- 私聊主动搭话：久不说话主动起话头
- 定时搭话：主人排好「几点、说什么」，到点她自己组织语言发
- 私聊↔群 记忆打通：两个独立开关（最近对话 / 长期记忆），只对插件名册里的 admin 生效，**详见 19**
- 群未读汇总：给人看的，跟补消息是两回事

### 2.3 依赖

**没有第三方依赖**。`bilibili_api` / `aiohttp` / `httpx` / `PIL` / `qrcode` / `certifi` / `bs4` / `lxml` 宿主环境里都有。
唯一缺的是 `curl_cffi`，所以网络请求一律走 aiohttp（且 `timeout=` 只能传 `ClientTimeout` 对象，传 float 会 ValueError）。

`plugin.toml` 不声明 `python_requirements`，靠宿主自带。

---

## 3. 目录结构（62 个 py，35,394 行）

```
src/qqbetter_auto_reply/
├── plugin.toml              清单：id/版本/入口/UI 面板/描述
├── __init__.py              2,262 行　插件类本体 + 52 个 entry + startup/shutdown
├── config_store.py            284 行　配置读写 + 89 个默认值
├── config.example.toml              配置示例（给人看的）
├── pyproject.toml / ruff.toml       发布用（市场 CI 会读）
│
├── 【连接层】
├── qq_connection.py           142 行　连接抽象基类
├── qq_client.py             2,431 行　OneBot v11 客户端
├── qq_open_plat.py          1,058 行　QQ 开放平台连接器
├── napcat_service.py          280 行　拉起/关闭 NapCat 进程
├── message_chain.py           352 行　消息链模型（对齐 KiraAI 的递归结构）
│
├── 【消息进入】
├── message_dispatcher.py    1,264 行　主循环、分支判定、图/表情预处理
├── handler_runtime_service.py   34 行　handler task 登记与回收
├── permission.py              209 行　用户权限名册（admin/trusted/normal）
├── group_permission.py        122 行　群权限
├── display_name_service.py    101 行　群名片解析与缓存
├── targets.py                  54 行　群/人目标解析 mixin
│
├── 【回复流水线】
├── reply_pipeline.py          680 行　五阶段编排
├── reply_decision_node.py     102 行　① 要不要回、怎么回
├── reply_context_node.py      563 行　② 拼上下文（记忆/提示词/工具）
├── reply_model_node.py         64 行　③ 调模型
├── reply_postprocess_node.py  635 行　④ 清洗输出、解析多模态块
├── reply_delivery_node.py     340 行　⑤ 真发出去
├── reply_relay_node.py         51 行　转发路径（不生成，只搬运）
├── pipeline_models.py         316 行　五阶段的数据模型
├── relay_service.py           149 行　转发执行
│
├── 【生成与节奏】
├── reply_generation_service.py 1,634 行　模型调用 + 工具挂载与分发
├── reply_buffer_service.py      980 行　回复缓冲与发送延迟
├── feedback_classifier.py        97 行　回复反馈分类
├── attention_service.py         895 行　注意力周期模型
├── attention_gate_service.py    578 行　注意力门控 + 群主动循环
├── fatigue_service.py           154 行　疲劳三维模型
├── proactive_private_service.py 723 行　私聊主动搭话
├── scheduled_greeting_service.py 323 行　定时搭话
│
├── 【会话与记忆】
├── session_memory_service.py  2,789 行　会话历史、长期记忆写入与召回（最大）
├── session_instruction_service.py 1,445 行　提示词分层装配
├── session_bootstrap_service.py 317 行　会话首次初始化
├── session_runtime_service.py   262 行　会话运行时状态
├── session.py                   125 行　会话 mixin
├── memory_bridge.py             557 行　与宿主记忆服务（48912）的桥
├── memory_tool_service.py       395 行　recall_memory 工具
├── summary_builder.py            62 行　上下文摘要
│
├── 【提示词】
├── prompting.py                 371 行　提示词 mixin
├── prompt_builder.py            137 行　提示词构建
├── prompt_fragment_templates.py 274 行　提示词片段（两条通道各自的文本）
├── scene_prompt_templates.py    128 行　场景提示词
│
├── 【内容能力】
├── vision_service.py            428 行　识图（VLM 描述 + 双键缓存）
├── gallery_service.py           797 行　本地图库
├── sticker_service.py           614 行　表情包库
├── face_index.py                809 行　QQ 表情索引（只存指针）
├── voice_reply_service.py       492 行　语音回复
├── bilibili_client.py           585 行　B 站访问层
├── bilibili_browse_service.py 1,382 行　定时刷视频攒见闻
├── bilibili_video.py            383 行　看指定视频
├── bilibili_login_service.py    299 行　扫码登录
├── open_url_service.py          683 行　open_url 工具
├── search_tool_service.py       492 行　search_web 工具
├── emoji.json                        表情名映射（给提示词用的词表）
│
├── 【未读与补消息】
├── backlog_store.py             420 行　群未读存储
├── backlog_service.py           228 行　未读汇总（给人看）
├── backlog_models.py             74 行　未读数据模型
├── catchup_service.py           720 行　断线补消息（给模型）
│
├── 【运行时与 UI】
├── runtime_ops_service.py       422 行　启停自动回复、发主动消息
├── runtime_service.py           326 行　运行时状态聚合
├── dashboard_service.py         818 行　UI 状态聚合 + entry 转发
├── settings_service.py        1,612 行　配置保存 + 运行时应用
├── ui_api.py                     10 行　UI API 薄封装
│
├── static/                  前端（8 个文件，约 290 KB）
│   ├── index.html               4.4 KB　入口，按模式跳 napcat / open_platform
│   ├── napcat.html            141.5 KB　主面板（配置引导都在这）
│   ├── open_platform.html      59.9 KB　开放平台面板
│   ├── script.js               62.7 KB　通用逻辑
│   ├── style.css               15.9 KB
│   ├── i18n.js                  4.2 KB　前端 i18n 加载
│   ├── ui-sse.js                3.9 KB　日志/状态推送
│   ├── old.html                19.1 KB　旧版（留着，未挂路由）
│   ├── Tutorial/                      教程页
│   └── cache/                         二维码缓存（打包排除）
│
├── i18n/                    9 种语言，扁平键，每种 728 个键
│   ├── zh-CN.json  zh-TW.json  en.json  en-US.json
│   └── ja.json  ko.json  es.json  pt.json  ru.json
│
└── tests/test_smoke.py      仓库里只有冒烟测试，正式测试在 _probe/
```

统计口径：`find . -name "*.py" | wc -l` = 62，`wc -l` 合计 35,394 行（含 `__init__.py` 2,262）。

---

## 4. 运行架构

### 4.1 分了几层

```
                    ┌──────────── 连接层 ────────────┐
  LLBot/NapCat ──WS─▶ qq_client（OneBot v11）         │
  QQ 开放平台 ──HTTP─▶ qq_open_plat                    │
                    └──────────────┬─────────────────┘
                                   │ 统一 dict 格式
                    ┌──────────────▼──────────────────────┐
                    │ message_dispatcher.process_messages  │ ← 常驻主循环
                    │  收 → 打接收边界快照 → 起 task        │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │ handle_message / _group / _private   │
                    │  图/表情预处理、权限、注意力闸、缓冲   │
                    └──────────────┬──────────────────────┘
                                   │ QQReplyRequest
                    ┌──────────────▼──────────────────────┐
                    │ reply_pipeline.run()                 │
                    │  decision → context → model →        │
                    │  postprocess → delivery              │
                    └──────────────┬──────────────────────┘
                                   ▼
                               QQ 服务端
```

那三十来个 `*Service` 都是被上面几层调用的，**彼此之间不直接通信**，一律通过 `self.plugin.<service>` 拿引用。这条规矩保证没有隐式依赖环。

### 4.2 启动顺序

`__init__.py` 的 `@lifecycle(id="startup")`，按实际执行顺序：

1. 确保 `business_config.json` 存在（不存在就建）
2. 载入配置、重建权限管理器、应用运行时配置
3. `attention_service.load_cached_state()` —— 注意力分数跨重启保留
4. `catchup_service.start()` —— **必须在连接之前**，游标要先就位，否则重连瞬间进来的消息会被当成"没见过"
5. `vision_service.start()` —— 只读磁盘，不等连接
6. `face_index.start()`
7. `sticker_service.start()`
8. `gallery_service.start()`
9. `attention_gate_service.start_proactive_loop()` —— 群主动发言循环
10. `bilibili_browse_service.start()` —— 见闻循环，不等连接
11. 注册静态 UI（`cache_control="no-cache"`）
12. 起三个后台任务（见 4.3）

**连接不在 startup 里建立**，要等用户点「启动自动回复」。这个区分很重要：**插件进程活着 ≠ 连着 QQ**。

### 4.3 常驻循环与后台任务

六个循环，全部由 `startup` 或 `start_auto_reply` 拉起，`shutdown` / `stop_runtime` 负责停。

| 循环 | 起于 | 周期 | 干什么 |
|---|---|---|---|
| 消息主循环 `_process_messages` | `start_auto_reply` | 事件驱动 | 从 WS 队列取消息，起 handler task |
| 注意力衰减 `start_decay_loop` | `start_auto_reply` | 秒级 | 每个群的注意力分数随时间回落 |
| 群主动发言 `start_proactive_loop` | startup + `start_auto_reply` | 判定周期 | 冷场时自己找话头（跟"回别人"是两条路） |
| 私聊主动搭话 `proactive_private_service` | `start_auto_reply` | 60 s | 久不说话主动起话头 |
| 定时搭话 `scheduled_greeting_service` | `start_auto_reply` | 30 s | 到点发排好的早安晚安 |
| B 站见闻 `bilibili_browse_service` | startup | 60 min | 定时刷视频攒素材 |

另外三个后台任务（一次或低频）：

| 任务 | 作用 |
|---|---|
| `_trust_migration_task` | 把旧的 `speaker_trust_profiles` 迁到宿主全局信赖度池，带退避重试 |
| `_session_housekeeping_loop` | 清理闲置会话、落盘记忆 |
| `_purge_old_reviewed_loop` | 清理已读的旧未读记录 |

> 为什么大部分循环挂在 `start_auto_reply` 而不是 `startup`：这些循环要连上 QQ 才有意义，挂 startup 会在没连接时也空转。B 站和注意力衰减是例外（不需要连接）。

### 4.4 关掉的顺序

`shutdown` 必须按依赖倒序，顺序写错会丢记忆：

```
停群主动循环 → 停 B 站 → 停 vision / faces / sticker → gallery.flush()
→ 停自动回复（含断连、停 NapCat）→ flush 所有记忆会话
→ 关注意力 → 取消后台任务
```

---

## 5. 一条群消息的完整旅程

拿"群里有人说了一句话"举例，每一步标出在哪个文件。

1. **收到**　`qq_client.receive_message()` 从 WS 队列取一条 dict。
2. **打接收边界快照**　`message_dispatcher.process_messages()` 在起 task **之前**往消息上挂快照字段：
   `_group_memory_at_receipt`、`_member_memory_at_receipt`、`_participant_memory_at_receipt`、
   `_speaker_channel_at_receipt`、`_group_speaker_permission_level_at_receipt`。
   **为什么要快照**：handler 可能因为并发闸排队几秒，处理时再读配置，会把"关记忆时代收到的消息"标成"已授权"。
3. **起 task**　交给 `handler_runtime_service.track_handler_task()` 跟踪，主循环立刻回去收下一条。并发上限 `max_concurrent_messages`（默认 3）。
4. **分支**　`handle_message()` 分流：戳一戳通知 / 好友请求 / 群消息 / 私聊。
5. **群消息预处理**（`handle_group_message`）：
   - 表情包观察（`sticker_lab` 那条线，先于未读记录，因为纯图消息会被未读丢掉）
   - 未读记录 `backlog_service.record_message()`
   - 图片描述注入 `_inject_image_descriptions()`：把 `<image>` 段换成"图片上写着……"这种模型能读的文本
   - 表情段转文本 `_sticker_text()`
6. **该不该回**　过三道闸：
   - 权限（`permission.py`）：这个人在不在名册、什么档
   - 注意力（`attention_gate_service`）：多群竞争，这个群热度够不够
   - 疲劳（`fatigue_service`）：她困不困
   不过闸就 `action="ignore"`，只留 trace，不发。
7. **回复缓冲**（`reply_buffer_service`）：攒一小会儿，可能合并多条一起回，也可能决定"算了不回"。
8. **进流水线**　`reply_pipeline.run()`，五阶段见第 6 节。
9. **发送**　`reply_delivery_node.deliver()`：解析 `<msg>` 块，按块发文字 / 表情 / 图片 / 语音 / 戳一戳 / 引用。
10. **落记忆**　`session_memory_service`：这一轮写进会话历史，按记忆政策决定是否写进长期记忆。

### 5.1 私聊有什么不同

走 `handle_private_message()`：

| 环节 | 群聊 | 私聊 |
|---|---|---|
| 注意力竞争 | 有 | **没有**（一对一没有焦点可争） |
| 未读汇总 | 有 | 有（只给主人看） |
| 群名片解析 | 有 | 跳过 |
| 记忆域 | `group:<gid>` | `private:<uid>`（admin 走 legacy 主线） |
| 主动搭话 | 群主动循环 | `proactive_private_service` + `scheduled_greeting_service` |

### 5.2 开放平台有什么不同

走 `qq_open_plat.py` 的消息拉取，只有 **@ 机器人**的才进链路。没有注意力，也没有 B 站 / 图库 / 主动搭话三个面板 —— 靠 @ 触发的通道加这些只会误导。

### 5.3 合成轮（synthetic turn）

有些轮不是真人在说话，而是插件自己造的一轮（主动搭话、补消息注入、群主动发言）。这类轮由 `is_synthetic_source()` 判定。

合成轮没有"真实发言人"，所以：

- 不能拿它去盖"发言人是 admin"的章
- 不能靠它触发群内认主人
- 记忆写入要标 ephemeral / `persist=False`

**写任何涉及身份判定的新代码，先问一句：这轮到底有没有真人？**

---

## 6. 回复流水线五阶段

入口 `reply_pipeline.py` 的 `QQReplyPipelineRunner.run(request)`。

| 阶段 | 文件（行数） | 输入 | 输出 | 内部做什么 | 失败怎么办 |
|---|---|---|---|---|---|
| ① decision | `reply_decision_node.py`（102） | `QQReplyRequest` | `QQReplyDecision` | 判 `ignore` / `relay` / `reply` | 判不了就 ignore，不硬回 |
| ② context | `reply_context_node.py`（563） | request + decision | `QQReplyContext` | 拼系统提示词、拉记忆、准备工具、装身份段 | 记忆拉不到降级为空段，仍继续 |
| ③ model | `reply_model_node.py`（64） | context | `QQModelResult` | 调宿主主模型（可超时、可回退） | 超时按无回复处理，留 trace |
| ④ postprocess | `reply_postprocess_node.py`（635） | context + model_result | `QQReplyOutcome` | 剥内部标记（DSML 等）、解析多模态块、兜底话术 | 解析不出内容就用兜底 |
| ⑤ delivery | `reply_delivery_node.py`（340） | outcome + plan | `QQDeliveryResult` | 真发，记录 `message_id` | 发失败重试一次，仍失败记 trace |

每个阶段都往 `outcome.traces` 追加一条 `QQPipelineStageTrace`，带 `stage` / `status` / `metadata`。

> **排查"为什么不回"，第一时间看 traces，比翻日志快。** 看 `stage` 停在哪、`status` 是什么。

### 6.1 relay 是另一条路

`relay` 不生成内容，只把别人的消息搬到另一个目标（`reply_relay_node` + `relay_service`），走完就结束。概率由 `normal_relay_probability` / `open_reply_probability` 控制。

### 6.2 postprocess 里最脏的一段

模型输出格式它不一定遵守，所以这一段按"它可能乱来"写：

- `<msg>` 块可能没闭合
- 一个 `<msg>` 里可能有多个 `<text>`（**`find` 只取第一个，必须 `findall`**）
- DSML 工具调用标记有一堆变体，要统一剥
- 空回复 / 只回了标点 / 只回了个表情，都要能兜

---

## 7. 横切机制

这几个东西不属任何阶段，但决定"她像不像人"，**改动风险最高**。

### 7.1 注意力竞争

`attention_service.py`（周期模型）+ `attention_gate_service.py`（门控）。

每个群一个注意力分数：随消息涨、随时间衰减、发言要消耗、夺冠后有一小段蜜月期。

```
基础增速   attention_base_rise_rate         0.02
消息加成   attention_message_boost          0.15
关键词加成 attention_keyword_boost_ratio    1.8
蜜月期     attention_honeymoon_seconds      60
回落窗口   attention_fall_seconds           30
回落速率   attention_fall_rate              0.015
发言消耗   attention_consume_ratio          0.10
```

三个阈值要分清，混用会出怪现象：

| 参数 | 默认 | 含义 |
|---|---|---|
| `group_attention_max_score` | 10.0 | 分数上限 |
| `group_attention_focus_threshold` | 4.0 | **够不够格插话** |
| `group_attention_focus_send_threshold` | 2.0 | **够不够格发送** |
| `group_attention_min_threshold` | 1.0 | 低于这个完全不看 |

> 发送线为什么比焦点线低：焦点群回一条就跌破焦点线，如果发送门控也用焦点线，回完第一句之后就再也发不出第二句。

### 7.2 疲劳

`fatigue_service.py`，三维：

| 维度 | 参数 | 默认 |
|---|---|---|
| 昼夜节律 | `fatigue_circadian_peak_hour` / `low_hour` | 15 点最清醒 / 3 点最困 |
| 连续说话消耗 | `fatigue_session_per_reply` | 每回复一次 +5 |
| 清醒时长 | `fatigue_awake_idle_timeout` | 空闲 10 s 回睡眠 |

疲劳高了她会少说、短说。`fatigue_enabled` 关掉就是恒定状态。

### 7.3 回复缓冲

`reply_buffer_service.py`（980 行），LLM 驱动的"要不要等等再说"：

- 能合并连续多条消息一起回
- 能在开口前改主意（决定不回）
- 能控制说话节奏（不是所有话一次倒出来）

**逻辑密度极高，改之前先读懂。** 这个文件的改动最容易引出"她怎么突然不理人 / 突然话痨"。

### 7.4 并发与锁

| 机制 | 参数/位置 | 说明 |
|---|---|---|
| 全局并发闸 | `max_concurrent_messages`（3） | 同时处理几条 |
| 会话串行 | 每会话一把锁 | 同一会话绝不并发 |
| 停止宽限 | `handler_shutdown_timeout_seconds`（10 s） | 停止时等 handler 多久 |
| 任务跟踪 | `handler_runtime_service` | 统一登记、统一取消 |
| 模型超时 | `ai_connect_timeout_seconds`（10）/ `ai_turn_timeout_seconds`（60） | 连接超时 / 单轮超时 |

### 7.5 权限三档

`permission.py` 只有三档，写在 `VALID_LEVELS = {"admin", "trusted", "normal"}`：

| 档 | 含义 | 效果 |
|---|---|---|
| `admin` | 主人 | 私聊走 legacy 主线记忆；群里发言算主人；跨域记忆只对他生效 |
| `trusted` | 信任 | 正常聊，不进主人记忆库 |
| `normal` | 普通 | 按概率回话 |
| （不在名册） | 陌生人 | 查出来是 `none`，基本不回 |

**「认主人」只认这份名册**，跟 QQ 群里的群主 / 群管理员身份**完全无关**。陌生人哪怕是某群群主，名册里查不到就不是主人。

名册存在 `business_config.json` 的 `trusted_users`，格式 `[{"qq":"123","level":"admin","nickname":"小明"}]`。

### 7.6 记忆政策快照

见 5 节第 2 步。规矩一句话：

> **任何"读当前配置做判定"的新代码，先想一遍：这个判定该用处理时刻的配置，还是接收时刻的？记忆和权限相关的，一律用接收时刻。**

原因：handler 排队时配置可能被改（用户在另一页面关了开关），用处理时刻读会把边界搞乱。

### 7.7 群聊私聊记忆打通（摘要）

界面叫「私聊↔群 记忆打通」，代码叫 `cross_domain`。**完整说明单独放在第 19 章**——
这是唯一一处把私聊内容搬进群聊的逻辑，牵涉隐私，值得单独立章。这里只留个索引：

- 两个独立开关：`cross_domain_owner_memory`（最近对话）/ `cross_domain_owner_longterm_memory`（长期记忆），默认都关。
- 生效条件**四个**：开关开 + 是群聊轮 + 发言人是名册里的 admin + 不是合成轮。
- 单向：私聊 → 群，反向永久不通。
- 可撤销：装配 → `reply_context_node._strip_owner_private_if_revoked()`
  （**一个函数，靠 `setting_key` 参数服务两个开关**）→ `reply_generation_service` 生成前复检。
- 0.8.6 修过一次根因：群消息原先带的是**群等级**不是发言人档位，注入段从来没触发过。

去 19 章看：版本演进、四个配置项、十几个文件逐一把关、两段提示词原文、缓存与撤销、
真机验证脚本、排查清单、已知限制。

---

## 8. 模块逐个详解（62 个）

按层分组。每个文件给三样东西：**管什么**、**关键入口**、**改它的风险**。

风险标记说明：🟢 随便改　🟡 改前读懂　🔴 别碰，除非你打算重做这一块。

### 8.1 骨架与配置（2 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `__init__.py` | 2,262 | 插件类本体：所有 service 的实例化、52 个 entry、startup/shutdown、消息主循环入口 | `QQAutoReplyPlugin` | 🔴 改动影响全局装配顺序 |
| `config_store.py` | 284 | 配置文件的读写与 89 个默认值。**所有默认值只在这一个地方定义** | `QQAutoReplyConfigStore.default_config()` / `load()` / `exists()` | 🟡 加字段必须同时改四处（见 11.3） |

`__init__.py` 里除了类本体，还有三块值得单独知道：

- `_create_business_config()` —— 首次启动写默认配置
- `_process_messages()` —— 消息主循环体，在 `runtime_ops_service.start_auto_reply` 里被 `create_task` 拉起
- 三个后台任务循环：`_trust_migration_loop` / `_session_housekeeping_loop` / `_purge_old_reviewed_loop`

### 8.2 连接与协议（5 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `qq_connection.py` | 142 | 抽象基类。定义 `connect` / `disconnect` / `call_action` / `receive_message` / 各类 send_* | `QQConnectionBase` | 🟡 两条通道的契约，加方法两边都要实现 |
| `qq_client.py` | 2,431 | OneBot v11 客户端：正向/反向 WS、事件分发、`call_action` 拆信封、发图发语音、`normalize_image_locator` | `QQClient` | 🔴 协议层，最不该乱动 |
| `qq_open_plat.py` | 1,058 | QQ 开放平台连接器：鉴权、消息拉取、被动回复、Ark 卡片 | `QQOpenPlatformConnection` | 🔴 同上 |
| `napcat_service.py` | 280 | 拉起/关闭 NapCat 进程、探测 WebUI 地址、二维码落地 | `QQNapcatService` | 🟡 涉及外部进程 |
| `message_chain.py` | 352 | 消息链递归结构（`Text` / `Image` / `At` / `Reply` …），两种协议的段格式归一到这里 | `MessageChain`、`MessageElement` | 🟡 改结构会牵动所有发送路径 |

`qq_client.py` 里两个必须知道的细节：

- **`normalize_image_locator()`**：NapCat 只认 `file:///Q:/...`（三斜杠 + 正斜杠）。裸 Windows 路径发出去，OneBot **不回任何响应**、卡到超时 —— 现象是"发了没反应、日志一片安静"。
- **`call_action()` 拆信封**：OneBot 返回 `{"status":"ok","retcode":0,"data":...}`，不同后端对 `data` 的包装不一致，这里统一拆。

### 8.3 消息进入（6 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `message_dispatcher.py` | 1,264 | 主循环、分支判定、接收边界快照、图/表情预处理、开放平台身份认领 | `QQMessageDispatcher.process_messages()` / `handle_group_message()` | 🔴 全链路起点 |
| `handler_runtime_service.py` | 34 | handler task 的登记与回收，极小但关键 | `track_handler_task()` | 🟡 停止时统一取消靠它 |
| `permission.py` | 209 | 用户权限名册：`admin`/`trusted`/`normal`，昵称校验 | `PermissionManager.get_permission_level()` | 🟡 认主人的唯一判据 |
| `group_permission.py` | 122 | 群档位、群的转发/开口概率 | `GroupPermissionManager` | 🟢 |
| `display_name_service.py` | 101 | 群名片解析与缓存（避免每条消息都查一次） | `group_display_name()` | 🟢 |
| `targets.py` | 54 | 群/人目标解析 mixin，entry 里到处用 | `QQAutoReplyTargetsMixin` | 🟢 |

`message_dispatcher.py` 里最容易被忽略的一段：**接收边界快照**。它在起 task 之前把配置值盖在消息 dict 上（`_*_at_receipt`）。加新配置项且要"按接收时刻判定"的，就在这里补一个字段。

### 8.4 回复流水线（9 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `reply_pipeline.py` | 680 | 五阶段编排、trace 汇总、群内 admin 覆盖、缓冲与重试 | `QQReplyPipelineRunner.run()` | 🔴 |
| `reply_decision_node.py` | 102 | ① 判 `ignore` / `relay` / `reply` | `decide()` | 🟡 |
| `reply_context_node.py` | 563 | ② 拼系统提示词、拉记忆、装身份段、撤销检查 | `build()` | 🔴 提示词都在这成形 |
| `reply_model_node.py` | 64 | ③ 调宿主主模型 | `generate()` | 🟢 薄 |
| `reply_postprocess_node.py` | 635 | ④ 剥 DSML、解析多模态块、兜底 | `finalize()` / `strip_dsml()` / `build_delivery_plan()` | 🟡 解析逻辑脏但独立 |
| `reply_delivery_node.py` | 340 | ⑤ 真发，按块调 send_* | `deliver()` | 🟡 |
| `reply_relay_node.py` | 51 | 转发路径的 plan / execute | `build_plan()` | 🟢 |
| `relay_service.py` | 149 | 转发执行 + `send_backlog_reply_direct` | `QQRelayService` | 🟢 |
| `pipeline_models.py` | 316 | 五阶段的数据模型（6 个 dataclass） | `QQReplyRequest` / `QQReplyContext` / `QQInstructionBundle` … | 🟡 加字段要全链路照顾 |

`pipeline_models.py` 里的 6 个模型：

| 模型 | 什么时候产生 | 谁读 |
|---|---|---|
| `QQReplyRequest` | 消息进入时 | 全链路 |
| `QQReplyDecision` | decision 阶段 | context / 后续 |
| `QQInstructionBundle` | 提示词装配完 | context node |
| `QQReplyContext` | context 阶段 | model / postprocess / delivery |
| `QQModelResult` | model 阶段 | postprocess |
| `QQReplyOutcome` | postprocess 阶段 | delivery / 记忆 |
| `QQPipelineStageTrace` | 每阶段追加 | 调试面板 |

> `QQReplyContext` 和 `QQInstructionBundle` 都带 `owner_private_section` / `owner_longterm_section` 两个原文字段 —— 那是记忆打通的撤销凭据（见 19.10）。

### 8.5 生成（3 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `reply_generation_service.py` | 1,634 | 模型调用、**工具挂载与分发**、工具超时预算、会话主调用 | `run_primary_session_call()` / `_arm_turn_tools()` | 🔴 第 9 节的九个头工具都在这挂上 |
| `reply_buffer_service.py` | 980 | 缓冲、合并、发送延迟、改主意 | `pre_buffer()` / `schedule_reply()` | 🔴 逻辑密度最高 |
| `feedback_classifier.py` | 97 | 回复反馈分类（她这句对方是满意还是烦） | `classify()` | 🟢 |

`reply_generation_service.py` 里有一个**极易踩的坑**：客户端只接一个 `set_tool_call_handler`，挂两次后者覆盖前者。所以九个头工具共用一个 `_dispatch_tool_call` 闭包，按 `tool_call.name` 分流。**加新工具必须加进这个 if 链**，否则工具挂上了但永远走不到。

### 8.6 行为与节奏（5 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `attention_service.py` | 895 | 注意力周期模型：涨、衰减、蜜月、消耗 | `update_on_message()` / `recompute_score()` | 🟡 参数敏感 |
| `attention_gate_service.py` | 578 | 门控判定、焦点切换、回溯补回、群主动循环 | `evaluate()` / `start_proactive_loop()` | 🔴 她"回不回"的总闸 |
| `fatigue_service.py` | 154 | 三维疲劳 | `calculate_fatigue()` | 🟢 |
| `proactive_private_service.py` | 723 | 私聊主动搭话：闲置判定、间隔、日限、静默时段、没回音退避 | `try_proactive()` | 🟡 |
| `scheduled_greeting_service.py` | 323 | 定时搭话：到点、去重、宽限窗、生成 | `tick()` / `_fire_entry()` | 🟢 新写的，结构简单 |

`scheduled_greeting_service.py` 的几个设计点（0.8.7 新增）：

- 条目形态 `[{"time": "08:00", "instruction": "说早安", "target": "可选QQ"}]`
- **每条每天只发一次**，状态存 `scheduled_greeting_state.json`
- 到点时没连 QQ → 宽限窗（默认 10 分钟）内重试，过期不补（早上的早安中午补发会吓人）
- 提示词里注入当前日期、星期、时刻，硬性要求"不报时、不说按设定"

### 8.7 会话与记忆（8 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `session_memory_service.py` | 2,789 | 会话历史、长期记忆写入与召回、记忆域（legacy / scoped / participant）、结算、清理 | `QQSessionMemoryService` | 🔴 最大最复杂 |
| `session_instruction_service.py` | 1,445 | 提示词分层装配，`{sticker_catalog}` 之类占位符替换，身份段、跨域段 | `build_session_instructions()` | 🔴 所有提示词的源头 |
| `session_bootstrap_service.py` | 317 | 会话首次初始化（建 session、prime 状态） | `ensure_generation_session()` | 🟡 |
| `session_runtime_service.py` | 262 | 会话运行时状态、每会话锁、key 构造 | `build_generation_session_key()` / `run_with_session_lock()` | 🟡 |
| `session.py` | 125 | 会话 mixin | `QQAutoReplySessionMixin` | 🟢 |
| `memory_bridge.py` | 557 | 与宿主记忆服务（端口 48912）通信：bootstrap 拉取、scope 判定 | `fetch_bootstrap_memory()` / `fetch_scoped_bootstrap_memory()` | 🟡 跨进程契约 |
| `memory_tool_service.py` | 395 | `recall_memory` 工具的构造与执行 | `build_recall_tool_definition()` | 🟢 |
| `summary_builder.py` | 62 | 上下文摘要 | `build_group_summary()` | 🟢 |

`session_memory_service.py` 有 2,789 行，是全项目最大的一块。它管的事：

- 会话历史（内存 + 落盘）
- 三种记忆域：`legacy`（主人私聊主线）/ `scoped`（群、普通参与者）/ `participant`（群成员独立域）
- 未授权边界 `nonconsent_history_end`：开关 OFF 时代的行，翻 ON 后绝不回溯入库
- 结算（digest）与清理
- 跨重启恢复（`_HistoryBoundary`）

**改这里的唯一建议：先读 4 小时，再动手。**

### 8.8 提示词（4 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `prompting.py` | 371 | 提示词 mixin，给插件类用 | `QQAutoReplyPromptingMixin` | 🟡 |
| `prompt_builder.py` | 137 | user_title / 角色卡字段 / 记忆开关判定 / prompt_message | `QQPromptBuilder` | 🟡 |
| `prompt_fragment_templates.py` | 274 | 提示词片段模板：两条通道各自的文本、禁用项 | 模块常量 | 🟢 改文案常在这 |
| `scene_prompt_templates.py` | 128 | 场景提示词（不同情境不同口吻） | 模块常量 | 🟢 |

分层优先级（重要）：**i18n bundle → 用户 `prompt_overrides` → 代码常量**。
也就是说，如果某个 key 在 i18n 里存在，改代码常量是不生效的。改文案前先 grep 一下 i18n。

### 8.9 内容能力（11 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `vision_service.py` | 428 | 识图：VLM 描述 + 双键缓存（URL 键 / 内容 sha 键），跨重启复用 | `describe()` / `peek()` | 🟡 花钱的地方 |
| `gallery_service.py` | 797 | 本地图库：入库、md5 去重、检索、目录搬迁、换头像 | `remember()` / `search()` | 🟡 |
| `sticker_service.py` | 614 | 表情包库：上传、列表、按感觉检索、发送 | `save()` / `search()` / `send()` | 🟡 |
| `face_index.py` | 809 | QQ 表情索引：商城表情 + 收藏表情，只存指针不存文件 | `remember()` / `catalog_lines()` / `search()` | 🟡 |
| `voice_reply_service.py` | 492 | 语音回复（TTS 供应商来自宿主） | `synthesize_reply_voice_*()` | 🟢 |
| `bilibili_client.py` | 585 | B 站访问层，所有接口收敛在这 | `search_videos()` / `video_detail()` / `get_danmakus()` | 🟡 |
| `bilibili_browse_service.py` | 1,382 | 定时刷视频攒见闻：打分、配额、深看、落盘 | `browse_once()` / `recent_notes()` | 🟡 |
| `bilibili_video.py` | 383 | 看指定视频（别人发链接时） | `watch()` | 🟢 |
| `bilibili_login_service.py` | 299 | 扫码登录 | `start()` / `check()` | 🟢 |
| `open_url_service.py` | 683 | `open_url` 工具：真抓网页、按轮计额度 | `execute_open()` | 🟡 |
| `search_tool_service.py` | 492 | `search_web` 工具：国内优先、过滤境外站 | `execute_search()` | 🟡 |

`vision_service.py` 的 `peek(locator, kind)` 是**只查缓存、绝不调模型**。用它把"已经以普通图片身份花过钱认过的图"复用到表情库，避免付第二遍钱。

`face_index.py` 有两条完全不同的路，别只做一条：

| 路 | 判据 | 主键 | 重发方式 |
|---|---|---|---|
| 商城表情 | 段类型 `market_face` / `mface` | `emoji_id[:12]` | 塞 `mface` 段，0 成本 |
| **收藏表情** | `type=image` 且 `subType=1` | 图片 md5 | 下载 → `file:///` |

### 8.10 未读与补消息（4 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `backlog_store.py` | 420 | 群未读的持久化（`backlog_state.json`） | `append_message()` / `purge_old_reviewed()` | 🟢 |
| `backlog_service.py` | 228 | 未读汇总、通知节流、标记已读 | `get_summary_payload()` | 🟢 |
| `backlog_models.py` | 74 | 未读数据模型 | `QQGroupBacklog` 等 | 🟢 |
| `catchup_service.py` | 720 | 断线补消息：游标对账、窗口限制、错峰注入 | `start()` / `on_connected()` / `catch_up()` | 🔴 游标搞错就重复/漏消息 |

**未读汇总 ≠ 断线补消息**，两件事：

| | 未读汇总 | 断线补消息 |
|---|---|---|
| 给谁看 | 人（主人） | 模型 |
| 数据源 | 本地记录的所有群消息 | 和 QQ 历史对账 |
| 目的 | "这段时间群里说了啥" | "漏掉的话要接上" |

`catchup_service.py` 的铁律：**游标只认 `message_seq`，拿不到就不推进，绝不拿时间戳凑。** 拿时间凑会错位，现象是重连后重复补或漏补。

### 8.11 运行时与 UI（5 个）

| 文件 | 行数 | 管什么 | 关键入口 | 风险 |
|---|---|---|---|---|
| `runtime_ops_service.py` | 422 | 启停自动回复、发主动消息、模式切换重连；内含 `QQProactiveMessageService`（主动发消息的唯一出口） | `start_auto_reply()` / `stop_runtime()` | 🔴 启停链 |
| `runtime_service.py` | 326 | 运行时状态聚合、pipeline trace 缓存、登录态刷新 | `build_runtime_status()` | 🟢 |
| `dashboard_service.py` | 818 | UI 状态聚合（一个 payload 给前端）、多数 entry 的转发层 | `build_dashboard_state()` / `save_settings()` | 🟡 save_settings 的第二层 |
| `settings_service.py` | 1,612 | 配置保存、校验钳制、运行时生效、落盘失败回滚、图库搬迁、保存后重连 | `_save_settings_locked()` / `_persist_with_consent_rollback()` | 🔴 最长的坑在这 |
| `ui_api.py` | 10 | UI API 薄封装 | — | 🟢 |

**`save_settings` 的三层结构**（加字段必踩）：

```
entry 签名（__init__.py 的 save_settings 参数表 + input_schema）
   → dashboard_service.save_settings(...)   ← 中间这层容易用 **_ 静默吃掉
      → settings_service._save_settings_locked(**kwargs)  ← 真正写
```

中间那层如果用 `**_` 接参数，新字段会被静默丢弃：保存返回成功、值没变、日志一片安静。**加配置项三处都要改**（第四处是 `config_store` 默认值，第五处是前端控件）。

---

## 9. 她手里的工具（9 个 ToolDefinition）

这是「工具」的第二层意思：**猫娘在生成回复时，可以主动调用的能力**。
不是给用户点的按钮，是她自己想不想用。

### 9.1 怎么挂上去的

全在 `reply_generation_service.py` 的 `_arm_turn_tools()`，**每一轮生成前重新挂一次**。

```
取 user_session.set_tools / set_tool_call_handler
  ↓ 没有这两个方法 → warning「[工具诊断] 挂载跳过」→ 本轮无工具
按条件构造 ToolDefinition 列表
  ↓ 每个构造都包 try/except，单个失败只 warning，不影响其它
set_tools(tools) + set_handler(_dispatch_tool_call)
  ↓
log「[工具诊断] 已挂载 N 个: xxx, yyy」
```

三个必须知道的机制：

1. **只有一个 handler**。客户端只接一个 `set_tool_call_handler`，挂两次后者覆盖前者。
   所以九个工具共用一个 `_dispatch_tool_call` 闭包，按 `tool_call.name` 分流。
   **加新工具必须同时加进那个 if 链**，否则挂上了永远走不到，现象是"模型调了但没反应"。
2. **单个工具坏了不影响整轮**。每个 `build_definition()` 都包了 try/except，失败只留一条 warning（如「search_web 工具构建失败（本轮不联网）」）。
3. **诊断日志是唯一的证据链**。三处：挂载跳过 / 已挂载 N 个 / 收到工具调用。
   如果"收到工具调用"从来不出现，问题在模型或宿主侧，不在执行侧。

### 9.2 九个工具总表

| # | 工具名 | 实现文件 | 挂载条件 | 参数 | 耗时 |
|---|---|---|---|---|---|
| 1 | `recall_memory` | `memory_tool_service.py` | **有记忆授权**（`use_memory_context`） | `query` / `time`（至少一个） | 快（本地/宿主记忆服务） |
| 2 | `search_web` | `search_tool_service.py` | 总是 | `query` / `max_results`（2–8，默认 5） | 中（1–3 s） |
| 3 | `open_url` | `open_url_service.py` | 总是 | `url` | 中（抓取，按轮计额度） |
| 4 | `bilibili_recent` | `bilibili_browse_service.py` | 总是 | `query` / `limit`（1–6，默认 3） | 极快（纯本地读） |
| 5 | `bilibili_watch` | `bilibili_video.py` | 总是 | `url`（链接 / BV / av / 带 p=） | **慢**（4–5 个请求，6–12 s） |
| 6 | `save_sticker` | `sticker_service.py` | 总是 | `note` / `count`（1–3，默认 1） | 快 |
| 7 | `send_sticker` | `sticker_service.py` | 总是 | `query`（感觉词） | 快 |
| 8 | `browse_gallery` | `gallery_service.py` | 总是 | `query` / `limit`（默认 8，最多 20） | 快 |
| 9 | `set_avatar` | `gallery_service.py` | 总是 | `id`（`g3` 或关键词） | 中（要上传到 QQ） |

记忆没开时挂 8 个，开了挂 9 个。日志里会写实际数量。

### 9.3 逐个详解

#### ① `recall_memory` —— 翻自己的记忆

| 项 | 内容 |
|---|---|
| 实现 | `memory_tool_service.py`，`build_recall_tool_definition()` / `execute_recall()` |
| 干什么 | 从长期记忆里检索跟当前话题有关的事，返回成文本给模型 |
| 什么时候用 | 对方提到"上次那个""你还记得吗"、或者话题明显指涉过去的事 |
| 参数 | `query`（检索词）、`time`（时间线索，如"上周"）。**两个都空则早退**返回"没有找到相关记忆" |
| 挂载条件 | `context.use_memory_context` 为真才挂。记忆授权关着时这个工具根本不存在 |
| 坑 | 工具描述是多语言的（`_loc()` 读 i18n），改文案要改 9 份 |
| 失败 | 抛异常时返回空结果文本，不中断本轮 |

> 注意区分：**`recall_memory` 是模型主动去查**，而 `cross_domain_owner_longterm_memory` 是**插件自动注入**。两个来源不同，别混。

#### ② `search_web` —— 联网搜

| 项 | 内容 |
|---|---|
| 实现 | `search_tool_service.py`，`build_definition()` / `execute_search()` |
| 干什么 | 搜索引擎检索，返回带真实 URL 的条目 |
| 什么时候用 | 想知道"现在的事"、找视频找新闻、要链接 |
| 参数 | `query`（保留中文原话，不翻译）、`max_results`（2–8，默认 5） |
| 特点 | **已优先国内站点**，境外站可能被过滤 |
| 挂载条件 | 服务存在就挂。**从记忆开关里解耦了** —— 记忆关了也能联网 |
| 坑 | 结果里的 URL 可以直接写进回复，但一定要真打开确认（`open_url`），别只看标题猜 |

#### ③ `open_url` —— 真打开一个链接

| 项 | 内容 |
|---|---|
| 实现 | `open_url_service.py`，`build_definition()` / `execute_open()` |
| 干什么 | 抓网页真实内容：标题、作者、简介、正文 |
| 什么时候用 | ① 对方发来链接，想知道里面是什么；② 自己要分享链接，先确认内容对得上 |
| 参数 | `url`（必须带 http/https） |
| 额度 | **按轮计**，每轮挂载时 `reset_turn_budget()` 清零。不清的话聊几轮就再也开不了链接 |
| 坑 | 对 B 站视频只能拿到标题，看不到内容 → 视频必须用 `bilibili_watch` |
| 失败 | 打不开就直说，**不允许编造页面内容**（提示词里硬写） |

#### ④ `bilibili_recent` —— 回想自己刷过什么

| 项 | 内容 |
|---|---|
| 实现 | `bilibili_browse_service.py`，`build_definition()` |
| 干什么 | 从自己的见闻库里翻出最近刷的视频（标题、UP、弹幕原话、**能发出去的链接**） |
| 什么时候用 | 别人问"你最近看什么"、聊到视频/梗、想找个话头 |
| 参数 | `query`（想看什么相关，可空）、`limit`（1–6，默认 3） |
| 特点 | **纯本地读**，见闻库早就落盘了，零网络开销，所以不算进超时预算 |
| 关键 | 对方要"你刚才说的那个视频"的链接时，**必须用它查出来**，绝不能让模型凭记忆写 BV 号（会发错视频） |

#### ⑤ `bilibili_watch` —— 真看一条视频

| 项 | 内容 |
|---|---|
| 实现 | `bilibili_video.py`，`build_definition()` / `execute_watch()` / `watch()` |
| 干什么 | 打开指定视频，取标题、UP 主、简介、标签、**观众弹幕、字幕原文** |
| 什么时候用 | 对方发来 B 站链接（`bilibili.com/video/...`、`b23.tv/...`、裸 BV 号）**必须用这个** |
| 参数 | `url`（链接 / BV / av，带 `p=2` 也认） |
| 耗时 | 本轮最重的工具：4–5 个请求、节流后 6–12 s，**单独占一份超时预算** |
| 坑 | 未登录也能拿弹幕，但**字幕要登录**，且未登录时字幕返回空列表不报错 —— 别以为拿不到字幕是代码坏了 |
| 失败 | 看不了就说看不了，不许编视频内容 |

#### ⑥ `save_sticker` —— 收藏表情

| 项 | 内容 |
|---|---|
| 实现 | `sticker_service.py`，`build_save_definition()` |
| 干什么 | 把对方刚发的表情包收进自己的库，顺手起个名字 |
| 参数 | `note`（起的名字，如"翻白眼的猫"）、`count`（收最近几张 1–3，默认 1） |
| 特点 | 只记标识不存文件（商城表情），收多少都不占地方 |
| 与旧机制的关系 | 这是 0.8.x 新增的工具路线，替代原来"模型写 `<sticker>` 标签"的老路 |

#### ⑦ `send_sticker` —— 甩一张表情

| 项 | 内容 |
|---|---|
| 实现 | `sticker_service.py`，`build_send_definition()` |
| 干什么 | 按"感觉"检索表情包并立刻发出去 |
| 参数 | `query`（`无语`/`狂笑`/`摆烂` 这种感觉词） |
| 硬限制 | **一轮最多发一张**。这个在执行层拦（`already_sent` 标记），不靠模型自觉 —— 群里连着甩三张就是刷屏 |
| 失败 | 库里没合适的就别硬发，提示词里明说了不许自己编 ID 写 `<sticker>` 标签 |

#### ⑧ `browse_gallery` —— 翻自己的图库

| 项 | 内容 |
|---|---|
| 实现 | `gallery_service.py`，`build_browse_definition()` |
| 干什么 | 翻图库，每条带描述。想看有什么素材、找特定的图、挑一张当头像 |
| 参数 | `query`（如"猫耳""无语"）、`limit`（默认 8，最多 20） |
| 特点 | 不带关键字就返回最近收到的几张 |

#### ⑨ `set_avatar` —— 换 QQ 头像

| 项 | 内容 |
|---|---|
| 实现 | `gallery_service.py`，`build_avatar_definition()`，handler 在 `_build_avatar_handler()` |
| 干什么 | 把 QQ 头像换成图库里某一张 |
| 什么时候用 | **对方让你换头像、或发图说"你用这张"时必须调** —— 只在回复里说"换了"不算做到 |
| 参数 | `id`（图库编号 `g3`，或描述关键词） |
| 提示词里的引导 | 不确定编号先 `browse_gallery` 看一眼；换成功后用一句话告诉对方 |
| 设计意图 | 给"看"和"换"两个工具而不是一个，是因为**必须是她自己选**，不替她决定 |

### 9.4 老路：标签模式

工具之外还有一条老路 —— 模型在正文里写标签，postprocess 解析出来执行：

| 标签 | 作用 | 现状 |
|---|---|---|
| `<msg>...</msg>` | 一条消息，可含多个 `<text>` | 主路径，仍在用 |
| `<sticker>3</sticker>` | 发表情包（数字 ID） | 保留，但**提示词要求优先用 `send_sticker` 工具** |
| `<image>` / `<face>` / `<record>` | 图片 / QQ 表情 / 语音 | 仍在用 |

为什么保留两套：工具是"她决定用"，标签是"她顺手写"。有些模型对工具调用支持不稳（宿主换模型时会变），标签是兜底。

解析标签的坑：**`find` 只取第一个，必须 `findall`**。同一个 `<msg>` 里第二个 `<text>` 经常正好装着链接，用 `find` 会静默丢掉 —— 现象是"她说发了、对方说没收到"，两边都没错、日志全绿。

### 9.5 超时预算

| 情形 | 预算来源 |
|---|---|
| 无工具轮 | 按 `ai_turn_timeout_seconds`（60 s） |
| 挂了工具 | 在 60 s 基础上按工具追加 |
| `bilibili_watch` | 单独占一份（6–12 s 是它自己的，不算在别人的时间里） |
| `recall_memory` | 不计额外预算（宿主记忆服务在本地） |

`_arm_turn_tools()` 返回值就是"这轮到底有没有真挂上工具"，调用方据此决定用哪套预算。全挂不上就按无工具轮算，不会白等。

### 9.6 加一个新工具，七步

1. 在对应 service 里写 `build_xxx_definition()`，返回 `ToolDefinition(name=..., description=..., parameters={...}, metadata={"source": "qqbetter_auto_reply"})`
2. 写执行函数 `execute_xxx(arguments)`，**失败要 return 错误文本，不要抛**
3. 工具名定义成模块级常量（如 `XXX_TOOL_NAME = "xxx"`），别在字符串里散着写
4. `reply_generation_service._arm_turn_tools()` 里 `tools.append(...)` + 包 try/except + 记 warning
5. **同一个函数里的 `_dispatch_tool_call` 加一条 `if name == XXX_TOOL_NAME`** ← 最容易忘
6. 提示词里加"什么时候该用"的引导（`prompt_fragment_templates.py` 或 i18n）
7. 单测：构造假 session，断言 `set_tools` 收到的列表含新工具、dispatch 能路由到

---

## 10. 插件入口（52 个 entry）

这是「工具」的第三层意思：**宿主 / 前端页面 / 外部脚本能调用的入口**。
猫娘自己不用这些，是给人用的。

### 10.1 entry 是什么

```python
@plugin_entry(id="save_settings", description="...")
async def save_settings(self, **kwargs): ...
```

调用方式（不需要前端）：

```python
POST http://127.0.0.1:48916/runs
{"plugin_id": "qqbetter_auto_reply", "entry_id": "save_settings", "args": {...}}
→ 拿 run_id
GET  /runs/{run_id}          # 轮询状态
GET  /runs/{run_id}/export   # 取结果
```

返回统一是 `Ok({...})` / `Err(SdkError("..."))`，前端解包后拿 `data`。

> 宿主端口每次启动可能变，别在代码里写死。2026-11-08 实测是 48916。

### 10.2 全表（52 个）

#### A. 状态查询（只读，14 个）

| entry | 参数 | 返回/用途 |
|---|---|---|
| `get_dashboard_state` | — | **主入口**。一次性拿全部状态：runtime / settings / 群列表 / 图库 / 表情 / B 站 / 补消息 |
| `get_attention_state` | — | 各群注意力分数、当前焦点群、是否全局休眠 |
| `get_buffer_state` | — | 正在缓冲待发的回复（`pending` / `count`） |
| `get_catchup_state` | — | 每会话处理到哪一条（`message_seq`）、上次补消息结果与分项计数 |
| `get_bilibili_state` | — | 开关、登录态、见闻库条数、最近刷到的视频、下一轮时间 |
| `get_proactive_private_state` | — | 主动搭话：上次时间、当日计数、退避状态 |
| `get_backlog_summary` | — | 群未读汇总（给人看的） |
| `get_group_backlog_detail` | `group_id` | 单群未读明细 |
| `get_recent_logs` | `lines`（默认 100） | 内存日志缓冲 + NapCat 输出 |
| `get_user_profiles` | — | 提示词里用过的用户画像缓存 |
| `get_prompt_editor_state` | `mode` / `locale` | 提示词编辑器要的全部数据（含当前生效文本） |
| `get_group_prompts` | — | 所有群的自定义提示词映射 |
| `list_stickers` | — | 表情包清单 |
| `list_identity_claims` | — | 开放平台身份认领列表 |

#### B. 配置写入（7 个）

| entry | 参数 | 说明 |
|---|---|---|
| `save_settings` | **一长串**（见 11） | 唯一的总保存入口，前端所有面板都走它 |
| `init_config` | `guide_step_config_done` | 首次初始化配置 |
| `configure_onebot_nl` | `message` | **自然语言**配 OneBot（解析"设置地址为 ws://... token 为 abc"） |
| `save_prompt_override` | `locale` / `layer_id` / `text` | 存提示词覆盖（优先级高于代码常量） |
| `reset_prompt_override` | `locale` / `layer_id` | 撤销覆盖，回到代码/i18n 的文本 |
| `save_group_prompt` | `group_id` / `text` | 单群专属提示词（空串 = 删除） |
| `delete_group_prompt` | `group_id` | 删单群提示词 |

#### C. 启停与触发（9 个）

| entry | 参数 | 说明 |
|---|---|---|
| `start_auto_reply` | — | **真正连 QQ**：connect → 起衰减循环 → 起群主动循环 → 起主动私聊 → 起定时搭话 → 起消息主循环 |
| `stop_auto_reply` | — | 停连接与所有循环（插件进程还在） |
| `ensure_napcat` | — | 只拉起 NapCat 进程，**不连接**。失败会明确报 `NAPCAT_START_FAILED`，不给假象 |
| `trigger_catchup` | `force` | 立刻和 QQ 历史对账补消息 |
| `trigger_bilibili_browse` | — | 立刻刷一轮 B 站，不等定时 |
| `trigger_proactive_private` | `target` / `force` | 立刻让她主动搭一次话 |
| `send_private_proactive_message` | `target` / `message` | 指定内容发私聊（不生成，直接发） |
| `send_group_proactive_message` | `group_id` / `message` | 同上，发群 |
| `send_backlog_reply_direct` | `source_type` / `target_id` / `original_message` / `reply_text` / `sender_id` / `message_id` | 对某条未读直接回一句（可带引用） |

#### D. 信任关系与身份（8 个）

| entry | 参数 | 说明 |
|---|---|---|
| `add_trusted_user` | `qq_number` / `level` / `nickname` / `normal_relay_probability` | 加名册。**level 只能是 admin/trusted/normal** |
| `remove_trusted_user` | `qq_number` | 移出名册 |
| `add_trusted_group` | `group_id` / `level` / `normal_relay_probability` / `open_reply_probability` | 加信任群 |
| `remove_trusted_group` | `group_id` | 移除 |
| `set_user_nickname` | `qq_number` / `nickname` | 设昵称（最长 64 字，禁 `[]|`） |
| `refresh_actual_contacts` | — | 从 QQ 拉真实联系人刷新列表 |
| `bind_identity_account` | `user_id` / `target_user_id` | 开放平台身份绑定 |
| `unbind_identity_account` | `user_id` | 解绑 |

#### E. 内容能力（9 个）

| entry | 参数 | 说明 |
|---|---|---|
| `bilibili_login_start` | `force_new` | 生成登录二维码（base64 PNG） |
| `bilibili_login_check` | — | 查扫码状态：`waiting` / `scanned` / `confirmed` / `done` / `timeout` |
| `bilibili_logout` | — | 删本地 B 站凭据 |
| `upload_sticker` | `filename` / `data_base64` / `desc` | 上传表情图并注册 |
| `register_sticker` | `image_path` / `desc` | 按路径注册表情 |
| `sticker_lab` | `action` / `url` / `note` / `sticker_id` / `target_qq` | 表情链路自测（四个动作独立，失败直接报错不吞） |
| `gallery_lab` | `action` / `url` / `query` / `limit` | 图库自测 |
| `forget_group_memory` | `group_id` | 忘掉某个群的记忆（调宿主记忆服务的 scoped forget） |
| `save_proactive_topics` | `topics` | 存主动搭话的话题池 |

#### F. 调试与其他（5 个）

| entry | 参数 | 说明 |
|---|---|---|
| `adjust_group_attention` | `group_id` / `delta` | 手动调某群注意力分数（`delta` 必须是数字） |
| `pick_directory` | — | 系统目录选择器。**tkinter 在子进程可能崩溃，这里包了异常返回错误，绝不崩进程** |
| `get_napcat_webui` | — | NapCat WebUI 地址 + 最近日志行 |
| `sync_qrcode` | — | 把 NapCat 二维码同步到 static 目录给前端显示 |
| `mark_group_backlog_reviewed` | `group_id` | 标记已读，清掉该群未读 |

### 10.3 加 entry 的三条规矩

1. **`input_schema` 必须和签名一致**，`additionalProperties` 该写 `False` 就写 `False`。前端拿它渲染表单，不一致就渲染不出来。
2. **要经过 `save_settings` 的字段，三层都要改**（entry 签名 → dashboard → settings）。
3. **entry 里不要吞异常**。`Ok(...)` / `Err(SdkError(...))` 二选一，让前端能区分"成功但是空"和"失败"。

---

## 11. 配置项全表（89 项）

全部默认值只在 `config_store.default_config()` 里定义，落盘到 `data/business_config.json`。

### 11.1 连接与基础（10）

| 键 | 默认 | 说明 |
|---|---|---|
| `qq_connection_mode` | `"napcat"` | `napcat` = **反向**（自己开端口等人连）/ `napcat_forward` = **正向**（主动连 3001）/ `open_platform`。**名字是反的，别按字面理解** |
| `onebot_url` | `"ws://0.0.0.0:6199"` | 反向时是监听地址，正向时是要连的地址 |
| `token` | `""` | OneBot 鉴权 token，LLBot 一般是空 |
| `napcat_directory` | `""` | NapCat 安装目录。**填了不存在的路径会启动失败**，没有就留空 |
| `show_napcat_window` | `True` | 是否显示 NapCat 窗口 |
| `reply_mode` | `"text"` | 回复形态（text / 其它由 `normalize_reply_mode` 归一） |
| `max_concurrent_messages` | `3` | 同时处理几条消息 |
| `ai_connect_timeout_seconds` | `10.0` | 连模型超时 |
| `ai_turn_timeout_seconds` | `60.0` | 单轮生成超时 |
| `handler_shutdown_timeout_seconds` | `10.0` | 停止时等 handler 多久 |

> 最常见的新手坑：新装默认是**反向监听 6199**，而 LLBot 一般配的是**正向连 3001**。两边都等着对方连，于是一条消息都不回、日志还正常。改 `qq_connection_mode=napcat_forward` + `onebot_url=ws://127.0.0.1:3001`。

### 11.2 开放平台（3）

| 键 | 默认 | 说明 |
|---|---|---|
| `qq_open_app_id` | `""` | 开放平台 AppID |
| `qq_open_client_secret` | `""` | 开放平台密钥 |
| `qq_open_identity_probe_enabled` | `False` | 关闭。打开后每条群/私聊事件都往持久日志里写一行标识符字段，只有做取证时才需要 |

### 11.3 权限与概率（5）

| 键 | 默认 | 说明 |
|---|---|---|
| `trusted_users` | `[]` | 名册 `[{"qq","level","nickname","normal_relay_probability"}]`。**level 只能 admin/trusted/normal** |
| `trusted_groups` | `[]` | 信任群列表 |
| `speaker_trust_profiles` | `{}` | 旧的信赖度账本，**只做一次性迁移源**，已被宿主全局池取代 |
| `normal_relay_probability` | `0.1` | 普通用户触发转发/接话的概率 |
| `open_reply_probability` | `0.1` | 开放群开口概率 |

### 11.4 引导（4）

| 键 | 默认 |
|---|---|
| `show_onboarding` | `True` |
| `guide_step_napcat_done` | `False` |
| `guide_step_config_done` | `False` |
| `guide_step_runtime_done` | `False` |

### 11.5 注意力与策略（20）

| 键 | 默认 | 说明 |
|---|---|---|
| `enable_group_attention` | `True` | `neko_dynamic` 模式下强制启用多群注意力 |
| `strategy_mode` | `"neko_dynamic"` | `neko_dynamic`（主）/ `neko_scene`（退级） |
| `group_attention_max_score` | `10.0` | 分数上限 |
| `group_attention_focus_threshold` | `4.0` | 够不够格插话 |
| `group_attention_focus_send_threshold` | `2.0` | 够不够格发送（**故意低于焦点线**） |
| `group_attention_min_threshold` | `1.0` | 低于此完全不看 |
| `group_attention_message_gain` | `0.25` | 每条消息给群加多少分 |
| `attention_base_rise_rate` | `0.02` | 基础增速 |
| `attention_message_boost` | `0.15` | 消息加成 |
| `attention_keyword_boost_ratio` | `1.8` | 关键词加成倍率 |
| `attention_honeymoon_seconds` | `60` | 夺冠蜜月期 |
| `attention_fall_seconds` | `30` | 回落窗口 |
| `attention_fall_rate` | `0.015` | 回落速率 |
| `attention_consume_ratio` | `0.10` | 发言消耗比例 |
| `icebreaker_cold_threshold` | `3` | 冷场破冰阈值 |
| `retroactive_review_max_messages` | `30` | 回溯最多取多少条被忽略消息 |
| `retroactive_review_max_reply` | `5` | 回溯最多补回几条 |
| `neko_dynamic_idle_timeout_seconds` | `10.0` | **已废弃**（注意力系统下不再使用） |
| `neko_dynamic_waking_users` | `[]` | **已废弃** |
| `neko_dynamic_waking_keywords` | `[]` | **已废弃** |

### 11.6 未读（5）

| 键 | 默认 | 说明 |
|---|---|---|
| `backlog_retention_limit` | `200` | 每群保留多少条未读 |
| `backlog_summary_threshold` | `10` | 攒够多少条才汇总 |
| `backlog_notify_cooldown_seconds` | `900` | 通知冷却 15 分钟 |
| `backlog_issue_notify_threshold` | `1` | 达到几条才提示 |
| `backlog_labels` | 内置默认 | 未读分类标签（关键词 → 标签） |

### 11.7 疲劳（5）

| 键 | 默认 |
|---|---|
| `fatigue_enabled` | `True` |
| `fatigue_circadian_peak_hour` | `15` |
| `fatigue_circadian_low_hour` | `3` |
| `fatigue_session_per_reply` | `5.0` |
| `fatigue_awake_idle_timeout` | `10.0` |

### 11.8 记忆（8）

| 键 | 默认 | 说明 |
|---|---|---|
| `group_memory_enabled` | `False` | 群聊长期记忆，**显式 opt-in** |
| `group_member_memory_enabled` | `False` | 群成员分桶记忆，是上面那条的子开关，默认关（会增加按成员分桶的提取调用） |
| `private_participant_memory_enabled` | `False` | 私聊 participant 域（普通用户自己的记忆），绝不进主人 legacy 语料 |
| `allow_cross_group_context` | `False` | 跨群实时话题。默认严格隔离 |
| `cross_domain_owner_memory` | `False` | 私聊最近对话 → 群 |
| `cross_domain_owner_context_rounds` | `8` | 带多少条（双向合计，8 ≈ 最近 4 轮） |
| `cross_domain_owner_longterm_memory` | `False` | 私聊长期记忆 → 群（独立开关） |
| `cross_domain_owner_longterm_max_chars` | `1200` | 长期记忆段字符上限，按行截断 |

后四项是 19 章那个功能，取值钳制和 `or` 陷阱见 19.3。

### 11.9 主动搭话与定时搭话（12）

| 键 | 默认 | 说明 |
|---|---|---|
| `proactive_private_enabled` | `True` | 私聊主动搭话总开关 |
| `proactive_private_targets` | `[]` | 空 = 用第一个 admin |
| `proactive_private_min_idle_minutes` | `30` | 距上次互动多久才开口 |
| `proactive_private_min_gap_minutes` | `45` | 两次主动之间至少隔多久 |
| `proactive_private_daily_limit` | `6` | 每天最多几次 |
| `proactive_private_quiet_hours` | `""` | 静默时段，跨午夜写法 `"23:00-08:00"`，空 = 不静默 |
| `proactive_private_unanswered_backoff_hours` | `4` | 主动了没回音，每次多等几小时 |
| `proactive_private_max_fatigue` | `70` | 疲劳超过此值不打扰 |
| `proactive_private_check_interval_seconds` | `60` | 后台判定周期 |
| `scheduled_greeting_enabled` | `False` | 定时搭话开关（**显式排好的打扰，默认关**） |
| `scheduled_greetings` | `[]` | `[{"time":"08:00","instruction":"跟他说早安","target":"可选QQ"}]` |
| `scheduled_greeting_check_interval_seconds` | `30` | 到点检查周期 |

### 11.10 断线补消息（6）

| 键 | 默认 | 说明 |
|---|---|---|
| `catchup_enabled` | `True` | |
| `catchup_window_hours` | `12` | 只补这个窗口内的，更老不补 |
| `catchup_max_per_conversation` | `12` | 单会话最多补几条（多了像刷屏） |
| `catchup_max_total` | `30` | 一次重连总共最多补几条 |
| `catchup_stagger_ms` | `120` | 补的消息之间隔多久注入 |
| `catchup_sweep_minutes` | `5` | 定期对账周期。`0` = 关闭。**用途**：LLBot 与 QQ 断了但我们与 LLBot 没断时，收不到任何重连信号，只能靠定时翻历史比对 |

### 11.11 B 站（8）

| 键 | 默认 | 说明 |
|---|---|---|
| `bilibili_browse_enabled` | `False` | 见闻循环总开关，默认关 |
| `bilibili_browse_interval_minutes` | `60` | 多久刷一轮 |
| `bilibili_browse_batch_size` | `20` | 一轮收集多少条 |
| `bilibili_browse_deep_count` | `5` | 其中几条深看（抓弹幕 + 字幕） |
| `bilibili_interest_keywords` | `[]` | 空 = 用内置默认兴趣词（猫咪/做饭/恐怖游戏/冷知识…） |
| `bilibili_hot_ratio` | `0.15` | 掺多少全站热门 |
| `bilibili_quiet_hours` | `"02:00-08:00"` | 夜里不刷，免得像机器 |
| `bilibili_credential` | `{}` | 手动填 cookie 的兜底通道（正常走扫码登录） |

### 11.12 图库与提示词（3）

| 键 | 默认 | 说明 |
|---|---|---|
| `gallery_dir` | `""` | 空 = `<data>/gallery`；**只认绝对路径**，相对路径猜错一次图就散在谁也找不到的地方 |
| `prompt_overrides` | `{}` | 提示词覆盖（`locale → layer_id → text`），优先级高于代码常量 |
| `group_prompts` | `{}` | 按群自定义提示词（`group_id → 文本`） |

### 11.13 加一个配置项，五处都要改

```
1. config_store.default_config()            默认值
2. __init__.py  save_settings 签名 + input_schema
3. dashboard_service.save_settings()        转发（别用 **_ 静默吃掉）
4. settings_service._save_settings_locked() 校验、钳制、写运行时
5. static/napcat.html                       控件 + applySettings + doSave
```

少任何一处都不报错，只是静默失效。**改完必须在真机调一次 `save_settings` 并回读**，单测和语法检查都发现不了中间层吞参数。

---

## 12. 数据文件全表

`<数据目录>\plugins\qqbetter_auto_reply\data\`

| 文件/目录 | 内容 | 丢了会怎样 | 迁移时带不带 |
|---|---|---|---|
| `business_config.json` | 全部业务配置（89 项） | 回出厂默认，群设置全丢 | **带** |
| `business_config.json.bak-*` | 继承时的备份 | — | 不带 |
| `backlog_state.json` | 群未读消息（实测 91 KB） | 未读汇总从零开始，不影响聊天 | **不带**（运行时状态） |
| `message_cursor.json` | 补消息游标（每会话最后的 seq） | **会错位**，可能重复补或漏补 | **绝不带** |
| `proactive_private_state.json` | 主动搭话状态（上次时间、当日计数） | 节奏重置 | 不带 |
| `scheduled_greeting_state.json` | 定时搭话状态（每条今天发没发） | 当天可能重发一次 | 不带 |
| `bilibili_credential.json` | B 站登录态 | 要重新扫码 | 带（换机要重扫） |
| `bilibili_memories.json` | 刷到的见闻（实测 451 KB） | 见闻清零，重新攒 | 可选 |
| `vision_cache.json` | 识图缓存（只存 `sha:` 内容键） | 下次看图重新花钱识别 | 带 |
| `qq_faces.json` | 表情索引（id/md5 → 描述） | 重新识别，**花钱** | 带 |
| `sticker.json` + `sticker/` | 表情包库（数字 ID） | 表情包丢失 | 带 |
| `faces/` | 收藏表情本地副本（`<md5>.jpg`，实测 896 KB） | QQ 的 URL 带 rkey，过期就取不回来 | 带 |
| `gallery/`（或 `gallery_dir`） | 图库 `index.json` + `blobs/`（实测 2.5 MB） | 图库清空 | 带 |
| `gallery.bak-*` | 搬迁时的备份 | — | 不带 |

**为什么运行时状态不能带**：旧游标搬过去会让对账从头错位 —— 上一台机器处理到 1000，新机器从 1000 开始，中间的消息全当"已经补过"。

---

## 13. 提示词系统

### 13.1 谁负责装配

`session_instruction_service.build_session_instructions()` —— 一个入口，返回 `QQInstructionBundle`（含 `system_prompt` 和几个可撤销段的原文）。

装配顺序（按 `build_session_instructions()` 里的真实调用序，共 13 段，最后 `_compose_sections()` 拼成一个 `system_prompt`）：

| # | 段 | 由谁产出 | 备注 |
|---|---|---|---|
| 1 | 核心记忆 | `core_memory_text` 直接 append | 记忆授权关着时为空 |
| 2 | 用户画像 | `_append_user_profile_section()` | 合成轮（buffer 总结/破冰/回溯）`memory_sender_id` 为空，跳过 |
| 3 | 角色卡 | `_append_role_card_section()` | 来自宿主角色数据 |
| 4 | 场景段 | `_build_scene_section()` → `_build_group_scene_section()` | **主人身份确认（`admin_line`）就拼在这一段开头**，只有发言人是名册里的 admin 才会出现 |
| 5 | 黑名单 | `_append_blacklist_section()` | 黑名单用户不生成 |
| 6 | 群自定义提示词 | `_append_group_custom_prompt_section()` | `group_prompts` 里有就加 |
| 7 | 跨群段 | `_append_cross_group_section()` | `allow_cross_group_context` 时；**可撤销** |
| 8 | 私聊→群（最近对话） | `_append_owner_private_section()` | `cross_domain_owner_memory` 时；**可撤销** |
| 9 | 私聊长期记忆 | `_append_owner_longterm_section()`（async） | `cross_domain_owner_longterm_memory` 时；**可撤销** |
| 10 | 注意力上下文 | `_append_attention_context_section()` | 群场景才有 |
| 11 | 情绪 | `_append_emotion_section()` | 群场景才有 |
| 12 | 细节约束 | `detail_constraints_section`（静态层） | 走 i18n / overrides |
| 13 | 输出格式 | `output_prompt_section`（静态层） | 走 i18n / overrides |

7/8/9 三段是「可撤销段」：函数返回值被存进 `QQInstructionBundle`，后续在生成前还要复检开关（见 19.10）。

### 13.2 静态层的三级优先级

```
i18n bundle（i18n/<locale>.json）
    ↓ 没有就用
prompt_overrides（用户在提示词编辑器里存的）
    ↓ 没有就用
代码常量（prompt_fragment_templates.py）
```

**这意味着改代码常量可能不生效** —— 如果那个 key 在 i18n 里存在。改文案前先 grep i18n。

（历史记录：某次改 `_resolve_static_layer` 的默认文案，改完没生效，查了半天才发现 i18n 里有同名 key。）

### 13.3 占位符

| 占位符 | 填什么 | 谁填 |
|---|---|---|
| `{sticker_catalog}` | 表情包清单（只有带描述的） | `sticker_service.catalog_lines()` |
| `{her_name}` | 角色名 | 宿主角色数据 |
| `{master_name}` | 主人称呼 | 权限名册 |
| `{group_name}` / `{group_id}` | 群信息 | 消息上下文 |

### 13.4 编辑器

前端「提示词」页 → `get_prompt_editor_state` 拿当前生效文本（已按三级优先级解析）→ 改完 `save_prompt_override` 存覆盖。

注意 locale 用**全码**（`zh-CN` / `zh-TW`），用短码 `zh` 会让繁中用户看到并覆盖简体那份。

### 13.5 i18n

9 种语言，每种 728 个键，扁平结构：

```json
{
  "ui.napcat.guide.step_sched": "定时搭话",
  "prompts.owner_private_context": "..."
}
```

加键的规矩：**9 个文件都要加**，缺一个就是那个语言下显示原始 key。用脚本加，别手改（逗号容易错，JSON 一坏整个界面白屏）。

---

## 14. 前端

### 14.1 页面结构

```
index.html（4.4 KB）
  ├─ 读 qq_connection_mode
  ├─ napcat / napcat_forward → napcat.html（141.5 KB）
  └─ open_platform            → open_platform.html（59.9 KB）
```

| 文件 | 作用 |
|---|---|
| `index.html` | 只做跳转，附带全局样式和 `?v=N` 缓存版本 |
| `napcat.html` | 主面板：连接、权限、注意力、记忆、主动搭话、定时搭话、跨域、表情、图库、B 站、调试。**配置引导也在这** |
| `open_platform.html` | 开放平台面板，比主面板少三块（B 站 / 图库 / 主动搭话） |
| `script.js` | 通用逻辑：请求、toast、格式化、SSE 对接 |
| `i18n.js` | 前端 i18n 加载（读 `/ui/i18n/<locale>`） |
| `ui-sse.js` | 日志与状态推送 |
| `style.css` | 全站样式 |
| `old.html` | 旧版，留着但没挂路由 |

### 14.2 引导（14 步）

引导是 `napcat.html` 里 `updateGuide()` 手工维护的列表，不是配置驱动的。当前 14 步：

```
step-napcat → step-onebot → step-users → step-running → step-bilibili
→ step-gallery → step-proactive → step-sched → step-crossdomain
→ step-sticker → step-review → step-strategy → step-linux → step-debug
```

按 `updateGuide()` 里 `add()` 的**实际调用顺序**抄的。第 9 步 `step-crossdomain`
就是群聊私聊记忆打通（见 19.5）。

加一步要动四处：

1. `napcat.html` 加 `<div id="guide-detail-step-xxx">` 面板
2. `updateGuide()` 里 `add("step-xxx", 标题, 是否完成, 提示文案, !0)`
3. `openGuideModal()` 里加标题映射 + `else if(id==="step-xxx")fillXxxGuide()`
4. i18n 加 `ui.napcat.guide.step_xxx` / `modal_xxx` 等键

### 14.3 缓存版本号

`index.html` 里引用 `napcat.html?v=7`。**改任何前端文件都要提这个数字**，否则浏览器拿缓存的旧文件，你会以为代码没生效。

当前值：`v=7`（0.8.8 时提的）。

静态资源本身已设 `cache_control="no-cache"`，但 HTML 的跳转引用还是要靠 `?v=`。

### 14.4 前端不做的事

- 不做任何业务判断（谁该回、什么时候回全在后端）
- 不缓存配置（每次 `get_dashboard_state` 重新拿）
- 不直接连 QQ（所有操作都走 entry）

---

## 15. 开发流程

### 15.1 环境

| 东西 | 路径 / 值 |
|---|---|
| 项目根 | `Q:\neko开发\` |
| 开发 venv | `Q:\neko开发\.venv`（aiohttp + Pillow + pytest + ruff） |
| ruff（CI 同版本 0.12.4） | `Q:\neko开发\_tools\ruff\bin\ruff.exe` |
| 测试垫片 | `Q:\neko开发\_probe\_guide_testroot\` |
| 宿主嵌入 Python | `Q:\PerfLogs\steam\steamapps\common\n.e.k.o\resources\bin`（3.11.13） |
| 宿主 API | `http://127.0.0.1:48916`（**端口每次启动可能变**） |
| 宿主 OpenAPI | `http://127.0.0.1:48916/openapi.json`（105 个端点） |

宿主的 Python 里有什么（实测）：`httpx` / `aiohttp` / `certifi` / `qrcode` / `PIL` / `bs4` / `lxml` / `bilibili_api` / `playwright`。
缺 `curl_cffi`。所以网络请求一律 aiohttp，且 `timeout=` 只能传 `ClientTimeout` 对象 —— 传 float 会 ValueError。

### 15.2 改代码的规矩

1. **只改 `src/qqbetter_auto_reply/`。** 源码是唯一副本，运行实例目录是同步出来的，不要直接改。
2. **不往 C 盘写任何东西。** 插件产生的数据、测试日志、脚本输出一律放插件自己的 data 目录或 `Q:\neko开发\` 下。
3. **改完三处同步**：代码 + `plugin.toml` 的 `description` + `README.md`（发版时还要加安装说明）。
4. **加配置项五处都要改**（见 11.13）。
5. **吞异常必须留计数**。见 17 节最后两条。

### 15.3 让改动生效

```bash
# 1) 同步源码到运行实例（排除开发残留）
cd "Q:/neko开发" && python -X utf8 -c "
import shutil, pathlib
src = pathlib.Path('src/qqbetter_auto_reply')
dst = pathlib.Path(r'Q:/N.E.K.O_0.8.3.1_win/N.E.K.O/.neko-plugin-installations/plugins/qqbetter_auto_reply')
skip = {'__pycache__','.git','.pytest_cache','.ruff_cache','tests','.vscode','.github'}
n = 0
for p in src.rglob('*'):
    if not p.is_file(): continue
    rel = p.relative_to(src)
    if any(x in rel.parts for x in skip): continue
    t = dst / rel; t.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, t); n += 1
print('同步', n, '个文件')
"

# 2) reload 插件进程（约 3~5 秒）
curl -s -X POST http://127.0.0.1:48916/plugin/qqbetter_auto_reply/reload

# 3) 确认宿主已加载新版本
curl -s http://127.0.0.1:48916/plugins | python -c "
import json,sys
d = json.load(sys.stdin)
items = d if isinstance(d, list) else d.get('plugins') or d.get('items') or []
for p in items:
    if 'qqbetter' in str(p.get('id','')):
        print({k: p.get(k) for k in ('id','version','source','status')})
"

# 4) 重启自动回复（reload 后连接是断的，必须重连 WS）
#    用 5.3 那段 call('start_auto_reply') 脚本，或前端点「启动」
```

**只改前端时不用 reload**，但要提 `index.html` 里的 `?v=N`（现在是 `v=7`），然后浏览器强刷。

**验证代码真的加载了**：模块顶部插一行"导入即写文件"的标记，或者看某个新字段在 `get_prompt_editor_state` / `get_dashboard_state` 的返回里有没有。**看 pyc 时间戳不可靠。**

### 15.4 测试

```bash
cd Q:/neko开发/_probe/_guide_testroot
PYTHONPATH="Q:/neko开发/_probe/_guide_testroot" \
  Q:/neko开发/.venv/Scripts/python.exe -m pytest -q
```

也可以单跑某个脚本：

```bash
PYTHONPATH="Q:/neko开发/_probe/_guide_testroot" python test_sched_greeting.py
```

**垫片原理**（`_guide_testroot/sitecustomize.py`）：

- 把 `plugin.plugins.qqbetter_auto_reply.__path__` 指到 `src/`
- 提供宿主替身：`utils/file_utils.py`、`main_logic/tool_calling.py`、`config/*`

**没有这层垫片，插件模块 import 就失败**（它依赖宿主包）。这也是为什么 `PYTHONPATH` 必须设。

现有测试脚本：

| 脚本 | 覆盖 |
|---|---|
| `test_cross_domain.py` | 跨域最近对话（**17 项**，实跑计数） |
| `test_cross_domain_longterm.py` | 跨域长期记忆（22 项） |
| `test_sched_greeting.py` | 定时搭话（45 项） |
| `test_group_admin_identity.py` | 群内 admin 身份（10 项探针） |

前三套跟 19 章直接相关，覆盖点清单见 19.13。

### 15.5 Lint

CI 用的是**严格模式**，本地要复现同一条命令：

```bash
Q:/neko开发/_tools/ruff/bin/ruff.exe check \
  --ignore-noqa --isolated --no-respect-gitignore \
  --target-version py311 --line-length 120 \
  --select E4,E7,E9,F,I --exclude vendor .
```

三个反直觉的点：

- `--ignore-noqa` 让 `# noqa` 注释**失效**
- `--no-respect-gitignore` 让 `.gitignore` **不生效**（调试脚本必须物理移出目录）
- 只查 `E4/E7/E9/F/I` 五组，不管行宽以外的风格

### 15.6 打包

```bash
cd Q:/neko开发
.venv/Scripts/python.exe build_and_install.py \
  --plugin qqbetter_auto_reply \
  --name "QQ集成" \
  --version 0.8.8 \
  --desc "一句话说明这次改了什么" \
  --build-only
```

**必须显式传 `--plugin qqbetter_auto_reply`** —— 脚本默认打的是围棋插件。

参数：

| 参数 | 作用 |
|---|---|
| `--build-only` | 只打包不安装 |
| `--check` | 只探测目录，不做事 |
| `--dir` | 手动指定数据目录 |
| `--skip-dirs` | 逗号分隔，追加排除目录 |

**`.neko-plugin` 是个 zip**，结构：

```
manifest.toml                    id / name / version / 依赖 / sdk 版本要求
payload/
  plugin.toml                    插件清单
  dependencies.toml              依赖声明（算 hash 时也算它）
  *.py / static/ / i18n/ ...
```

**payload hash 算法**（打包脚本已实现，改脚本时别动这段）：

```
sha256( payload/ 下所有文件
        按去掉 "payload/" 前缀的相对路径排序
        逐个 update(相对路径) + \0 + 内容 + \0 )
```

少算一个文件（比如 `dependencies.toml`），装的时候报"安装包内容与校验信息不一致"。
验证口径对不对：拿官方包（`sandbox_tool` / `neko_fishpower` / `luogu`）用同一段代码复算，能对上才算对。

### 15.7 安装 / 升级

`build_and_install.py` 不带 `--build-only` 时会：

```
写包到 .neko-plugin-packages/<id>.neko-plugin
  → 更新 plugins.lock.json（先备份）
  → 逐文件覆盖到 .neko-plugin-installations/plugins/<id>/
```

**全程不删文件**（某些环境 `unlink` 直接 FAIL_CLOSED，所以脚本只做新建/覆盖）。

宿主 UI 里导入 `.neko-plugin` 也行，会做 hash 校验。

### 15.8 发布到插件市场

```
1. 本地改完 → 推 GitHub（main 或 feat 分支）
2. 等 Actions 的 Verify 工作流跑过（ruff + 冒烟测试 + 结构检查）
3. Market 登录（https://market.project-neko.cn）
4. 投稿页填仓库地址 → 选分区/标签 → 提交审核（人工）
5. 审核通过后：uv run neko-plugin publish <plugin-id>
6. 生成 GitHub Release → Market 出现可安装版本
```

市场只收 GitHub 仓库，**不接受直接上传 `.neko-plugin` 文件**。

仓库必须带这些文件（官方 init 模板）：

```
.github/workflows/verify.yml     uses: Project-N-E-K-O/N.E.K.O/.github/workflows/plugin-market-verify.yml@main
.github/workflows/release.yml    uses: .../plugin-market-release.yml@main
.vscode/settings.json            ← 缺了 Verify 会失败
.vscode/tasks.json               ← 同上
ruff.toml
pyproject.toml                   dependencies = []
config.example.toml
tests/test_smoke.py
.gitignore
```

推 GitHub 的现实问题（这台机器）：

- `github.com` 的 DNS 只解析到被墙 IP，**HTTPS 推送通时断**
- 解法：git 走 SSH over 443，或者用系统代理（端口会变，`git config --global http.proxy` 显式配，git 不读环境变量）
- `raw.githubusercontent.com` 被阻断 → 用 `api.github.com` + `Accept: application/vnd.github.raw+json` 取原文

---

## 16. 调试手册

### 16.1 先看哪

| 现象 | 先看什么 | 结论方向 |
|---|---|---|
| 一条消息都不回 | `get_dashboard_state` 的 `runtime.onebot_connected` | 连不上 → 11.1 那个反向/正向的坑 |
| 连上了但不回 | 日志里有没有 `[收到消息]` | 没有 → LLBot/QQ 侧没推事件，不在插件 |
| 收到了不回 | pipeline traces 停在哪一阶段 | 停在 decision → 被闸拦了；停在 model → 模型超时 |
| 回的牛头不对马嘴 | traces 的 `context` 阶段 metadata | 记忆拉错 / 提示词拼错 |
| 工具明明调了没反应 | 日志搜 `[工具诊断] 收到工具调用` | 有这条但没效果 → dispatch 的 if 链漏了 |
| 配置保存成功但没变 | `get_dashboard_state` 的 `settings` 段回读 | 三层吞参数（11.13） |
| 界面看到旧页面 | 浏览器是不是拿缓存 | 提 `?v=N` |
| 换了代码行为没变 | `plugins` 里 `source` 是不是 `user` | 装成内建版了 |

### 16.2 日志

```
<数据目录>\logs\plugin\N.E.K.O_Plugin_qqbetter_auto_reply_<YYYYMMDD>.log    ← 插件自己的
<数据目录>\logs\plugin\N.E.K.O_Plugin_qqbetter_auto_reply_error.log          ← 插件异常
<数据目录>\logs\N.E.K.O_Plugin_error.log                                     ← 宿主侧的（插件起不来时看这个）
```

**最后一份最容易漏**。插件如果在 import 期就挂了（比如 `plugin.toml` 语法错），插件自己的日志一片安静，只有宿主这份里有 traceback。

常搜的关键词：

| 关键词 | 含义 |
|---|---|
| `[工具诊断] 已挂载 N 个` | 本轮挂了哪些工具 |
| `[工具诊断] 收到工具调用` | 模型真的发了 tool_calls |
| `[收到消息] 群 123 from 456` | 消息到插件了（0.8.6 新增） |
| `[ScheduledGreet]` | 定时搭话循环 |
| `Forward WS connected` | OneBot 连上了 |
| `ScheduledGreet 循环已启动，N 条定时任务` | 定时搭话起来了 |

### 16.3 调 entry 的脚本（复制即用）

```python
import json, urllib.request, time

BASE = 'http://127.0.0.1:48916'

def call(entry, args=None, timeout=90):
    body = json.dumps({
        'plugin_id': 'qqbetter_auto_reply',
        'entry_id': entry,
        'args': args or {},
    }).encode()
    req = urllib.request.Request(
        BASE + '/runs', data=body,
        headers={'Content-Type': 'application/json'}, method='POST')
    rid = json.load(urllib.request.urlopen(req, timeout=timeout))['run_id']

    for _ in range(timeout):
        time.sleep(1)
        rec = json.load(urllib.request.urlopen(BASE + '/runs/' + rid, timeout=10))
        if rec.get('status') in ('succeeded', 'failed', 'canceled', 'timeout'):
            break

    exp = json.load(urllib.request.urlopen(BASE + '/runs/' + rid + '/export', timeout=10))
    item = next(i for i in exp['items'] if i.get('type') == 'json' and i.get('json'))
    raw = item['json']
    # 剥两层信封：{success, data:{...}} 再剥一次
    while isinstance(raw, dict) and isinstance(raw.get('data'), dict) and (
        'success' in raw['data'] or 'error' in raw['data']
    ):
        raw = raw['data']
    if isinstance(raw, dict) and raw.get('success') and isinstance(raw.get('data'), dict):
        raw = raw['data']
    return raw

# 例子
print(call('get_dashboard_state')['runtime'])
print(call('save_settings', {'scheduled_greeting_enabled': True})['success'])
```

### 16.4 三招立刻见效的排查

1. **导入即写文件**：在模块顶部加
   `open(r'Q:/neko开发/_probe/_loaded.txt','a').write('loaded\n')`
   重载后看有没有新行 —— 立刻知道代码到底加载了没。
2. **回读配置**：`save_settings` 之后一定跟一次 `get_dashboard_state` 看 `settings` 段，
   或者直接读 `data/business_config.json`。别信 `success=True`。
3. **看 traces 不看日志**：`get_dashboard_state` 返回里有最近的 pipeline traces，
   比翻几千行日志快十倍。

---

## 17. 踩坑清单

按"现象 → 根因 → 怎么办"整理，全是真实踩过的。

### 17.1 连接与消息

| 现象 | 根因 | 怎么办 |
|---|---|---|
| 装完一条消息都不回，日志正常 | 默认是**反向监听 6199**，LLBot 一般是**正向 3001** | 改 `qq_connection_mode=napcat_forward` + `ws://127.0.0.1:3001` |
| 连接正常但完全没事件 | LLBot / QQ 侧没推消息（插件与 LLBot 通，LLBot 与 QQ 断） | 看 LLBot 自己的日志和账号状态；`catchup_sweep_minutes` 能兜住一部分 |
| 图片发出去没反应、日志安静 | NapCat 只认 `file:///Q:/...`（三斜杠 + 正斜杠），裸 Windows 路径会让 OneBot **不回任何响应**卡到超时 | 统一走 `normalize_image_locator()` |
| 重连后消息重复或漏 | 游标错位（曾经拿时间戳凑 seq） | 游标只认 `message_seq`，拿不到就不推进 |
| 群消息的"发言人身份"不对 | 早期群消息装的是**群等级**（trusted/open/normal），不是发言人档位 | 0.8.6 已修：`reply_pipeline._run_context` 用 `group_speaker_permission_level_at_receipt` 覆盖 |

### 17.2 配置与保存

| 现象 | 根因 | 怎么办 |
|---|---|---|
| 保存返回成功但值没变 | `save_settings` 三层，中间层用 `**_` 静默吃掉新字段 | 三处都要改（11.13） |
| 保存后要手动重启才生效 | 早期连接字段变更不触发重连 | 0.8.6 已修：`settings_service` 保存后自动 stop+start，延迟 3 秒（1 秒会被 save 入口取消） |
| `int(x or 8)` 把合法 0 变成默认值 | 0 是 falsy | 显式判 `is None` |
| 报 `NameError` 但单测全过 | 变量在 A 函数定义、B 函数引用（真机才触发） | 加配置字段必须在真机调一次 `save_settings` |
| `plugin.toml` 改了宿主拒绝启动，插件日志无异常 | description 里插了**真实换行**，TOML 基本字符串不许跨行 | 一律走脚本 + `tomllib.loads` 校验，别手写。现成脚本 `_probe/fix_plugin_toml_desc.py` |

### 17.3 前端

| 现象 | 根因 | 怎么办 |
|---|---|---|
| 界面还是旧页面 / JS 改了没反应 | 浏览器缓存 | 提 `index.html` 的 `?v=N` |
| 引导显示"OneBot 未配置"但已连上 | 判定条件要求 token 非空，而 LLBot 的 token 本来就空 | 已修：以实际连接状态为准，另给"填了地址但没连上"单独文案 |
| 新版功能在界面上找不到 | 只同步了 py，没同步 static | 同步脚本要包含 `static/` |

### 17.4 内容能力

| 现象 | 根因 | 怎么办 |
|---|---|---|
| B 站字幕一直空、不报错 | cookie 过期是**静默**的，字幕接口只回空列表 | 带登录态却一条字幕都没有时才去探 nav（`code=-101` 即过期）。别每轮都探 |
| 用 `User.get_user_info()` 判登录态永远"有效" | 那是公开接口，不带 cookie 也返回昵称 | 只能用 `nav` 接口看 `data.isLogin` |
| 表情重发失败 | 收藏表情的 URL 带 rkey，几小时后是死链 | 认的时候就把图落盘 `faces/<md5>.jpg` |
| 表情识别费用高 | 每张都重新调 VLM | 认一次落盘（`qq_faces.json` / 图片 md5），之后 0 成本 |
| 只认 `market_face` 导致全程空转 | LLBot 把 `customFace` **降级成普通图片**（`type=image, subType=1`），`emoji_id` 全丢 | 两条路都要做（见 8.9 的表） |
| 图库换了目录还显示旧图 | `start()` 里 `if self._loaded: return`，目录变了内存索引没换 | 已修：记 `_loaded_root`，不一致就重读 |
| 换目录后新目录 0 张、旧图还在原处 | 只改配置没搬文件 | 已修：`_relocate_gallery()` 整体搬迁，目标有库不覆盖，搬失败回滚配置 |

### 17.5 打包与安装

| 现象 | 根因 | 怎么办 |
|---|---|---|
| 安装报"内容与校验信息不一致" | payload hash 少了 `dependencies.toml` | 见 15.6 算法，用官方包交叉验证 |
| 打了包但装完行为没变 | 装的是内建版（`source=builtin`），代码在 exe 里 | 确认 `source=user`；内建插件改不了 |
| 沙箱/权限不让删文件 | 某些环境 `unlink` 直接 FAIL_CLOSED | 打包安装全程只做新建/覆盖 |
| CI Verify 失败：ruff 一堆错 | CI 用 `--ignore-noqa --no-respect-gitignore` | 用 15.5 那条完整命令本地复现；调试脚本物理移出目录 |
| CI Verify 失败：Release check | 缺 `.vscode/settings.json` / `tasks.json` | 补上 |

### 17.6 写代码的两个通用教训

**① 吞异常必须留计数。**

`except: return default` 会把代码级错误（参数名改了、库版本变了）伪装成"这条本来就没数据"。
现象是功能一直空着，而日志一片安静。规矩：

- 每次吞异常记 `self.diag[key] += 1`
- 把"空"拆成可区分的几种（没权限 / 本来就没有 / 调用失败），三种处置完全不同
- default 用 `None` 当哨兵，别用 `[]` / `{}`，否则"失败"和"空结果"长得一样
- 连续失败 3 次把日志升到 warning

**② 解析模型输出，`find` 是"取第一个"，`findall` 才是"取全部"。**

`msg_el.find("text")` 让同一个 `<msg>` 里的第二个 `<text>`（正好装着链接）被静默丢弃。
现象是"她说发了、对方说没收到"，两边都没错、日志全绿。

**模型不遵守格式是常态，解析器必须按"它可能乱来"写。**

---

## 18. 已知限制 / 待办

1. **两个「QQ集成」并存。** 内建的 `qq_auto_reply` 不能改代码但能停用，**只能开一个**，同时开会重复回复。
2. **开放平台面板没有 B 站 / 图库 / 主动搭话三项。** 开放平台靠 @ 触发，主动私聊不适用，加了反而误导。
3. **表情包「从群聊自动收集」没有。** 本地这份只有手动上传 + 表情索引；自动收集 + 三档闸在上游 PR，未合入。
4. **群主动消息仍是 ephemeral + `persist=False`**，不进记忆。主动发的图片/表情同样不进记忆。
5. **上游四个 PR 仍是分开的分支**（三个修复、表情包收集、断线补消息、图库换头像），未合并成可安装的一版。
6. **版本号两条线并行**：内建 0.8.0 → 本插件 0.8.8；社区版已到 0.14.x。别拿两边版本号对比功能。
7. **git 里只有 7 个 commit**，0.8.4~0.8.8 的改动还在工作区没提交。要发版前得整理一次提交历史。

---

## 19. 专题：群聊私聊记忆打通（跨域记忆）

> 这一章单独写，是因为它是本插件**唯一一处把私聊内容搬进群聊**的逻辑，牵涉隐私，
> 横跨 0.8.5 / 0.8.6 / 0.8.8 三个版本、十几个文件。7.7 只给了摘要，这里给全部。
>
> 界面上叫「私聊↔群 记忆打通」，代码里叫 `cross_domain`，早期笔记里叫「跨域记忆」——
> **三个叫法是同一件事**。

### 19.1 要解决的问题

用户最初的原话是「为什么我私聊和他说的话到群里就不记得了」。

这不是 bug，是设计：`session_key` 分域，两套记忆库互不检索。

| 场景 | session_key | 记忆落在哪 |
|---|---|---|
| 主人私聊 | `private:<uid>` | **legacy 主线语料**（`/new_dialog/<her_name>` 那份服务端渲染文本） |
| 群聊 / 普通用户 | `group:<gid>` | **scoped 分域档案** |
| 群成员独立桶 | `participant:*` | 独立桶，绝不进主人 legacy |

分域的理由写在 `memory_bridge.py` 的 `participant_subject` docstring 里。所以她在群里
「一概想不起来」私聊说过什么，是预期行为。

产品决定（0.8.5 定的，后面两版没改）：**只补「带一点过来」，不动库结构、不迁移、
不新增任何写入路径**。关掉开关就等于没发生过。这条决定了后面所有实现细节。

### 19.2 三个版本各自做了什么

| 版本 | 干了什么 | 为什么 |
|---|---|---|
| **0.8.5** | 打通「最近对话」：`cross_domain_owner_memory` + 轮数，12 个文件 + 9 个 i18n | 先解决「刚才说到哪」 |
| **0.8.6** | **修根因**：群内发言人的 admin 身份终于能传到提示词层 | 0.8.5 的开关**形同虚设**，见下 |
| **0.8.8** | 补「长期记忆」第二个独立开关 + 引导里立面板 | 用户要求「把长期记忆加上吧，加一个按钮决定打不打通，加在引导里面」 |

**0.8.6 那个根因值得单说**，它是整个功能最容易误判的地方：

`message_dispatcher.py` 在群消息的 `decision.permission_level` 里装的是**群等级**
（`trusted` / `open` / `normal`），**不是发言人档位**。而注入函数的第一行就是
`if permission_level != "admin": return ""` —— 于是 0.8.5 做好的注入段从来没有触发过，
连 `admin_line`（主人身份确认）也从来没在群里出现过。表面现象是「开关开了没效果」，
实际是授权判据拿错了字段。

修法：`message_dispatcher` 在接收边界盖一个 `group_speaker_permission_level_at_receipt`
（值来自 `permission_mgr.get_permission_level(sender_id)`），
`reply_pipeline._run_context` 用它覆盖 `decision.permission_level` 再传给上下文。

> 0.8.6 另外两项（保存连接设置后自动重连、收到消息打界面日志）不属本功能，只是同版搭车。

还有一句反复澄清过的话，写在这里当规矩：

> **「认主人」只认插件自己的权限名册（admin），永远不认 QQ 群里的群主 / 群管理员。**
> 群里的管理员若不在名册里，`get_permission_level()` 查出来是 `none`，整条路径根本不执行。

### 19.3 用户能开的两个开关

| | 最近对话 | 长期记忆 |
|---|---|---|
| 配置键 | `cross_domain_owner_memory` | `cross_domain_owner_longterm_memory` |
| 默认 | `False` | `False` |
| 回答的问题 | 「**刚才说到哪**」 | 「**她记不记得我**」 |
| 带什么过去 | 最近 N 条私聊消息，双向合计、**含她自己的回复** | 她在私聊里长期攒下的事实、印象、你的近况 |
| 数据来源 | 活会话内存 `_user_sessions['private:<QQ>'].session._conversation_history` | 记忆库 legacy 语料 `fetch_bootstrap_memory(her_name)` |
| 注入函数 | `_append_owner_private_section()`（同步） | `_append_owner_longterm_section()`（**async**） |
| 单条处理 | 每条截前 120 字，按 role 标成「主人：」/「你：」 | 整体按行截断 |

**四个配置项**（`config_store.py:157~169`）：

| 键 | 默认 | 钳制 | 说明 |
|---|---|---|---|
| `cross_domain_owner_memory` | `False` | — | 最近对话开关 |
| `cross_domain_owner_context_rounds` | `8` | `min(40, max(2, n))` | 带几条；8 ≈ 最近 4 轮，下限 2 是为了至少凑出「你一句 + 她一句」 |
| `cross_domain_owner_longterm_memory` | `False` | — | 长期记忆开关 |
| `cross_domain_owner_longterm_max_chars` | `1200` | `min(4000, max(200, n))` | 长期记忆段字符上限 |

> **两个数值都必须写 `8 if raw is None else int(raw)`，不能写 `int(raw or 8)`。**
> `0` 是合法输入（用户想压到最短），`or` 会把它悄悄换成默认值，配 0 等于没配。
> 这是 0.8.5 真踩过的坑，两个函数里都留了注释。

**为什么分成两个开关而不是一个**：这两件事回答的不是同一个问题，可以只开一个；
关一个不影响另一个；连生成中途撤销也各撤各的（见 19.10）。

### 19.4 生效判据（四个同时满足）

1. 对应开关为 `True`
2. 是群聊轮（`is_group=True`）且 `sender_id` 非空
3. `permission_level == "admin"` —— **插件权限名册里的主人**，不是 QQ 群主 / 管理员
4. **不是合成轮**（`is_synthetic_source()` 为假）

第 3 条的完整链路：

```
message_dispatcher（接收边界）
  group_speaker_permission_level_at_receipt = permission_mgr.get_permission_level(sender_id)
      ↓
reply_pipeline._run_context（0.8.6 加的覆盖）
  if is_group and not is_synthetic_source(...) and 快照 == "admin":
      effective_permission = "admin"
      ↓
build_session_instructions(permission_level=...)
      ↓
两个 _append_owner_*_section() 的授权判据
```

**为什么用接收边界快照而不是处理时刻再查**：handler 可能因为并发闸排队几秒，
这期间用户可能在另一页面改了配置 / 名册。记忆和权限相关的判定一律用接收时刻（见 7.6）。

**第 4 条为什么必要**：主动搭话、群主动发言、补消息注入这些轮是插件自己造的，
没有真实发言人，不能拿去盖「发言人是 admin」的章，也不能靠它触发群内认主人。

### 19.5 界面在哪

**引导第 9 步** `step-crossdomain`（`updateGuide()` 里 `add()` 的第 9 个），
面板 `<div id="guide-detail-step-crossdomain">`。

| 元素 id | 作用 |
|---|---|
| `xg-owner-recent` | 最近对话开关 |
| `xg-owner-rounds` | 条数（2~40，`placeholder=8`） |
| `xg-owner-longterm` | 长期记忆开关 |
| `crossDomainGuideSave()` | 保存按钮，调 `save_settings` 三个字段 |

`fillCrossDomainGuide()` 从 `state.settings` 读三个键回填。引导列表的完成态有四种，
由 i18n 键 `step_crossdomain_recent` / `_long` / `_both` / `_off` 决定显示哪句。

**设置页「记忆」区还有一份同样的控件**（不是引导，是老入口）：

`cfg-owner-private-memory`、`cfg-owner-private-rounds`、`cfg-owner-longterm-memory`，
走 `applySettings()` 回填 + `doSave()` 提交。两处改的是同一份配置，别只改一处。

**i18n 键（9 个语言文件都有）**：

```
ui.memory.owner_private / owner_private_depends / owner_private_rounds
ui.memory.owner_longterm / owner_longterm_depends
ui.napcat.guide.step_crossdomain / _recent / _long / _both / _off
ui.napcat.guide.modal_crossdomain
prompts.owner_private_context / prompts.owner_longterm_context
```

前端缓存版本 `index.html` 里是 `napcat.html?v=7`（0.8.8 提的）。
改任何前端文件都要 bump，否则浏览器拿旧页面，会以为代码没生效。

### 19.6 保存链路五层（新增字段必须全走一遍）

```
napcat.html crossDomainGuideSave()
  → POST save_settings          __init__.py：entry 签名 + input_schema 白名单 + 转发
  → dashboard_service           签名 + 转发 + get_dashboard_state 暴露
  → settings_service            _save_settings_locked：写 _qq_settings、钳制、consent 快照
  → business_config.json        落盘
```

四个容易漏的地方：

1. `__init__.py` 的 `input_schema` 带 `additionalProperties: False`，新字段忘了加会被直接拒。
2. 三层函数签名都要加 `Optional[...] = None` 参数，漏一层就 `TypeError`。
3. `settings_service` 的 `deferred_opt_ins` / `_persist_with_consent_rollback`：
   `cross_domain_owner_*_before/after` 四个快照要在写盘失败时回滚运行时策略，
   否则界面显示「已保存」但策略已经变了。
4. **0.8.5 的教训**：`cross_domain_before` 定义在 `_save_settings_locked` 里，却在
   `_persist_with_consent_rollback` 里被引用，真机报 `NameError`。语法检查、单测、
   静态接线全过，还是炸了。

> **规矩：每加一个配置字段，必须真机调一次 `save_settings` 并回读。**
> 见 19.12 的脚本。这条是拿两次线上炸出来的。

### 19.7 后端逐文件职责

| 文件 | 在这个功能里负责什么 |
|---|---|
| `config_store.py` | 4 个配置项的默认值 |
| `__init__.py` | `save_settings` entry 的签名、`input_schema` 白名单、参数转发 |
| `dashboard_service.py` | 保存签名转发；`get_dashboard_state` 把 4 个值暴露给前端 |
| `settings_service.py` | 写 `_qq_settings`、数值钳制、consent 前后快照、写盘失败回滚 |
| `static/napcat.html` | 引导面板 + `fillCrossDomainGuide()` / `crossDomainGuideSave()` + 设置页三控件 |
| `static/index.html` | `napcat.html?v=7` 缓存版本 |
| `i18n/*.json` ×9 | 引导文案、设置页文案、**两段提示词的可覆盖文本** |
| `message_dispatcher.py` | 接收边界快照 `group_speaker_permission_level_at_receipt` |
| `permission.py` | 三档名册 `admin` / `trusted` / `normal`，`get_permission_level()` |
| `reply_pipeline.py` | `_run_context` 群内 admin 覆盖（0.8.6） |
| `session_instruction_service.py` | 两段注入 + legacy 拉取 + 进程内缓存 + 按行截断 |
| `pipeline_models.py` | `QQInstructionBundle` / `QQReplyContext` 各存两个原文段，作为撤销凭据 |
| `reply_context_node.py` | `_strip_owner_private_if_revoked()`：**一个函数、靠 `setting_key` 参数化** |
| `reply_generation_service.py` | consent 快照 + 生成前最后一次复检、摘除 |

### 19.8 两段提示词原文

两个段头都走 `i18n.t()`，可被 9 语言文件覆盖。代码里的 default 全文：

**最近对话**（`prompts.owner_private_context`）：

```
## 你和主人的私聊（Owner Private Chat）
以下是主人和你在 QQ 私聊里最近说过的话，只有你能看到。
**它们只能用来理解主人此刻指的是什么，绝对不要在群里复述原文、引用细节，也不要把私聊里的事主动讲给群里其他人。**
如果主人问的是私聊里提过的事，用「你之前跟我说过」这种含糊说法接住就行，不要说出具体内容。
```

**长期记忆**（`prompts.owner_longterm_context`）：

```
## 你记得的关于主人的事（Owner Long-Term Memory）
以下是你在私聊里长期记住的关于主人的事，只有你能看到。
**在群里用到它们要格外克制：不要主动展开、不要说具体细节、不要把主人的私事讲给群里其他人。**
主人问起的时候，用「你之前跟我说过」这种含糊说法接住，让他知道你没忘就够了。
```

四句硬约束：不复述原文、不引用细节、不主动讲给群里其他人、被问起时用含糊说法接住。

**装配位置**：13 段系统提示词里的第 **8**、**9** 段，紧跟跨群段（第 7）之后、
注意力上下文（第 10）之前，见 13.1。两段各 append 一次，互不含对方内容。

### 19.9 长期记忆的读取链路

```
_append_owner_longterm_section()            [async]
  → _fetch_owner_legacy_memory(her_name)
      → memory_bridge.fetch_bootstrap_memory(key)   # /new_dialog/<her_name> 渲染语料
  → _truncate_memory_text(text, max_chars)
  → header + trimmed → sections.append()
```

细节：

- **进程内缓存** `_owner_legacy_cache[her_name] = (monotonic, text)`。
  成功结果 TTL **300 秒**（`_OWNER_LONGTERM_CACHE_SECONDS`），
  空结果 / 读失败 TTL **60 秒**（`_OWNER_LONGTERM_FAIL_CACHE_SECONDS`）。
  所以不会每句话都去翻记忆库。
- **失败不抛**。`_owner_legacy_diag` 分 `ok` / `empty` / `error` 三态计数
  （踩过「吞异常伪装成本来就没数据」的坑）；连续第 3 次失败起日志升到 `warning`。
  读失败时**缓存上一次成功的值**，别因为一次抖动让整段记忆消失。
- **按行截断** `_truncate_memory_text()`：逐行累加，超上限就停，**不在句子中间砍一刀**；
  整段只有一行时才硬切。
- 不新建记忆库、不产生写入路径。读的是私聊主线本来就读的那份语料，只是换个调用点。

> 别和 `recall_memory` 搞混：那个是**模型主动决定去查**的工具（9.3），
> 这里是**插件自动注入**，来源和触发时机都不同。

### 19.10 撤销链路（三处复检）

```
装配返回值 → QQInstructionBundle.owner_private_section / owner_longterm_section（原文）
  → reply_context_node._strip_owner_private_if_revoked(
        system_prompt, text, setting_key="cross_domain_owner_memory"           # 或 longterm
    )
  → QQReplyContext 继续带着原文
  → reply_generation_service：_capture_consent_snapshot() 记开关值
                              + 生成前 _strip_section_text() 最后一次复检
```

> **注意函数名**：`reply_context_node` 里**只有** `_strip_owner_private_if_revoked()`
> 一个函数，靠 `setting_key` / `label` 两个参数服务两个开关。
> 7.7 早期笔记里写过的 `_strip_owner_longterm_if_revoked` **并不存在**。

为什么要查三处：从「装配提示词」到「真发出去」中间夹着 login / bootstrap / recall
好几个 `await` 窗口，用户可能正好在这一秒把开关关了。不摘除的话，一小时前装配好的
私聊原文就会跟着这一轮发到群里。

`setting_key` 参数化保证了「**只关长期记忆，不会连最近聊到的几句一起丢**」——
两个开关各撤各的。

### 19.11 隐私边界（八条）

1. **单向**：私聊 → 群。反向永久不通，私聊里不会出现群聊的事。
2. **只对 admin**：判据是插件权限名册，与 QQ 群主 / 群管理员身份无关。
3. **只在主人本人发言的那一轮注入**。群里别人说话时整条路径不执行——否则任何群友
   都能从她的回答里套出主人的私事。
4. **合成轮不注入**（没有真实发言人）。
5. **不写回、不迁移、不改库结构**：只是本轮提示词里临时拼一段文本。
6. **关掉不删除**：已经记住的东西还在，只是不再注入。
7. **提示词硬约束**：见 19.8 四句。
8. **可撤销**：生成前复检，见 19.10。

### 19.12 真机验证记录（0.8.8 当天）

```python
# 改完后必跑：保存 → 回读 → 看落盘 → reload → 重启自动回复
call("save_settings", {
    "cross_domain_owner_memory": True,
    "cross_domain_owner_longterm_memory": True,
    "cross_domain_owner_context_rounds": 10,
})
call("get_dashboard_state", {})   # 回读三个键
```

完整调 entry 的脚本在 16.3，复制即用。

**落盘路径**（容易找错，代码和数据不在一个目录）：

```
Q:/N.E.K.O_0.8.3.1_win/N.E.K.O/plugins/qqbetter_auto_reply/data/business_config.json
```

`.neko-plugin-installations/plugins/qqbetter_auto_reply/` 那份是**代码**，配置数据在上面这个
`plugins/` 目录下。0.8.8 验证时三个值落盘为 `True / True / 10`，
reload 后宿主显示 `0.8.8 / user / running`，`start_auto_reply` 后 OneBot 连回。

### 19.13 测试

| 脚本 | 项数 | 覆盖 |
|---|---|---|
| `test_cross_domain.py` | **17** | 最近对话：权限门、开关、条数、角色标注、段头约束、撤销 |
| `test_cross_domain_longterm.py` | **22** | 长期记忆：5 档身份都不注入、关开关时**压根不请求记忆库**、缓存只读一次、读失败不抛且计数、按行截断、`max_chars` 配 0 被钳到下限 |
| `test_group_admin_identity.py` | 10 | 0.8.6 的群内 admin 覆盖探针 |
| `test_sched_greeting.py` | 45 | 回归用，同轮改过前端和保存链路 |

跑法见 15.4（`PYTHONPATH` 必须设，垫片缺了 import 就失败）。

### 19.14 排查清单：她在群里想不起私聊的事

按顺序查，别跳步：

1. **开关是不是真开着** —— `get_dashboard_state` 回读，别只看界面勾选框。
2. **发言的 QQ 是不是名册里的 admin** —— 看 `business_config.json` 的 `trusted_users`。
   群里是群主但不在名册里，一样不生效。
3. **是不是合成轮** —— 主动搭话、群主动发言、补消息注入都不注入。
4. **私聊那侧有没有东西可带**：
   - 最近对话读的是**活会话内存**，宿主刚重启时为空是正常的；
   - 长期记忆 `fetch_bootstrap_memory` 返回空也正常，看 `_owner_legacy_diag` 的 `empty` 计数，
     别和 `error`（服务器挂了）混为一谈。
5. **是不是被撤销了** —— 日志搜「已关闭，注入段在生成前撤除」。
6. **前端缓存** —— 改了 `napcat.html` 没 bump `?v=`，页面还是旧的。
7. **是不是开了两个「QQ集成」** —— 见 18 章第 1 条。

### 19.15 已知限制 / 以后要改

- **只在提示词层临时拼一段**，legacy / scoped 两套库并没有合并。模型这一轮「知道了」，
  下一轮不注入就又「忘了」。这是 0.8.5 定的取舍，不是遗漏。
- **全局开关，不能按群配**。想「A 群开、B 群关」得把配置结构改成按 `group_id` 存。
- **长期记忆是全文按行裁剪，不是相关性检索**。语料一长，头部之后的内容就看不到；
  要改可复用 `recall_memory` 那套检索。
- **最近对话固定取末尾 N 条**，没有时间窗、没有按重要性衰减。
- **只注入，不回写**：群里聊出来的新事不会进私聊 legacy。
- **两条注入都是纯文本**，没有置信度 / 来源标注。
- **提示词约束靠模型自觉**，输出侧没有过滤。如果她在群里提私事还是太主动，
  改 `prompts.owner_private_context` / `prompts.owner_longterm_context` 的措辞（9 个语言文件都要改），
  不要去动装配逻辑。

---

## 附录 A　名词表

| 词 | 意思 |
|---|---|
| **宿主** | N.E.K.O. 本体程序，插件跑在它里面 |
| **插件** | 本仓库这份代码，作为独立进程被宿主管理 |
| **entry** | 插件对外暴露的可调用入口（52 个） |
| **ToolDefinition** | 给猫娘自己用的工具（9 个） |
| **legacy / scoped / participant** | 三种记忆域：主人私聊主线 / 群与普通用户 / 群成员独立桶 |
| **跨域记忆 / cross_domain / 记忆打通** | 同一个功能：把主人私聊的「最近对话」和「长期记忆」单向带进群，见 **19** |
| **合成轮** | 插件自己造的一轮，没有真实发言人 |
| **接收边界快照** | 消息刚收到时盖在 dict 上的配置快照（`_*_at_receipt`） |
| **注意力** | 多群竞争发言权的分数模型 |
| **缓冲** | 攒一攒再回的机制 |
| **payload hash** | `.neko-plugin` 安装时的内容校验值 |
| **LLBot** | LuckyLilliaBot，一个 OneBot v11 后端（LLOneBot 的继任者） |
| **NapCat** | 另一个 OneBot v11 后端，通过 PMHQ 注入 QQ |

## 附录 B　命令速查

```bash
# 同步源码到运行实例（存成 sync.py 更省事，见 15.3 的多行版）
cd Q:/neko开发 && python -X utf8 sync.py

# 重载插件
curl -s -X POST http://127.0.0.1:48916/plugin/qqbetter_auto_reply/reload

# 看插件状态
curl -s http://127.0.0.1:48916/plugins | python -m json.tool | grep -A3 qqbetter

# 跑测试
cd Q:/neko开发/_probe/_guide_testroot && PYTHONPATH="$PWD" python -m pytest -q

# lint（CI 同款）
Q:/neko开发/_tools/ruff/bin/ruff.exe check --ignore-noqa --isolated --no-respect-gitignore --target-version py311 --line-length 120 --select E4,E7,E9,F,I --exclude vendor Q:/neko开发/src/qqbetter_auto_reply

# 打包
cd Q:/neko开发 && .venv/Scripts/python.exe build_and_install.py --plugin qqbetter_auto_reply --name "QQ集成" --version 0.8.8 --desc "..." --build-only

# 语法检查所有改动的文件
cd Q:/neko开发/src/qqbetter_auto_reply && python -m py_compile *.py && echo OK

# 校验 plugin.toml（必做）
python -c "import tomllib;d=tomllib.loads(open('plugin.toml',encoding='utf-8').read());print(d['plugin']['version'])"
```

## 附录 C　版本历史（这条线）

| 版本 | 改了什么 |
|---|---|
| 0.8.3 | 首次独立发布（初始化仓库、补市场发布文件、CI 全绿） |
| 0.8.5 | **记忆打通（最近对话）**：私聊刚说的几句能带进群，`cross_domain_owner_memory` + 轮数（详见 19） |
| 0.8.6 | **群内认主人**（修群等级冒充发言人档位的 bug，**0.8.5 的开关此前形同虚设**，详见 19.2）；保存连接设置后自动重连；收到消息打界面日志 |
| 0.8.7 | **定时搭话**：引导里配「几点 + 让她做什么」，到点生成私聊；每天一次、宽限窗重试 |
| 0.8.8 | **记忆打通（长期记忆）**：`cross_domain_owner_longterm_memory`；引导里做成「私聊↔群 记忆打通」面板，两个独立开关（详见 19） |

更早的改动（0.8.4 及以前）git 里没留提交，以 `dist/` 里的历史包为准。

## 附录 D　依赖关系（谁调谁）

```
__init__.py（装配一切）
├── config_store ──────────── 配置读写
├── settings_service ──────── 保存/校验/生效
│   └── config_store
├── dashboard_service ─────── UI 聚合 + entry 转发
│   ├── settings_service
│   └── 几乎所有 service（只读状态）
├── qq_client / qq_open_plat ─ 连接
│   └── qq_connection（基类）
├── message_dispatcher ────── 主循环
│   ├── permission / group_permission
│   ├── attention_gate_service
│   ├── backlog_service
│   ├── vision_service / sticker_service / face_index
│   └── reply_pipeline ────── 五阶段
│       ├── reply_decision_node
│       ├── reply_context_node
│       │   ├── session_instruction_service
│       │   ├── session_memory_service
│       │   └── memory_bridge
│       ├── reply_model_node ─ reply_generation_service
│       │                        └── 9 个工具 + 各自的 execute_*
│       ├── reply_postprocess_node
│       └── reply_delivery_node ─ qq_client
├── reply_buffer_service ──── 缓冲
├── fatigue_service ───────── 疲劳
├── proactive_private_service ─ 主动私聊
│   └── runtime_ops_service.QQProactiveMessageService
├── scheduled_greeting_service ─ 定时搭话
│   └── runtime_ops_service.QQProactiveMessageService
├── catchup_service ───────── 断线补消息
├── bilibili_browse_service ─ 见闻循环
│   └── bilibili_client
└── gallery_service / sticker_service / voice_reply_service
```

一句话记法：**入口在 `__init__`，主动脉是「dispatcher → pipeline → 五个 node」，横切面是注意力 / 疲劳 / 缓冲 / 记忆，能力件是那九个工具。**

---

*本文档由代码实测生成，数字（62 个 py / 35,394 行 / 89 项配置 / 52 个 entry / 9 个工具 / 728 个 i18n 键）均为 2026-11-08 当次统计。改代码后记得回来看一眼对应数字。*
