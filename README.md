# KiraAI_Default-Chat-Z-默认消息处理插件上下文选取优化版
修改原版开启上下文收听后默认所有图片、合并转发消息都识别的逻辑，减轻小水管识图模型的负担。当前基于Default Chat1.2，KiraAI2.9.1+。理论上作为一种插件，KiraAI本体版本高低不影响使用。

此修改版本默认开启只有明确唤醒（如at、关键词和引用回复时的消息中带有的）的图片和转发消息才会被识别。如果关闭设置里的开关，则除了唤醒消息的图片外，其他按概率和数量选取，转发消息全部阅读。

安装方法：根据个人喜好可采取两种方式——

方式一：复制文件夹内容替换KiraAI-main\core\plugin\builtin_plugins\chat文件夹下内容，即直接替代原版Default Chat插件。

方式二：复制文件夹到KiraAI-main\data\plugins路径下，但必须webui里关闭原版Default Chat插件或更旧版的Message Debounce插件以免冲突。
