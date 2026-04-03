# SBS1 双臂平台描述包

基于 ROS2 的 ALOHA 风格双臂操作平台 URDF 描述包。

## 概述

本包提供双臂平台的 URDF/xacro 描述，包含两个 Airbot Play G2 机械臂，安装在简单的长方体底座上。设计采用 ALOHA 风格的平行双臂布局。

提供两个版本：
- **sbs1** (默认): 不带相机的双臂平台
- **sbs1_camera**: 带 D435 相机的双臂平台

```
平台布局 (俯视图):

        Y+
        ^
        |   [左臂]
        |     |
   -----+-----o------> X+
        |     |
        |   [右臂]

   原点: base_link 中心 (底板上表面)
```

```
TF 树结构:

world (固定世界坐标系)
  │
  ↓ [fixed joint]
base_link (平台底板上表面，双臂的父坐标系)
  │
  ├──→ left_base_link
  │        └──→ left_link1 → ... → left_end_link
  │
  └──→ right_base_link
           └──→ right_link1 → ... → right_end_link
```

## 包结构

```
sbs1_description/
├── CMakeLists.txt
├── package.xml
├── README.md
├── launch/
│   └── view_xacro.launch.py      # RViz 可视化
├── urdf/
│   ├── sbs1_no_camera.xacro      # 默认：不带相机的双臂平台
│   ├── sbs1.xacro                # 带相机的双臂平台
│   └── arms/
│       ├── play_g2.xacro         # 机械臂宏 (不带相机)
│       └── play_g2_d435.xacro    # 机械臂宏 (带 D435 相机)
├── meshes/
│   └── arm/                      # 机械臂 STL 网格 (15个文件)
│       ├── base_link.STL
│       ├── link1.STL ~ link6.STL
│       ├── g2_base_link.STL, g2_left_link.STL, g2_right_link.STL
│       ├── eef_connect_base_link.STL
│       ├── cam_connect_base_link.STL
│       └── D435_base_link.STL
└── rviz/
    └── view_robot.rviz
```

## 硬件规格

### Airbot Play G2 机械臂 (x2)

| 组件 | 规格 |
|------|------|
| 自由度 | 6 个旋转关节 |
| 夹爪 | G2 平行夹爪 (1主动 + 2从动) |
| 相机 | Intel RealSense D435 (可选) |
| 负载 | ~1.5 kg |

### 平台底座

| 参数 | 值 | 说明 |
|------|-----|------|
| `base_length` | 0.18 m | X 方向长度 |
| `base_width` | 0.8 m | Y 方向宽度 |
| `base_height` | 0.03 m | Z 方向厚度 |
| `arm_y_offset` | 0.30 m | 从中心到各臂 base_link 的距离 |
| 两臂间距 | 0.6 m | 两个 base_link 之间 |

## 依赖

- ROS2 Jazzy
- `robot_state_publisher`
- `joint_state_publisher_gui`
- `rviz2`
- `xacro`

所有依赖通过 pixi 管理。

## 快速开始

### 构建

```bash
pixi run build
```

### 在 RViz 中可视化

```bash
# 不带相机版本 (默认)
pixi run sbs1

# 带相机版本
pixi run sbs1_camera
```

启动后会打开:
- `robot_state_publisher` - 发布机器人 TF 变换
- `joint_state_publisher_gui` - 关节控制滑块 GUI
- `rviz2` - 3D 可视化

### 生成 URDF

```bash
# 不带相机版本
pixi run -e ros2 bash -c "source install/setup.bash && xacro \$(ros2 pkg prefix sbs1_description)/share/sbs1_description/urdf/sbs1_no_camera.xacro"

# 带相机版本
pixi run -e ros2 bash -c "source install/setup.bash && xacro \$(ros2 pkg prefix sbs1_description)/share/sbs1_description/urdf/sbs1.xacro"
```

## 坐标系

### 主要坐标系

| 坐标系 | 说明 |
|--------|------|
| `world` | 固定世界参考系 |
| `base_link` | 平台底板上表面 (原点) |
| `left_base_link` | 左臂基座 |
| `right_base_link` | 右臂基座 |

### 机械臂坐标系 (前缀: `left_` 或 `right_`)

| 坐标系 | 说明 |
|--------|------|
| `{prefix}base_link` | 臂基座 |
| `{prefix}link1` ~ `link6` | 臂连杆 |
| `{prefix}end_link` | 末端执行器 |
| `{prefix}g2_base_link` | 夹爪基座 |
| `{prefix}g2_left_link` | 左手指 |
| `{prefix}g2_right_link` | 右手指 |
| `{prefix}D435_base_link` | 相机 (仅带相机版本) |

## 关节名称

### 臂关节 (左/右)

| 关节 | 类型 | 限位 |
|------|------|------|
| `{prefix}joint1` ~ `{prefix}joint6` | 旋转 | 见 xacro |
| `{prefix}g2_joint` | 移动 | 0 ~ 0.072 m |

## 自定义配置

编辑 `urdf/sbs1_no_camera.xacro` 或 `urdf/sbs1.xacro`:

### 调整臂间距

```xml
<!-- 臂间距: 从中心到各臂 base_link 的距离 -->
<xacro:property name="arm_y_offset" value="0.30"/>
```

### 调整底座尺寸

```xml
<xacro:property name="base_length" value="0.18"/>
<xacro:property name="base_width" value="0.8"/>
<xacro:property name="base_height" value="0.03"/>
```

### 调整平台高度

```xml
<!-- 在 world_to_base 关节中 -->
<origin xyz="0 0 0.03" rpy="0 0 0"/>
```

## 故障排除

**RViz 中看不到模型或模型很小:**
- 按 `F` 键聚焦到机器人
- 检查 Fixed Frame 是否设为 `world`
- 用滚轮放大视角

**xacro 编译错误:**
```bash
xacro --check-order urdf/sbs1.xacro
```

**TF 变换未发布:**
```bash
ros2 run tf2_tools view_frames
```

## 文件来源

- 机械臂 URDF 基于: `ros2_play_g2_d435/play_g2_d435`
- 原始机械臂网格来自 Airbot 官方包
