# iptv-pipeline

自动采集、聚合、去重、验证公开 IPTV 直播源，定时产出统一的 `m3u` / `txt` 播放列表，供「小董电视」等客户端**透明消费**。

本项目是「聚合聚合器」：上游本身就是别人跑好的自动化管道产物（vbskycn 每 6h 扫描、bjzhou 每日 ffmpeg 深测、iptv-org 每日校验……）。管道把多家成品做**交叉去重 + 统一命名 + 分组 + 分层验证**，同时发布：

- `all.m3u`：宽松候选池，便于诊断和后续发现，不承诺可播；
- `stable.m3u`：只有本轮 FFmpeg 实际解码通过或处于短时 GRACE 的线路，供客户端默认订阅。

## 功能

- **多源聚合**：`config/upstreams.txt` 配置任意多个上游（`.m3u` / `.m3u8` / `.txt`），并发拉取。
- **频道归一化**：`config/aliases.json` 把 `CCTV1` / `CCTV-1` / `央视1台` 等归并为规范名 `CCTV-1`。
- **跨源去重**：同名 + 同 URL 的流只保留一条；同频道的多条不同线路自动聚合。
- **分组排序**：`config/groups.json` 按 央视 / 卫视 / 港澳台 / 国际 分组，组内按优先级 + 自然序排序。
- **黑名单过滤**：`config/blacklist.txt` 关键字命中频道名或 URL 即剔除（占位、成人、低质中转域名等）。
- **增强 L0 快筛**：校验 HTTP 状态、响应体、HTML/JSON 错误页、HLS 结构与有限 VOD；明确硬失败不会进入 stable。
- **真实媒体深验**：FFprobe 识别视频轨/codec/分辨率，FFmpeg 下载子资源并解码数秒；strict stable 还必须通过 GStreamer discoverer。当前无法等价注入 HLS 子请求头的线路 fail closed，仅保留在 `all`。
- **正向准入状态机**：新流必须 `PASS` 才进入 stable；仅基础设施软失败可在最近一次 PASS 后短时 `GRACE`；4xx、格式或解码失败立即移出。
- **请求头透传**：保留公开安全的 User-Agent / Referer / Origin 等头，验证条件与小董电视播放条件一致；Cookie/Authorization 不进入公共产物。
- **多格式产出**：`stable.m3u` / `stable.txt`（严格）、`all.m3u` / `all.txt`（候选），以及从 stable 派生的 `cn.m3u` / `global.m3u`；`meta.json` 提供来源和验证证据。
- **隔离与原子发布**：GitHub Actions 每 6 小时在禁用 IPv6、阻断私网/metadata egress 的容器中准备并以 6 个只读分片深验；质量门禁通过后，产物、状态、元数据以同一 generation 单提交发布到 `output`。

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
```

产物默认写到 `dist-output/`。客户端默认订阅严格产物：

```
https://raw.githubusercontent.com/<owner>/iptv-pipeline/output/stable.m3u
```

`stable` 的含义是“从该轮 GitHub 托管 runner 网络视角可由 FFmpeg 解码并通过 GStreamer discoverer”，不是对所有国家、运营商或终端平台的绝对可播承诺。当前验证器无法等价覆盖的自定义请求头、IPv6 / 非 HTTP 流只保留在 `all.m3u`。

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
output 分支（产物/状态同一 generation）──▶ 小董电视 stable 内置源
```

模块职责：

| 模块 | 职责 |
|------|------|
| `config.py` | 加载上游、别名、黑名单、分组和严格验证参数 |
| `fetch.py` | 并发拉取上游，失败降级跳过 |
| `parse.py` | M3U / TXT(#genre#) 自动识别解析 |
| `normalize.py` | 归一化 key、黑名单、去重、分组、自然排序 |
| `probe.py` | aiohttp 增强 L0，识别错误页、空 HLS、有限 VOD和网络失败 |
| `deep_probe.py` | 有界并发 FFprobe 元数据检查、FFmpeg 短时解码与 GStreamer 兼容门禁 |
| `state.py` | broad 连续失败计数；stable 的 PASS / GRACE / REJECT 状态机 |
| `rank.py` | 按深验状态、历史成功、延迟和多源佐证选择每频道前两条 |
| `artifacts.py` / `ci.py` | CI 候选/分片结果契约、完整性检查与发布质量门禁 |
| `emit.py` | 产出 m3u / txt / meta.json / manifest.json |
| `pipeline.py` | 编排全流程 |
| `main.py` | CLI 入口 |

## TODOs

- [x] FFprobe 元数据检查 + FFmpeg 短时解码 + GStreamer discoverer；无法执行 GStreamer 等价验证的线路 fail closed
- [x] 产出 `stable.m3u`、`meta.json`、generation manifest 与跨轮健康状态
- [x] 公共请求头解析、深验和 M3U 透传
- [ ] 持续审核 `meta.json.alias_candidates` 并补充 `config/aliases.json`
- [ ] 有国内 VPS / NAS 后增加独立国内验证视角；在此之前不宣称国内运营商可播率
- [ ] 可选：稳定源质量闭环后再评估关键字搜索增量采集

## Notes

- **IPv6 与海外 runner**：GitHub 托管 runner 不支持原生 IPv6 出网（[官方 issue #668](https://github.com/actions/runner-images/issues/668)）。未验证 IPv6 不进 stable；WARP 也不等同中国大陆网络，不能作为国内可播证明。
- **FFmpeg 与 GStreamer**：`stable` 线路必须同时经过两套栈；需 UA/Referer 的线路因 `gst-discoverer` CLI 无法注入 HLS 子请求头，暂只进入 `all`。验证镜像固定 Python 基础镜像 digest 及 FFmpeg/GStreamer 包版本；升级工具链必须同步升级 `VALIDATION_SCOPE`。Windows/Android 与 Linux runner 仍可能存在 TLS/CDN/插件差异。
- **状态与仓库历史**：`.state/health.json` 与产物一起放在 force-with-lease 更新的 `output` 单提交中，main 不再每 6 小时累积大状态文件。
- **验证范围迁移**：`meta.json.quality_scope` 与状态文件记录当前准入定义；门禁定义变化时旧 PASS/GRACE 会失效，首轮新基线必须手动触发 workflow 并显式批准 `approve_quality_scope_migration`。
- **公开仓库**：定时 output commit 同时作为仓库活动；验证 job 无写权限，只有通过质量门禁的发布 job 能更新 output。
- **产物公开**：客户端只内置公开产物 URL，不接触本仓库的采集逻辑与凭据。

## References

- [iptv-org/iptv](https://github.com/iptv-org/iptv) — 全球最大公开 IPTV 频道库
- [Guovin/iptv-api](https://github.com/Guovin/iptv-api) — 全自动采集/测速/生成平台（Python）
- [bjzhou/iptv-collector](https://github.com/bjzhou/iptv-collector) — 小董电视现内置源，ffmpeg 深测型（最小原型参考）
- [cs3306/IPTV-Sources](https://github.com/cs3306/IPTV-Sources) — ffprobe + freezedetect 过滤
- [fanmingming/live](https://github.com/fanmingming/live) — 台标事实标准

## License

The Unlicense（对齐 iptv-org 生态；仅聚合公开链接，不托管任何内容）
