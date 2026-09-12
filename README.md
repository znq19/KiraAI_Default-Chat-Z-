# KiraAI_Default-Chat-Z- 默认消息处理插件优化版 v1.8.7

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/znq19/KiraAI_Default-Chat-Z-)

修改原版默认所有语音、图片、合并转发都识别的逻辑，减轻小水管模型负担。v1.8.4，KiraAI 2.29.6+ 可用（原生多模态兼容需 2.31.0+）。

默认仅唤醒消息（at/关键词/引用回复）中的语音、图片、转发才会识别。关闭对应开关后，非唤醒消息的图片按概率和数量选取，语音/转发全部阅读。


## 亮点

- **队列合并**：积压批次自动合并为一次推送，省 token、不刷屏，超时兜底防死锁
- **并行媒体识别**：图片 VLM 与语音 STT 并行预处理，推送时零等待，三级并发控制可配
- **存在感节流**：统计 bot 发言占比，回少提高概率、回多降低；累计评分门槛过滤（deny）+ 补偿触发（boost）独立控制；**私聊有独立参数**（窗口 10 条、阈值 30、加分 2 扣分 3、目标占比 0.7），默认开，可关掉与群聊共享
- **骚扰感知化**：戳/连续 at/关键词/引用达到阈值 → System 通知 → bot 用 XML tag 决策屏蔽
- **休眠时段**：可配休眠时间窗 + 起夜概率 + 维持期（续窗/一次性/次数上限）
- **热重载不丢消息**：终止时积压批次安全重发，消息不丢失
- **原生多模态兼容**：native 图片模式下图片保留在链中由框架原生直传（官方压缩 + 持久化引用），本插件不预置 caption、不截断数量限制；转发/语音策略照旧（转发仅唤醒才保留、语音 STT 由本插件处理）

