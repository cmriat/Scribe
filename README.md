# Scribe

**LeRobot 数据集可视化与标注工具**

基于 Flask 的 Web 应用，用于可视化和标注 [LeRobot v2.1](https://github.com/huggingface/lerobot) 格式的机器人操作数据集。支持本地数据集和 HuggingFace Hub 远程数据集。

## 核心功能

**可视化**
- 多相机视频同步播放
- Dygraph 时序曲线（关节状态、动作等）
- SBS1 双臂 3D 模型联动（URDF 驱动）
- 表格数据勾选显隐
- 本地 Lance 数据集按需可视化（保持原始帧/机械臂数据对齐）

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
│   ├── annotation_store.py      # 标注 sidecar 存储（CRUD + 校验）
│   ├── export.py                # 离线导出脚本
│   ├── templates/
│   │   └── visualize.html       # 前端主界面（Alpine.js）
│   └── vendor/                  # 前端依赖（需下载，见下文）
├── scripts/
│   ├── run.sh                   # 启动脚本
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
| `LANCE_VIDEO_WORKERS` | `1` | Lance 视频 materialize 并发数 |

对齐原则：

- 机械臂时序数据保持 Lance 原始采样行，不做行降采样。
- 视频优先将 Lance 中的 H264 GOP 直接 copy remux 为 MP4；仅在 copy remux 失败时才回退到编码。
- 前端使用 Lance 的 `*_gop_index` 与 `*_frame_index_in_gop` 推导出的 MP4 内部帧号进行 seek，确保每一行机械臂数据对应原始 Lance 记录中的相机帧。
- 播放时优先使用浏览器 `requestVideoFrameCallback` 按实际呈现的视频帧同步曲线、表格和 3D 机械臂。

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
| `GET /` | 主页/跳转 |
| `GET /<ns>/<name>/episode_<id>` | Episode 可视化页 |
| `GET /<ns>/<name>/episode_<id>.json` | Episode 数据（JSON，用于同页切换） |

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
- **依赖管理**：pixi (conda-forge + PyPI)
- **代码检查**：Ruff

## License

本项目可视化部分基于 [lerobot v0.3.3](https://github.com/huggingface/lerobot) 提取改造，原始代码遵循 Apache License 2.0。
