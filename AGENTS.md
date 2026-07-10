# AGENTS.md

本文件约束本仓库内 AI/自动化代理的默认行为。

## Project Overview

`iptv-pipeline` 是「小董电视」的配套数据管道：定时采集多个公开 IPTV 上游成品列表，做归一化、跨源去重、分层验证，发布宽松候选池 `all.m3u` 与正向深验准入的 `stable.m3u`。小董电视只把 `stable.m3u` 作为内置源。

- 语言：Python 3.10+，依赖管理用 `uv`。
- 与小董电视**完全解耦**：两者通过 `stable.m3u` 产物契约连接。FFmpeg 只在本管道/runner 中使用，不进入 GStreamer App。
- 默认沟通语言：中文。

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
  → probe(L0全线路) → CI只读分片 deep_probe(FFprobe+FFmpeg)
  → state(PASS/GRACE/REJECT) → rank(每频道最多2条)
  → emit(stable/all/meta/manifest) → [质量门禁] → output 单提交
```

模块（`src/iptv_pipeline/`）：`config` `fetch` `parse` `normalize` `safety` `probe` `deep_probe` `state` `rank` `emit` `artifacts` `ci` `pipeline` `main`。详见 README.md 的模块职责表。

## Conventions

- **双轨不变式**：`all` 可宽松保留候选，但 `stable` 只能包含本轮 PASS 或最近 12 小时内、最多连续两轮的基础设施软失败 GRACE。4xx、格式或解码失败必须立即退出 stable。
- **验证边界**：`stable` 只表示 GitHub runner 的 FFmpeg 可解码；不可写成 GStreamer/国内运营商“保证可播”。未验证 IPv6/非 HTTP 流不得进入 stable。
- **产物契约**：App 只消费 `stable.m3u`；`all.m3u`/`all.txt` 仅诊断。`cn.m3u`/`global.m3u` 从 stable 派生；所有产物与 `meta.json`、`.state/health.json` 必须共享同一 generation。
- **原子发布**：状态只放 output 分支；验证 job 无写权限。质量门禁失败时 output SHA 必须不变，更新必须使用代际校验/`force-with-lease`，禁止盲目 force-push。
- **公共头安全**：只透传 UA/Referer/Origin/Accept 类头，禁止 Cookie、Authorization、CR/LF 进入产物或日志；FFmpeg 命令不得拼 shell 字符串。
- **别名表** `config/aliases.json` 是唯一需要人工边跑边补的「脏活」。新增频道归并写这里，不要硬编码进代码。
- 新增依赖优先 `uv add`，不手改 `uv.lock`。
- 所有测试必须真实断言；改解析/归一化/准入/状态机/发布契约须补对应测试。

## Notes

- 上游失效：在 `config/upstreams.txt` 行首加 `#` 停用，不要直接删（方便恢复）。
- CI 产物用 `force-with-lease` 单提交写 `output` 分支，避免历史膨胀并防并发覆盖。
- 关键字搜索增量会扩大坏源池，只有 stable 质量指标稳定后才评估。
