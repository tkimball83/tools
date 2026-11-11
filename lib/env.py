import os

from lib.output import status


def positive_int(name: str, default: int) -> int:
    """Read a positive integer from the environment, warning on bad values."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        status(f"WARNING: ignoring invalid {name}={value!r}, using {default}")
        return default
    return number
