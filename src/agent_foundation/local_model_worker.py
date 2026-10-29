"""Run one queued model job using explicitly configured local credentials."""
from __future__ import annotations
import asyncio
from .local_runtime import build_local_application, build_model_adapter


async def main() -> None:
    result = await build_local_application().run_model_worker_once(build_model_adapter())
    print('no_pending_job' if result is None else 'job_completed')


if __name__ == '__main__':
    asyncio.run(main())
