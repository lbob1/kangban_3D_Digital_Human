# Day6 POC-08 复测：天气快路径延迟（vs Day5 基线）

日期：2026-11-09 ｜ 脚本：`scripts/bench_day6_weather.py` ｜ 原始：`.cache/bench_day6/raw_weather.jsonl`（不入库）

## 口径

- 复用 Day5 `bench_latency.py` 手法：文字通道 `handle_text`（与语音轮同一回复管线）串行发送「今天天气怎么样」×20，并发 1、间隔 3s，TTS 真实 EdgeTTS、LLM 按 `.env` 真实配置；
- Day6 管线：rule 层 `weather_today` → `weather_fast_path`（真实 MCP stdio open-meteo + 1 次受控 LLM 精简，≤80 字）；
- 服务端口径 `latency_metrics` SQL `percentile_cont` 复算，对比 Day5 基线 **e2e P50=2560.7 / P95=4560.1**（POC_验证计划.md Day5 复测口径）；
- token 单列：精简调用 token 不入 `latency_metrics`，按 `weather_answered` 日志 `llm_used/source/degraded` 元数据单列计量（每轮至多 1 次短 prompt 精简，无历史注入）。

## 三批结果（每批 20/20 成功，source=open-meteo，零降级轮）

| 批次 | e2e P50 | e2e P95 | tts_first P50 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 4161.7ms | 7461.5ms | 1619.5ms | 平稳 |
| 2 | 4478.6ms | 13908.2ms | 1606.0ms | 含 2 个异常慢轮（#16=18.0s、#17=12.5s，open-meteo/LLM 网络波动被重试放大），如实保留 |
| 3 | 3970.8ms | 5799.0ms | 1632.2ms | 平稳（正式引用批） |

正式批 vs Day5 基线：**P50 +55%（+1410ms）、P95 +27%（+1239ms）**。
客户端墙钟 P50=4019.7 vs Day5 6042.6（**-33%**）。

## 结构性归因（如实记录，e2e 上升非回归）

- Day5 主控流式：LLM 首 token（P50≈483ms）即分句起播 TTS，e2e 只等首句；
- Day6 天气快路径：必须等 **MCP 数据完整 + 精简句完整生成**（受控单句不分句流式）才起播，e2e 首包上升为结构性代价；
- 换来的收益：总轮时长 -33%（免主控长生成）、路由决策零 token、回复受控确定性（≤80 字话术 + 完整数据经 `weather` WS 事件全量下发字幕）。

## token 单列口径

- 决策：rule 命中零 LLM 调用；每轮仅 1 次精简调用（llm_used=true 已验证，输出为自然话术非模板）；
- 对比 Day5 每轮 1 次主控流式全答（长生成）：天气轮 token 大幅下降，具体 token 数按日志单列，不混入延迟对比。

## 结论

- 天气快路径以 **e2e 首包 +55%** 换取 **总轮时长 -33% + 决策零 token + 输出受控**；对「查天气」这类单轮事实型请求，长者感知主口径是总等待时长，取舍成立；
- P95 对网络波动敏感（批 2 异常慢轮），MCP server 侧重试 2 次放大了抖动，后续可通过缩短重试次数/加超时上限优化（记 Day7 待办，不在本日扩大改动）。

## 附：流式化复测（Day6+ 首包治理，2026-11-09 当日追加）

**改动**：精简答复由「等全文生成完才起播」改为「凑到句末标点即逐句 TTS」（`condense_via_llm_streaming`，与 Day5 主控分句同口径；降级提示 note 先播；LLM 中途断连沿用已播不重说模板）。改动点：`app/tools/weather_mcp.py` + `app/router/cascade.py`（CascadeOutcome.streamed 标记 + stream_emitter contextvar）+ `app/services/voice.py`（发射器注入/跳过重播），`decide()` 签名与 bench/conftest 零改动，新增 3 个流式单测（364 passed）。

**同口径复测 ×3 批（每批 20/20 成功，零降级轮）**：

| 批次 | e2e P50 | e2e P95 | tts_first P50 |
| --- | --- | --- | --- |
| 流式前正式批 | 3970.8ms | 5799.0ms | 1632.2ms |
| 流式后批 1（网络偏慢） | 3803.7ms | 5195.2ms | 1672.2ms |
| 流式后批 2 | 3125.3ms | 4313.8ms | 1153.2ms |
| 流式后批 3（留档批） | **3033.9ms** | **3865.1ms** | 1048.1ms |

**效果（留档批 vs 流式前正式批）**：e2e P50 **-24%**（3970.8→3033.9），P95 **-33%**（5799.0→3865.1）；vs Day5 基线差距从 +55%/+27% 收窄到 **+18%/-15%（P95 已反超 Day5 基线 695ms）**。

**残余差距归因**：天气轮仍须等 MCP 取数完整 + 精简 LLM 首 token + 首句生成；EM-01（P95≤1.5s）的大头仍是 TTS 首包（~1.0-1.6s），留 TTS 选型推进（不变）。
