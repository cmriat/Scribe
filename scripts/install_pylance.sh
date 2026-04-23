#!/usr/bin/env bash
#
# 从 fecet/lance fork 拉取指定 commit 并编译安装 pylance
# 用法: pixi run build-pylance
#

set -euo pipefail

OFF='\033[0m'
GRN='\033[0;32m'
RED='\033[0;31m'
BOLD=$(tput bold 2>/dev/null || true)
NORM=$(tput sgr0 2>/dev/null || true)

# ===== 锁定版本 =====
LANCE_REPO="https://github.com/fecet/lance.git"
LANCE_COMMIT="27c26ac9f99fdd82b5442c6f069f29bfbb2cd1f1"
LANCE_VERSION="5.0.0-beta.2"

PROJECT_ROOT="${PIXI_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# clone 到项目内 .lance-src（已在 .gitignore 中忽略）
LANCE_SRC="${PROJECT_ROOT}/.lance-src"

echo -e "${GRN}[pylance] Installing pylance ${LANCE_VERSION} (commit ${LANCE_COMMIT:0:8})${OFF}"

# Step 1: Clone or update
if [ -d "$LANCE_SRC" ]; then
  echo -e "${GRN}[pylance] Found existing source at ${LANCE_SRC}, verifying commit...${OFF}"
  cd "$LANCE_SRC"
  CURRENT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
  if [ "$CURRENT_COMMIT" = "$LANCE_COMMIT" ]; then
    echo -e "${GRN}[pylance] Already at correct commit, skipping clone${OFF}"
  else
    echo -e "${GRN}[pylance] Commit mismatch (${CURRENT_COMMIT:0:8} != ${LANCE_COMMIT:0:8}), re-cloning...${OFF}"
    cd "$PROJECT_ROOT"
    rm -rf "$LANCE_SRC"
    git clone "$LANCE_REPO" "$LANCE_SRC"
    cd "$LANCE_SRC"
    git checkout "$LANCE_COMMIT"
  fi
else
  echo -e "${GRN}[pylance] Cloning lance fork...${OFF}"
  git clone "$LANCE_REPO" "$LANCE_SRC"
  cd "$LANCE_SRC"
  git checkout "$LANCE_COMMIT"
fi

# Step 2: Build with maturin develop
echo -e "${GRN}[pylance] Building pylance from source (this may take several minutes)...${OFF}"
cd "$LANCE_SRC/python"

PROTOC="$CONDA_PREFIX/bin/protoc" \
LIBCLANG_PATH="$CONDA_PREFIX/lib" \
PKG_CONFIG="$CONDA_PREFIX/bin/pkg-config" \
PKG_CONFIG_PATH="$CONDA_PREFIX/lib/pkgconfig" \
  maturin develop --release

# Step 3: Verify
echo -e "${GRN}[pylance] Verifying installation...${OFF}"
python -c "import lance; from lance import Blob, blob_array; print(f'[pylance] OK: lance {lance.__version__}, Blob/blob_array available')"

echo -e "${GRN}${BOLD}[pylance] Installation complete!${NORM}${OFF}"
