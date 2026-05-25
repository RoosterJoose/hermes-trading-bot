#!/usr/bin/env python
"""Launch the trading bot with proper asyncio event loop."""
import sys
import os

# Ensure we're in the right directory
os.chdir("/opt/data/hermes-trading")
sys.path.insert(0, "/opt/data/hermes-trading")

from hermes_trading import main
main()
