# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

## [0.1.0] - 2026-04-03
### Screenshots
![alt text](assets/CHANGELOG/image.png)
### Added

- 多相机视频同步播放 + Dygraph 时序曲线
- SBS1 双臂 3D 联动（URDF 驱动）
- Episode Curation（Keep / Delete 标记）
- Sparse Stage Segment 标注
- Frame Event 帧级标注
- 离线导出训练工件（progress label、clip manifest）
- Dark / Light 主题切换
- Episode 同页切换（无刷新）

### Changed

- 包名重命名为 `scribe`，模块拆分为 `app` / `routes` / `data` / `annotation_store` / `export`
- 使用 pixi 管理依赖，ruff 进行代码检查
- 标注数据采用 sidecar 存储，不改写原始数据集
