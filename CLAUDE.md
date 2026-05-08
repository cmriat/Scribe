# CLAUDE.md — Scribe

本文件用于帮助 AI 助手快速理解 Scribe 的结构、运行方式与当前开发状态。

最后更新：2026-04-27

当前版本：v0.2.0

v0.2.0 主线：Lance 数据集可视化适配与性能优化。该版本已经支持本地 `.lance` episode/episode 目录进入 Scribe 的可视化界面，并完成三路相机 H264 GOP blob 到 MP4 的按需 materialize、row-to-video-frame seek 映射、原始机械臂行数据同步展示。Lance 数据集上的标注流程尚未完成专项测试，下一版本再做 Lance 标注适配和验证。

---

## 0) 开发环境

- 进入开发环境：`pixi shell`
- 环境依赖更新 → 修改 `pixi.toml`
- pylance 安装：
  ```bash
  pixi run build-pylance        # 从 fecet/lance 固定 commit 源码编译
  pixi run build-pylance-wheel  # 使用 third_party 中缓存的 wheel，不重装 pixi 依赖
  ```
- Python 语法检查：
  ```bash
  ./.pixi/envs/default/bin/python -m py_compile \
    scribe/app.py \
    scribe/routes.py \
    scribe/data.py \
    scribe/annotation_store.py \
    scribe/export.py
  ```

## 1) 目标与范围

- 目标：可视化 + 标注 **LeRobot v2.1** 数据集（本地/Hub），核心能力分为两层：
  - **可视化层**：多相机视频同步播放、Dygraph 时序曲线、SBS1 双臂 3D 联动、表格勾选
  - **标注层**：Episode 级 curation (K/D)、Sparse stage segment 标注、Frame event 标注、离线导出训练工件
- Lance 本地数据集可视化目标：快速按需读取，同时保持每一行机械臂数据与 Lance 记录中的相机帧严格对齐。不要通过降采样、跳帧、缩分辨率等方式牺牲“原封不动”可视化。

## 2) 快速启动

```bash
scripts/download_vendor.sh   # 首次或 vendor 缺失时执行一次
scripts/run.sh
```

3D 开关：

```bash
ARM3D=true  bash scripts/run.sh   # 显示 3D 机械臂（默认）
ARM3D=false bash scripts/run.sh   # 关闭 3D 机械臂面板
```

Lance 性能开关：

```bash
LANCE_PREENCODE_ALL=false bash scripts/run.sh  # 默认：不启动全量视频 materialize
LANCE_PRELOAD_NEXT=false bash scripts/run.sh   # 关闭下一个 episode 的后台预加载
LANCE_VIDEO_WORKERS=3 bash scripts/run.sh      # 默认：三路相机并行 materialize
```

等价模块入口：

```bash
python -m scribe \
  --root /path/to/local_dataset \
  --repo-id local \
  --output-dir ./.visualizer_runtime \
  --host 0.0.0.0 \
  --port 9006 \
  --3darm true
```

> `repo-id` 建议使用 `namespace/name`。若只给单段（如 `local`），后端会自动规范化为 `local/<root目录名>`，避免首页重定向报错。

## 3) 总体架构图

```text
scripts/run.sh
   └─ python -m scribe
      └─ __main__.py → app.main()
         ├─ argparse 解析参数
         ├─ 构造 dataset:
         │  ├─ 本地: LeRobotDataset(repo_id, root=...)
         │  ├─ Lance: LanceDataset(repo_id, root=..., runtime_dir=...)
         │  └─ Hub:  data.get_dataset_info(repo_id) → IterableNamespace
         └─ app.visualize_dataset_html(...)
            ├─ 准备 output_dir/static + robot/vendor 资源
            └─ routes.run_server(...)
               └─ Flask routes (详见§4)
```

## 4) 完整路由表（routes.py）

### 页面路由

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 主页/跳转 |
| `/<ns>/<name>` | GET | 跳首个 episode |
| `/<ns>/<name>/episode_<id>` | GET | 主可视化页（模板渲染） |
| `/<ns>/<name>/episode_<id>.json` | GET | episode payload（前端同页切换） |
| `/local_videos/<path>` | GET | 本地视频透传 |

### Episode Curation API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/<ns>/<name>/api/episode-curation` | GET | 读取全部/单条 curation |
| `/<ns>/<name>/api/episode-curation` | POST | 更新 episode 决策 (keep/delete_candidate) |
| `/<ns>/<name>/api/episode-curation/export-delete-list` | GET | 导出 delete 候选列表 |

