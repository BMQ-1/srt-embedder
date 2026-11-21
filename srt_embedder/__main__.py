"""Entry point for both `python -m srt_embedder` and PyInstaller frozen binaries."""

import sys
from srt_embedder.cli import main

if __name__ == "__main__":
    sys.exit(main())
