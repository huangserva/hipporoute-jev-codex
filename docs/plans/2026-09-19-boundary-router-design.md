# Codex + Jev 边界路由器设计

## 架构

服务采用 Python 3.11+ 标准库包，监听回环地址并暴露 `/health`、`/v1/models`、`/v1/responses`。HTTP handler 只解析请求、调用路由引擎和转发上游；策略、线程状态、Jev 客户端、SSE 解析分别放在独立模块。上游抽象同时支持 Codex Router caller edge 和 ChatGPT Codex 直连，直连 HTTPS 使用环境中的 `HTTPS_PROXY` CONNECT 隧道。进入上游的请求永远强制 `stream: true`，但 handler 保存调用方原始 stream 选择：流式调用方收到重新声明 Content-Type 的 chunked SSE，非流式调用方收到从完整 SSE 组装的 JSON。

## 决策与状态

线程键按 header、body `client_metadata`、`x-codex-turn-metadata` 顺序解析并交叉校验；冲突不建立或更新线程状态，直接 astra@medium。每个线程保存 model、effort、最近上下文 token、turn ID、免费重选标记、父线程和决策时间。只有线程首请求、子 agent 首请求、新用户轮次、压缩检查点会改变状态；同 turn 的工具输出续跑只复用。压缩本次固定 sol@high，并使下一轮绕过成本门免费重选。普通新轮次先问 Jev，再检查 20k 降级硬阈值和缓存重建成本差。

## 失败、安全与测试

Jev key 只从环境变量或 `~/.jev.env` 读取，不写日志。Jev 两次重试意味着最多三次尝试，4 秒单次超时，退避可注入以便测试。无 key、超时、网络错误、坏答案和线程标识冲突均 fail-open；kill switch 直接使用 astra@medium；shadow 记录预期结果但实际使用 astra@medium。状态采用临时文件加 `os.replace` 原子落盘，决策日志使用 0600 JSONL，不记录授权头或完整 prompt。测试分纯策略单元测试、Jev 假客户端测试、状态持久化测试和本地假上游 HTTP 集成测试。
