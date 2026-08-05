# AGENTS.md

本文件约束本仓库内 AI/自动化代理的默认行为。

## Project Overview

`iptv-pipeline` 定时采集多个公开 IPTV 上游成品列表，做归一化、跨源去重、分层验证，发布宽松候选池 `all.m3u` 与正向深验准入的 `stable.m3u`。任意 M3U 客户端都可订阅产物；本仓库不绑定特定播放器品牌。

- 语言：Python 3.10+，依赖管理用 `uv`。
- 与下游播放器**解耦**：只通过公开 `stable.m3u` + `manifest.json`（schema v2 有序投递端点）交付。FFmpeg / GStreamer 工具链仅用于本管道验证，不进入客户端进程模型。
- 默认沟通语言：中文。
- **公开仓库纪律**：文档与注释禁止写下游私有产品名、内部模块路径、个人联系方式或未公开运维细节；需要对接专用客户端的说明放在私有仓库。

## Build & Test

```bash
uv sync                          # 安装依赖
uv run iptv-pipeline --no-probe  # 只聚合，不验证
uv run iptv-pipeline --skip-deep # 仅 L0 快筛，不产生 stable 条目
uv run iptv-pipeline             # 本地完整深验（需 ffmpeg，可能耗时较长）
uv run iptv-pipeline-ci --help   # CI 准备 / 分片验证 / 发布入口
uv run pytest -q                 # 单元测试
uv run ruff check src tests      # lint
uv run ruff format src tests     # 格式化
```

也可用 `just`（见 `justfile`）：`just`、`just run-fast`、`just run`、`just test`、`just lint`。

## Architecture

```text
config/upstreams.txt → fetch → parse(headers/m3u/txt) → normalize
  → probe(L0全线路) → CI只读分片 deep_probe(FFprobe+FFmpeg+GStreamer)
  → state(PASS/GRACE/REJECT) → rank(每频道最多5条，优先不同 host)
  → emit(stable/all/meta/manifest，stable 带 x-tier) → [质量门禁] → output 单提交
```

模块（`src/iptv_pipeline/`）：`config` `fetch` `parse` `normalize` `safety` `probe` `deep_probe` `gst_play_probe` `state` `rank` `emit` `artifacts` `ci` `pipeline` `main`。详见 README.md。

运维（投递端点 / 国内探测 / self-hosted runner）见 `docs/operations.md`。

## Conventions

- **双轨不变式**：`all` 可宽松保留候选，但 `stable` 只能包含本轮 PASS 或最近 12 小时内、最多连续两轮的基础设施软失败 GRACE。4xx、格式或解码失败必须立即退出 stable。
- **验证边界**：`stable` 表示当前验证 runner 的 FFmpeg 可解码且通过 GStreamer 门禁。无自定义头走 `gst-discoverer`；带 UA/Referer 等公开头的线路走 `gst_play_probe`（playbin3+demux2 优先，否则 playbin；双信号注入 `souphttpsrc`）。探针不可用时仍 fail closed。不可写成全平台“保证可播”。未验证 IPv6/非 HTTP 流不得进入 stable。
- **工具链范围**：验证镜像必须固定基础镜像 digest 与 FFmpeg/GStreamer 直接包版本；任何版本升级都必须同步升级 `VALIDATION_SCOPE`。
- **产物契约**：默认订阅面是 `stable.m3u`；`all.m3u`/`all.txt` 仅诊断。`cn.m3u`/`global.m3u` 从 stable 派生；所有产物与 `meta.json`、`.state/health.json` 必须共享同一 generation。投递端点写在 `config/delivery.json`，由 `write_outputs` 嵌入 `manifest.json`；自有域名或 Pages 主站插到列表最前即可，订阅端按 manifest 热更新，无需为换 URL 改客户端二进制。
- **原子发布**：状态只放 output 分支；验证 job 无写权限。频道/线路跌幅等硬门禁失败时 output SHA 必须不变；GRACE 占比偏高只告警仍发布。更新必须使用代际校验/`force-with-lease`，禁止盲目 force-push。
- **验证 scope 不变式**：改变严格准入定义必须同步升级 `VALIDATION_SCOPE`；旧 PASS/GRACE 不得跨 scope 宽限，首轮基线只能通过手动 workflow 的 `approve_quality_scope_migration` 显式批准。
- **公共头安全**：只透传 UA/Referer/Origin/Accept 类头，禁止 Cookie、Authorization、CR/LF 进入产物或日志；FFmpeg 命令不得拼 shell 字符串。
- **网络隔离**：CI 的 prepare/verify 必须在无凭据容器内运行，并通过 `DOCKER-USER` 阻断私网、metadata、组播目标；禁止改回 host `OUTPUT` 防火墙（会切断 Actions runner 心跳）。
- **分组判定三级顺序**（`normalize.assign_group`，详见 `config/groups.json` 的 `_comment`）：频道名关键字 → 上游分组映射 → 境外兜底 → `default_group`。三条都不能动：(1) **关键字必须压过上游分组**；(2) **故意不给 iptv-org 的英文分类建映射**；(3) **分组必须等一个频道的所有流都归并完再判**。繁体变体必须显式列出。混杂分组名（如「数字频道」「4K频道」「APTV专享」）**故意不映射**——它们题材不纯，硬映射会静默污染桶。
- **`order` 与 `display_order` 是两件事**：`order` 是判定优先级（题材优先于地域）；`display_order` 是产物顺序（常看优先）。两个列表必须含相同分组名。
- **`cn.m3u` / `global.m3u` 归属由 `groups.json` 的 `scope` 驱动**，不得写回代码里的名单。
- **别名表** `config/aliases.json` 与 **分组的 `upstream` 表**靠人工边跑边补，不要硬编码进代码。
- **上游 URL 一律优先写 GitHub raw**，不要作者自建 CDN。
- **死上游只降级、不失败，所以必须主动点名**：`ci.prepare` 无条件写 `upstream_report.json` / `.md`；markdown 必须在库里渲染以便单测盯住。
- **台标改道**只许换前缀、发生在去重之前；已改道 host 不得同时挂在 `upstreams.txt`。
- 新增依赖优先 `uv add`，不手改 `uv.lock`。
- 所有测试必须真实断言；改解析/归一化/准入/状态机/发布契约须补对应测试。

## Notes

- 上游失效：在 `config/upstreams.txt` 行首加 `#` 停用，不要直接删。维护时先跑 `bash scripts/probe_upstreams.sh`。
- 默认境外供给已收敛：iptv-org 的全球 news/sports/movies 默认停用，保留 `languages/zho.m3u`；需要 `global.m3u` 体量时再显式打开。
- `aktv.space/live.m3u` 仍 404，未找到官方继任地址；港澳台继续依赖 suxuang + iptv-org hk/tw/mo。
- CI 产物用 `force-with-lease` 单提交写 `output` 分支。
- 关键字搜索增量会扩大坏源池，只有 stable 质量指标稳定后才评估。
