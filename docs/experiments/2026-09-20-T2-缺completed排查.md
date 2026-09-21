# T2：缺 `response.completed` 排查（2026-09-20）

**一句话结论：**缺 completed 不是上游改了终止事件，也不是 SSE 解析器在 chunk 边界丢数据；根因是 Codex 客户端在父线程工具调用流中途断开后，路由器立即因 BrokenPipe 退出并关闭上游，没有继续读到随后的 completed。改为“下游断开后停止写客户端，但继续排空上游并送入 tracker”后，同一任务 6 次 live 共 127 个请求的缺失率从 7 条降为 0。

## 调试捕获

路由器增加了两个可配置路径：

```toml
[paths]
stream_debug = "~/.codex/codex-jev-router/stream.debug"
raw_stream_dir = "~/.codex/codex-jev-router/raw-streams"
```

只有 `stream_debug` 哨兵文件存在时才捕获。每个上游 HTTP 流写一个权限 0600 的 JSONL，记录：

- `start`；
- 每次 `HTTPResponse.read1` 返回的 `chunk`，含序号、长度、base64 原字节、wall-clock 和 monotonic 时间；
- `client_disconnect`、`upstream_error`、`upstream_eof`；
- `end`，含 tracker 是否看到 completed。

捕获文件不写请求 header、body 或凭据。决策日志的 `stream_capture` 字段记对应文件名，用于按 request/thread 交叉核对。

## 修复前复现

任务使用阶段二 `s2_parallel_tests`，原始数据在 Git 忽略的 `runtime/bench/t2-pre-fix-20260920/`。有效样本是 3 次 live + 3 次 shadow；首个 live 有两次基础设施失败，驱动依规重跑，失败 attempt 没有混入有效汇总。

| 模式 | 有效会话 | 缺 completed | 每次缺失 |
|---|---:|---:|---|
| live | 3 | 7 | 3 / 2 / 2 |
| shadow | 3 | 1 | 0 / 0 / 1 |

对 8 条有效缺失流的原始捕获观察完全一致：

```text
start → chunk... → client_disconnect → end(response_completed=false)
```

没有 `upstream_eof`。最后的事件仍是 `response.output_text.delta`、`response.function_call_arguments.delta` 或 `response.content_part.added`，不存在未识别的替代终止事件。同一轮其他 125 条捕获则是 `start → ... → upstream_eof → end`。

## 分类结论

| 候选根因 | 结论 | 证据 |
|---|---|---|
| 上游根本没发 completed | 否（针对可复现的这组缺失） | 修复后同样的 client disconnect 发生后，继续读上游能拿到 completed |
| 客户端提前关闭 | **是，起点** | 每条缺失都记到 `client_disconnect` |
| 路由器提前关闭上游 | **是，直接原因** | write 抛 BrokenPipe 后跳到 finally，`connection.close()`，没有再 read |
| HTTPS 代理提前关闭 | 否 | 没有 `upstream_error`；修复后原连接能读到 EOF |
| 终止事件是别的 type | 否 | 缺失捕获在 delta/add 处截断，无其他 terminal type |
| parser 在 chunk 边界丢事件 | 否 | 原始字节中就没有 completed；既有 arbitrary-chunk 单测也通过 |

`codex_jev_router/sse.py` 的 tracker 会把任意 chunk 累积到换行符，只在拿到完整 `data:` 行后解 JSON；`finish()` 还会处理最后一条无换行行。修复前原始捕获中不存在 completed 字节，所以 tracker 没有识别机会。

## 修复

流式转发抽成 `relay_sse_response`：

1. 每次从上游读到 chunk，先写 debug capture，再 `tracker.feed`；
2. 如果下游正常，继续按 HTTP chunked 原样转发；
3. 如果下游抛 BrokenPipe/ConnectionReset，记 `client_disconnect`，不再对下游写；
4. 但仍继续读上游到 EOF，所有后续字节仍进 tracker，从 completed 取 model/usage；
5. 正常客户端的输出字节完全不变。

回归测试使用“第一次 write 立即 BrokenPipe”的 writer，上游把 completed 放在后续 chunk。修复前测试红灯（没有 relay/drain 路径）；修复后 tracker 取得 `input_tokens=4321`，捕获顺序为 `client_disconnect → upstream_eof → end(response_completed=true)`。

## 修复后 6 次验证

使用同一 `s2_parallel_tests`、live 路由连续跑 6 次，原始数据在 `runtime/bench/t2-post-fix-20260920/`。

| 指标 | 结果 |
|---|---:|
| 会话通过 | 6/6 |
| 父/子请求 | 127 |
| 有 `response.completed` | 127 |
| 缺 `response.completed` | **0** |
| 下游断开 | 14 |
| 断开后继续读到 completed | 14/14 |

14 条中，12 条的证据顺序是：

```text
start → chunk... → client_disconnect → chunk... → upstream_eof → end(true)
```

另 2 条是路由器已读到 EOF/completed，只在写最后的 HTTP 终止 chunk 时发现客户端已断开；它们同样有完整 usage。

## 影响与限制

- 这个修复让“客户端提前不再读”不再导致计费日志缺失，后续 T1 费用可以用 completed usage 直接计算。
- 排空上游会让该 HTTP handler 在客户端断开后多存活到上游 EOF；这是为取得已产生的模型 usage 有意接受的资源交换。
- 如果未来真正发生上游断流或没有 completed，仍会如实记 `upstream_error`/`upstream_eof + end(false)`，不会伪造 usage。
- 本次证据针对 Codex CLI 0.155.1、ChatGPT Codex 直连上游和本机 HTTPS 代理环境。

后续 T1 的 1,466 个有效请求中又出现 1 条 HTTP 200 但无 completed 的流。T1 未开 raw debug，因此该单条无法在上游 EOF 和 read 异常之间分类；它已在 T1 报告中标为费用下界。所以 T2 修复的精确声明是“消除客户端断开导致的系统性缺失，同任务 6 次验证为零”，而不是“任何上游流永远不会缺 completed”。

运行后已删 debug/shadow/off 哨兵，停止 4320 路由器，`~/.codex/config.toml` SHA-256 恢复为 `<redacted-sha256>`。
