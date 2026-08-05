# iptv-pipeline

自动采集、聚合、去重、验证公开 IPTV 直播源，定时产出统一的 `m3u` / `txt` 播放列表，供「小董电视」等客户端**透明消费**。

本项目是「聚合聚合器」：上游本身就是别人跑好的自动化管道产物（vbskycn 每 6h 扫描、bjzhou 每日 ffmpeg 深测、iptv-org 每日校验……）。管道把多家成品做**交叉去重 + 统一命名 + 分组 + 分层验证**，同时发布：

- `all.m3u`：宽松候选池，便于诊断和后续发现，不承诺可播；
- `stable.m3u`：只有本轮 FFmpeg 实际解码通过或处于短时 GRACE 的线路，供客户端默认订阅；每频道最多 5 条且优先不同 host，EXTINF 带 `x-tier="pass|grace"`。

## 功能

- **多源聚合**：`config/upstreams.txt` 配置任意多个上游（`.m3u` / `.m3u8` / `.txt`），并发拉取。
- **频道归一化**：`config/aliases.json` 把 `CCTV1` / `CCTV-1` / `央视1台` 等归并为规范名 `CCTV-1`。
- **跨源去重**：同名 + 同 URL 的流只保留一条；同频道的多条不同线路自动聚合。
- **分组排序**：`config/groups.json` 分 央视 / 卫视 / 地方台 / 港澳台 / 影视剧集 / 体育 / 少儿 / 纪录 / 春晚 / 音乐 / 国际 共 11 个桶。判定分三级——频道名关键字（高置信）→ 上游 `group-title` 映射（补地方台与点播剧集这类无名字特征的召回）→ 境外兜底（不含汉字归国际）。境内按题材分桶、境外统一进国际，组内按优先级 + 自然序排序。`order`（判定优先级）与 `display_order`（产物与侧栏顺序）分开配置。
- **黑名单过滤**：`config/blacklist.txt` 关键字命中频道名或 URL 即剔除（占位、成人、低质中转域名等）。
- **台标改道**：`config/logo_rewrites.json` 把上游内嵌的失效图床前缀换成可达镜像。台标地址是上游 m3u 里的字符串，上游不修就只能自己改道，而 `fanmingming` 是国内台标的事实标准、被大量上游引用，它一挂停用上游也解决不了（当前改道 1023 条）。
- **增强 L0 快筛**：校验 HTTP 状态、响应体、HTML/JSON 错误页、HLS 结构与有限 VOD；明确硬失败不会进入 stable。
- **真实媒体深验**：FFprobe 识别视频轨/codec/分辨率，FFmpeg 下载子资源并解码数秒；strict stable 还必须通过 GStreamer discoverer。当前无法等价注入 HLS 子请求头的线路 fail closed，仅保留在 `all`。
- **正向准入状态机**：新流必须 `PASS` 才进入 stable；仅基础设施软失败可在最近一次 PASS 后短时 `GRACE`；4xx、格式或解码失败立即移出。
- **请求头透传**：保留公开安全的 User-Agent / Referer / Origin 等头，验证条件与小董电视播放条件一致；Cookie/Authorization 不进入公共产物。
- **多格式产出**：`stable.m3u` / `stable.txt`（严格）、`all.m3u` / `all.txt`（候选），以及从 stable 派生的 `cn.m3u` / `global.m3u`；`meta.json` 提供来源和验证证据。
- **隔离与原子发布**：GitHub Actions 每 6 小时在禁用 IPv6、阻断私网/metadata egress 的容器中准备并以 6 个只读分片深验；频道跌幅等硬门禁通过后发布。GRACE 占比偏高只告警、不阻断整轮，避免上游抖动卡住更新。

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

产物默认写到 `dist-output/`。客户端默认订阅严格产物：

```
https://raw.githubusercontent.com/<owner>/iptv-pipeline/output/stable.m3u
```

