#!/usr/bin/env python3
"""
run.py — Hermes Trading Worker entrypoint.
Starts the async loop for all configured assets.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hermes_trading.__init__ import main

if __name__ == "__main__":
    main()
