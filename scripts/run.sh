#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
OUTPUT_DIR="${PROJECT_ROOT}/.visualizer_runtime"
HF_HOME="${PROJECT_ROOT}/.hf_cache"
HF_DATASETS_CACHE="${HF_HOME}/datasets"

# ---- 可改参数 ----
DATASET_ROOT="${DATASET_ROOT:-/home/jovyan/code/lance_data_collections/20260420_qz4_bigshirt}"
REPO_ID="${REPO_ID:-local/qz2-fold-bigshirt}"
PORT="${PORT:-9006}"
HOST="${HOST:-0.0.0.0}"
# ARM3D=true  -> 显示 3D 机械臂
# ARM3D=false -> 关闭 3D 机械臂面板
ARM3D="${ARM3D:-true}"
# Lance 视频默认按需 materialize，不在启动时全量扫完整数据集。
LANCE_PREENCODE_ALL="${LANCE_PREENCODE_ALL:-false}"
LANCE_PRELOAD_NEXT="${LANCE_PRELOAD_NEXT:-true}"
LANCE_VIDEO_WORKERS="${LANCE_VIDEO_WORKERS:-3}"

mkdir -p "${OUTPUT_DIR}" "${HF_DATASETS_CACHE}"
export HF_HOME HF_DATASETS_CACHE LANCE_PREENCODE_ALL LANCE_PRELOAD_NEXT LANCE_VIDEO_WORKERS
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

cd "${PROJECT_ROOT}"

# 优先使用 pixi env 里的 python，避免系统 python 没装依赖
PY="${PROJECT_ROOT}/.pixi/envs/default/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="python"
fi

echo "[run.sh] dataset : ${DATASET_ROOT}"
echo "[run.sh] repo-id : ${REPO_ID}"
echo "[run.sh] serving : http://${HOST}:${PORT}/"
echo "[run.sh] arm3d   : ${ARM3D}"
echo "[run.sh] lance   : preencode_all=${LANCE_PREENCODE_ALL}, preload_next=${LANCE_PRELOAD_NEXT}, video_workers=${LANCE_VIDEO_WORKERS}"

exec "${PY}" -m scribe \
      --root "${DATASET_ROOT}" \
      --repo-id "${REPO_ID}" \
      --output-dir "${OUTPUT_DIR}" \
      --host "${HOST}" \
      --port "${PORT}" \
      --3darm "${ARM3D}"
