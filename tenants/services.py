import json
import logging
import re
from datetime import time, timezone as dt_timezone
from urllib import error, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import get_object_or_404
from django.utils import timezone

from catalog.models import DietaryTag
from .models import BusinessHour, IntegrationEvent, Tenant, TenantIntegration

log = logging.getLogger(__name__)
_TEMPLATE_VARIABLE = re.compile(r"{{\s*([A-Za-z_][\w.-]*)\s*}}")


def get_tenant_by_slug(tenant_slug: str) -> Tenant:
    return get_object_or_404(Tenant, slug=tenant_slug, is_active=True)


def get_request_tenant(request) -> Tenant:
    tenant = getattr(request, "tenant", None)
    if tenant:
        return tenant
    raise ImproperlyConfigured("Tenant is required. Customer routes must include a tenant slug.")


def ensure_tenant_onboarding_defaults(tenant):
    for slug, name in (("gluten_free", "Gluten-free"), ("vegetarian", "Vegetarian"), ("vegan", "Vegan")):
        DietaryTag.objects.get_or_create(tenant=tenant, slug=slug, defaults={"name": name})
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
    order_ref = str(order.id)
    pickup_time = ""
    if order.pickup_at:
        try:
            pickup_zone = ZoneInfo(order.pickup_timezone or tenant.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            pickup_zone = dt_timezone.utc
        pickup_time = order.pickup_at.astimezone(pickup_zone).strftime("%I:%M %p").lstrip("0")
    return {
        "event": "order.paid",
        "type": "Website Carryout",
        "tenant_id": tenant.id,
        "tenant_slug": tenant.slug,
        "tenant_name": tenant.name,
        "order_id": order.id,
        "order_ref": order_ref,
        "orderNumber": order_ref,
        "stripe_session_id": order.stripe_session_id or "",
        "customer_name": order.customer_name,
        "customerName": order.customer_name,
        "customer_email": order.customer_email,
        "customer_phone": order.customer_phone,
        "business_email": tenant.business_email,
        "business_phone": tenant.business_phone,
        "order_summary": order.order_summary,
        "amount_paid": str(order.amount_paid),
        "paid_at": order.paid_at.isoformat() if order.paid_at else "",
        "pickup_at": order.pickup_at.isoformat() if order.pickup_at else None,
        "pickup_time": pickup_time,
        "pickUpTime": pickup_time,
        "pickup_timezone": order.pickup_timezone,
        "items": [{"name": item.name_snapshot, "quantity": item.quantity, "unit_price": str(item.unit_price),
                   "options": item.options_snapshot, "note": item.note} for item in order.items.all()],
    }


def _template_value(value, payload):
    if isinstance(value, dict):
        return {key: _template_value(item, payload) for key, item in value.items()}
    if isinstance(value, list):
        return [_template_value(item, payload) for item in value]
    if not isinstance(value, str):
        return value

    exact = _TEMPLATE_VARIABLE.fullmatch(value)
    if exact:
        return payload.get(exact.group(1), "")
    return _TEMPLATE_VARIABLE.sub(lambda match: str(payload.get(match.group(1), "")), value)


def build_integration_payload(order, integration):
    payload = build_order_event_payload(order)
    config = integration.config or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}
    template = config.get("request_body") if isinstance(config, dict) else None
    # Django admin users may enter the JSON body directly instead of wrapping it.
    if template is None and isinstance(config, dict) and config:
        template = config
    return _template_value(template, payload) if template else payload


def _headers_for(integration):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Carryout-Integration/1.0",
    }
    if integration.auth_token:
        headers["Authorization"] = f"Bearer {integration.auth_token}"
    return headers


def _post_json(url, payload, headers):
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=25) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def send_integration_event(order, integration):
    payload = build_integration_payload(order, integration)
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
    run_enabled_integrations(order)


def run_enabled_integrations(order):
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
