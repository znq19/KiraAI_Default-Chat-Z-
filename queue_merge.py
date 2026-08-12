"""队列合并 / 积压处理调度器（v2.3）

设计要点（对齐方案文档 v2.3）：
- 只在"当前批次（in-flight）处理中"时拦截后续批次进 pending；
- 推送决策三分支（都在 in-flight 完成时执行，串行）：
    分支① 软合并：pending 消息总数 <= 软合并上限 且 <= 合并消息数上限 且估 token <= 上限 -> 提前合并（不等超时）
    分支② 超时合并：pending 攒批时间 >= max_merge_seconds -> 合并（无论消息数，超限拆批）；
                   =0 时恒成立（不攒批，当前批次完成即全量合并，拆批由各上限控制）
    分支③ 独立推送：都不满足 -> 只推第一个批次（1:1），其余留 pending 等下一轮
- 用"事件配对"判定 in-flight 完成（0 延迟，无 release_delay）：
    ON_LLM_RESPONSE 无 tool_calls = 最后一步（文本收尾）-> 标记 _final_marked
    ON_STEP_RESULT（消息发送后触发）同 event_id 且已标记 -> 执行推送决策
- 自拦截防护：合并/重放批次打 _qm_self 自发布标记，on_batch_message 识别后无条件放行
  （不依赖 in-flight 匹配，对竞态/重复事件/重放路径免疫，防死循环）
- 推送决策双保险：on_step_result 传入 done_event_id，_push_pending 锁内确认 in-flight
  仍是本次完成事件才执行（hook 重复注册/事件重复广播时跳过，不误清 in-flight）
- 积压媒体限制（media_preprocess_enabled + media_preprocess_max_batches）：
    含媒体批次在 pending 已满上限时直接放行独立处理，避免媒体无限积压 + VLM/STT 重复预处理
- 调试日志开关（debug_log_enabled）：开启后打印放行/拦截/三分支/拆批等状态，便于排查
- 合并批次必须沿用原 KiraIMMessage 引用（并行识图 _pir_images 依赖，绝不克隆）
"""
from __future__ import annotations

import asyncio
import time