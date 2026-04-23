"""
Application entry point — CLI argument parsing, asset preparation, and server startup.
"""

import os
import re
import time
import shutil
import logging
import argparse
import tempfile
import threading
from pathlib import Path

from lerobot.datasets.lerobot_dataset import LeRobotDataset

from scribe.data import get_dataset_info
from scribe.routes import run_server
from scribe.lance_backend import LanceDataset, is_lance_root

# ---------------------------------------------------------------------------
# Robot asset constants
# ---------------------------------------------------------------------------

SBS1_DESCRIPTION_PACKAGE = "sbs1_description"
SBS1_DESCRIPTION_ROOT = Path(__file__).resolve().parent.parent / "robot_assets" / SBS1_DESCRIPTION_PACKAGE
SBS1_WEB_URDF_FILENAME = "sbs1_dual_arm_no_camera_web.urdf"
SBS1_PREEXPANDED_URDF_PATH = Path(__file__).resolve().parent.parent / "robot_assets" / "sbs1_dual_arm_no_camera.urdf"

ROBOT_DEFAULT_DRIVER = "observation.state"

# ---------------------------------------------------------------------------
# Vendor asset config
# ---------------------------------------------------------------------------

VENDOR_ASSET_PATHS = {
    "alpine_js": "vendor/alpinejs/cdn.min.js",
    "dygraph_js": "vendor/dygraphs/dygraph.min.js",
    "tailwind_js": "vendor/tailwind/tailwindcss.js",
    "three_module": "vendor/three/three.module.js",
    "orbit_controls": "vendor/three/examples/jsm/controls/OrbitControls.js",
    "stl_loader": "vendor/three/examples/jsm/loaders/STLLoader.js",
}

LOCAL_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
ASSET_CACHE_BUSTER = int(time.time())


# ---------------------------------------------------------------------------
# Robot joint config
# ---------------------------------------------------------------------------

ROBOT_JOINT_LIMITS = {
    "joint1": (-3.1416, 2.0944),
    "joint2": (-2.9671, 0.17453),
    "joint3": (-0.087266, 3.1416),
    "joint4": (-3.0107, 3.0107),
    "joint5": (-1.7628, 1.7628),
    "joint6": (-3.0107, 3.0107),
    "g2_joint": (0.0, 0.072),
}


def _build_robot_driver_joint_map() -> dict[str, dict[str, str]]:
    drivers = {}
    for driver_key in ["observation.state", "action"]:
        joint_map = {}
        for arm in ["left", "right"]:
            for joint_index in range(1, 7):
                source_name = f"{arm}_joint{joint_index}"
                joint_map[source_name] = source_name
            joint_map[f"{arm}_gripper"] = f"{arm}_g2_joint"
        drivers[driver_key] = joint_map
    return drivers


ROBOT_DRIVER_JOINT_MAP = _build_robot_driver_joint_map()


def _build_robot_joint_limits() -> dict[str, list[float]]:
    limits = {}
    for arm in ["left", "right"]:
        for joint_name, (lower, upper) in ROBOT_JOINT_LIMITS.items():
            limits[f"{arm}_{joint_name}"] = [lower, upper]
    return limits


def _build_robot_driver_config() -> dict[str, dict]:
    driver_config = {}
    for driver_key, joint_map in ROBOT_DRIVER_JOINT_MAP.items():
        label_prefix = "action." if driver_key == "action" else ""
        fallback_prefix = "observation.state." if driver_key == "observation.state" else ""

        label_candidates = {}
        for source_name in joint_map:
            candidates = []
            if label_prefix:
                candidates.append(f"{label_prefix}{source_name}")
            candidates.append(source_name)
            if fallback_prefix:
                candidates.append(f"{fallback_prefix}{source_name}")
            label_candidates[source_name] = list(dict.fromkeys(candidates))

        driver_config[driver_key] = {
            "joint_map": joint_map,
            "label_candidates": label_candidates,
        }

    return driver_config


# ---------------------------------------------------------------------------
# Asset preparation
# ---------------------------------------------------------------------------


