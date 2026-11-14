"""Entry point: ``python -m cloud.main`` (configuration via environment)."""

from __future__ import annotations

import uvicorn

from cloud.app import create_app
from cloud.settings import Settings

app = create_app(Settings.from_env())


def main() -> None:
    settings = app.state.settings
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
