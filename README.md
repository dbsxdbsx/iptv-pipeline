# iptv-pipeline

自动采集、聚合、去重、验证公开 IPTV 直播源，定时产出统一的 `m3u` / `txt` 播放列表，供任意兼容 M3U 的播放器订阅。

本项目是「聚合聚合器」：上游本身就是别人跑好的自动化管道产物（vbskycn 每 6h 扫描、bjzhou 每日 ffmpeg 深测、iptv-org 每日校验……）。管道把多家成品做**交叉去重 + 统一命名 + 分组 + 分层验证**，同时发布：

- `all.m3u`：宽松候选池，便于诊断和后续发现，不承诺可播；
- `stable.m3u`：只有本轮 FFmpeg 实际解码通过或处于短时 GRACE 的线路，供客户端默认订阅；每频道最多 5 条且优先不同 host，EXTINF 带 `x-tier="pass|grace"`。

## 功能

- **多源聚合**：`config/upstreams.txt` 配置任意多个上游（`.m3u` / `.m3u8` / `.txt`），并发拉取。
- **频道归一化**：`config/aliases.json` 把 `CCTV1` / `CCTV-1` / `央视1台` 等归并为规范名 `CCTV-1`。
- **跨源去重**：同名 + 同 URL 的流只保留一条；同频道的多条不同线路自动聚合。
- **分组排序**：`config/groups.json` 分 央视 / 卫视 / 地方台 / 港澳台 / 影视剧集 / 体育 / 少儿 / 纪录 / 春晚 / 音乐 / 国际 共 11 个桶。判定分三级——频道名关键字（高置信）→ 上游 `group-title` 映射（补地方台与点播剧集这类无名字特征的召回）→ 境外兜底（不含汉字归国际）。`order`（判定优先级）与 `display_order`（产物顺序）分开配置。
- **黑名单过滤**：`config/blacklist.txt` 关键字命中频道名或 URL 即剔除（占位、成人、低质中转域名等）。
- **台标改道**：`config/logo_rewrites.json` 把上游内嵌的失效图床前缀换成可达镜像。
- **增强 L0 快筛**：校验 HTTP 状态、响应体、HTML/JSON 错误页、HLS 结构与有限 VOD；明确硬失败不会进入 stable。
- **真实媒体深验**：FFprobe 识别视频轨/codec/分辨率，FFmpeg 下载子资源并解码数秒；strict stable 还必须通过 GStreamer 门禁。无自定义头走 `gst-discoverer`；带 UA/Referer 的线路走 playbin 短播探针（`element-setup` + `deep-element-added`）。
- **正向准入状态机**：新流必须 `PASS` 才进入 stable；仅基础设施软失败可在最近一次 PASS 后短时 `GRACE`；4xx、格式或解码失败立即移出。
- **请求头透传**：保留公开安全的 User-Agent / Referer / Origin 等头；Cookie/Authorization 不进入公共产物。
- **多格式产出**：`stable.m3u` / `stable.txt`（严格）、`all.m3u` / `all.txt`（候选），以及从 stable 派生的 `cn.m3u` / `global.m3u`；`meta.json` 提供来源和验证证据。
- **隔离与原子发布**：GitHub Actions 每 6 小时在禁用 IPv6、阻断私网/metadata egress 的容器中准备并以 6 个只读分片深验；频道跌幅等硬门禁通过后发布。

## 示例

```bash
# 安装依赖（首次）
uv sync

# 只聚合去重，不验证（开发诊断）
uv run iptv-pipeline --no-probe

# 本地完整流程（L0 + FFprobe/FFmpeg，需系统已安装 ffmpeg）
uv run iptv-pipeline

# 仅跑 L0，不产生 stable 条目
uv run iptv-pipeline --skip-deep

# CI 分阶段入口
uv run iptv-pipeline-ci prepare --bundle ci-work/candidates.json
uv run iptv-pipeline-ci verify --bundle ci-work/candidates.json \
  --output ci-results/deep-results-0.json --shard-index 0 --shard-count 1

# 自定义目录（本地完整流程）
uv run iptv-pipeline --config config --output dist-output --state state/health.json

# 跑测试
uv run pytest

# 逐个探测上游存活与体量（只读，维护上游列表时用）
bash scripts/probe_upstreams.sh
```

产物默认写到 `dist-output/`。客户端可订阅：

```
https://raw.githubusercontent.com/<owner>/iptv-pipeline/output/stable.m3u
```

投递契约见 `config/delivery.json` 与下文「运维」。`manifest.json`（schema v2）含有序 `endpoints` / `manifest_endpoints`，客户端可先拉 manifest 再按序回退 playlist。

`stable` 的含义是“从该轮验证 runner 网络视角可由 FFmpeg 解码并通过 GStreamer 门禁”，不是对所有国家、运营商或终端平台的绝对可播承诺。IPv6 / 非 HTTP 流仍只保留在 `all.m3u`。

## 架构

```text
config/upstreams.txt
      │  fetch.py  (aiohttp 并发拉取)
      ▼
原始内容 ── parse.py ──▶ Stream[]  (M3U / TXT 自动识别)
      │  normalize.py  (黑名单 → 规范化 → 去重 → 分组 → 排序)
      ▼
Channel[] ── probe.py ──▶ L0 全线路快筛
      │
      ├── deep_probe.py (FFprobe + FFmpeg + GStreamer 分片深验)
      │        │
      │        └── state.py + rank.py (PASS / GRACE / REJECT)
      │
      │  emit.py
      ▼
dist-output/{stable,all,cn,global}.m3u + meta.json + manifest.json
      │  GitHub Actions
      ▼
output 分支（产物/状态同一 generation）──▶ 公开订阅 URL
```

