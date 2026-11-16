"""Dependency-free runtime guard, also used before shell install/update steps."""

import sys


MIN_PYTHON = (3, 12)


def require_supported_python() -> None:
    if sys.version_info < MIN_PYTHON:
        found = ".".join(map(str, sys.version_info[:3]))
        raise SystemExit(
            f"MoM требует Python 3.12 или новее; найден {found}. "
            "Установите Python 3.12, переименуйте старую .venv и выполните ./install.sh. "
            "Каталоги data и backups, а также .env сохраните."
        )


if __name__ == "__main__":
    require_supported_python()