### Segment Annotation API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/<ns>/<name>/api/segment-annotations` | GET | 读取全部/单条 segment 标注 + 摘要 |
| `/<ns>/<name>/api/segment-annotations` | POST | 新增/更新 segment |
| `/<ns>/<name>/api/segment-annotations/<segment_id>` | DELETE | 删除 segment |

### Frame Event API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/<ns>/<name>/api/frame-events` | GET | 读取全部/按 episode 过滤 |
| `/<ns>/<name>/api/frame-events` | POST | 新增/更新 frame event |
| `/<ns>/<name>/api/frame-events/<event_id>` | DELETE | 删除 frame event |

### Task Annotation Config API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/<ns>/<name>/api/task-annotation-config` | GET | 读取任务模板配置 |

## 5) 关键文件与职责

### 后端（`scribe/` 包）

- **`app.py`** — 应用入口：CLI 参数解析、资源准备（vendor/robot assets）、`main()` 和 `visualize_dataset_html()` 入口函数
- **`routes.py`** — Flask 路由：所有页面和 API 路由处理函数、标注上下文构建、`run_server()` 创建 Flask app
- **`data.py`** — 数据层：episode 数据加载、LRU 缓存、CSV 生成（Dygraph）、时间戳、视频路径
- **`lance_backend.py`** — Lance 数据适配：episode 发现、非 blob 列读取、H264 GOP copy remux、MP4 内部帧号映射、视频缓存
- **`annotation_store.py`** — 标注存储：Sidecar 标注数据的完整 CRUD + 校验逻辑、任务模板解析
- **`export.py`** — 离线导出：消费 `segment_annotations.json`，产出训练工件（clip_manifest、progress parquet、stage_priors）

### 前端

- **`scribe/templates/visualize.html`** — Alpine.js 状态机驱动的主界面

### 脚本（`scripts/`）

- **`scripts/run.sh`** — 启动脚本（runtime 目录、HF 缓存、ARM3D 开关）
- **`scripts/download_vendor.sh`** — 下载前端依赖到 `scribe/vendor/`
- **`scripts/install_pylance.sh`** — 从 `fecet/lance` 固定 commit 编译安装带 `Blob` / `blob_array` 支持的 pylance；源码 clone 到本地 `.lance-src/`

### 资源

- **`robot_assets/`** — SBS1 双臂 URDF/Xacro/STL meshes

## 5.1) Lance 可视化不变量

- `LanceDataset` 将 robot state/action/velocity/effort 保持在原始行频率；`data.get_episode_data()` 不做行级降采样。
- Lance 相机数据以 H264 Annex B GOP blob 存储。后端有两种 materialize 策略，由 `LANCE_VIDEO_POLICY` env 选择：
  - `copy`（默认）：`ffmpeg -c:v copy` 将唯一 GOP 拼接并 remux 为浏览器可播 MP4，保留源码率，最快。
  - `reencode`：`libx264 -preset fast -crf 23 -bf 0 -fps_mode passthrough`，编完后用 `ffprobe -count_frames` 强制校验输出帧数 ≥ 行映射所需的最大帧号 + 1；不通过则自动 fallback 到 `copy`，绝不允许产出"帧映射悄悄破损"的 MP4。当源视频用 `speed-preset=ultrafast` 录制（典型 HIL 数据）时，文件可缩 7-20 倍。
- 任何 lossy re-encode 必须保持 row N → MP4 frame N 的索引契约：用 `-fps_mode passthrough` + `-bf 0` + 帧数断言；不允许丢帧、补帧、重排。
- GOP blob 写入 ffmpeg 时必须走 stdin 流式写入，避免把完整 episode 的 H264 bytes 一次性 `join` 到 Python 内存中。
- `LANCE_VIDEO_WORKERS` 默认值为 `3`，对应 `left` / `mid` / `right` 三路相机并行 materialize；调大前需要确认磁盘 IO 和 CPU 足够。
- Runtime 视频缓存路径包含 *策略版本号*（`h264copy_v1` / `h264reencode_crf23_v1` / 等），切换 `LANCE_VIDEO_POLICY` 自动落到不同子目录，新旧策略 MP4 不会混存。
- `get_episode_video_seek_info()` 返回 `video_frame_indices`，由每行的 `*_gop_index` 与 `*_frame_index_in_gop` 计算得到，表示该 robot 行对应 materialized MP4 的内部帧号。
- 前端 `frameIndexToVideoTime()` / `videoTimeToFrameIndex()` 必须优先使用 `video_frame_indices`；旧的 `frame_ids` 仅作为兼容 fallback。
- 播放同步优先使用 `requestVideoFrameCallback`，按实际呈现视频帧驱动 Dygraph selection、表格和 3D 机械臂；无该 API 时才回退到 `timeupdate`。
- 不要用低清预览、跳帧、只加载部分机械臂列作为默认性能优化；如果要新增此类模式，必须显式命名为 preview/debug，并默认关闭。

