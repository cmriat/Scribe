#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
OUTPUT_DIR="${PROJECT_ROOT}/.visualizer_runtime"
HF_HOME="${PROJECT_ROOT}/.hf_cache"
HF_DATASETS_CACHE="${HF_HOME}/datasets"
# ARM3D=true  -> 显示 3D 机械臂
# ARM3D=false -> 关闭 3D 机械臂面板
ARM3D="true"
mkdir -p "${OUTPUT_DIR}" "${HF_DATASETS_CACHE}"

export HF_HOME
export HF_DATASETS_CACHE

cd "${PROJECT_ROOT}"

# 修改为你的本地数据集路径
DATASET_ROOT="${DATASET_ROOT:-/path/to/your/dataset}"

python -m scribe \
      --root "${DATASET_ROOT}" \
      --repo-id local \
      --output-dir "${OUTPUT_DIR}" \
      --force-override 1 \
      --host 0.0.0.0 \
      --port 8011 \
      --3darm "${ARM3D}"
