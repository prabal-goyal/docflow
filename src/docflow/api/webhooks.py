"""Admin management of webhook endpoints and their deliveries."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from docflow import dispatch
from docflow.config import Settings
from docflow.deps import AppSettings, DbSession, require_roles
from docflow.errors import InvalidRequestError, NotFoundError
from docflow.models import Role, User, WebhookDelivery, WebhookEndpoint
from docflow.schemas import (
    WebhookDeliveryOut,
    WebhookEndpointIn,
    WebhookEndpointOut,
    WebhookEndpointUpdate,
    WebhookEndpointWithSecret,
)
from docflow.ssrf import Resolver, UnsafeUrlError, resolve_host, validate_target
from docflow.webhooks import WebhookEventType, emit, new_secret

router = APIRouter(tags=["webhooks"])

AdminUser = Annotated[User, Depends(require_roles(Role.ADMIN))]
MAX_PAGE_SIZE = 200


def get_resolver() -> Resolver:
    """DNS lookup used for SSRF checks; replaced in tests so they never touch the network."""
    return resolve_host


DnsResolver = Annotated[Resolver, Depends(get_resolver)]


def check_url(url: str, settings: Settings, resolve: Resolver) -> None:
    try:
        validate_target(url, settings.webhook_allow_private_targets, resolve)
    except UnsafeUrlError as error:
        raise InvalidRequestError(str(error)) from None


def get_endpoint(db: DbSession, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    endpoint = db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise NotFoundError("Webhook endpoint not found")
    return endpoint


@router.post("/webhooks", status_code=status.HTTP_201_CREATED)
def create_endpoint(
    body: WebhookEndpointIn,
    db: DbSession,
    _admin: AdminUser,
    settings: AppSettings,
    resolve: DnsResolver,
) -> WebhookEndpointWithSecret:
    """Register a receiver. The signing secret is returned now and never again."""
    check_url(body.url, settings, resolve)
    endpoint = WebhookEndpoint(
        url=body.url,
        secret=new_secret(),
        event_types=[t.value for t in body.event_types],
        description=body.description,
    )
    db.add(endpoint)
    db.commit()
    return WebhookEndpointWithSecret.model_validate(endpoint)


@router.get("/webhooks")
def list_endpoints(db: DbSession, _admin: AdminUser) -> list[WebhookEndpointOut]:
    endpoints = db.scalars(select(WebhookEndpoint).order_by(WebhookEndpoint.created_at))
    return [WebhookEndpointOut.model_validate(e) for e in endpoints]


@router.patch("/webhooks/{endpoint_id}")
def update_endpoint(
    endpoint_id: uuid.UUID,
    body: WebhookEndpointUpdate,
    db: DbSession,
    _admin: AdminUser,
    settings: AppSettings,
    resolve: DnsResolver,
) -> WebhookEndpointOut:
    """Change the URL, events or description, or set active=false. Endpoints are deactivated
    rather than deleted, so their delivery history stays."""
    endpoint = get_endpoint(db, endpoint_id)
    if body.url is not None:
        check_url(body.url, settings, resolve)
        endpoint.url = body.url
    if body.event_types is not None:
        endpoint.event_types = [t.value for t in body.event_types]
    if body.active is not None:
        endpoint.active = body.active
    if body.description is not None:
        endpoint.description = body.description
    db.commit()
    return WebhookEndpointOut.model_validate(endpoint)


@router.post("/webhooks/{endpoint_id}/rotate-secret")
def rotate_secret(
    endpoint_id: uuid.UUID, db: DbSession, _admin: AdminUser
) -> WebhookEndpointWithSecret:
    """Replace the signing secret. Deliveries sent from now on use the new one."""
    endpoint = get_endpoint(db, endpoint_id)
    endpoint.secret = new_secret()
    db.commit()
    return WebhookEndpointWithSecret.model_validate(endpoint)


@router.post("/webhooks/{endpoint_id}/test", status_code=status.HTTP_202_ACCEPTED)
def send_test_event(endpoint_id: uuid.UUID, db: DbSession, _admin: AdminUser) -> WebhookDeliveryOut:
    """Queue a webhook.test event to this endpoint only, so a customer can check their setup."""
    endpoint = get_endpoint(db, endpoint_id)
    if not endpoint.active:
        raise InvalidRequestError("The endpoint is deactivated")
    event = emit(
        db, WebhookEventType.TEST, {"message": "Test event from DocFlow"}, only_endpoint=endpoint
    )
    db.commit()
    delivery = db.scalars(select(WebhookDelivery).where(WebhookDelivery.event_id == event.id)).one()
    return WebhookDeliveryOut.model_validate(delivery)


@router.get("/webhooks/{endpoint_id}/deliveries")
def list_deliveries(
    endpoint_id: uuid.UUID,
    db: DbSession,
    _admin: AdminUser,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WebhookDeliveryOut]:
    get_endpoint(db, endpoint_id)
    deliveries = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id)
        .limit(limit)
        .offset(offset)
    )
    return [WebhookDeliveryOut.model_validate(d) for d in deliveries]


@router.post("/webhook-deliveries/{delivery_id}/redeliver", status_code=status.HTTP_202_ACCEPTED)
def redeliver(delivery_id: uuid.UUID, db: DbSession, _admin: AdminUser) -> WebhookDeliveryOut:
    """Queue the delivery again with a fresh set of attempts (sent within seconds)."""
    delivery = dispatch.redeliver(db, delivery_id)
    if delivery is None:
        raise NotFoundError("Delivery not found")
    return WebhookDeliveryOut.model_validate(delivery)
