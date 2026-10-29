"""Run the K17 local API on loopback; never binds a public interface."""
from __future__ import annotations
import os
import uvicorn
from .http_api import create_http_app
from .local_runtime import build_local_application


def main() -> None:
    port = int(os.environ.get('AGENT_LOCAL_PORT', '8765'))
    if not 1 <= port <= 65535:
        raise ValueError('AGENT_LOCAL_PORT is invalid')
    uvicorn.run(create_http_app(build_local_application()), host='127.0.0.1', port=port,
                access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
