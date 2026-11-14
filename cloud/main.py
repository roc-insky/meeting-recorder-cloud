"""API entry point: ``python -m cloud.main`` (configuration via environment).

The server runs behind the TLS proxy: forwarded headers are trusted only
from the configured proxy hops, and the server header is not advertised.
This module never imports the worker, so an API-only container cannot claim
it is processing audio.
"""

from __future__ import annotations

import uvicorn

from cloud.app import create_app
from cloud.settings import Settings

app = create_app(Settings.from_env())


def main() -> None:
    settings = app.state.settings
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        server_header=False,
    )


if __name__ == "__main__":
    main()
