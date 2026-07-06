#!/usr/bin/env bash
# training_lance_to_lerobot.sh — fastest-preset launcher for the v2.1 converter.
#
# Production-tuned defaults (≈ 22× faster than no-flags baseline on 180-core /
# H20Z host; 200 ep ≈ 100 s):
#   --num-workers 32                process pool size (mp.spawn, lance-safe)
#   --cams-parallel 1               1 codec instance per worker (avoids cache thrash;
#                                   cams_parallel=3 is ONLY a win at workers ≤ 8)
#   --encoder-threads 0             AUTO = cores // (workers × cams) per codec
#   --decoder-threads 2             modest decoder threading
#   --libx264-preset ultrafast      ~2-3× faster encode at marginal quality loss
#                                   (source GOPs are already lossy at ~crf 23,
#                                    so the second-pass quality floor is set there)
#   --image-sample-stride 0         skip image stats (LeRobot computes via
#                                    aggregate_stats on demand; saves ~15-20%)
#
# Usage:
#   1) 直接改下面「只改这两行」的 SRC_LANCE / OUT_DIR，然后运行：
#        ./training_lance_to_lerobot.sh
#   2) 或临时用位置参数覆盖（不改脚本）：
#        ./training_lance_to_lerobot.sh <SRC_LANCE> <OUT_DIR> [REPO_ID]
#
# If REPO_ID is omitted, derives one from the OUT_DIR basename.

set -euo pipefail

# =====================================================================
# 只改这两行：SRC_LANCE = build_training_lance.py 产出的训练就绪 .lance；
#            OUT_DIR   = LeRobot v2.1 输出目录（不能已存在且非空，脚本拒绝覆盖）。
# 命令行位置参数（$1 / $2 / $3）会覆盖这里的默认值。
# =====================================================================
SRC_LANCE="/home/jovyan/code/lance_data_collections/lance_raw_data/20260625_qz4_bigshirt_fold_30HZ.lance"
OUT_DIR="/home/jovyan/code/lance_data_collections/a_lerobot_test"

# ---- 位置参数覆盖（可选，不传就用上面写死的路径）----
SRC_LANCE="${1:-$SRC_LANCE}"
OUT_DIR="${2:-$OUT_DIR}"
REPO_ID="${3:-local/$(basename "$OUT_DIR")}"

if [[ ! -d "$SRC_LANCE" ]]; then
    echo "[error] SRC_LANCE not found or not a directory: $SRC_LANCE" >&2
    exit 2
fi
if [[ -e "$OUT_DIR" && -n "$(ls -A "$OUT_DIR" 2>/dev/null || true)" ]]; then
    echo "[error] OUT_DIR exists and is non-empty: $OUT_DIR" >&2
    echo "        (refusing to overwrite — pick a fresh path or delete first)" >&2
    exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
cd "$REPO"

echo "[launch] $(date -u +%FT%TZ)"
echo "  src:    $SRC_LANCE"
echo "  out:    $OUT_DIR"
echo "  repo:   $REPO_ID"
echo

t0=$SECONDS
pixi run python tools/training_lance_to_lerobot.py \
    --lance              "$SRC_LANCE" \
    --output-dir         "$OUT_DIR" \
    --repo-id            "$REPO_ID" \
    --num-workers        32 \
    --cams-parallel      1 \
    --encoder-threads    0 \
    --decoder-threads    2 \
    --libx264-preset     ultrafast \
    --image-sample-stride 0
echo
echo "[launch] wall: $((SECONDS - t0))s"
