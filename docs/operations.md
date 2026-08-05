# 运维手册

面向仓库维护者。不绑定任何特定播放器产品。

## 1. 投递端点（长期主站）

权威配置：`config/delivery.json`。发布时写入 `manifest.json` 的：

- `endpoints`：playlist（通常是 `stable.m3u`）有序回退列表
- `manifest_endpoints`：manifest 自身的有序回退列表

### 已内置的免费路径

1. GitHub `raw.githubusercontent.com`（`output` 分支）
2. jsDelivr `cdn.jsdelivr.net/gh/<owner>/iptv-pipeline@output/...`
3. （可选）GitHub Pages：`https://<owner>.github.io/iptv-pipeline/...`  
   启用方式见下节。

### 启用 GitHub Pages（推荐的零成本主站）

仓库管理员执行一次：

```bash
gh api -X POST "repos/<owner>/iptv-pipeline/pages" \
  -f build_type=legacy \
  -F source[branch]=output \
  -F source[path]=/
```

生效后把下列地址**插到** `config/delivery.json` 两个列表的**最前**（先于 raw / jsDelivr）：

```text
https://<owner>.github.io/iptv-pipeline/stable.m3u
https://<owner>.github.io/iptv-pipeline/manifest.json
```

推送 `main` 后下一轮 publish 会带上新 endpoints。订阅端若已按 manifest 热更新，**不必发版**。

### 自有域名

在任意静态托管（对象存储 / Nginx / Cloudflare Pages 等）同步 `output` 分支文件后，把 HTTPS 地址插到列表最前即可。同步可用：

```bash
git fetch origin output
git archive --format=tar origin/output | tar -x -C /var/www/iptv/
```

## 2. 国内探测视角 / 常驻探测机

默认 CI 跑在 GitHub 托管 runner（境外）。许多国内运营商 / 省网 / 酒店源从境外超时，会进不了 `stable`，即使大陆宽带可播。

### 最小方案

1. 在**大陆出口**机器（家宽 NAS、国内 VPS、长期开机的 PC）安装 [GitHub Actions self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners)。
2. 给 runner 打标签：`self-hosted`、`linux`、`china`（必须含 `china`）。
3. 机器上需能跑 Docker（与主 workflow 相同验证镜像），或至少安装本仓库 `Dockerfile` 里那套 FFmpeg/GStreamer/PyGObject。
4. 在 GitHub Actions 里手动运行 workflow **「国内视角深验」**（`domestic-verify.yml`），或等待其 schedule。

### 常驻

保持 runner 进程 systemd / 容器常开即可。比纯 GitHub cron 更密、更能复现「本机能播 / 托管 CI 判死」。

### 行为说明

- `domestic-verify` **不直接改** `output` 分支，避免境外/境内视角互相覆盖。
- 产物以 Actions artifact 形式上传（`candidates.json`、`deep-results-0.json`、上游报告），供人工对比或后续合并策略使用。
- 真正把「国内 PASS」并进主 `stable` 需要额外的合并策略；有稳定 runner 后再迭代，不要在没有大陆出口时空跑。

## 3. 配置维护（低优先）

| 文件 | 做什么 |
|---|---|
| `config/aliases.json` | 根据 `meta.json.alias_candidates` 补频道别名 |
| `config/groups.json` | 只为**题材可信**的上游分组名加 `upstream`；混杂名不要映射 |
| `config/upstreams.txt` | 失效源行首加 `#`；优先 raw.githubusercontent.com |
| `config/delivery.json` | 调整投递优先级 |

境外大盘：默认已停用 iptv-org 全球 `news` / `sports` / `movies`（保留 `languages/zho.m3u`）。若要重新扩 `global.m3u`，取消 `upstreams.txt` 里对应注释即可。

主动大幅收缩供给后，publish 可能因「频道跌幅 > 25%」失败。需手动触发 `更新直播源` workflow，并勾选 **`approve_stable_baseline_reset`**（仍强制 `minimum_stable_channels`）。之后新基线生效，日常跌幅门禁恢复。
