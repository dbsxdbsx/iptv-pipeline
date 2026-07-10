# AGENTS.md

本文件约束本仓库内 AI/自动化代理的默认行为。

## Project Overview

`iptv-pipeline` 是「小董电视」的配套数据管道：定时采集多个公开 IPTV 上游成品列表，做归一化、跨源去重、分组、宽松有效性验证，产出统一 `m3u`/`txt`，经 GitHub Actions 推到 `output` 分支，供小董电视 `LiveService` 作为内置源透明消费。

- 语言：Python 3.10+，依赖管理用 `uv`。
- 与小董电视**完全解耦**：两者唯一联系是一条产物 URL。FFmpeg（v2 深测才需要）只在本管道用，不进 App。
- 默认沟通语言：中文。

## Build & Test

```bash
uv sync                          # 安装依赖
uv run iptv-pipeline --no-probe  # v0：只聚合，不验证（几秒）
uv run iptv-pipeline             # v1：完整流程（含并发快筛，数分钟）
uv run pytest -q                 # 单元测试
uv run ruff check src tests      # lint
uv run ruff format src tests     # 格式化
```

也可用 `just`（见 `justfile`）：`just`、`just run-fast`、`just run`、`just test`、`just lint`。

## Architecture

```text
config/upstreams.txt → fetch(aiohttp并发) → parse(m3u/txt自动识别)
  → normalize(黑名单→别名规范化→去重→分组→排序)
  → probe(aiohttp快筛) + state(连续硬失败才删)
  → emit(m3u/txt) → dist-output/ → [CI] output 分支
```

模块（`src/iptv_pipeline/`）：`config` `fetch` `parse` `normalize` `probe` `state` `emit` `pipeline` `main`。详见 README.md 的模块职责表。

## Conventions

- **验证宽松原则**：GitHub Actions runner 无 IPv6 出网且在海外。IPv6/非 HTTP 流直通保留；软失败（超时）不删；只有连续 `HARD_FAIL_LIMIT`(=3) 轮硬失败（DNS/拒连/4xx/5xx）才剔除。改验证逻辑不得破坏此原则，否则会误删大量大陆/IPv6 优质源。
- **产物契约**：`all.m3u`/`all.txt`（全量）、`cn.m3u`（国内）、`global.m3u`（国际）。m3u 头带 `x-tvg-url`。改字段名/文件名前须确认小董电视端消费方式。
- **状态文件** `state/health.json` 提交进 main，跨 CI 运行保持连续性，顺带保活定时任务。勿加入 .gitignore。
- **别名表** `config/aliases.json` 是唯一需要人工边跑边补的「脏活」。新增频道归并写这里，不要硬编码进代码。
- 新增依赖优先 `uv add`，不手改 `uv.lock`。
- 所有测试必须真实断言；改解析/归一化逻辑须补对应测试。

## Notes

- 上游失效：在 `config/upstreams.txt` 行首加 `#` 停用，不要直接删（方便恢复）。
- CI 产物用 force-push 单提交写 `output` 分支，避免历史膨胀（对齐 bjzhou/iptv-collector 做法）。
- v2 计划见 README「TODOs」：ffprobe 抽样深测、`meta.json` 元数据、关键字搜索增量。
