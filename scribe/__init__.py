"""
Scribe — LeRobot Dataset Visualizer & Annotator

A standalone visualization and annotation tool for LeRobot v2.1 datasets.
Provides a Flask-based web interface with synchronized video playback,
interactive graphs, 3D arm visualization, and annotation capabilities.
"""

from .app import main, visualize_dataset_html

__version__ = "0.1.0"
__all__ = ["main", "visualize_dataset_html"]
