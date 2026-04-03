#!/usr/bin/env python3
"""
SBS1 Robot Visualization Launch File

SBS1 dual-arm visualization launch configuration.

This launch file starts RViz for visualizing the SBS1 dual-arm mobile platform.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import Command, FindExecutable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    # Declare launch arguments
    description_package_arg = DeclareLaunchArgument(
        "description_package",
        default_value="sbs1_description",
        description="Name of the robot description package"
    )

    description_file_arg = DeclareLaunchArgument(
        "description_file",
        default_value="sbs1.xacro",
        description="URDF/xacro file name"
    )

    use_sim_arg = DeclareLaunchArgument(
        "use_sim",
        default_value="false",
        description="Enable simulation mode"
    )

    # Get launch configurations
    pkg = LaunchConfiguration("description_package")
    file = LaunchConfiguration("description_file")
    use_sim = LaunchConfiguration("use_sim")

    # Generate robot description by compiling xacro
    robot_description_content = Command([
        FindExecutable(name="xacro"),
        " ",
        PathJoinSubstitution([
            FindPackageShare(pkg),
            "urdf",
            file
        ]),
        " ",
        "use_sim:=",
        use_sim
    ])

    robot_description = {
        "robot_description": ParameterValue(robot_description_content, value_type=str)
    }

    # Robot State Publisher Node
    robot_state_publisher_node = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[robot_description]
    )

    # Joint State Publisher GUI Node
    joint_state_publisher_gui_node = Node(
        package="joint_state_publisher_gui",
        executable="joint_state_publisher_gui",
        output="screen"
    )

    # RViz Node
    rviz_config_file = PathJoinSubstitution([
        FindPackageShare(pkg),
        "rviz",
        "view_robot.rviz"
    ])

    rviz_node = Node(
        package="rviz2",
        executable="rviz2",
        name="rviz2",
        output="screen",
        arguments=["-d", rviz_config_file]
    )

    return LaunchDescription([
        description_package_arg,
        description_file_arg,
        use_sim_arg,
        robot_state_publisher_node,
        joint_state_publisher_gui_node,
        rviz_node
    ])
