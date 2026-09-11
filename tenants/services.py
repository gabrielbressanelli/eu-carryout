import json
import logging
from datetime import time
from urllib import error, request

from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .models import BusinessHour, IntegrationEvent, Tenant, TenantIntegration

log = logging.getLogger(__name__)


def get_tenant_by_slug(tenant_slug: str) -> Tenant:
    return get_object_or_404(Tenant, slug=tenant_slug, is_active=True)


def get_request_tenant(request) -> Tenant:
    tenant = getattr(request, "tenant", None)
    if tenant:
        return tenant
    raise ImproperlyConfigured("Tenant is required. Customer routes must include a tenant slug.")


def ensure_tenant_onboarding_defaults(tenant):
    for day, _label in BusinessHour.DAY_CHOICES:
        BusinessHour.objects.get_or_create(
            tenant=tenant,
            day_of_week=day,
            defaults={
                "opens_at": time(11, 0),
                "closes_at": time(21, 0),
                "is_closed": day == BusinessHour.SUNDAY,
            },
        )

    for kind in [
        TenantIntegration.KIND_PRINT,
        TenantIntegration.KIND_EMAIL,
        TenantIntegration.KIND_SMS,
    ]:
        TenantIntegration.objects.get_or_create(tenant=tenant, kind=kind, defaults={"enabled": False})


def build_order_event_payload(order):
    tenant = order.tenant
    return {
        "event": "order.paid",
        "tenant_id": tenant.id,
        "tenant_slug": tenant.slug,
        "tenant_name": tenant.name,
        "order_id": order.id,
        "stripe_session_id": order.stripe_session_id or "",
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "customer_phone": order.customer_phone,
        "business_email": tenant.business_email,
        "business_phone": tenant.business_phone,
        "order_summary": order.order_summary,
        "amount_paid": str(order.amount_paid),
        "paid_at": order.paid_at.isoformat() if order.paid_at else "",
        "pickup_at": order.pickup_at.isoformat() if order.pickup_at else None,
        "pickup_timezone": order.pickup_timezone,
        "items": [{"name": item.name_snapshot, "quantity": item.quantity, "unit_price": str(item.unit_price),
                   "options": item.options_snapshot, "note": item.note} for item in order.items.all()],
    }


def _headers_for(integration):
    headers = {"Content-Type": "application/json"}
    if integration.auth_token:
        headers["Authorization"] = f"Bearer {integration.auth_token}"
    return headers


def _post_json(url, payload, headers):
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=25) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def send_integration_event(order, integration):
    payload = build_order_event_payload(order)
    event, created = IntegrationEvent.objects.get_or_create(
        order=order,
        tenant=order.tenant,
        kind=integration.kind,
        defaults={"request_payload": payload},
    )
    if not created and event.status == IntegrationEvent.STATUS_SENT:
        return event

    event.status = IntegrationEvent.STATUS_PENDING
    event.attempts += 1
    event.request_payload = payload
    event.last_error = ""
    event.save(update_fields=["status", "attempts", "request_payload", "last_error", "updated_at"])

    if not integration.endpoint_url:
        event.status = IntegrationEvent.STATUS_SKIPPED
        event.last_error = "Integration is enabled but has no endpoint URL."
        event.save(update_fields=["status", "last_error", "updated_at"])
        return event

    try:
        status, response_body = _post_json(
            integration.endpoint_url,
            payload,
            _headers_for(integration),
        )
        event.response_status = status
        event.response_body = response_body[:4000]
        if 200 <= status < 300:
            event.status = IntegrationEvent.STATUS_SENT
            event.sent_at = timezone.now()
        else:
            event.status = IntegrationEvent.STATUS_FAILED
            event.last_error = f"HTTP {status}"
    except error.HTTPError as exc:
        event.status = IntegrationEvent.STATUS_FAILED
        event.response_status = exc.code
        event.response_body = exc.read().decode("utf-8", errors="replace")[:4000]
        event.last_error = f"HTTP {exc.code}"
    except Exception as exc:
        event.status = IntegrationEvent.STATUS_FAILED
        event.last_error = str(exc)
        log.exception("Integration failed: order=%s kind=%s", order.id, integration.kind)

    event.save(
        update_fields=[
            "status",
            "response_status",
            "response_body",
            "last_error",
            "sent_at",
            "updated_at",
        ]
    )
    return event


def run_post_payment_workflow(order):
    integrations = order.tenant.integrations.filter(
        enabled=True,
        kind__in=[
            TenantIntegration.KIND_PRINT,
            TenantIntegration.KIND_EMAIL,
            TenantIntegration.KIND_SMS,
        ],
    )
    for integration in integrations:
        send_integration_event(order, integration)
