# Scribe

**LeRobot 数据集可视化与标注工具**

基于 Flask 的 Web 应用，用于可视化和标注 [LeRobot v2.1](https://github.com/huggingface/lerobot) 格式的机器人操作数据集。支持本地数据集、HuggingFace Hub 远程数据集，以及**直接从 BOS / S3 对象存储在线浏览 Lance 数据集**。

当前版本：v0.3.0

## 2026-08-07：Lance 多用户浏览与三视角快速 Seek 优化

本节记录 `chance/scribe-multiuser-lance-20260807` 分支相对 `main` 的全部改动，以及在
`192.168.4.90:9006` 验证通过的部署参数。目标是在不改变标注数据格式的前提下，解决 BOS
Lance 数据集首次打开阻塞、切换 episode 卡顿、大视频随机 seek 缓慢和多用户请求无边界增长等问题。

### 本分支包含的代码更新

| 文件 | 更新内容 |
|------|----------|
| `scribe/routes.py` | Lance 视频改为后台异步 materialize；新增进度/重试 API；同一 dataset/episode 去重；限制并发任务和状态数量；无人继续查看时取消任务；支持直接打开尚未从 landing 页注册的 BOS URL；left/mid/right 使用 Lance 有界线程池并行生成。 |
| `scribe/lance_backend.py` | GOP 流式读取、copy、reencode 和 fallback 编码链路增加进度回调；三路预加载可汇总到 episode 级进度；异常或取消时由原有 `.part` 清理逻辑回收临时文件。 |
| `scribe/data.py` | episode CSV/timestamp LRU 默认容量由固定 16 改为环境变量控制，默认 128、最小 16。 |
| `scribe/templates/visualize.html` | 页面在视频后台准备期间显示总进度和逐相机进度；支持失败重试；同页切换 episode 时复用页面而非完整刷新；停留后预取下一 episode；滑块拖动期间只更新预览，释放时才 seek；正常左右键/快进仍保持三视角同时 seek。 |

### 推荐的 9006 生产配置

```bash
export LANCE_VIDEO_POLICY=reencode
export LANCE_VIDEO_WORKERS=3
export LANCE_PRELOAD_NEXT=true
export LANCE_PREENCODE_ALL=false

export SCRIBE_VIDEO_JOB_WORKERS=2
export SCRIBE_VIDEO_JOB_STATE_LIMIT=512
export SCRIBE_VIDEO_JOB_INACTIVE_S=30
export SCRIBE_EPISODE_DATA_CACHE_MAXSIZE=128
export SCRIBE_DATASET_LRU=8

python -m scribe \
  --bos-prefix bos://srgdata/robot/lance_qz_training_data/ \
  --output-dir ./.visualizer_runtime \
  --host 0.0.0.0 \
  --port 9006 \
  --autosave-interval-s 60 \
  --3darm false
```

AWS/BOS 凭据仍必须通过环境变量传入，禁止写进脚本或提交到 Git：

```bash
export AWS_ENDPOINT_URL=https://s3.bj.bcebos.com
export AWS_ACCESS_KEY_ID=你的AK
export AWS_SECRET_ACCESS_KEY=你的SK
export AWS_DEFAULT_REGION=bj
```

### 新增运行参数

| 环境变量 | 代码默认值 | 90 推荐值 | 说明 |
|----------|------------|-----------|------|
| `LANCE_VIDEO_POLICY` | `copy` | `reencode` | `copy` 启动生成快但文件大；`reencode` 生成较慢，但 MP4 更小且关键帧更密，适合多用户随机 seek。两种缓存使用独立目录，可并存和回滚。 |
| `LANCE_VIDEO_WORKERS` | `3` | `3` | 单个 episode 内同时生成的相机数；三相机数据建议设为 3。 |
| `LANCE_PRELOAD_NEXT` | `true` | `true` | 当前 episode 准备好并稳定停留后，预取下一个 episode。 |
| `SCRIBE_VIDEO_JOB_WORKERS` | `2` | `2` | 同时运行的 episode 视频准备任务数；每个任务内部仍受 `LANCE_VIDEO_WORKERS` 限制。 |
| `SCRIBE_VIDEO_JOB_STATE_LIMIT` | `512` | `512` | 内存中保留的视频任务状态上限；超限时清理已完成/失败的旧状态，不删除 MP4。 |
| `SCRIBE_VIDEO_JOB_INACTIVE_S` | `30` | `30` | 浏览器不再轮询后，后台任务允许继续存活的秒数，最小 15 秒。 |
| `SCRIBE_EPISODE_DATA_CACHE_MAXSIZE` | `128` | `128` | episode CSV 和 timestamp LRU 容量，最小 16；非法值回退到 128。 |

### 页面加载与预取行为

1. 打开 Lance episode 后，metadata/CSV 立即构建，视频在后台生成，页面不再等待三路 MP4 全部完成才返回。
2. 前端每 2 秒查询当前 episode 视频进度，并显示 left/mid/right 的 GOP 进度。
3. 三路视频由 `LANCE_VIDEO_WORKERS=3` 并行 materialize，全部就绪后保持三视角同步播放和同步 seek。
4. 当前 episode 就绪并停留 8 秒后，浏览器只预取一个下一 episode；相同任务在服务端去重。
5. 预取状态每 4 秒续询；标签页隐藏、切换 episode 或离开页面时停止该浏览器的预取。
6. 同页切换到已预取 episode 时复用内存中的 payload，避免再次下载数 MB 的 episode JSON。
7. 进度任务长时间无人查看会自动取消；ffmpeg 子进程终止后删除未完成的 `.part` 文件。

### 为什么 90 推荐 reencode

在 `20260727_qz4_bigshirt_fold_30HZ.lance` 上的实测结果：

| 项目 | `copy` | `reencode` |
|------|--------|------------|
| 单路约 121 秒 MP4 | 约 169 MB | 约 9.5–11 MB |
| 三路合计 | 约 507 MB | 约 31 MB |
| 关键帧间隔 | 约 2.13 秒 | 1 秒 |
| 缓存目录后缀 | `h264copy_v1` | `h264reencode_crf23_v1` |

`reencode` 的第一次生成会消耗 CPU，并需要等待数十秒；生成完成后重复访问直接复用小文件缓存。
不要删除旧 `copy` 缓存来切换策略，两种策略的目录隔离，修改环境变量并重启即可回滚。

三视角连续 seek 验证（episode 75，暂停后连续 30 次右方向键）：

```text
left : seeking=30, seeked=30
mid  : seeking=30, seeked=30
right: seeking=30, seeked=30
三路最终时间差 < 0.001 秒，readyState 均为 4
```

### 多用户边界

- 多个标注员连接**同一个 Scribe 进程**时，视频准备任务会按 dataset/episode 去重，并受全局并发和状态上限保护。
- 前端每个标签页最多预取一个下一 episode；不会递归预取整个数据集。
- 当前 BOS sidecar 同步仍没有跨服务实例的 ETag/版本冲突检测。不要让 90 和 164 同时修改同一个数据集的标注；多人标注应统一使用同一个 9006 服务。
- 本分支不修改标注 JSON/JSONL schema，也不修改 curation、segment annotation、frame event 的写入接口。

### 回归检查

提交或部署前至少执行：

```bash
python -m py_compile scribe/routes.py scribe/lance_backend.py scribe/data.py
pixi run check
```

浏览器侧检查：

1. 新开未缓存 episode，确认三相机进度同时从 `reading` 到 `ready`。
2. 暂停后长按左右键，确认三画面连续变化且最终同步。
3. 停留 8 秒后检查只出现一个下一 episode JSON 请求。
4. 保存一条测试标注并确认 autosave/sync 状态正常。

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
