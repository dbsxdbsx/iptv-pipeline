# iptv-pipeline 常用命令

# 默认：跑测试 + lint
default: test lint

# 安装依赖
sync:
    uv sync

# v0：只聚合去重，不验证（最快）
run-fast:
    uv run iptv-pipeline --no-probe

# v1：完整流程（含验证）
run:
    uv run iptv-pipeline

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