def _copytree_overwrite(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def prepare_frontend_vendor_assets(static_dir: Path) -> dict:
    vendor_target_dir = static_dir / "vendor"
    if LOCAL_VENDOR_DIR.exists():
        _copytree_overwrite(LOCAL_VENDOR_DIR, vendor_target_dir)

    missing = []
    status = {}
    for key, rel_path in VENDOR_ASSET_PATHS.items():
        asset_path = static_dir / rel_path
        status[key] = f"{rel_path}?v={ASSET_CACHE_BUSTER}"
        if not asset_path.exists():
            missing.append(rel_path)

    status["ready"] = len(missing) == 0
    status["missing"] = missing

    if missing:
        logging.warning(
            "frontend vendor assets missing: %s. Please run scripts/download_vendor.sh once.",
            ", ".join(missing),
        )

    return status


def _render_sbs1_dual_arm_no_camera_urdf(description_root: Path) -> str | None:
    arm_macro_path = description_root / "urdf" / "arms" / "play_g2.xacro"
    if not arm_macro_path.exists():
        return None

    arm_macro_text = arm_macro_path.read_text(encoding="utf-8")
    macro_match = re.search(
        r"<xacro:macro[^>]*name=\"play_g2_arm\"[^>]*>(?P<body>.*?)</xacro:macro>",
        arm_macro_text,
        re.DOTALL,
    )
    if macro_match is None:
        return None

    macro_body = macro_match.group("body").strip()

    def instantiate_arm(prefix: str, y_offset: float) -> str:
        arm_xml = macro_body.replace("${prefix}", prefix)
        arm_xml = arm_xml.replace("${parent}", "base_link")
        arm_xml = arm_xml.replace(
            '<xacro:insert_block name="origin"/>',
            f'<origin xyz="0 {y_offset} 0" rpy="0 0 0"/>',
        )
        return arm_xml

    left_arm_xml = instantiate_arm("left_", 0.3)
    right_arm_xml = instantiate_arm("right_", -0.3)

    return f"""<?xml version="1.0"?>
<robot name="sbs1_dual_arm_no_camera">
  <link name="world"/>

  <link name="base_link">
    <inertial>
      <origin xyz="0 0 0" rpy="0 0 0"/>
      <mass value="5.0"/>
      <inertia ixx="0.1" ixy="0.0" ixz="0.0" iyy="0.15" iyz="0.0" izz="0.2"/>
    </inertial>
    <visual>
      <origin xyz="0 0 -0.015" rpy="0 0 0"/>
      <geometry>
        <box size="0.18 0.8 0.03"/>
      </geometry>
      <material name="base_material">
        <color rgba="1.0 1.0 1.0 1.0"/>
      </material>
    </visual>
    <collision>
      <origin xyz="0 0 -0.015" rpy="0 0 0"/>
      <geometry>
        <box size="0.18 0.8 0.03"/>
      </geometry>
    </collision>
  </link>

  <joint name="world_to_base" type="fixed">
    <parent link="world"/>
    <child link="base_link"/>
    <origin xyz="0 0 0.03" rpy="0 0 0"/>
  </joint>

{left_arm_xml}

{right_arm_xml}

</robot>
"""


def prepare_sbs1_robot_assets(static_dir: Path) -> dict:
    if not SBS1_DESCRIPTION_ROOT.exists():
        logging.warning("SBS1 description root not found: %s", SBS1_DESCRIPTION_ROOT)
        return {"enabled": False}

    robot_static_dir = static_dir / "robot_description"
    robot_static_dir.mkdir(parents=True, exist_ok=True)

    package_dir = robot_static_dir / "packages" / SBS1_DESCRIPTION_PACKAGE
    _copytree_overwrite(SBS1_DESCRIPTION_ROOT, package_dir)

    if SBS1_PREEXPANDED_URDF_PATH.exists():
        urdf_content = SBS1_PREEXPANDED_URDF_PATH.read_text(encoding="utf-8")
        logging.info("Using pre-expanded SBS1 URDF: %s", SBS1_PREEXPANDED_URDF_PATH)
    else:
        urdf_content = _render_sbs1_dual_arm_no_camera_urdf(SBS1_DESCRIPTION_ROOT)

    if urdf_content is None:
        logging.warning("Failed to load/render sbs1_dual_arm_no_camera URDF")
        return {"enabled": False}

    urdf_output_path = robot_static_dir / SBS1_WEB_URDF_FILENAME
    urdf_output_path.write_text(urdf_content, encoding="utf-8")

    return {
        "enabled": True,
        "urdf_static_path": f"robot_description/{SBS1_WEB_URDF_FILENAME}",
        "package_static_paths": {SBS1_DESCRIPTION_PACKAGE: f"robot_description/packages/{SBS1_DESCRIPTION_PACKAGE}"},
        "drivers": _build_robot_driver_config(),
        "joint_limits": _build_robot_joint_limits(),
        "default_driver": ROBOT_DEFAULT_DRIVER,
    }


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def init_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _parse_bool_flag(value, flag_name: str) -> bool:
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False

    raise ValueError(f"Invalid value for {flag_name}: '{value}'. Use true/false.")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return _parse_bool_flag(value, name)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def visualize_dataset_html(
    dataset: LeRobotDataset | LanceDataset | None,
    episodes: list[int] | None = None,
    output_dir: Path | None = None,
    serve: bool = True,
    host: str = "127.0.0.1",
    port: int = 9090,
    force_override: bool = False,
    enable_3darm: bool = True,
) -> Path | None:
    init_logging()

    template_dir = Path(__file__).resolve().parent / "templates"

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="lerobot_visualize_dataset_")

    output_dir = Path(output_dir)
    if output_dir.exists():
        if force_override:
            shutil.rmtree(output_dir)
        else:
            logging.info(f"Output directory already exists. Loading from it: '{output_dir}'")

    output_dir.mkdir(parents=True, exist_ok=True)

    static_dir = output_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    if enable_3darm:
        robot_kinematic_config = prepare_sbs1_robot_assets(static_dir)
    else:
        logging.info("3D arm visualization disabled by --3darm flag")
        robot_kinematic_config = {"enabled": False}
    vendor_assets = prepare_frontend_vendor_assets(static_dir)

    if dataset is None:
        if serve:
            run_server(
                dataset=None,
                episodes=None,
                host=host,
                port=port,
                static_folder=static_dir,
                template_folder=template_dir,
                robot_kinematic_config=robot_kinematic_config,
                vendor_assets=vendor_assets,
            )
    else:
        if serve:
            run_server(
                dataset,
                episodes,
                host,
                port,
                static_dir,
                template_dir,
                robot_kinematic_config,
                vendor_assets,
            )