## 5.2) v0.2.0 Lance 实现方案

### 数据发现与 duck typing

- `app.py` 通过 `is_lance_root(root)` 判断本地路径是否为 Lance 数据源。
- `discover_lance_episodes(root)` 同时支持：
  - `root` 本身是单个 `episode_xxxx.lance` 目录。
  - `root` 是包含多个 `episode_*.lance` 子目录的数据集目录。
- `LanceDataset` 提供 Scribe 现有代码所需的 LeRobotDataset 风格接口，包括 `repo_id`、`num_episodes`、`num_frames`、`fps`、`features`、`meta`、`hf_dataset` shim、`get_episode_video_seek_info()` 等。
- `_EpisodeLance` 是单 episode Lance wrapper，负责 schema metadata、非 blob 列读取、相机 fps/resolution 探测、GOP layout 和 seek 映射缓存。

### 机械臂数据处理

- 机械臂状态/action/velocity/effort 从 Lance 非 blob 列读取，并转换成 Scribe 前端已有的 feature 结构。
- `timestamp`、`frame_index`、`episode_index`、`index`、`task_index` 等字段按 Scribe/LeRobot 预期补齐或派生。
- CSV 仍由 `data.get_episode_data()` 统一生成，当前版本没有为 Lance 单独做分块加载。
- 当前版本不做行降采样，不跳过机械臂采样点。

### 相机视频处理

- Lance 相机列为 H264 Annex B GOP blob，常见列名为 `left`、`mid`、`right`。
- 每个相机有配套列：
  - `<cam>_gop_index`
  - `<cam>_frame_index_in_gop`
  - `<cam>_frame_id`
  - `<cam>_timestamp_ns`
- `_gop_layout(cam)` 一次扫描 `*_gop_index` 和 `*_frame_index_in_gop`：
  - 记录每个 GOP 第一次出现的 Lance row index。
  - 按 GOP index 排序，得到 materialize MP4 时的 GOP 顺序。
  - 计算每一行 robot 数据对应 materialized MP4 的内部 frame index。
- `write_h264_gops(cam, stream)` 使用 `take_blobs(cam, indices=first_indices)` 读取唯一 GOP，并逐个写入 ffmpeg stdin。
- `_ensure_video()` 优先执行 `ffmpeg -c:v copy` remux；失败时 fallback 到 `libx264 -preset ultrafast -g 1`。
- 输出 MP4 写到 `.visualizer_runtime/lance_runtime/videos/<dataset_namespace>/...`，并通过 `/local_videos/<path>` 提供给前端。

### 性能策略

- 默认不执行全数据集预生成：`LANCE_PREENCODE_ALL=false`。
- 打开当前 episode 时，三路相机 materialize 与 CSV/metadata 构建并行。
- 当前 episode 返回前会等待 materialize 完成，保证 `videos_info` 指向可访问文件。
- 下一集按 `LANCE_PRELOAD_NEXT=true` 后台预加载，提升连续浏览体验。
- `LANCE_VIDEO_WORKERS=3` 默认让三路相机并行生成。
- GOP layout、row slice、video frame indices 都有 episode 内缓存，并用 `RLock` 防止并发 materialize 时 cache 竞态。

### 已验证命令

```bash
pixi run check
```

真实 Lance smoke test 数据：

```text
/home/jovyan/code/lance_data_collections/20260420_qz4_bigshirt/episode_0005.lance
```

验证内容：

- `LanceDataset` 初始化正常。
- `_preload_videos(0)` 可生成三路 MP4。
- `get_episode_data(ds, 0)` 可生成 CSV。
- `get_episode_video_seek_info(0)` 返回 `observation.images.left` / `mid` / `right`。
- 输出 MP4 文件非空。

### v0.2.0 边界

- Lance 标注能力还没有完整验证。下一版需要重点检查：
  - `annotations/` sidecar 文件在 Lance root 下的存放位置是否符合预期。
  - Episode index 与 `episode_*.lance` 文件排序是否与标注 API 完全一致。
  - Segment 起止帧、frame event、episode curation 是否能正确回放和导出。
  - 导出逻辑是否需要为 Lance 数据结构新增分支。
