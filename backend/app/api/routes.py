from __future__ import annotations

import logging
import time
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.core.proxy_manager import ProxyManager
from app.schemas.models import (
    ProductDetail,
    ProxyConfigPayload,
    ProxyErrorEvent,
    ProxyRecord,
    ProxyStatus,
    ProxyTogglePayload,
    SearchResponse,
    SourceName,
)
from app.services.marketplace_service import MarketplaceService

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

_proxy_manager = ProxyManager()
_marketplace_service = MarketplaceService(_proxy_manager)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/search", response_model=SearchResponse)
async def search(query: str = Query(..., min_length=2)) -> SearchResponse:
    request_id = str(uuid4())
    started_at = time.perf_counter()
    response = await _marketplace_service.search(query, request_id=request_id)
    logger.info(
        "api_search_done request_id=%s latency_ms=%s query_len=%s",
        request_id,
        int((time.perf_counter() - started_at) * 1000),
        len(query),
    )
    return response


@router.get("/product-details", response_model=ProductDetail)
async def product_details(source: SourceName, product_url: str) -> ProductDetail:
    request_id = str(uuid4())
    started_at = time.perf_counter()
    try:
        detail = await _marketplace_service.get_product_details(
            source=source,
            product_url=product_url,
            request_id=request_id,
        )
        logger.info(
            "api_detail_done request_id=%s source=%s latency_ms=%s",
            request_id,
            source.value,
            int((time.perf_counter() - started_at) * 1000),
        )
        return detail
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        logger.warning(
            "api_detail_failed request_id=%s source=%s latency_ms=%s err=%s",
            request_id,
            source.value,
            int((time.perf_counter() - started_at) * 1000),
            exc,
        )
        if message == "Ozon requires browser verification. Product details are temporarily unavailable.":
            raise HTTPException(status_code=503, detail=message) from exc
        raise HTTPException(status_code=502, detail=f"Failed to fetch product details: {message}") from exc


@router.post("/proxies/file", response_model=list[ProxyRecord])
async def upload_proxies_file(file: UploadFile = File(...)) -> list[ProxyRecord]:
    payload = await file.read()
    text = payload.decode("utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines()]
    return _proxy_manager.load_from_lines(lines)


@router.post("/proxies/text", response_model=list[ProxyRecord])
async def upload_proxies_text(payload: ProxyConfigPayload) -> list[ProxyRecord]:
    lines = [line.strip() for line in payload.proxies_text.splitlines()]
    return _proxy_manager.load_from_lines(lines)


@router.post("/proxies/toggle", response_model=ProxyStatus)
async def toggle_proxies(payload: ProxyTogglePayload) -> ProxyStatus:
    _proxy_manager.set_enabled(payload.enabled)
    return _proxy_manager.status()


@router.get("/proxies/status", response_model=ProxyStatus)
async def proxy_status() -> ProxyStatus:
    return _proxy_manager.status()


@router.get("/proxies/errors", response_model=list[ProxyErrorEvent])
async def proxy_errors(limit: int = 50) -> list[ProxyErrorEvent]:
    return _proxy_manager.recent_errors(limit=limit)
