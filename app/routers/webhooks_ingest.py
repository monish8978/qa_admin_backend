"""Public conversation-ingestion endpoint — /api/v1/webhooks/ingest."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from ..common.exceptions import unauthorized
from ..common.responses import build_response
from ..deps import get_db, get_request_id
from ..schemas.billing import IngestRequest
from ..services import webhooks_ingest_service as svc

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = logging.getLogger("qa.api.routers.webhooks_ingest")


@router.post("/ingest")
def ingest(
    body: IngestRequest,
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
):
    log.info("[%s] Received conversation ingest request", rid)
    if not x_api_key:
        log.warning("[%s] Missing X-Api-Key header", rid)
        raise unauthorized("MISSING_API_KEY", "X-Api-Key header is required")
    
    tenant_id = svc.resolve_tenant_by_api_key(x_api_key)
    log.info("[%s] Resolved tenantId: %s. Ingesting %d conversations via channel: %s", rid, tenant_id, len(body.conversations), body.channel)
    
    payload = svc.ingest_conversations(
        db,
        tenant_id,
        channel=body.channel,
        conversations=[c.model_dump() for c in body.conversations],
    )
    log.info("[%s] Successfully ingested conversations for tenantId: %s", rid, tenant_id)
    return build_response(payload, rid)

