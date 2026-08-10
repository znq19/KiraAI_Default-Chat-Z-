# KiraAI_Default-Chat-Z-默认消息处理插件优化版v1.5.3

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/znq19/KiraAI_Default-Chat-Z-)

修改原版开启上下文收听后默认所有语音、图片、合并转发消息都识别的逻辑，减轻小水管模型的负担。当前版本 z 1.5.3，KiraAI2.29.6+ 可用。

此修改版本默认开启只有明确唤醒（如at、关键词和引用回复时的消息中带有的）的语音、图片和转发消息才会被识别。如果关闭设置里的开关，则除了唤醒消息的图片外，其他按概率和数量选取，语音、转发消息全部阅读。

## 新版特性：回复更快、更省 token

- **队列合并（积压处理）**：LLM 处理慢、消息爆发时，同一会话的积压批次自动合并为一次推送，上下文只发送一次、减少 LLM 调用次数——**更省 token**，更不刷屏。默认"不攒批"（当前批次一完成立即合并推送），软合并/超时合并阈值均可配置（`section_queue_merge`）。内置「批次卡死超时」兜底：当前批次超时无响应（LLM 挂起/异常崩溃）时强制推送积压批次，避免会话队列死锁（默认自动跟随 LLM 模型超时）。
- **并行媒体识别**：图片 VLM 与语音 STT 并行预处理，积压批次排队期间媒体即识别完成，推送时零等待——**回复更快**。并发限制分三级（批次级/会话级/全局级）可配，兼容并行识图插件（`section_media_recognition`）；同一消息内的每个媒体最多识别一次，模型限流（429）时不会反复重试。
- **热重载不丢消息**：插件终止/重载时积压批次会以全新批次安全重发，不再出现消息积压后永久无法处理的问题。

## 更新日志

### v1.5.3

**队列合并自拦截死锁修复（与 ContextCondensation 等阻塞型插件共存时稳定复现）**

- **根因**：`BatchMergeScheduler._push_pending()` 调用的 `_decide_and_apply_locked()` 会**无条件清空 `_inflight[sid]`**（即使 pending 为空）；而 KiraAI `EventBus.publish()` 只是**异步入队**（`asyncio.Queue.put`，见 `core/event_bus.py`），发布后的合并批次要等事件循环调度才到达 `on_batch_message`。在这个异步窗口内，若同一会话再次触发 `_push_pending`（ON_STEP_RESULT 重复广播、插件 hook 重复注册、tick 竞争等——`core/message_manager.py::send_llm_text()` 在 Agent 每一步都会触发 ON_STEP_RESULT），会把刚发布的合并批次的 inflight 标记清掉，导致该批次到达 `on_batch_message` 时匹配不上 `_inflight`，被误判为外部批次 `event.stop()` 拦截进 pending，会话队列永久死锁
- **日志特征**：`进入最后一步（文本收尾）` 打印两次（同 event_id）；`发布批次 xxx` 后紧跟 `拦截批次 xxx 进 pending（pending=1）`；之后新消息全部 `拦截进 pending` 且数量只增不减
- **修复**：
  1. `_push_pending(sid, done_event_id)` 增加完成批次校验：锁内先确认 `_inflight[sid]` 仍是本次完成的 event_id 才执行推送决策，重复/并发事件直接跳过，不再误清 in-flight 状态
  2. `_build_merged_batch` 为合并批次打 `_qm_self` 自发布标记，`on_batch_message` 识别后无条件放行并恢复 inflight 跟踪——双保险，对一切竞态路径（含 tick、shutdown 重发）免疫自拦截

安装方法：根据个人喜好可采取两种方式——

方式一：复制文件夹内容替换KiraAI-main\core\plugin\builtin_plugins\chat文件夹下内容，即直接替代原版Default Chat插件。

方式二：复制文件夹到KiraAI-main\data\plugins路径下，但必须webui里关闭原版Default Chat插件或更旧版的Message Debounce插件以免冲突。
