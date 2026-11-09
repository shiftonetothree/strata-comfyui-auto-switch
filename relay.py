#!/usr/bin/env python3
"""Convenience launcher: ``python3 relay.py`` (see ``python3 relay.py --help``)."""

from relay.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
