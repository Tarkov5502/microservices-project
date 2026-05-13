"""
api-gateway/app/routes/proxy.py

Transparent reverse proxy: forwards requests to the appropriate
backend service, stripping/adding headers as needed.
"""
import logging
from fastapi import APIRouter, Request, Response, HTTPException
import httpx

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Map URL prefixes to upstream service URLs
ROUTE_MAP = {
    "/api/v1/users": settings.user_service_url,
    "/api/v1/auth":  settings.user_service_url,
    "/api/v1/tasks": settings.task_service_url,
    "/api/v1/projects": settings.task_service_url,
    "/api/v1/notifications": settings.notification_service_url,
}

# Headers we strip before forwarding (they're gateway-internal)
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailers", "upgrade", "proxy-authorization",
}


async def _get_client() -> httpx.AsyncClient:
    """Get the shared HTTP client from app state."""
    from app.main import http_client
    if http_client is None:
        raise HTTPException(status_code=503, detail="Gateway not ready")
    return http_client


def _resolve_upstream(path: str) -> tuple[str, str]:
    """
    Find the upstream service URL for a given request path.
    Returns (upstream_base_url, upstream_path).
    """
    for prefix, upstream in ROUTE_MAP.items():
        if path.startswith(prefix):
            return upstream, path
    raise HTTPException(status_code=404, detail=f"No route for path: {path}")


@router.api_route(
    "/api/v1/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy(full_path: str, request: Request) -> Response:
    """Catch-all proxy handler for all /api/v1/* paths."""
    path = f"/api/v1/{full_path}"
    upstream_url, upstream_path = _resolve_upstream(path)

    # Build forwarded URL including query string
    target = f"{upstream_url}{upstream_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    # Forward headers, excluding hop-by-hop and adding forwarding metadata
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }
    forward_headers["X-Forwarded-For"] = request.client.host
    forward_headers["X-Forwarded-Proto"] = request.url.scheme
    # Pass authenticated user info downstream (set by auth middleware)
    if user_id := request.state.__dict__.get("user_id"):
        forward_headers["X-User-Id"] = str(user_id)

    body = await request.body()
    client = await _get_client()

    try:
        upstream_response = await client.request(
            method=request.method,
            url=target,
            headers=forward_headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.error("Upstream unreachable: %s", target)
        raise HTTPException(status_code=503, detail="Upstream service unavailable")
    except httpx.TimeoutException:
        logger.error("Upstream timeout: %s", target)
        raise HTTPException(status_code=504, detail="Upstream service timed out")

    # Strip hop-by-hop headers from upstream response
    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