模块职责：

| 模块 | 职责 |
|------|------|
| `config.py` | 加载上游、别名、黑名单、台标改道表、分组和严格验证参数 |
| `fetch.py` | 并发拉取上游，失败降级跳过 |
| `parse.py` | M3U / TXT(#genre#) 自动识别解析，保留上游分组名 |
| `normalize.py` | 归一化 key、黑名单、台标改道、去重、三级分组判定、自然排序 |
| `probe.py` | aiohttp 增强 L0，识别错误页、空 HLS、有限 VOD 和网络失败 |
| `deep_probe.py` | 有界并发 FFprobe / FFmpeg / GStreamer 兼容门禁 |
| `gst_play_probe.py` | 带头源的 playbin 短播探针（子进程；双信号注入头） |
| `state.py` | broad 连续失败计数；stable 的 PASS / GRACE / REJECT 状态机 |
| `rank.py` | 按深验状态、历史成功、延迟和多源佐证选择每频道最多 5 条 |
| `artifacts.py` / `ci.py` | CI 候选/分片结果契约、完整性检查与发布质量门禁 |
| `emit.py` | 产出 m3u / txt / meta.json / manifest.json |
| `pipeline.py` | 编排全流程 |
| `main.py` | CLI 入口 |

## 运维

细节步骤见 [`docs/operations.md`](docs/operations.md)。摘要：

| 项 | 现状 | 你需要做什么 |
|---|---|---|
| **投递端点** | `delivery.json` 已含 GitHub raw + jsDelivr；可再启用 GitHub Pages 或自有域名 | 有域名时把主站 URL 插到 `playlist_endpoints` / `manifest_endpoints` **最前** |
| **国内探测视角** | 默认只在 GitHub 托管 runner（境外）验证 | 在大陆机器注册 `self-hosted` + `china` runner 后，手动/定时跑 `domestic-verify` workflow |
| **常驻探测** | 同上，依赖长期在线的 self-hosted runner | 家宽 NAS / 国内 VPS 上保持 runner 进程 |

## TODOs

- [x] FFprobe + FFmpeg + GStreamer 门禁（discoverer / 带头 playbin 短播探针）
- [x] 产出 `stable.m3u`、`meta.json`、generation manifest 与跨轮健康状态
- [x] 公共请求头解析、深验和 M3U 透传
- [x] 三级分组 11 桶；失效上游与台标改道观测
- [x] 收敛默认境外大盘：停用 iptv-org 全球 news/sports/movies 订阅（保留 `languages/zho.m3u`）；混杂上游分组名（数字频道/4K/APTV 专享）故意不映射
- [ ] 持续审核 `meta.json.alias_candidates` 并补充 `config/aliases.json`
- [ ] 有大陆出口后启用独立国内验证视角（见 `docs/operations.md`）
- [ ] 可选：稳定后再评估关键字搜索增量采集

## Notes

- **IPv6 与海外 runner**：GitHub 托管 runner 不支持原生 IPv6 出网（[官方 issue #668](https://github.com/actions/runner-images/issues/668)）。未验证 IPv6 不进 stable；WARP 也不等同中国大陆网络。
- **FFmpeg 与 GStreamer**：`stable` 线路必须同时经过两套栈。无自定义头用 `gst-discoverer`；需 UA/Referer 的线路用 `python -m iptv_pipeline.gst_play_probe`。验证镜像固定基础镜像 digest 与工具链版本；升级必须同步 `VALIDATION_SCOPE`。Debian bookworm 镜像通常只有 legacy `hlsdemux`，探针会回退旧 `playbin`。
- **验证范围迁移**：门禁定义变化时旧 PASS/GRACE 失效，首轮新基线须手动 workflow 并批准 `approve_quality_scope_migration`。
- **公开仓库**：验证 job 无写权限；只有通过质量门禁的发布 job 能更新 `output`。
- **分组硬约束**：关键字必须压过上游分组；不给 iptv-org 英文全球分类建映射；分组须等同一频道全部流归并完再判。详见 `AGENTS.md` 与 `config/groups.json`。
- **死上游会静默腐烂**：拉取失败只降级跳过；`prepare` 会写 `upstream_report.md` 点名失败 URL。
- **上游优先 GitHub raw**：自建 CDN 是列表主要腐烂来源。
- **台标改道只许换前缀**，且必须发生在去重之前。

## References

- [iptv-org/iptv](https://github.com/iptv-org/iptv) — 全球公开 IPTV 频道库
- [Guovin/iptv-api](https://github.com/Guovin/iptv-api) — 采集/测速/生成参考
- [bjzhou/iptv-collector](https://github.com/bjzhou/iptv-collector) — ffmpeg 深测型聚合参考
- [cs3306/IPTV-Sources](https://github.com/cs3306/IPTV-Sources) — ffprobe + freezedetect 过滤
- [fanmingming/live](https://github.com/fanmingming/live) — 台标资源参考

## License

The Unlicense（对齐 iptv-org 生态；仅聚合公开链接，不托管任何内容）
