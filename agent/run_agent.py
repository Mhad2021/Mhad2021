#!/usr/bin/env python3
"""Launcher for the Presence tracker agent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker.main import main

if __name__ == "__main__":
    sys.exit(main())
