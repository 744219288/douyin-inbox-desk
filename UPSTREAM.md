# 上游项目

来源：https://github.com/cv-cat/Douyin_Spider

固定版本：fc8b9c2490a9561265425c26058290f01c064eda

工作台通过 desk/transport.py 适配该固定版本，不执行上游 main.py 的私信示例。公开仓库不包含 vendor/Douyin_Spider 源码；需要真实连接时单独运行 scripts/fetch_vendor.ps1 获取。上游许可边界见 THIRD_PARTY_NOTICES.md。

适配层对上游统一 HTTP 出口强制开启 TLS 证书校验，并把每个请求超时限制为20秒；覆盖上游部分端点显式传入的 verify=False。

本应用只需 requirements-live.txt 中的私信相关依赖。不安装 blackboxprotobuf，因为本适配器不调用需要它的 get_conversation_list 方法，从而避免其旧 protobuf 依赖冲突。完整上游采集、创作者功能不属于本面板。

DeepSeek API 依据：https://api-docs.deepseek.com/api/create-chat-completion/

本次核查默认模型为 deepseek-flash；面板可修改模型名。模型可用性以你的账号和服务商为准。

## 登录适配说明

陌生人收件箱适配参考公开协议结构（不采用其硬编码 token/签名）：
https://github.com/Rockedw/douyin-web-api-sdk/tree/master/src/main/java/com/dy_web_api/sdk/message
本项目自行编码当前账号请求，cmd=1001、inbox_type=1、body=1000，使用只读 get_conversation_list。协议参考为第三方实现，不是抖音官方兼容性保证。

陌生人实时通知与消息批次结构另参考：https://github.com/zhinjs/douyin-im/blob/master/src/services/im/proto/im.proto 及同仓库 src/services/im/stranger.ts。ResponseBody 的 503 字段承载陌生人通知，其字段 2 为 MessageBody；消息补拉使用 cmd=1002、body=1001、reset_unread_count=false。此适配不接受请求、不标记已读、不互关。

普通私信恢复依据同仓库 src/services/im/inbox.ts：cmd=203 /v2/message/get_by_user_init，cmd=301 /v1/message/get_by_conversation；读取所有者发言作为已回复边界。仅处理一对一消息，忽略群聊。

上游 README 推荐官网登录后复制完整 Cookie 至 `.env`；`DouyinAuth.from_qrcode_login` 是纯 HTTP 扫码。私信示例 `dy_apis/douyin_recv_msg.py` 还手工提供 `web_protect_str`、`keys_str`，由 `perepare_auth` 解析配套 ticket、ts_sign、client_cert、ec_privateKey。

本面板新增 `desk/browser_login.py`：用户在独立可见浏览器中完成官网登录，仅读取本次浏览器的抖音 Cookie 和两个签名存储项，保存至本机。不会发送测试私信；“凭证已保存”与“私信已连接”分别显示。

2046 与二次身份验证的关系参考其他实现的人工复现，非抖音官方错误码定义：
https://github.com/HuangYincan/VideoNote-MCP/blob/main/docs/CHANGELOG.md

两个存储键的实现证据：
https://github.com/Levius-Fubuki/douyin-ai-leadgen/blob/main/docs/%E7%A7%81%E4%BF%A1%E8%87%AA%E5%8A%A8%E5%8C%96%E5%AE%9E%E7%8E%B0%E5%8E%86%E7%A8%8B.md

保留纯 HTTP 诊断入口：`python login.py --method http`。遇到 2046 时该路径不能展示官网验证页面，默认应使用浏览器方式。
