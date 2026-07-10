"""命令行入口。

用法：
    uv run iptv-pipeline                 # 完整流程（含验证）
    uv run iptv-pipeline --no-probe      # v0 模式：只聚合不验证，最快跑通
    uv run iptv-pipeline --config config --output dist-output
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .pipeline import run_pipeline


def _setup_logging() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IPTV 直播源自动聚合管道")
    p.add_argument("--config", default="config", help="配置目录（默认 config）")
    p.add_argument("--output", default="dist-output", help="产出目录（默认 dist-output）")
    p.add_argument(
        "--state",
        default="state/health.json",
        help="健康状态文件（默认 state/health.json）",
    )
    p.add_argument(
        "--no-probe",
        action="store_true",
        help="跳过有效性验证（v0 模式，只聚合去重）",
    )
    p.add_argument("--probe-timeout", type=int, default=8, help="单条流探测超时秒数")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = _parse_args(argv)

    stats = asyncio.run(
        run_pipeline(
            config_dir=Path(args.config),
            output_dir=Path(args.output),
            state_path=Path(args.state),
            do_probe=not args.no_probe,
            probe_timeout=args.probe_timeout,
        )
    )

    if stats.upstreams_ok == 0:
        logging.error("没有任何上游拉取成功，产出可能为空")
        return 1
    if stats.channels == 0:
        logging.error("解析后频道数为 0，请检查上游格式")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
