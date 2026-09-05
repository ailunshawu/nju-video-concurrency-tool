"""Dependency probe invoked directly by the ASCII Windows launcher."""

import sys


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        raise SystemExit(1)
    import playwright  # noqa: F401

    print("VIDEO_QA_READY")
