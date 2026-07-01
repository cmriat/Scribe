# Scribe

**LeRobot 数据集可视化与标注工具**

基于 Flask 的 Web 应用，用于可视化和标注 [LeRobot v2.1](https://github.com/huggingface/lerobot) 格式的机器人操作数据集。支持本地数据集、HuggingFace Hub 远程数据集，以及**直接从 BOS / S3 对象存储在线浏览 Lance 数据集**。

当前版本：v0.3.0

v0.3.0 新增 **BOS / S3 在线可视化**：给一个对象存储前缀即可在登录页列出其下所有 Lance 数据集，点击后按需打开，数据通过 Lance 的 object_store 直接从 BOS 流式读取，无需先整份下载；标注 sidecar 在打开时拉到本地、编辑落本地、再自动回写 BOS。详见下文 [BOS / S3 在线可视化](#bos--s3-在线可视化)。

v0.2.0 完成本地 Lance 数据集可视化适配：支持 `.lance` episode/episode 目录按需打开，支持三路相机 H264 GOP blob materialize 为 MP4，并保持每一行机械臂数据与原始 Lance 相机帧对齐。Lance 数据集上的标注流程尚未完成专项测试和适配。

## 核心功能

**可视化**
- 多相机视频同步播放
- Dygraph 时序曲线（关节状态、动作等）
- SBS1 双臂 3D 模型联动（URDF 驱动）
- 表格数据勾选显隐
- 本地 Lance 数据集按需可视化（保持原始帧/机械臂数据对齐）
- BOS / S3 在线 Lance 数据集浏览（登录页选数据集，数据直读对象存储）

**标注**
- Episode 级 Curation（Keep / Delete 标记）
- Sparse Stage Segment 标注（支持自定义任务模板）
- Frame Event 标注（抓取成功、碰撞等帧级事件）
- 离线导出训练工件（progress label、clip manifest）

## 项目结构

```
Scribe/
├── scribe/                      # 主 Python 包
│   ├── app.py                   # CLI 入口、资源准备、服务启动
│   ├── routes.py                # Flask 路由（页面 + API）
│   ├── data.py                  # 数据加载、LRU 缓存、CSV 生成
│   ├── lance_backend.py         # Lance 数据集适配、GOP 视频 materialize、帧映射（支持 bos:// 直读）
│   ├── bos_discovery.py         # BOS/S3 前缀下 Lance 数据集发现（merged / raw_episodes）
│   ├── dataset_registry.py      # 进程级数据集注册表、远程数据集 LRU 懒加载
│   ├── bos_sync.py              # 标注 sidecar 的 BOS pull/push + AutoSaver
│   ├── annotation_store.py      # 标注 sidecar 存储（CRUD + 校验）
│   ├── export.py                # 离线导出脚本
│   ├── tools/                   # 数据处理工具（如 Lance instruction 改写）
│   ├── templates/
│   │   ├── visualize.html       # 前端主界面（Alpine.js）
│   │   └── landing.html         # BOS 数据集选择登录页
│   └── vendor/                  # 前端依赖（需下载，见下文）
├── scripts/
│   ├── run.sh                   # 本地单数据集启动脚本
│   ├── run_bos.sh               # BOS / S3 landing 模式启动脚本
│   └── download_vendor.sh       # 下载前端依赖
├── robot_assets/                # SBS1 双臂 URDF/Xacro/STL
├── pixi.toml                    # 依赖管理 + task 定义
├── ruff.toml                    # 代码检查配置
└── CLAUDE.md                    # AI 助手开发文档
```

## 快速开始

### 环境安装

本项目使用 [pixi](https://pixi.sh) 管理依赖：

```bash
# 安装 pixi（如果还没有）
curl -fsSL https://pixi.sh/install.sh | bash

# 安装所有依赖
pixi install
```

### 首次运行

```bash
# 1. 下载前端依赖（只需执行一次）
pixi run vendor

# 2. 启动服务器
DATASET_ROOT=/path/to/your/lerobot/dataset pixi run serve
```

浏览器打开 `http://localhost:8011` 即可访问。

### 模块直接调用

```bash
pixi shell

python -m scribe \
  --root /path/to/dataset \
  --repo-id local \
  --output-dir ./.visualizer_runtime \
  --host 0.0.0.0 \
  --port 8011 \
  --3darm true
```

> `--repo-id` 建议使用 `namespace/name` 格式。若只给单段（如 `local`），后端会自动规范化为 `local/<目录名>`。

### 3D 机械臂开关

```bash
ARM3D=false pixi run serve   # 关闭 3D 面板
```

### BOS / S3 在线可视化

无需把数据集下载到本地，给一个对象存储前缀即可在登录页浏览其下所有 Lance 数据集。

```bash
# 1. 在 shell 里 export BOS 凭据（不要写进脚本提交到 git）
export AWS_ENDPOINT_URL=https://s3.bj.bcebos.com
export AWS_ACCESS_KEY_ID=你的AK
export AWS_SECRET_ACCESS_KEY=你的SK
export AWS_DEFAULT_REGION=bj

# 2. 启动 landing 模式（只需改脚本顶部 BOS_PREFIX 一行）
bash scripts/run_bos.sh
```

等价模块入口：

```bash
python -m scribe \
  --bos-prefix bos://srgdata/robot/lance_qz_training_data/ \
  --output-dir ./.visualizer_runtime \
  --host 0.0.0.0 --port 9006 \
  --autosave-interval-s 60
```

浏览器打开后会看到登录页，列出前缀下识别到的两种 Lance 数据集：

- **merged** — 单个 `<name>.lance` 目录（降采样合并后的训练输出）
- **raw_episodes** — 含 `episode_*.lance` 子目录的目录（每集原始采集）

点击任意数据集即按需打开。要点：

- `--bos-prefix` 与 `--root` / `--repo-id` / `--load-from-hf-hub` **互斥**。
- 数据直读：BOS 与 S3 协议兼容，代码在边界把 `bos://` 改写成 `s3://`，endpoint 由 `AWS_ENDPOINT_URL` 决定；机械臂列和相机 GOP blob 通过 Lance object_store 流式读取，只有 materialize 出的 MP4 缓存到本地。
- 标注 sidecar 打开时从 `<uri>/annotations/` 拉到本地缓存 `<output_dir>/bos_annotation_cache/<ns>__<name>/annotations/`，编辑落本地文件，再由 Save / `AutoSaver`（默认 60s）/ LRU 驱逐 / 进程退出自动回写 BOS。
- **单用户 MVP**：无多用户冲突检测，多人同时标注同一数据集会互相覆盖。

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `--bos-prefix` | — | BOS / S3 前缀 URI（必填，进入 landing 模式） |
| `--autosave-interval-s` | `60` | 标注后台回写 BOS 的间隔（秒） |
| `SCRIBE_DATASET_LRU` | `3` | 同时常驻内存的远程数据集数量上限 |
| `AWS_ENDPOINT_URL` | — | BOS endpoint（如 `https://s3.bj.bcebos.com`） |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | — | BOS 凭据（请走环境变量，勿硬编码提交） |

### Lance 数据集可视化

当 `--root` / `DATASET_ROOT` 指向单个 `.lance` 目录或包含 `episode_*.lance` 的目录时，后端会自动使用 `LanceDataset`：

```bash
DATASET_ROOT=/path/to/lance_root REPO_ID=local/my-dataset pixi run serve
```

如果环境中的 `lance` 缺少 `Blob` / `blob_array` 支持，可以使用固定版本安装任务：

```bash
pixi run build-pylance        # 从 fecet/lance 固定 commit 源码编译安装
pixi run build-pylance-wheel  # 使用 third_party 中已缓存的 pylance wheel 安装，不重装 pixi 依赖
```

Lance 视频会按需 materialize 到 `.visualizer_runtime/lance_runtime/videos/` 下。默认不会在启动时全量扫描并生成所有 episode，避免大量 CPU/磁盘 IO 影响实时浏览。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `LANCE_PREENCODE_ALL` | `false` | 是否启动后后台 materialize 全部 episode 视频 |
| `LANCE_PRELOAD_NEXT` | `true` | 打开当前 episode 后是否后台预加载下一个 episode |
| `LANCE_VIDEO_WORKERS` | `3` | Lance 视频 materialize 并发数，默认对应 left/mid/right 三路相机 |

对齐原则：

- 机械臂时序数据保持 Lance 原始采样行，不做行降采样。
- 视频优先将 Lance 中的 H264 GOP 直接 copy remux 为 MP4；仅在 copy remux 失败时才回退到编码。
- 前端使用 Lance 的 `*_gop_index` 与 `*_frame_index_in_gop` 推导出的 MP4 内部帧号进行 seek，确保每一行机械臂数据对应原始 Lance 记录中的相机帧。
- 播放时优先使用浏览器 `requestVideoFrameCallback` 按实际呈现的视频帧同步曲线、表格和 3D 机械臂。

### Lance instruction 改写工具

`scribe.tools.rewrite_lance_instruction` 用于把一个或多个 Lance 数据集复制到新位置，并把实际 `language_instruction` 列改成指定的新指令。它适合修正已经 merge 后、需要继续用于训练的 Lance 数据集。

使用方式：

```bash
python -m scribe.tools.rewrite_lance_instruction \
  --source bos://srgdata/robot/test_data/20260420_qz4_bigshirt_mid30.lance \
  --target bos://srgdata/robot/test_data/20260420_qz4_bigshirt_mid30_fixed.lance \
  --instruction "准确的新 instruction" \
  --overwrite
```

路径规则：

- `--source` 可以是单个 `.lance`，也可以是包含多个直接子级 `*.lance` 的目录/前缀。
- 当 `--source` 是单个 `.lance` 且 `--target` 是目录时，输出文件名默认沿用源文件名。
- 当 `--source` 是单个 `.lance` 且要在同一目录生成修正版时，建议把 `--target` 显式写成 `<源文件名>_fixed.lance`。
- 当 `--source` 是目录/前缀时，`--target` 也必须是目录/前缀；工具会处理其下一层的所有 `*.lance`，并保持原文件名。
- `bos://` 会在工具内部改写为 Lance/fsspec 可用的 `s3://`，endpoint 仍由 `AWS_ENDPOINT_URL` 决定。

正式执行前建议先 dry run：

```bash
python -m scribe.tools.rewrite_lance_instruction \
  --source bos://srgdata/robot/test_data/source_folder \
  --target bos://srgdata/robot/test_data/fixed_folder \
  --instruction "准确的新 instruction" \
  --dry-run
```

批量改写可以使用 JSON 配置：

```json
{
  "items": [
    {
      "source": "bos://srgdata/robot/lance_qz_training_data/a.lance",
      "target": "bos://srgdata/robot/lance_qz_training_data/a_fixed.lance",
      "instruction": "准确的新 instruction A",
      "overwrite": true
    },
    {
      "source": "bos://srgdata/robot/lance_qz_training_data/b.lance",
      "target": "bos://srgdata/robot/lance_qz_training_data/b_fixed.lance",
      "instruction": "准确的新 instruction B"
    }
  ]
}
```

执行：

```bash
python -m scribe.tools.batch_rewrite_lance_instruction \
  --config batch_rewrite.json \
  --dry-run

python -m scribe.tools.batch_rewrite_lance_instruction \
  --config batch_rewrite.json
```

每个 item 的 `overwrite` 可选；缺省时不覆盖已有目标。也可以在命令行传 `--overwrite`，作为所有未声明 `overwrite` 的 item 的默认值。批量执行中某个 item 失败时会在终端打印 `[error] ...`，继续处理后续 item，并在最后汇总成功/失败数量；只要有失败，命令最终返回非 0。

安全边界：

- 工具不会 in-place 修改源数据；`source` 只读，结果写到新的 `target`。
- `--overwrite` 只会删除已存在的目标路径，不会删除源路径。
- 如果中途网络中断，目标路径可能留下半成品；重新使用同一目标路径并加 `--overwrite` 跑一遍即可。
- 对含 Lance blob 视频列的数据集，工具不会解码或重写 blob。它只复制 Lance 文件树，然后按 `airbot_play_ws` 的 stamp 方式改轻量列和 schema metadata：`language_instruction` 全量替换，`task_index` 统一为 `0`，`lerobot:tasks_json` 同步为新的单任务映射。

#### v0.2.0 Lance 处理方案

Scribe 对 Lance 的处理分为四步：

1. 发现 episode
   - 如果 `DATASET_ROOT` 本身是 `.lance` 目录，则视为单 episode。
   - 如果 `DATASET_ROOT` 是普通目录，则扫描其中的 `episode_*.lance` 子目录。
   - episode 按文件名稳定排序，对外映射为 Scribe 的 `episode_index`。

2. 保持机械臂行数据原样
   - `observation.state`、`action`、velocity、effort 等字段从 Lance 非 blob 列读取。
   - `timestamp`、`frame_index`、`episode_index`、`index`、`task_index` 按 Scribe 需要补齐。
   - 当前版本不做行降采样、不跳帧、不缩减默认机械臂列。

3. materialize 相机视频
   - Lance 相机数据以 H264 Annex B GOP blob 存储，常见 blob 列为 `left`、`mid`、`right`。
   - 后端读取 `<cam>_gop_index` 和 `<cam>_frame_index_in_gop`，找到每个唯一 GOP 第一次出现的 row index。
   - 唯一 GOP 按 GOP index 排序后，通过 `take_blobs()` 读取。
   - GOP blob 逐个流式写入 ffmpeg stdin，避免把完整 episode 拼成一个大 bytes。
   - ffmpeg 优先 `-c:v copy` 生成 MP4；失败时 fallback 到 `libx264` intra-frame encode。

4. 建立 row-to-frame seek 映射
   - `_gop_layout(cam)` 计算每个 GOP 在 materialized MP4 中的 frame offset。
   - 每一行 robot 数据通过 `gop_offset + frame_index_in_gop` 得到 MP4 内部帧号。
   - `get_episode_video_seek_info()` 将该映射返回给前端。
   - 前端用该映射进行 video time 和 table/chart row 的双向同步。

#### v0.2.0 性能优化

- 当前 episode 的视频 materialize 与 CSV/metadata 构建并行执行，减少首次打开等待。
- 三路相机默认并行 materialize：`LANCE_VIDEO_WORKERS=3`。
- GOP layout 和 row-to-video-frame 映射按 episode/camera 缓存，避免重复扫描 Lance 标量列。
- ffmpeg 输入改为流式 GOP 写入，降低 Python 内存峰值。
- 下一集可后台预加载：`LANCE_PRELOAD_NEXT=true`。
- materialized MP4 缓存在 `.visualizer_runtime/lance_runtime/videos/`，重复打开同一 episode 时可复用。

#### v0.2.0 验证状态

已验证：

- `pixi run check` 通过。
- 真实 Lance 数据 smoke test 通过：

```text
/home/jovyan/code/lance_data_collections/20260420_qz4_bigshirt/episode_0005.lance
```

验证内容包括：

- `LanceDataset` 初始化。
- 三路相机 MP4 materialize。
- episode CSV 生成。
- `observation.images.left` / `mid` / `right` seek 映射生成。
- 输出 MP4 文件非空。

尚未验证：

- Lance 数据集上的 episode curation。
- Lance 数据集上的 sparse segment annotation。
- Lance 数据集上的 frame event annotation。
- Lance 数据集上的离线 export。

这些内容会作为下一版本的主要工作。

## Pixi Tasks

| 命令 | 说明 |
|------|------|
| `pixi run serve` | 启动可视化服务器 |
| `pixi run vendor` | 下载前端 vendor 依赖 |
| `pixi run build-pylance` | 从固定 commit 编译安装支持 Lance blob 的 pylance |
| `pixi run build-pylance-wheel` | 从 `third_party/` 缓存 wheel 安装 pylance，不重装 pixi 依赖 |
| `pixi run export` | 导出标注工件 |
| `pixi run check` | 一键全量检查（lint + 格式 + 语法） |
| `pixi run lint` | Ruff 代码检查 |
| `pixi run lint-fix` | Ruff 自动修复 |
| `pixi run format` | Ruff 格式化 |
| `pixi run syntax` | Python 语法验证 |

## 标注系统

### Sidecar 原则

所有标注数据存储为 sidecar 文件，**不修改** LeRobot 原始 `data/`、`videos/`、`meta/` 目录。

标注文件位于 `<dataset_root>/annotations/` 下：

| 文件 | 说明 |
|------|------|
| `episode_curation.json` | Episode 级 Keep/Delete 决策 |
| `task_annotation_config.json` | 任务模板定义（stage 顺序/颜色） |
| `segment_annotations.json` | 核心段标注数据 |
| `frame_events.jsonl` | 帧级事件标注 |

### 内置任务模板

- **`fold_long_horizon`**：`pick_up → spread → fold → place`（自动匹配折叠类任务）
- **`generic_long_horizon`**：`acquire → arrange → operate → place`（通用兜底模板）

### 离线导出

```bash
DATASET_ROOT=/path/to/dataset pixi run export
```

产出结构：

```
exports/subtask_export/
├── clip_manifest.jsonl        # 每个 stage segment 一条 clip 记录
├── progress/
│   └── episode_XXXXXX.parquet # 逐帧 progress label
├── meta/
│   ├── subtasks.parquet       # stage 目录
│   └── stage_priors.json      # 各 stage 平均时间占比
└── export_report.json         # 导出报告
```

## API 路由

### 页面路由

| 路由 | 说明 |
|------|------|
| `GET /` | 主页/跳转；landing 模式下渲染 BOS 数据集列表 |
| `GET /<ns>/<name>/episode_<id>` | Episode 可视化页 |
| `GET /<ns>/<name>/episode_<id>.json` | Episode 数据（JSON，用于同页切换） |

### BOS / 同步 API（仅 landing 模式）

| 路由 | 方法 | 说明 |
|------|------|------|
| `/api/bos/refresh` | POST | 清除发现缓存、刷新数据集列表 |
| `/<ns>/<name>/api/sync-now` | POST | 立即把该数据集标注回写到 BOS |
| `/<ns>/<name>/api/sync-status` | GET | 查询该数据集的同步状态 |

### 标注 API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/api/episode-curation` | GET/POST | Episode Curation |
| `/api/episode-curation/export-delete-list` | GET | 导出删除候选列表 |
| `/api/segment-annotations` | GET/POST | Segment 标注 |
| `/api/segment-annotations/<id>` | DELETE | 删除 Segment |
| `/api/frame-events` | GET/POST | Frame Event |
| `/api/frame-events/<id>` | DELETE | 删除 Event |
| `/api/task-annotation-config` | GET | 任务模板配置 |

> 所有 API 路由前缀为 `/<namespace>/<dataset_name>/`。标注 API 仅在本地数据集模式下可用。

## 快捷键

| 按键 | 功能 |
|------|------|
| `Space` | 播放/暂停 |
| `←` / `→` | 前/后一帧 |
| `↑` / `↓` | 上/下一个 Episode |

## 技术栈

- **后端**：Python 3.10 + Flask + LeRobot
- **前端**：Alpine.js + Tailwind CSS + Dygraph + Three.js
- **3D 渲染**：Three.js + STLLoader（URDF 驱动）
- **数据格式**：LeRobot v2.1 + Lance（本地 / BOS / S3）
- **对象存储**：Lance object_store（数据直读）+ s3fs / fsspec（发现与标注同步）
- **依赖管理**：pixi (conda-forge + PyPI)
- **代码检查**：Ruff

## License

本项目可视化部分基于 [lerobot v0.3.3](https://github.com/huggingface/lerobot) 提取改造，原始代码遵循 Apache License 2.0。
