"""Run the API. `python -m aeo_validator_service` or the installed script."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8091"))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run("aeo_validator_service.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
