#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from nearme.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