想要更多能力？推荐安装 **sustained-chat**（[KiraAI_sustained_chat_plugin](https://github.com/znq19/KiraAI_sustained_chat_plugin)），支持群聊持续对话、私聊主动、定时任务等完整主动社交能力。

## 安装

方式一：复制文件夹替换 `KiraAI-main\core\plugin\builtin_plugins\chat`

方式二：复制到 `KiraAI-main\data\plugins`——原版 Default Chat 或旧版 Message Debounce 插件会被**自动检测并停用**（唤醒词自动迁移，已填写则不覆盖），无需手动关闭

## 🙏 致谢

本插件的存在感节流（回少提高/回多降低）、休眠时段（起夜概率 + 维持期）等机制，在设计上参考并致敬了 **NoriEngine Chat**（[skyzhishui/kira-ai-plugin-noriengine-chat](https://github.com/skyzhishui/kira-ai-plugin-noriengine-chat)）的评分引擎思路——它率先用"存在感抑制 + 时段调度"让 KiraAI 在群聊中也有了心跳包的感受，监听全局消息成为可能，融合版在此基础上把语义判断交还给 LLM，规则只做节流与状态管理。感谢 skyzhishui 的先行探索。

<details>
<summary>更新日志</summary>

### v1.8.7
- **新增「官方 VLM 保护网」**（`guard_framework_vlm`，默认开）：框架自己那条付费识图链路被彻底堵住
  - **问题**：官方 VLM 全项目只有一个触发条件 —— `ele.caption is None`，而它的**渲染发生在所有批次钩子之前**（`message_manager.handle_im_batch_message` 先渲染批次、后派发 `ON_IM_BATCH_MESSAGE`）。也就是说 QueueMerge 的拦截、Midflight 的拦截、本模块 stage2/stage3 的抢救**全都发生在"钱已经花掉"之后**。只要有一条消息的图片没被预置 caption（第三方插件抢先 `stop()`、钩子顺序异常、stage1 异常……），官方就会付费识图，**事后无法挽回**（日志表现为突然冒出 `[llm] Describing image using …`）
  - **修复**：新增最早执行的钩子（`on.im_message` **SYS_HIGH**，早于所有 HIGH 钩子）`guard_captions()` —— 只做一件事：把链上 `caption is None` 的图片/表情**占成空串**（官方空占位 `[Image ]` / `[Sticker ]`）
  - **只占位，不做别的**：不暂存 `_pir_media`、不预取、不发起任何识别、不改任何消息策略（不 buffer/discard/stop）、不删不换元素；识别仍由 `handle_msg` + stage1 在 HIGH 按「仅唤醒识别/概率/超限」决定，**省 VLM 语义完全不变**；只在 caption **是 None** 时写 `""`，**绝不覆盖已有描述**；PIR 接管图片 / 原生多模态模式下一律不碰（与 stage1 跳过条件一致）；「启用并行媒体识别」关闭时不生效（那种配置下本来就该由框架识图）
  - **兼容性**：框架里 `caption is None` 只用在官方识图那一个判断上；本插件全程用 `(caption or "")`，**空串与 None 完全等价** ⇒ 自己的行为零变化。唯一影响：依赖「`caption is None` = 框架还没识图」这一信号的第三方插件，会看到该字段被提前占位（关掉本开关即可恢复原状）
- **修复「bot 自己发的图/表情被当成识别对象」**：框架的钩子循环**只在 `stop()` 时中断，`discard()` 不中断**（`core/message_manager.py`），所以宿主按"机器人自身消息"`discard()` 之后，stage1 **照样会执行**——把 bot 自己那条消息里的图片/表情设成待识别并登记进本回合暂存索引。而 stage3 判断"这条媒体在不在请求里"用的是**文本锚点**，官方空占位的锚点是**通配**的（`Sticker` 是 `[Sticker ]`，无路径无 id；`Image` 落盘失败是 `[Image ]`）⇒ 只要请求里存在任意一个未识别表情包的空占位，暂存索引里**所有** caption 为空的表情包都会被命中，**包括 bot 自己那张**：白跑一次 VLM，还会把它的描述与 `file_path` 填进**用户那张**的空占位（张冠李戴）
  - **修复**：stage3 抢救新增**准入条件** `_batch_media_ids(event)` —— 只有**元素确实出现在本批次消息链里**（键与 stage1 同源，取 `elem._pir_short_id`）的媒体才允许被抢救；通配文本锚点不再能单独作为"这条在请求里"的证据
- 版本 v1.8.6 → v1.8.7

### v1.8.6
- **修复「第三方插件重建的媒体副本」触发官方 VLM（付费）**：会话合并 / 上下文压缩类插件在 `on_llm_request` 用历史重建请求时，会把被回复消息的媒体**重新下载成另一个临时文件**（`download_10.jpg` → `download_11.jpg`）——此刻链上已无对应元素，空占位只以**文本**形式嵌在 `Reply.content` 里，元素级兜底拿不到元素 → 框架 `if ele.caption is None` 成立 → 官方 VLM 付费调用
- **修复**：新增**文本级兜底** `_fill_empty_official_by_path()`（置于 `if not need: return` 之前）——按空占位里的 `file_path` 取**文件内容 md5** 查描述缓存（与框架 `hash_image()` 的 path 分支同口径），命中**就地替换**文本；md5 不同（重压缩 / 改尺寸）用 **dHash 感知哈希（汉明距离 ≤2）** 兜底；**只做缓存命中补齐、不发起任何新识别**（拿不到元素就拿不到"跳过标记"）
- **效果**：同图换文件名 / 重压缩副本 / 嵌在回复 `content` 内三种形态全部补齐，**0 次新增 VLM**
- 版本 v1.8.5 → v1.8.6

### v1.8.5
- **关键修复**：媒体识别回填被后续插件覆盖
- **根因**：本插件的媒体兜底（stage3）注册在 `Priority.HIGH`（最先执行），而 **KSM 会话合并(-50)** / **CC 上下文压缩(-51)** 等插件会在 `on_llm_request` 里**重建 `req.messages`** —— 我们在它们之前回填，结果被整体覆盖，请求里仍是空占位 `[Image , file_path: p]` / `[Sticker ]`（表现为"看不见图"）。
- **修复**：媒体 stage3 优先级改为 **-60（最后一个执行）**，确保回填落在最终请求文本上；同时保留写回 `elem.caption`，任何后续重渲染也带着描述。
- 版本 v1.8.4 → v1.8.5

### v1.8.4
- **修复空占位「看不见图」+ 真·媒体预处理（预取）**
- **官方怎么做的**：框架 render 里 `if ele.caption is None: desc_img(...)` —— 只要媒体要渲染进 LLM 请求就识别（内置聊天插件从不碰 media）。「跳过识别」是本插件独有的省钱机制。
- **真·预处理（本次新增）**：消息**确定进入批次**（`event.buffer()`）时立刻在后台预取它的媒体 —— 而这段时间正是「上一个批次的 LLM 还在跑 / 本批次在队列里排队」的空窗。放行时 stage2 直接从结果池命中，**本批次关键路径零识别开销**（实测放行后 VLM=0）。被 `discard()` 的消息走不到 buffer ⇒ 不预取、不浪费。私聊同样生效。
- **尊重「仅唤醒识别」**：非唤醒消息的媒体（`_media_skip_reason=mention`）、概率未中、超出每消息上限的，**既不预取也不兜底**——空占位是这些开关的既定代价，不是 bug。
- **兜底只救「本该识别却没补上」的**：`on_llm_request` 时仍无描述、且**没有跳过标记**的媒体（stage2 没跑 / md5 键变化 / 批次被第三方插件截断）→ 现场补识别并回填 prompt 与 `caption`。
- **引用链**：唤醒消息里 `Reply.chain` 上的媒体按唤醒处理 → 会识别（「引用那条带图消息 + 叫我」能看图）。
- **顺带修复**：兜底把 `Sticker` 误判为音频走了 STT 分支（应与 stage2 的 `type in ("Image","Sticker")` 一致走 VLM）。
- **不需要任何新配置。**
- 版本 v1.8.3 → v1.8.4

### v1.8.3
- **追加修复（媒体空占位兜底 + 合并批次模型组）**
- **新增「官方空占位」兜底抢救**：此前只能在 LLM 请求前抢救 `[Image #id: ]` 形式的标识符，而图片/表情包实际渲染成官方空占位 `[Image , file_path: p]` / `[Sticker ]`——一旦 stage2 因异常或第三方插件（如批次级拦截插件）**stop 掉批次**而未回填，这类空占位会被原样送进 LLM（表现为"看不见图"），且**没有任何兜底**。现在 stage1 就把待识别索引登记到会话回合表，stage3 在 LLM 请求前反查「caption 仍为空」的媒体并现场补齐。
- **合并批次继承 `model_group`**：此前合并批次未继承原批次的自定义模型组，配置了会话级模型组的批次经队列合并后会**静默改用默认模型**。
- 版本 v1.8.2 → v1.8.3

### v1.8.2
- **新增「评分加减关键词」+ 自己消息过滤修正**
- **评分加减关键词**（存在感节流区新增 4 项配置）：自定义「加分关键词 / 减分关键词」，命中的用户消息直接给累计分加减分，从而影响评分补正的触发。**只统计用户消息，bot 自己的发言一律不计分。**
  - 词与分值都是**标签输入**（输入一个按回车 = 一个标签），按**标签顺序一一对应**：第 1 个分值对第 1 个关键词。
  - 配对规则：只填 1 个分值 → 所有词都用它；分值标签少于词数 → **最后一个分值沿用给后面的词**；多于词数 → **多余的忽略**；留空 → 默认 5。
  - 容错：分值标签里写成 `10,5`／`10 5`／`10、5` 会自动拆成两个分值，不必纠结写法。
  - 大小写不敏感子串匹配；同一个词在一条消息里出现多次**只计一次**；加分词与减分词可同时命中。群聊/私聊**共用同一份**配置。
- **修正机器人自己发言的过滤**：此前本插件**完全没有**该过滤，bot 自己的消息（适配器可能作为普通消息送达，如 NapCat 的 `reportSelfMessage`）会被当成用户消息——存在感占比、累计评分、额外信号、骚扰检测被自身发言污染，且与发送事件重复计数。现在**群聊/私聊一致丢弃**。bot 自身发言的正确统计口径是发送事件（`on.message_sent` 的 `bot_speech`），不受影响。
- 版本 v1.8.1 → v1.8.2

### v1.8.1
- **媒体识别修复（“VLM 跑了却看不见图”）**
- **修复识别结果被静默丢弃**（关键）：框架在批次处理时会先**压缩图片**（`compress_image_element` 置 `media.md5 = None`），渲染时又**重新 `hash_image()`**，导致元素 md5 与 stage1 记录的键不再一致。此前 stage2 从 `elem.md5` 反推查找键 → 查不到 → 识别结果被静默丢弃，LLM 只收到空的 `[Image , file_path: ...]`。现在 stage1 会把键钉在元素上（`_pir_short_id`），stage2 优先使用它；md5 / `noid_` 兜底保留兼容。
- **修复会话级能力判定分叉**：框架按「会话级生效能力」（`session_mgr.get_effective_capabilities`，会话覆盖优先于全局）解析 `image_recognition.mode` / `desc_prompt`，而此前本插件只读全局 `bot_config`。一旦某会话单独覆盖过配置就会出现：全局 native + 会话 vlm → 我们跳过、框架自己识图；全局 vlm + 会话 native → 我们照常识图而框架走原生直传、**本次 VLM 完全白跑**。现已严格对齐框架口径。
- **识图日志可观测**：VLM 调用此前完全静默，无法与框架自身的识图日志区分。现使用专用日志器 `MediaRecognize`（**紫色**，与框架 `llm` / 并行识图插件 `parallel_vlm` 同款配色），输出与官方同款文案 `Describing image using <model> (<provider>)`。
- 版本 v1.8.0 → v1.8.1

### v1.8.0
- **稳定性与接管完善**
- **自动接管默认聊天插件**：检测到框架内置 `default-chat` 已加载时自动停用，并**迁移其唤醒词**（仅迁移 `waking_words`；本插件已填写唤醒词则不迁移、不覆盖）。避免两者同时启用造成双重防抖/buffer（顺延延迟翻倍、批次计数错乱）。独立防骚扰插件（`anti-harass`）同样自动停用。
- **修复 VLM 泄露**（评分门控降级）：消息被评分门控判定为“不触发”而从唤醒降级为围观时，此前按唤醒口径保留的待识别图片会被继续送 VLM/STT——消息最终不进入 LLM，识别成本全部白付。现在降级时同步回补非唤醒口径的媒体标记，识别成本为 0。
- **通知合并任务自清理**：`_flush_later` 结束后主动释放自身引用，避免已完成 Task 对象按会话累积。
- 版本 v1.7.8 → v1.8.0

### v1.7.8
- **媒体管线重构（与 **Plus-One 复读插件**兼容 + 官方格式对齐）**
- **Image/Sticker 元素保留**：表情包可被 Plus-One 正确复读；图片元素保留则纯图片消息天然不参与复读。识别结果预置官方 `caption`，渲染官方 `[Image 描述, file_path: ...]` / `[Sticker 描述]`。
- **仅唤醒识别完整保留**：非唤醒媒体预置空 caption（官方空占位），零 VLM、LLM 知道有媒体。
- **自动互斥接管**（默认开）：自动关闭并行识图插件（PIR），识别完全由本插件接管；同样自动停用框架内置 `default-chat` 与独立 `anti-harass`（详见 v1.8.0 说明）。
- **原生多模态不截断**：数量限制在 native 模式自动跳过（全直传，框架压缩控 token）。
- **native 超限占位**：native 模式超限图片替换为 `[Image attached]` 占位拦直传（省 token，LLM 仍知道有图）；Sticker 永不占位（复读优先）。
- **唤醒消息图片上限**（`max_images_per_message_mentioned`，默认 0 = 不限制）：唤醒消息超限图片同样占位省 token。
- **native 仅唤醒识别生效**：仅唤醒开时非唤醒图片占位拦直传、唤醒图片保留直传（LLM 直接看图）。
- **native 表情包跟随仅唤醒**（`native_sticker_follow_mention`，默认开，受上级仅唤醒开关门控）：非唤醒表情包占位 `[Sticker attached]` 省 token；注意开启后 Plus-One 复读表情包会不正确（复读占位文本），酌情关闭以保复读。
- 版本 v1.7.7 → v1.7.8

### v1.7.7
- **-1 永久不再绕过钳制**：设置最大时长限制（max_duration/extra_max_duration>0）后，bot 输入 -1 按最大允许值执行（不再永久）；仅未启用上限时 -1 才真正永久；allow_bot_duration=False 时 -1 也强制默认时长。hint 已同步更新
- **白名单豁免**：`harass_whitelist_users` / `harass_whitelist_sessions` 中的用户/会话不受任何屏蔽影响（消息照常进入 LLM）——原先白名单仅挡检测不挡屏蔽
- **额外信号独立钳制配置**：user_msgs / bot_speech / session_msgs 不再兜落 poke 配置，新增 `extra_max_duration`（默认 300，0=不钳制）/ `extra_allow_bot_duration`（默认开）——bot 自设时长钳到上限，关闭则强制默认时长
- **通知动态教"允许最大值"**：额外信号通知里建议的 duration 动态取 `extra_max_duration`（未启钳制回落 `extra_default_duration`）；不再教 `-1`（永久仅在 hint 中说明，避免绕过钳制）
- **bot_speech 开关**：新增 `bot_speech_block_session`（默认开）——检测到 bot 发言过多时，通知教会话级拉黑标签 `<ignore>all|duration:N</ignore>`（输入 = 拉黑当前会话，所有消息停止进入 LLM，N 秒后自动恢复）；关闭则仅提醒（bot 自觉）
- **hint 补全**：`<ignore>` / `<poke_ignore>` 标签描述补"-1 表示永久"
- 版本 v1.7.6 → v1.7.7

### v1.7.6
- **过滤空通知事件（QQ 戳一戳别人等系统通知）**：框架把所有 notice（poke 别人/运气王/头衔/荣誉/进退群/管理员等）以"message_id=None、零内容"的消息事件广播给插件，此前会进入评分（+3）、前文缓冲、主动概率判定（刷"评分补正"日志）与顺延重置（刷"顺延开始"日志）
- 修复后：`is_notice` 且消息链完全为空 → 丢弃（群聊/私聊一致），不参与评分/前文/判定/顺延；有内容的全部保留（poke bot 的 `[Poke …]` 文本、`[System: …]` 系统提示、图片/语音/贴纸/文本消息）
- 兼容 qq-enhance（Priority.HIGH+1 先增强 bot 相关通知 → chain 非空 → 保留）；只依赖框架核心字段 `event.is_notice` + `message.chain`
- 版本 v1.7.2 → v1.7.6

### v1.7.2
- **消息缓冲模型重构（前文+批次）**：与原版语义对齐并修复丢消息——
  - `max_unmentioned_messages`：唤醒消息**之前**的非唤醒前文上限（超限弹最老前文，唤醒出现后前文锁定不裁剪）
  - `max_buffer_messages`：**从首个唤醒消息起**（含它）进入 buffer 的消息数，达到即满即推；批次内唤醒/普通消息一视同仁（不重置）
  - 推送内容 = 前文 + 批次全部；未满即推则顺延到点推送
- **修复配置迁移写回崩溃**：首次更新自动迁移时缺少 `import json`（NameError），且旧实现 `open("w")` 先截断再 dump 导致失败时配置文件被清空（下次启动报 Expecting value）——现改为先序列化再原子写回，失败也保证原配置文件完好
- **修复非唤醒消息裁剪丢失唤醒消息**：buffer 满（max_unmentioned_messages）时旧逻辑直接弹最老消息，会把唤醒消息一并弹掉（用户实测"导员1111"被丢弃）。现裁剪只弹非唤醒消息，唤醒消息永不被裁剪
- **顺延容量安全阀**：buffer 达到 max_buffer_messages 时立即 flush（框架 SessionBuffer 不自动 flush），避免顺延期间消息无限积压/被裁剪丢弃
- 版本 v1.7.1 → v1.7.2

### v1.7.1
- **修复非唤醒消息不重置顺延**：之前 merge_window_seconds 顺延只被唤醒消息重置，非唤醒消息（receive_unmentioned）到达后计时器不重置——导致顺延形同"首条唤醒消息后固定 N 秒"。现在非唤醒消息也会重置计时器，真正实现"最后一条消息到达后 N 秒无新消息才 flush"
- 版本 v1.7.0 → v1.7.1

### v1.7.0
- **配置分组升级**：配置项改为与 sustained-chat 一致的分组模式（section_basic / section_media / section_presence / section_dm_presence / section_poke / section_at / section_keyword / section_reply / section_dormant / section_harass_scope），WebUI 更清晰
- **首次更新自动迁移**：旧版扁平配置升级后自动迁移为分组结构（仅迁移一次，config_version 标记），老用户无需手动改配置
- **消息合并顺延默认启用**：`merge_window_seconds` 默认 -1（自动取 WebUI 设置值），新装/升级后立刻体现合并顺延特性
- **顺延调试日志**：`section_basic.debug_log_enabled`（默认关），开启后打印顺延开始/重置/结束日志
- **清理死代码**：`queue_merge.py` 中未使用的 `merge_window_seconds` 字段移除（积压队列合并仍由 `max_merge_seconds` 超时控制）
- 版本 v1.6.6 → v1.7.0

### v1.6.6
- **私聊独立存在感节流**：私聊有独立评分/k_prob 参数（窗口 10、占比 0.7、阈值 30、加分 2 扣分 3），默认开
- **评分补正细化**：`proactive_score_gate_deny/boost`（默认开）+ `mentioned_*`（群聊/私聊，默认关）
- **概率调节独立开关**：`proactive_k_prob_enabled`（默认开）
- 版本 v1.6.2 → v1.6.6

### v1.6.2
- 评分补正拆为 `score_gate_deny`（门槛过滤）+ `score_gate_boost`（补偿触发），三条通路独立控制
### v1.6.0 ~ v1.6.1
- 存在感节流 + 骚扰感知化 + 休眠时段完整能力
- 拉黑语义：屏蔽=该用户/会话所有消息不再进入；poke 单独屏蔽只挡戳一戳
- 累计评分：用户消息 +1、bot 回复 -5，攒到阈值补触发
- tick 防抖：修复积压批次被单独发布不合并的问题
- XML 合并：`at_ignore`/`kw_ignore`/`reply_ignore` 合并为 `<ignore>`（拉黑）
### v1.5.x
- 队列合并、并行媒体识别、热重载不丢消息、原生多模态兼容
- 最后一步带工具即时收尾、媒体识别填充修复

</details>