投递契约：`config/delivery.json` 写入 `manifest.json` 的有序 `endpoints` / `manifest_endpoints`（schema v2）。小董电视内置源先拉 manifest 再按序回退 playlist；本地还写死了 GitHub raw + jsDelivr 引导地址，管道尚未发 v2 也能工作。以后有自有域名时，把主地址插到 `delivery.json` 列表最前即可，**不必发版 App**。

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
| `config.py` | 加载上游、别名、黑名单、台标改道表、分组（判定优先级 / 展示顺序 / cn-global scope）和严格验证参数 |
| `fetch.py` | 并发拉取上游，失败降级跳过 |
| `parse.py` | M3U / TXT(#genre#) 自动识别解析，保留上游分组名 |
| `normalize.py` | 归一化 key、黑名单、台标改道、去重、三级分组判定、自然排序 |
| `probe.py` | aiohttp 增强 L0，识别错误页、空 HLS、有限 VOD和网络失败 |
| `deep_probe.py` | 有界并发 FFprobe 元数据检查、FFmpeg 短时解码与 GStreamer 兼容门禁 |
| `state.py` | broad 连续失败计数；stable 的 PASS / GRACE / REJECT 状态机 |
| `rank.py` | 按深验状态、历史成功、延迟和多源佐证选择每频道最多 5 条，优先不同 host |
| `artifacts.py` / `ci.py` | CI 候选/分片结果契约、完整性检查与发布质量门禁 |
| `emit.py` | 产出 m3u / txt / meta.json / manifest.json |
| `pipeline.py` | 编排全流程 |
| `main.py` | CLI 入口 |

## TODOs

- [x] FFprobe 元数据检查 + FFmpeg 短时解码 + GStreamer discoverer；无法执行 GStreamer 等价验证的线路 fail closed
- [x] 产出 `stable.m3u`、`meta.json`、generation manifest 与跨轮健康状态
- [x] 公共请求头解析、深验和 M3U 透传
- [x] 保留上游 `group-title` / `#genre#` 并按三级判定分 11 个桶，「其他」从 86% 降到 0.6%
- [x] 修复失效上游并补上游存活观测：`fanmingming` 改走 GitHub raw（自建 CDN 挂了但仓库内容完好）、`YueChan` 换成改名后的 `IPTV.m3u`、`aktv.space` 停用并以 iptv-org 的 hk/tw/mo 子集补港澳台；14/14 拉取成功，候选池 3474 → 3604
- [x] 把 `live.fanmingming.cn` / `.com` 的 1023 条死台标改道到 GitHub raw
- [ ] 持续审核 `meta.json.alias_candidates` 并补充 `config/aliases.json`
- [ ] 补 `config/groups.json` 的 `upstream` 表：`数字频道`、`4K频道`、`APTV专享` 等混杂分组暂未映射
- [ ] 决策：是否收敛境外供给。iptv-org 三个全球 categories 贡献 2274 条候选（约一半），且它们的台标集中在 `i.imgur.com`（1388 条）与 `upload.wikimedia.org`（226 条）——这两个 host 在国内基本拉不出来，而 App 的台标回退只覆盖中文台标，境外频道等于「主 URL 被墙 + 回退必然 404」
- [ ] 为 `aktv.space` 找官方新址或替代的港台专项源
- [ ] 有国内 VPS / NAS 后增加独立国内验证视角；在此之前不宣称国内运营商可播率。2026-08-05 补二线上游后：东方卫视 HD 已有 PASS 进 stable，但省网 RTP/CMCC/酒店类仍大量 `unverified`，且东方卫视4K 可能因 GRACE 到期掉出——国内探测视角仍是补备份线路的正道
- [ ] 可选：App 侧央视频/BestV 短效签名即时解析（与 M3U 管道正交，不塞进 upstreams）
- [ ] 可选：稳定源质量闭环后再评估关键字搜索增量采集

## Notes

- **IPv6 与海外 runner**：GitHub 托管 runner 不支持原生 IPv6 出网（[官方 issue #668](https://github.com/actions/runner-images/issues/668)）。未验证 IPv6 不进 stable；WARP 也不等同中国大陆网络，不能作为国内可播证明。
- **FFmpeg 与 GStreamer**：`stable` 线路必须同时经过两套栈；需 UA/Referer 的线路因 `gst-discoverer` CLI 无法注入 HLS 子请求头，暂只进入 `all`。验证镜像固定 Python 基础镜像 digest 及 FFmpeg/GStreamer 包版本；升级工具链必须同步升级 `VALIDATION_SCOPE`。Windows/Android 与 Linux runner 仍可能存在 TLS/CDN/插件差异。
- **状态与仓库历史**：`.state/health.json` 与产物一起放在 force-with-lease 更新的 `output` 单提交中，main 不再每 6 小时累积大状态文件。
- **验证范围迁移**：`meta.json.quality_scope` 与状态文件记录当前准入定义；门禁定义变化时旧 PASS/GRACE 会失效，首轮新基线必须手动触发 workflow 并显式批准 `approve_quality_scope_migration`。
- **公开仓库**：定时 output commit 同时作为仓库活动；验证 job 无写权限，只有通过质量门禁的发布 job 能更新 output。
- **产物公开**：客户端只内置公开产物 URL，不接触本仓库的采集逻辑与凭据。
- **分组的三条硬约束**：关键字必须压过上游分组；不给 iptv-org 的英文全球分类建映射；分组要等一个频道的所有流归并完再判。繁体变体必须显式列出（iptv-org 的中文频道一律用繁体）。`order` 与 `display_order` 结论不同、必须分开配置。详见 `AGENTS.md` 与 `config/groups.json` 的 `_comment`，守卫测试在 `tests/test_config.py`。
- **死上游会静默腐烂**：拉取失败只降级跳过（这是对的——拿剩下的源照常产出比停更好），但也因此不会让任何一轮失败：质量门禁看的是 stable 频道数，少几个源照样够。11 个源里死了 3 个而产出全程正常，就是这么发生的。现在 `prepare` 会在 bundle 旁写 `upstream_report.json` 与渲染好的 `upstream_report.md`，workflow 直接贴进 Step Summary（渲染放在库里才能被测试盯住）。**看到「上游存活 N/M」不等于 M 个都好使，要看点名列表**。
- **上游 URL 一律优先写 GitHub raw 而非作者自建 CDN**：拉取发生在 GitHub Actions 里（raw 在那儿很快），而自建 CDN 是这份列表的主要腐烂来源——`fanmingming` 的域名挂掉时，同一批内容在仓库里完好无损。
- **台标改道只许换前缀**：`config/logo_rewrites.json` 的替换必须保持路径与文件名不变，改了路径就是把台标整批打成 404。改道要发生在去重之前——`logo` 是「取第一个非空」，晚了既要改好几处、`artifacts` 里落下的还是旧地址。

## References

- [iptv-org/iptv](https://github.com/iptv-org/iptv) — 全球最大公开 IPTV 频道库
- [Guovin/iptv-api](https://github.com/Guovin/iptv-api) — 全自动采集/测速/生成平台（Python）
- [bjzhou/iptv-collector](https://github.com/bjzhou/iptv-collector) — 小董电视现内置源，ffmpeg 深测型（最小原型参考）
- [cs3306/IPTV-Sources](https://github.com/cs3306/IPTV-Sources) — ffprobe + freezedetect 过滤
- [fanmingming/live](https://github.com/fanmingming/live) — 台标事实标准

## License

The Unlicense（对齐 iptv-org 生态；仅聚合公开链接，不托管任何内容）
