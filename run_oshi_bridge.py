#!/usr/bin/env python3
"""Run the OSHI <-> MeshCore bridge: python run_oshi_bridge.py -c oshi_bridge.ini"""

import sys

from oshi_bridge.runner import main

if __name__ == "__main__":
    sys.exit(main())
