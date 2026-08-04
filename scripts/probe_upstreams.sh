#!/usr/bin/env bash
# 逐个探测 config/upstreams.txt 里的上游：HTTP 状态、字节数、#EXTINF 条数、耗时。
# 只做只读探测，不改任何配置。
#
# 用法：
#   bash scripts/probe_upstreams.sh                 # 探测 config/upstreams.txt
#   bash scripts/probe_upstreams.sh candidates.txt  # 探测指定列表

set -uo pipefail

LIST="${1:-config/upstreams.txt}"
TIMEOUT="${TIMEOUT:-25}"

printf '%-8s %-10s %-8s %-7s  %s\n' STATUS BYTES CHANNELS TIME URL
printf '%s\n' "--------------------------------------------------------------------------------"

while IFS= read -r line; do
	line="${line%%[[:space:]]#*}"
	line="$(printf '%s' "$line" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
	[ -z "$line" ] && continue
	case "$line" in \#*) continue ;; esac

	body="$(mktemp)"
	meta="$(curl -sS -L --max-time "$TIMEOUT" \
		-w '%{http_code} %{size_download} %{time_total}' \
		-o "$body" "$line" 2>/dev/null)" || meta="ERR 0 0"

	set -- $meta
	code="${1:-ERR}"
	bytes="${2:-0}"
	elapsed="${3:-0}"

	# .m3u 数 #EXTINF；.txt 数 "名字,url" 行（排除 #genre# 段头）
	if grep -qi '^#EXTM3U' "$body" 2>/dev/null; then
		count="$(grep -ci '^#EXTINF' "$body" 2>/dev/null || echo 0)"
	else
		count="$(grep -cE '^[^#][^,]*,[a-zA-Z]+://' "$body" 2>/dev/null || echo 0)"
	fi
	rm -f "$body"

	printf '%-8s %-10s %-8s %-7s  %s\n' "$code" "$bytes" "$count" "$elapsed" "$line"
done <"$LIST"
