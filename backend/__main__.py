from __future__ import annotations

import uvicorn

from .config import HOST, PORT


def main() -> None:
    uvicorn.run("backend.main:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
