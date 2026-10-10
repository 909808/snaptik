"""``python -m app``: run the server with settings from the environment."""
import logging

import uvicorn

from .config import load_settings


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=settings.access_log,
    )


if __name__ == "__main__":
    main()
