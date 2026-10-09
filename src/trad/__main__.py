"""Allow ``python -m trad`` to validate a configuration."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
