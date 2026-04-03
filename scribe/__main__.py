#!/usr/bin/env python
"""
Entry point for running Scribe as a module.

Usage:
    python -m scribe --root /path/to/dataset --repo-id local/task --host 0.0.0.0 --port 9008
"""

from .app import main

if __name__ == "__main__":
    main()
