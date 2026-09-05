# KiraAI_Default-Chat-Z- 默认消息处理插件优化版 v1.7.5

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/znq19/KiraAI_Default-Chat-Z-)

修改原版默认所有语音、图片、合并转发都识别的逻辑，减轻小水管模型负担。v1.7.2，KiraAI 2.29.6+ 可用（原生多模态兼容需 2.31.0+）。

默认仅唤醒消息（at/关键词/引用回复）中的语音、图片、转发才会识别。关闭对应开关后，非唤醒消息的图片按概率和数量选取，语音/转发全部阅读。

## 亮点

- **队列合并**：积压批次自动合并为一次推送，省 token、不刷屏，超时兜底防死锁
- **并行媒体识别**：图片 VLM 与语音 STT 并行预处理，推送时零等待，三级并发控制可配
- **存在感节流**：统计 bot 发言占比，回少提高概率、回多降低；累计评分门槛过滤（deny）+ 补偿触发（boost）独立控制；**私聊有独立参数**（窗口 10 条、阈值 30、加分 2 扣分 3、目标占比 0.7），默认开，可关掉与群聊共享
- **骚扰感知化**：戳/连续 at/关键词/引用达到阈值 → System 通知 → bot 用 XML tag 决策屏蔽
- **休眠时段**：可配休眠时间窗 + 起夜概率 + 维持期（续窗/一次性/次数上限）
- **热重载不丢消息**：终止时积压批次安全重发，消息不丢失
- **原生多模态兼容**：native 图片模式下保留图片链直传框架，本插件只做音频 STT

想要更多能力？推荐安装 **sustained-chat**（[KiraAI_sustained_chat_plugin](https://github.com/znq19/KiraAI_sustained_chat_plugin)），支持群聊持续对话、私聊主动、定时任务等完整主动社交能力。

## 安装

方式一：复制文件夹替换 `KiraAI-main\core\plugin\builtin_plugins\chat`

方式二：复制到 `KiraAI-main\data\plugins`，需在 WebUI 关闭原版 Default Chat 或旧版 Message Debounce 插件

## 🙏 致谢

本插件的存在感节流（回少提高/回多降低）、休眠时段（起夜概率 + 维持期）等机制，在设计上参考并致敬了 **NoriEngine Chat**（[skyzhishui/kira-ai-plugin-noriengine-chat](https://github.com/skyzhishui/kira-ai-plugin-noriengine-chat)）的评分引擎思路——它率先用"存在感抑制 + 时段调度"让 KiraAI 在群聊中也有了心跳包的感受，监听全局消息成为可能，融合版在此基础上把语义判断交还给 LLM，规则只做节流与状态管理。感谢 skyzhishui 的先行探索。

<details>
<summary>更新日志</summary>

### v1.7.2

- **消息缓冲模型重构（前文+批次）**：与原版语义对齐并修复丢消息——
  - `max_unmentioned_messages`：唤醒消息**之前**的非唤醒前文上限（超限弹最老前文，唤醒出现后前文锁定不裁剪）
  - `max_buffer_messages`：**从首个唤醒消息起**（含它）进入 buffer 的消息数，达到即满即推；批次内唤醒/普通消息一视同仁（不重置）
  - 推送内容 = 前文 + 批次全部；未满即推则顺延到点推送
- **修复配置迁移写回崩溃**：首次更新自动迁移时缺少 `import json`（NameError），且旧实现 `open("w")` 先截断再 dump 导致失败时配置文件被清空（下次启动报 Expecting value）——现改为先序列化再原子写回，失败也保证原配置文件完好
- **修复非唤醒消息裁剪丢失唤醒消息**：buffer 满（max_unmentioned_messages）时旧逻辑直接弹最老消息，会把唤醒消息一并弹掉（用户实测"导员1111"被丢弃）。现裁剪只弹非唤醒消息，唤醒消息永不被裁剪
- **顺延容量安全阀**：buffer 达到 max_buffer_messages 时立即 flush（框架 SessionBuffer 不自动 flush），避免顺延期间消息无限积压/被裁剪丢弃

### v1.7.1

- **修复非唤醒消息不重置顺延**：之前 merge_window_seconds 顺延只被唤醒消息重置，非唤醒消息（receive_unmentioned）到达后计时器不重置——导致顺延形同"首条唤醒消息后固定 N 秒"。现在非唤醒消息也会重置计时器，真正实现"最后一条消息到达后 N 秒无新消息才 flush"

### v1.7.0

- **配置分组升级**：配置项改为与 sustained-chat 一致的分组模式（section_basic / section_media / section_presence / section_dm_presence / section_poke / section_at / section_keyword / section_reply / section_dormant / section_harass_scope），WebUI 更清晰
- **首次更新自动迁移**：旧版扁平配置升级后自动迁移为分组结构（仅迁移一次，config_version 标记），老用户无需手动改配置
- **消息合并顺延默认启用**：`merge_window_seconds` 默认 -1（自动取 WebUI 设置值），新装/升级后立刻体现合并顺延特性
- **顺延调试日志**：`section_basic.debug_log_enabled`（默认关），开启后打印顺延开始/重置/结束日志
- **清理死代码**：`queue_merge.py` 中未使用的 `merge_window_seconds` 字段移除（积压队列合并仍由 `max_merge_seconds` 超时控制）

### v1.6.6

- **私聊独立存在感节流**：私聊有独立评分/k_prob 参数（窗口 10、占比 0.7、阈值 30、加分 2 扣分 3），默认开
- **评分补正细化**：`proactive_score_gate_deny/boost`（默认开）+ `mentioned_*`（群聊/私聊，默认关）
- **概率调节独立开关**：`proactive_k_prob_enabled`（默认开）

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