- 当前 episode 首次打开仍要等待视频生成完成。
- 超长 episode 的 CSV payload 仍可能较大，后续可改为分块数据接口。

## 6) 标注系统数据架构

### Sidecar 原则

所有标注走 sidecar 文件，**不改写** LeRobot 原始 `data/` `videos/` `meta/`。

### 四个持久化文件（均在 `<dataset_root>/annotations/` 下）

| 文件 | 格式 | 说明 |
|------|------|------|
| `episode_curation.json` | JSON | episode 级 K/D 决策 |
| `task_annotation_config.json` | JSON | 任务模板定义（stage 顺序/颜色/显示名） |
| `segment_annotations.json` | JSON | 核心段标注：`items[episode_index].schemes.sparse = [segments]` |
| `frame_events.jsonl` | JSONL | 帧级事件（grasp_success / collision / failure_recovery / stage_note） |

### Segment 记录结构

```json
{
  "segment_id": "segment_xxxxxxxxxxxx",
  "task_name": "fold_long_horizon",
  "scheme": "sparse",
  "stage_label": "pick_up",
  "display_label": "Pick Up",
  "frame_start": 0,
  "frame_end": 120,
  "time_start_s": 0.0,
  "time_end_s": 4.0,
  "operator": "anonymous",
  "updated_at": "2026-03-31T00:00:00+00:00",
  "is_custom_label": false
}
```

### 内置任务模板（annotation_store.py）

- **`fold_long_horizon`**：`pick_up → spread → fold → place`
- **`generic_long_horizon`**：`acquire → arrange → operate → place`

### 校验规则（写入时检查）

- `frame_start <= frame_end`，范围在 `[0, frame_count-1]`
- 不允许 overlap（同 scheme 内 segment 重叠）
- 标准 stage 按 `stage_order` 顺序，不允许逆序或重复
- Custom label 可存储但排除在 export 之外
- `time_start_s / time_end_s` 由后端从真实 timestamp 补齐

## 7) 离线导出

```bash
python -m scribe.export \
  --dataset-root /path/to/dataset \
  --output-dir /path/to/export \
  --scheme sparse \
  --overwrite
```

## 8) 高频改动指南

| 要改什么 | 去哪里 |
|----------|--------|
| 新增/修改路由 | `routes.py` → `run_server()` |
| 标注 sidecar schema / 校验 | `annotation_store.py` |
| Lance 读取 / 视频 materialize / 帧对齐 | `lance_backend.py` |
| 视频显示顺序 | `data.py` → `VIDEO_DISPLAY_ORDER` |
| 数据加载 / 缓存 | `data.py` |
| CLI 参数 / 资源准备 | `app.py` |
| 页面布局样式 | `templates/visualize.html` |
| 图表联动/勾选 | `createAlpineData()` |
| 标注 UI / timeline | `createAlpineData()` 中 `saveAnnotationSegment()` / `setDraftBoundary()` |
| 离线导出逻辑 | `export.py` |
| 任务模板定义 | `annotation_store.py` → `DEFAULT_TASK_ANNOTATION_STORE` |

## 9) 当前已知风险

- 前端将 `videos[0]` 作为时间基准；若首个视频异常会影响整体联动
- Lance 当前 episode 首次打开仍会等待该 episode 的 MP4 materialize；默认只关闭全量预生成，不跳过当前 episode 生成
- H264 copy remux 失败时会 fallback 到 intra-frame encode，文件会更大、耗时更高
- `requestVideoFrameCallback` 不可用的浏览器会回退到 `timeupdate`，播放同步粒度较粗
- 标注 API 仅支持本地 `--root` 数据集，Hub 模式下标注功能 disabled
- 缓存默认 16 个 episode，大维度数据集需关注内存
- 标注文件写入用线程锁保护，仅限单进程安全

## 10) 恢复开发状态检查清单

1. `scripts/download_vendor.sh && scripts/run.sh` 是否能启动
2. 打开 `http://<host>:<port>`，确认可进入某个 episode
3. 视频播放时曲线和帧号同步变化
4. episode 同页切换不整页刷新
5. 3D 双臂加载正常
6. 标注功能：Set Start/End、Save Segment、badge 正确
7. Episode Curation：K/D 标记、Export delete list
8. 快捷键：Space、←/→、↑/↓
