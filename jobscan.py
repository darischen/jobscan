#!/usr/bin/env python3
"""Convenience entry point so `python jobscan.py --company nvidia` works."""
import sys
from jobscan.cli import main
sys.exit(main())
