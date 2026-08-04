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
  → probe(L0全线路) → CI只读分片 deep_probe(FFprobe+FFmpeg+GStreamer)
  → state(PASS/GRACE/REJECT) → rank(每频道最多5条，优先不同 host)
  → emit(stable/all/meta/manifest，stable 带 x-tier) → [质量门禁] → output 单提交
```

模块（`src/iptv_pipeline/`）：`config` `fetch` `parse` `normalize` `safety` `probe` `deep_probe` `state` `rank` `emit` `artifacts` `ci` `pipeline` `main`。详见 README.md 的模块职责表。

## Conventions

- **双轨不变式**：`all` 可宽松保留候选，但 `stable` 只能包含本轮 PASS 或最近 12 小时内、最多连续两轮的基础设施软失败 GRACE。4xx、格式或解码失败必须立即退出 stable。
- **验证边界**：`stable` 表示 GitHub runner 的 FFmpeg 可解码且通过 GStreamer discoverer；当前无法等价注入 HLS 子请求头的线路必须 fail closed，仅留在 `all`。不可写成 Windows/Android/国内运营商“保证可播”。未验证 IPv6/非 HTTP 流不得进入 stable。
- **工具链范围**：验证镜像必须固定基础镜像 digest 与 FFmpeg/GStreamer 直接包版本；任何版本升级都必须同步升级 `VALIDATION_SCOPE`，使旧 PASS/GRACE 自动失效。
- **产物契约**：App 只消费 `stable.m3u`；`all.m3u`/`all.txt` 仅诊断。`cn.m3u`/`global.m3u` 从 stable 派生；所有产物与 `meta.json`、`.state/health.json` 必须共享同一 generation。
- **原子发布**：状态只放 output 分支；验证 job 无写权限。频道/线路跌幅等硬门禁失败时 output SHA 必须不变；GRACE 占比偏高只告警仍发布。更新必须使用代际校验/`force-with-lease`，禁止盲目 force-push。
- **验证 scope 不变式**：改变严格准入定义必须同步升级 `VALIDATION_SCOPE`；旧 PASS/GRACE 不得跨 scope 宽限，首轮基线只能通过手动 workflow 的 `approve_quality_scope_migration` 显式批准。
- **公共头安全**：只透传 UA/Referer/Origin/Accept 类头，禁止 Cookie、Authorization、CR/LF 进入产物或日志；FFmpeg 命令不得拼 shell 字符串。
- **网络隔离**：CI 的 prepare/verify 必须在无凭据容器内运行，并通过 `DOCKER-USER` 阻断私网、metadata、组播目标；禁止改回 host `OUTPUT` 防火墙（会切断 Actions runner 心跳）。
- **分组判定三级顺序**（`normalize.assign_group`，详见 `config/groups.json` 的 `_comment`）：频道名关键字 → 上游分组映射 → 境外兜底 → `default_group`。三条都不能动：(1) **关键字必须压过上游分组**，反过来会让 CCTV-5 被某些上游的「咪咕赛事」拽出央视，而产物里看不出任何异常；(2) **故意不给 iptv-org 的英文分类建映射**（`General` / `News` / `Movies` 是全球分类），否则含汉字的国内频道会被误判成国际，也会让 479 条境外体育台淹掉几十条国内体育台；(3) **分组必须等一个频道的所有流都归并完再判**，同一频道来自多个上游、各家给的分组名不同，只看第一条流会丢掉后来那条才带的信息（内置源 bjzhou 整份没有 `group-title` 且排在 `upstreams` 首位）。繁体变体必须显式列出，iptv-org 的中文频道一律用繁体，漏写「衛視」就会把北京衛視丢进「其他」。
- **`order` 与 `display_order` 是两件事**：`order` 是判定优先级，要「题材优先于地域」——地方台的关键字是行政区名，排在题材桶之前会把「山东体育」「四川妇女儿童」抢走（实测体育少 6 个、少儿少 10 个，而分布表看上去依然合理），所以它必须垫在所有题材桶之后当残余桶；`display_order` 是产物与侧栏顺序，要「常看的排前面」——地方台有四百多频道，排第 10 位意味着用户在遥控器上按九次右键。两个列表必须含相同分组名，漂移时那一整组会被静默甩到末尾。
- **`cn.m3u` / `global.m3u` 归属由 `groups.json` 的 `scope` 驱动**，不得写回代码里的名单：写死时每加一个分组桶都要记得同步改判定函数，忘了就整组判成境外，而这个错没有任何报错出口。漏写 `scope` 会静默落到 `auto` 走汉字启发式，对「影视剧集」里 CHC / NewTV 这类拉丁名频道就会判错。
- **别名表** `config/aliases.json` 与 **分组的 `upstream` 表**是需要人工边跑边补的两处「脏活」。新增频道归并写前者，新上游带来的新分组名写后者，都不要硬编码进代码。
- **上游 URL 一律优先写 GitHub raw，而不是作者自建 CDN**：拉取发生在 GitHub Actions 里（raw 在那儿很快，国内慢与此无关），而自建 CDN 是这份列表**唯一的**腐烂来源——`fanmingming` 的 `live.fanmingming.cn` 挂掉时，同一批 m3u 与 933 张台标在 `fanmingming/live` 仓库里完好无损，换成 raw 路径当场全活。
- **死上游只降级、不失败，所以必须主动点名**：`fetch_all` 拉取失败就跳过（这是对的，拿剩下的源产出比停更好），质量门禁看的是 stable 频道数、少几个源照样够——于是 11 个源里死了 3 个而产出全程正常、日志之外毫无迹象。`ci.prepare` 现在无条件在 bundle 旁写 `upstream_report.json` 与 `upstream_report.md`（随 `ci-work/` 成为 artifact），workflow 那步只做一次 `cat`。两条要求：**报告必须无条件落盘**（workflow 在文件缺失时静默跳过，不写就等于悄悄失去这层观测），**markdown 必须在库里渲染**而不是写成 workflow 内联脚本——这层观测坏掉时不会有任何报错，只会安静地不再报死上游，正是它要防的那种失效，所以渲染得能被单元测试盯住。摘要必须**点名**失败的 URL，只报个数等于还要去翻日志。
- **台标改道**（`config/logo_rewrites.json` + `normalize.rewrite_logo_url`）三条约束：(1) **只许换前缀**，路径与文件名必须原样保留，改路径就是把台标整批打成 404；(2) **必须发生在去重之前**——`Stream.merge_provenance` 与频道级 `logo` 都是「取第一个非空」，晚了既要改好几处、`artifacts` 里落下的还是旧地址；(3) **已判失效到需要改道的 host 不得同时还挂在 `upstreams.txt` 里**等着超时（守卫测试 `test_no_upstream_points_at_a_host_we_rewrite_away`）——`fanmingming` 那次就是既是死上游又是死台标源，两处各自都只是静默失败、看起来都不像问题。
- 新增依赖优先 `uv add`，不手改 `uv.lock`。
- 所有测试必须真实断言；改解析/归一化/准入/状态机/发布契约须补对应测试。

## Notes

- 上游失效：在 `config/upstreams.txt` 行首加 `#` 停用，不要直接删（方便恢复）。维护时先跑 `bash scripts/probe_upstreams.sh` 拿到每个上游的状态码与频道数。
- **2026-08-04 修完的三个死上游**（原因各不相同，值得记住区别）：`live.fanmingming.cn` 是**自建 CDN 挂了而仓库完好**（CNAME 到 `cnlive.pages.dev`，DNS 解析正常但 TLS 握手被重置），改走 raw 即恢复且拿到更大的 `itv.m3u`（189 条）；`YueChan/Live/main/APTV.m3u` 是**文件改名**（现为 `IPTV.m3u`），仓库一直活着；只有 `aktv.space/live.m3u` 是真 404 且未找到新址，已停用，港澳台缺口由 iptv-org 的 hk/tw/mo 子集部分补上。**先查是不是改名或换域名，再考虑找替代**——三个里有两个都不需要替代。
- **台标的可达性和境外占比是同一个问题的两面**。已发布产物 1868 频道里 1850 有台标，但 host 分布是：`i.imgur.com` 715、`images.pluto.tv` 235、`upload.wikimedia.org` 121——加起来 57% 的台标挂在国内基本拉不出来的 host 上，而这 57% 恰好就是那批境外频道。更糟的是 App 的台标回退（`live_panel` 的 `_ChannelLogoImage` 拼 `fanmingming/live/main/tv/{频道名}.png`）里全是**中文**台标，给 `Al Jazeera Arabic` 拼一个必然 404——境外频道等于「主 URL 被墙 + 回退必然 404」，两次失败请求换一个灰图标，还白烧 App 侧 `image_fetcher` 的熔断与限流预算。
- 候选池境外占比约一半（4610 条流里 2308 归「国际」），根源是订阅了 iptv-org 的三个**全球** categories（news / sports / movies 合计 2274 条，绝大多数小语种台）。**是否砍掉是产品决策，不是纯技术修复**：砍了等于放弃 `global.m3u` 的主要内容，同时省下深验预算（最贵的一环）与上面那批必然失败的台标请求。分组修复只是把它们从「其他」里正确标注出来，没有改变体积。
- CI 产物用 `force-with-lease` 单提交写 `output` 分支，避免历史膨胀并防并发覆盖。
- 关键字搜索增量会扩大坏源池，只有 stable 质量指标稳定后才评估。