def main():
    parser = argparse.ArgumentParser(description="Scribe — LeRobot Dataset Visualizer & Annotator")

    parser.add_argument(
        "--repo-id",
        type=str,
        default=None,
        help="Name of hugging face repositery containing a LeRobotDataset dataset (e.g. `lerobot/pusht` for https://huggingface.co/datasets/lerobot/pusht).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Root directory for a dataset stored locally (e.g. `--root data`). By default, the dataset will be loaded from hugging face cache folder, or downloaded from the hub if available.",
    )
    parser.add_argument(
        "--load-from-hf-hub",
        type=int,
        default=0,
        help="Load videos and parquet files from HF Hub rather than local system.",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        nargs="*",
        default=None,
        help="Episode indices to visualize (e.g. `0 1 5 6` to load episodes of index 0, 1, 5 and 6). By default loads all episodes.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory path to write html files and kickoff a web server. By default write them to a temp directory.",
    )
    parser.add_argument(
        "--serve",
        type=int,
        default=1,
        help="Launch web server.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Web host used by the http server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9090,
        help="Web port used by the http server.",
    )
    parser.add_argument(
        "--force-override",
        type=int,
        default=0,
        help="Delete the output directory if it exists already.",
    )

    parser.add_argument(
        "--tolerance-s",
        type=float,
        default=1e-4,
        help=(
            "Tolerance in seconds used to ensure data timestamps respect the dataset fps value"
            "This is argument passed to the constructor of LeRobotDataset and maps to its tolerance_s constructor argument"
            "If not given, defaults to 1e-4."
        ),
    )
    parser.add_argument(
        "--3darm",
        dest="arm3d",
        type=str,
        default="true",
        help="Enable 3D SBS1 arm visualization (true/false).",
    )

    args = parser.parse_args()
    kwargs = vars(args)
    repo_id = kwargs.pop("repo_id")
    load_from_hf_hub = kwargs.pop("load_from_hf_hub")
    root = kwargs.pop("root")
    tolerance_s = kwargs.pop("tolerance_s")
    enable_3darm = _parse_bool_flag(kwargs.pop("arm3d"), "--3darm")
    kwargs["enable_3darm"] = enable_3darm

    dataset = None
    if repo_id:
        if "/" not in repo_id:
            root_name = root.name if root is not None else repo_id
            # Strip .lance suffix so repo-id stays clean for URLs.
            if root_name.endswith(".lance"):
                root_name = root_name[: -len(".lance")]
            normalized_repo_id = f"local/{root_name}"
            logging.info(
                "repo-id '%s' does not include namespace/name, normalized to '%s'",
                repo_id,
                normalized_repo_id,
            )
            repo_id = normalized_repo_id

        if not load_from_hf_hub and is_lance_root(root):
            # Ensure output_dir is resolved before building LanceDataset so the
            # runtime MP4s persist across restarts in a stable location.
            output_dir_arg = kwargs.get("output_dir")
            if output_dir_arg is None:
                output_dir_arg = tempfile.mkdtemp(prefix="scribe_lance_")
                kwargs["output_dir"] = Path(output_dir_arg)
            runtime_dir = Path(output_dir_arg) / "lance_runtime"
            logging.info("detected lance root at %s; using runtime dir %s", root, runtime_dir)
            dataset = LanceDataset(repo_id=repo_id, root=root, runtime_dir=runtime_dir)
            if _env_bool("LANCE_PREENCODE_ALL", False):
                def _bg_encode():
                    for ep_i in range(dataset.num_episodes):
                        try:
                            dataset._preload_videos(ep_i)
                        except Exception:
                            logging.warning("background video materialization failed ep=%d", ep_i, exc_info=True)
                    logging.info("background video materialization complete (%d episodes)", dataset.num_episodes)
                threading.Thread(target=_bg_encode, daemon=True).start()
            else:
                logging.info("Lance full-dataset video pre-materialization disabled (LANCE_PREENCODE_ALL=false)")
        else:
            dataset = (
                LeRobotDataset(repo_id, root=root, tolerance_s=tolerance_s)
                if not load_from_hf_hub
                else get_dataset_info(repo_id)
            )

    visualize_dataset_html(dataset, **kwargs)


if __name__ == "__main__":
    main()
