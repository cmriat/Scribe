#!/usr/bin/env bash
set -euo pipefail

# =====================================================================
# Scribe —— 直接从 BOS 可视化 Lance 数据集（landing 模式）
# 只需修改下面这一行 BOS_PREFIX，指向放着数据集的 BOS 目录前缀。
# 该前缀下的每个 <name>.lance / 含 episode_*.lance 的子目录都会出现在登录页。
# =====================================================================
BOS_PREFIX="${BOS_PREFIX:-bos://srgdata/robot/lance_qz_training_data/}"


# ---- BOS / S3 凭据 ----
# Lance 的 object_store 把 bos:// 当 s3:// 直读，凭据走标准 AWS_* 变量。
# !!! 不要把真实 AK/SK 写进本文件并提交到 git !!!
# 运行前在你自己的 shell 里 export，例如：
#   export AWS_ENDPOINT_URL=https://s3.bj.bcebos.com
#   export AWS_ACCESS_KEY_ID=你的AK
#   export AWS_SECRET_ACCESS_KEY=你的SK
#   export AWS_DEFAULT_REGION=bj

# ---- 其余一般不用改 ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
OUTPUT_DIR="${PROJECT_ROOT}/.visualizer_runtime"
HF_HOME="${PROJECT_ROOT}/.hf_cache"
HF_DATASETS_CACHE="${HF_HOME}/datasets"

PORT="${PORT:-9006}"
HOST="${HOST:-0.0.0.0}"
ARM3D="${ARM3D:-false}"                 # true=显示 3D 机械臂面板
AUTOSAVE_INTERVAL_S="${AUTOSAVE_INTERVAL_S:-60}"  # 标注后台回写 BOS 的间隔(秒)

# Lance 视频按需 materialize 的开关（含义见 run.sh）
LANCE_PREENCODE_ALL="${LANCE_PREENCODE_ALL:-false}"
LANCE_PRELOAD_NEXT="${LANCE_PRELOAD_NEXT:-true}"
LANCE_VIDEO_WORKERS="${LANCE_VIDEO_WORKERS:-3}"
LANCE_VIDEO_POLICY="${LANCE_VIDEO_POLICY:-reencode}"

mkdir -p "${OUTPUT_DIR}" "${HF_DATASETS_CACHE}"
export HF_HOME HF_DATASETS_CACHE LANCE_PREENCODE_ALL LANCE_PRELOAD_NEXT LANCE_VIDEO_WORKERS LANCE_VIDEO_POLICY
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

cd "${PROJECT_ROOT}"

if [[ -z "${AWS_ACCESS_KEY_ID}" || -z "${AWS_SECRET_ACCESS_KEY}" ]]; then
  echo "[run_bos.sh] 警告: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 为空，请先在 shell 里 export 凭据再运行。" >&2
fi

# 优先用 pixi env 里的 python
PY="${PROJECT_ROOT}/.pixi/envs/default/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="python"
fi

echo "[run_bos.sh] bos-prefix : ${BOS_PREFIX}"
echo "[run_bos.sh] endpoint   : ${AWS_ENDPOINT_URL}"
echo "[run_bos.sh] serving    : http://${HOST}:${PORT}/"
echo "[run_bos.sh] arm3d      : ${ARM3D}"
echo "[run_bos.sh] lance      : preencode_all=${LANCE_PREENCODE_ALL}, preload_next=${LANCE_PRELOAD_NEXT}, video_workers=${LANCE_VIDEO_WORKERS}, video_policy=${LANCE_VIDEO_POLICY}"

exec "${PY}" -m scribe \
      --bos-prefix "${BOS_PREFIX}" \
      --output-dir "${OUTPUT_DIR}" \
      --host "${HOST}" \
      --port "${PORT}" \
      --autosave-interval-s "${AUTOSAVE_INTERVAL_S}" \
      --3darm "${ARM3D}"
