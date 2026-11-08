# 工作区后端接口契约

此文件描述已实现的后端模块；页面和路由集成由主代理负责。视觉方案已获用户确认，整体验收仍需由主代理完成。

## DeskService 方法

- `workspace_state()`：返回 `mode, knowledge[], replies[], materials[], overview, stages[]`。
- `workspace_action(resource, body)`：`resource` 为 `knowledge/replies/materials`；`action` 为 `save/delete/search`，默认 `save`。保存/删除返回工作区状态；知识检索返回 `{documents:[]}`。已有 ID 必须属于当前模式和资源。新增不得自定 ID。页面应提交 `expected_mode`；如当前模式已改变，同一写入锁内拒绝操作，避免检查与提交之间的切换竞态。旧调用未提供该字段仍兼容。
- `customer_update(cid, body)`：部分更新 `profile,tags,notes,stage,follow_up`，返回会话详情。资料和计划只保存本机，不触发发送。
- `handoff(cid, reason)`：取消未提交回复，转 `waiting` 待接手，保存原因，返回会话详情；保留原有同会话发送锁语义。`takeover(cid)` 才转 `human` 已接手，并保留交接原因、记录接手时间。
- `analyze(cid, {source})`：`source` 为 `local/model/agent`，默认 `local`。模型与 Agent 仅显式调用；无配置报错。结果写摘要，不自动修改阶段、客户画像或备注。
- `test_integration(kind, query)`：`kind` 为 `local/knowledge/agent/model`；返回 `{ok,kind,detail,documents?,reply?,context?,handoff?}`。明确测试所选服务，可能联网调用配置服务，但从不调用抖音传输层。

所有输入/输出为 JSON 可序列化对象，参数无效抛 `ValueError`。外部 HTTP 错误使用 `IntegrationError`；网络/超时/返回大小/JSON协议错误使用 `IntegrationProtocolError`，均为固定脱敏诊断。不要把通用 `RuntimeError` 的任意文本返回页面。路由应保留同源与请求令牌校验。

## 工作区条目

共有：`id,title,content,tags[],updated_at`。`knowledge` 另有 `source,status:draft|published,audience:public|internal,version`。只有已发布且允许对客的资料参与自动回复检索。版本号每次保存递增，当前不提供历史版本回滚。

`materials` 另有 `kind:link|image|file,url,asset_id,name?,size?`。填写附件 ID 时调用 `AssetStore.get(id,mode)` 验证归属，名称/大小/类型来自服务端附件元数据。这里只负责素材条目，物理上传/下载/发送由 `desk/assets.py` 负责。

知识检索为本地词项与中文双字匹配，标题/标签加权；不是向量检索。返回至多6条 `{id,title,text,source,version,score}`。长条目选择包含匹配词的原文片段，每条最多提供2500字给模型，避免命中后半文却返回无关开头；空匹配返回空数组。

## 客户与交接

`conversation(cid)` 额外返回 `workspace`：

```json
{
  "profile": "操作员确认的业务画像",
  "tags": [],
  "notes": "本机内部备注",
  "stage": "new",
  "summary": "原文摘录或待核对AI分析",
  "summary_source": "local",
  "summary_updated_at": "",
  "summary_through_message_id": "",
  "summary_stale": false,
  "evidence": [{"message_id": "本地消息ID", "quote": "逐字原文片段"}],
  "recent_messages": [],
  "handoff": {"reason": "", "at": "", "context": "最近客户原文摘录", "status": "waiting", "accepted_at": "", "resolved_at": ""},
  "follow_up": {"at": "", "note": "", "done": false},
  "updated_at": ""
}
```

阶段为 `new/interested/negotiating/won/closed`；`stages` 给出中文名称。阶段仅由操作员保存客户资料时改变，不从模型分析自动推断意向或成交。跟进时间需 ISO8601 且包含时区，空字符串清除。到期统计根据真实时间和 `done` 计算；不会到期自动发送私信。

每条新文字消息更新最近原文与交接上下文，不额外调用模型。已有模型/Agent摘要遇到新客户文字时标记过时，保留旧结果供核对；本地摘要自动刷新。模型和 Agent 分析均要求至少一条输入中真实客户消息ID及逐字片段，否则拒绝保存；旧版已存的零证据摘要也不会用于自动回复。异步分析期间会话或客户资料变化时不覆盖较新内容。

手动回复自动进入 `human`，交接元数据标记 `accepted`。恢复AI时元数据标记 `resolved` 并记录时间，保留历史原因和接手时间供核对；界面应将其作为历史交接显示。新的交接会重新进入 `waiting`。

`state()` 会话列表另有 `stage,tags,follow_up_at,follow_up_done` 便于筛选。`overview` 包含真实会话数、待人工数、到期跟进数、已发布知识数。

## 自动回复集成

保留 `responder(settings,history,text)` 原接口。调度时复制设置并加入私有 `_local_knowledge` 和 `_customer_context`；不持久化或公开这些临时键。客户参考包含已填写业务画像、标签、阶段、当前摘要及其来源/过时标识、最近客户原文；过时模型摘要不注入新回复，内部备注和跟进计划留在本机。

知识修改、撤回、改内部、删除后，当前模式尚处于 `pending` 的 AI 回复会废弃并用新知识重新生成。客户画像/标签/阶段或分析摘要改变时，只替换当前会话的 pending 回复。仅修改本机备注/跟进计划不会额外调用模型。所有替换在资料更新同一个锁内完成，不重试失败历史，不解除人工状态。已进入 `sending` 的发送已经提交，结果会据实记录，不撤回、不重发，也不因资料变化生成第二条。

外部 Agent 保留原 `message/history/knowledge` 请求字段，并增加 `customer`。空响应不作为成功处理；`handoff:true` 转人工；返回内容作为参考数据传给模型，不执行外部操作。分析请求额外带 `action:analyze,messages`；可返回 `summary/context/reply` 作为摘要文本，但必须同时返回非空 `evidence:[{message_id,quote}]`，引用逐条验证后才保存。普通咨询和显式接口连通测试仍兼容旧 `context/reply` 协议。

SQLite 表 `workspace_items` 按 mode 分区；`customer_workspace` 使用包含 mode 的会话 ID。旧数据库可直接添加表，无需迁移已有聊天记录。

## 验证与当前限制

新功能回归在 `tests/test_workspace.py`：持久化、跨模式拒绝、草稿/内部资料排除、版本、客户更新原子性、跟进统计、素材归属、原文证据、摘要过时、分析冲突、Agent空响应、显式测试无私信发送。

测试使用本地临时 SQLite 与模拟外部响应；尚未验证用户真实模型和 Agent 配置。新版UI由前端代理与主代理集成验收。
