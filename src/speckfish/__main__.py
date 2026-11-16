"""Allow `python -m speckfish`.

Windows environments with Device Guard / App Control block the generated console-script
`speckfish.exe` (unsigned stub) while `python.exe` runs fine, so the module entry point is
the supported fallback:  python -m speckfish doctor
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
