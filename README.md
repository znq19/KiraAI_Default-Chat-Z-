# KiraAI_Default-Chat-Z- 默认消息处理插件优化版 v1.6.5

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/znq19/KiraAI_Default-Chat-Z-)

修改原版默认所有语音、图片、合并转发都识别的逻辑，减轻小水管模型负担。v1.6.5，KiraAI 2.29.6+ 可用（原生多模态兼容需 2.31.0+）。

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

### v1.6.5

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