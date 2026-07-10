# iptv-pipeline

自动采集、聚合、去重、验证公开 IPTV 直播源，定时产出统一的 `m3u` / `txt` 播放列表，供「小董电视」等客户端**透明消费**。

本项目是「聚合聚合器」：上游本身就是别人跑好的自动化管道产物（vbskycn 每 6h 扫描、Guovin 每日测速、bjzhou 每日 ffmpeg 深测、iptv-org 每日校验……），本项目做的是把多家成品拉下来做**交叉去重 + 统一命名 + 分组 + 宽松验证**，再产出一份自己的高质量列表。

## 功能

- **多源聚合**：`config/upstreams.txt` 配置任意多个上游（`.m3u` / `.m3u8` / `.txt`），并发拉取。
- **频道归一化**：`config/aliases.json` 把 `CCTV1` / `CCTV-1` / `央视1台` 等归并为规范名 `CCTV-1`。
- **跨源去重**：同名 + 同 URL 的流只保留一条；同频道的多条不同线路自动聚合。
- **分组排序**：`config/groups.json` 按 央视 / 卫视 / 港澳台 / 国际 分组，组内按优先级 + 自然序排序。
- **黑名单过滤**：`config/blacklist.txt` 关键字命中频道名或 URL 即剔除（占位、成人、低质中转域名等）。
- **宽松有效性验证**：`aiohttp` 并发快筛，区分硬失败 / 软失败；**IPv6 流与非 HTTP 流直通保留**（GitHub Actions runner 无 IPv6 出网，无法验证）；只有**连续多轮硬失败**的流才被剔除，避免海外 runner 单次误判误删。
- **多格式产出**：`all.m3u` / `all.txt`（全量）、`cn.m3u`（国内）、`global.m3u`（国际），m3u 头带 EPG 地址。
- **定时自动化**：GitHub Actions 每 6 小时跑一次，产物 force-push 到 `output` 分支。

## 示例

```bash
# 安装依赖（首次）
uv sync

# v0：只聚合去重，不验证（最快，先跑通全链路）
uv run iptv-pipeline --no-probe

# v1：完整流程（含并发快筛与宽松删除）
uv run iptv-pipeline

# 自定义目录
uv run iptv-pipeline --config config --output dist-output --state state/health.json

# 跑测试
uv run pytest
```

产物默认写到 `dist-output/`。客户端订阅 `output` 分支的 raw 地址即可，例如：

```
https://raw.githubusercontent.com/<owner>/iptv-pipeline/output/all.m3u
```

## 架构

```text
config/upstreams.txt
      │  fetch.py  (aiohttp 并发拉取)
      ▼
原始内容 ── parse.py ──▶ Stream[]  (M3U / TXT 自动识别)
      │  normalize.py  (黑名单 → 规范化 → 去重 → 分组 → 排序)
      ▼
Channel[]  ── probe.py + state.py ──▶  宽松验证 (IPv6 直通 / 连续硬失败才删)
      │  emit.py
      ▼
dist-output/{all,cn,global}.{m3u,txt}
      │  GitHub Actions
      ▼
output 分支 (raw URL) ──▶ 小董电视 LiveService 内置源
```

模块职责：

| 模块 | 职责 |
|------|------|
| `config.py` | 加载 4 个配置文件，展开别名映射与分组规则 |
| `fetch.py` | 并发拉取上游，失败降级跳过 |
| `parse.py` | M3U / TXT(#genre#) 自动识别解析 |
| `normalize.py` | 归一化 key、黑名单、去重、分组、自然排序 |
| `probe.py` | aiohttp 并发快筛，区分 OK / 软失败 / 硬失败 / 跳过 |
| `state.py` | 跨轮次健康状态，实现"连续硬失败才删"的宽松删除 |
| `emit.py` | 产出 m3u / txt |
| `pipeline.py` | 编排全流程 |
| `main.py` | CLI 入口 |

## TODOs

- [ ] v2：`ffprobe` 抽样深测（对每频道前 N 条候选做真实解码验证 + freezedetect 过滤静止画面）
- [ ] v2：产出 `meta.json`（每条流的来源、最后验证时间、置信度），供客户端智能层使用
- [ ] v2（可选）：关键字搜索增量采集（tonkiang / foodie 线上检索）
- [ ] 持续补充 `config/aliases.json`（唯一需要边跑边维护的"脏活"）
- [ ] 若海外 runner 误判严重，平移到国内 VPS / NAS / 自托管 runner

## Notes

- **IPv6 与海外 runner**：GitHub 托管 runner 不支持 IPv6 出网（[官方 issue #668](https://github.com/actions/runner-images/issues/668)），且 runner 在海外，大陆源易误判。因此验证策略刻意"宽松"：IPv6 / 非 HTTP 流直通，软失败不删，只有连续 3 轮硬失败（DNS/拒连/4xx/5xx）才剔除。
- **FFmpeg**：仅 v2 的深度质检才需要，且只在管道（本项目 / runner）里使用，与小董电视（GStreamer 内核）完全解耦。
- **公开仓库**：Actions 分钟数不限量。注意 GitHub 会停用 60 天无活动仓库的定时 workflow，管道自身的定期 commit 一般足以保活。
- **产物公开**：客户端只内置公开产物 URL，不接触本仓库的采集逻辑与凭据。

## References

- [iptv-org/iptv](https://github.com/iptv-org/iptv) — 全球最大公开 IPTV 频道库
- [Guovin/iptv-api](https://github.com/Guovin/iptv-api) — 全自动采集/测速/生成平台（Python）
- [bjzhou/iptv-collector](https://github.com/bjzhou/iptv-collector) — 小董电视现内置源，ffmpeg 深测型（最小原型参考）
- [cs3306/IPTV-Sources](https://github.com/cs3306/IPTV-Sources) — ffprobe + freezedetect 过滤
- [fanmingming/live](https://github.com/fanmingming/live) — 台标事实标准

## License

The Unlicense（对齐 iptv-org 生态；仅聚合公开链接，不托管任何内容）
