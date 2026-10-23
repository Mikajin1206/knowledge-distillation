"""Progress for synchronous MCP tools; notifications never claim model analysis."""
from anyio import from_thread
import logging
from mcp.server.mcpserver import Context

_LOG = logging.getLogger(__name__)


def report_progress(ctx: Context | None, progress: float, total: float | None,
                    message: str) -> None:
    # MCPServer runs synchronous tools in an AnyIO worker thread. The SDK sends
    # nothing when the caller has not requested progress for this request.
    if ctx is not None:
        meta = ctx.request_context.meta
        requested = bool(meta is not None and (meta.get('progress_token') is not None
                                              or meta.get('progressToken') is not None))
        _LOG.info('MCP progress request=%s requested=%s progress=%s total=%s',
                  ctx.request_id, requested, progress, total)
        from_thread.run(ctx.report_progress, progress, total, message)
