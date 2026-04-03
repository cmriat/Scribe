# CLAUDE.md — Scribe

本文件用于帮助 AI 助手快速理解 Scribe 的结构、运行方式与当前开发状态。

最后更新：2026-04-03

---

## 0) 开发环境

- 进入开发环境：`pixi shell`
- 环境依赖更新 → 修改 `pixi.toml`
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
- **`annotation_store.py`** — 标注存储：Sidecar 标注数据的完整 CRUD + 校验逻辑、任务模板解析
- **`export.py`** — 离线导出：消费 `segment_annotations.json`，产出训练工件（clip_manifest、progress parquet、stage_priors）

### 前端

- **`scribe/templates/visualize.html`** — Alpine.js 状态机驱动的主界面

### 脚本（`scripts/`）

- **`scripts/run.sh`** — 启动脚本（runtime 目录、HF 缓存、ARM3D 开关）
- **`scripts/download_vendor.sh`** — 下载前端依赖到 `scribe/vendor/`

### 资源

- **`robot_assets/`** — SBS1 双臂 URDF/Xacro/STL meshes

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
