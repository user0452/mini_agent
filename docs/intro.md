# Mini Agent 文档

这个示例不依赖 LangGraph、OpenHands、OpenClaw、LangChain 等 Agent 框架。

Agent 的主流程只有四个要素：消息历史、模型调用、工具注册表、while 循环。
当模型返回 `tool_calls` 时，程序执行对应函数，把结果作为 `role=tool` 消息追加到历史，
然后继续请求模型；当模型不再返回工具调用时，`content` 就是给用户的最终答案。
