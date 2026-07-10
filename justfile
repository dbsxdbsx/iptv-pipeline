# iptv-pipeline 常用命令

# 默认：跑测试 + lint
default: test lint

# 安装依赖
sync:
    uv sync

# v0：只聚合去重，不验证（最快）
run-fast:
    uv run iptv-pipeline --no-probe

# 完整流程（含 FFmpeg 深验，可能耗时较长）
run:
    uv run iptv-pipeline

# 仅 L0 快筛，不产生 stable 条目
run-fast-check:
    uv run iptv-pipeline --skip-deep

# CI 候选准备阶段
prepare:
    uv run iptv-pipeline-ci prepare --bundle ci-work/candidates.json

# 单元测试
test:
    uv run pytest -q

# lint 检查
lint:
    uv run ruff check src tests

# 格式化
fmt:
    uv run ruff format src tests

# 清理产物与缓存
clean:
    rm -rf dist-output .pytest_cache .ruff_cache
