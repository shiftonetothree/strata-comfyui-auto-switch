"""Allow ``python3 -m relay``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
