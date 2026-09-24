import json
import logging
import asyncio
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from catalog.models import MenuItem
from catalog.pricing import validate_and_price, effective_option_delta
from tenants.hours import OrderingHours
from tenants.services import get_tenant_by_slug, run_enabled_integrations

from ordering.services import create_order_from_agent_cart
from ordering.views import _platform_fee_amount_cents
from ordering.cart import Cart
from .auth import authenticate_agent_request
from .matching import search_menu, search_menu_by_category
from .models import AgentCallCart, AgentCallCartItem
from .order_summary import compute_total_from_summary
from .order_summary import resolve_order_summary


STALE_CART_MAX_AGE = timedelta(hours=4)
log = logging.getLogger(__name__)


def _tenant_or_404(request, tenant_slug):
    tenant = get_tenant_by_slug(tenant_slug)
    request.tenant = tenant
    return tenant


def _require_agent_auth(request, tenant):
    if authenticate_agent_request(request, tenant):
        return None
    return JsonResponse({"message": "Unauthorized"}, status=401)


def _cleanup_stale_carts(tenant):
    cutoff = timezone.now() - STALE_CART_MAX_AGE
    AgentCallCart.objects.filter(tenant=tenant, updated_at__lt=cutoff).delete()


def _dietary_filter(request):
    raw = request.GET.get("dietary", "")
    return [value.strip().lower().replace("-", "_") for value in raw.split(",") if value.strip()]


def _tag_payload(tags):
    return [{"name": tag.name, "slug": tag.slug} for tag in tags]


def _modifier_group_payload(menu_group, dietary_tags=None):
    group = menu_group.group
    options = group.options.filter(is_active=True).prefetch_related("dietary_tags").order_by("sort_order", "name")
    if dietary_tags:
        for tag in dietary_tags:
            options = options.filter(dietary_tags__slug=tag)
        options = options.distinct()
    return {
        "id": group.id,
        "name": group.name,
        "type": "single_choice" if menu_group.effective_max() == 1 else "multi_choice",
        "required": menu_group.effective_required(),
        "min_choices": menu_group.effective_min(),
        "max_choices": menu_group.effective_max(),
        "options": [
            {
                "id": option.id,
                "name": option.name,
                "price_adjustment": str(option.price_delta),
                "price_multiplier": str(option.price_multiplier),
                "effective_price_adjustment": str(effective_option_delta(menu_group.menu_item, option)),
                "is_default": option.is_default,
                "dietary_tags": _tag_payload(option.dietary_tags.all()),
                "aliases": [alias.alias for alias in option.aliases.all()],
            }
            for option in options
        ],
    }


def _item_payload(item, dietary_tags=None):
    groups = (
        item.modifier_groups
        .select_related("group")
        .prefetch_related("group__options")
        .order_by("sort_order")
    )
    modifier_payloads = [_modifier_group_payload(group, dietary_tags) for group in groups]
    return {
        "id": item.id,
        "name": item.name,
        "category": item.category.name,
        "base_price": str(item.price),
        "description": item.description,
        "dietary_tags": _tag_payload(item.dietary_tags.all()),
        "aliases": [alias.alias for alias in item.aliases.all()],
        "required_modifiers": [payload for payload in modifier_payloads if payload["required"]],
        "optional_modifiers": [payload for payload in modifier_payloads if not payload["required"]],
    }


def _search_result_payload(query, result, dietary_tags=None):
    if result["match_status"] == "matched":
        return {
            "match_status": "matched",
            "confidence": result.get("confidence"),
            "item": _item_payload(result["item"], dietary_tags),
        }
    if result["match_status"] == "ambiguous":
        return {
            "match_status": "ambiguous",
            "query": query,
            "matches": [
                {
                    "item_id": row["item"].id,
                    "name": row["item"].name,
                    "confidence": row["confidence"],
                    "description": row["item"].description,
                    "category": row["item"].category.name,
                }
                for row in result["candidates"]
            ],
        }
    return {"match_status": "no_match", "query": query}


def _cart_item_payload(item):
    return {
        "line_id": f"line_{item.id}",
        "item_id": item.menu_item_id,
        "display_line": f"{item.quantity}x {item.menu_item.name}",
        "quantity": item.quantity,
        "unit_price": str(item.unit_price),
        "line_total": str(item.line_total()),
        "modifiers": item.options,
        "special_instructions": item.special_instructions,
    }


def _cart_payload(cart):
    items = list(cart.items.select_related("menu_item").order_by("created_at", "id"))
    return {
        "session_id": cart.session_id,
        "items": [_cart_item_payload(item) for item in items],
        "cart": {"subtotal": str(cart.subtotal())},
    }


def _parse_line_id(line_id):
    raw = (line_id or "").removeprefix("line_")
    return int(raw) if raw.isdigit() else None


def _quantity_from_payload(payload):
    try:
        return max(1, int(payload.get("quantity", 1)))
    except (TypeError, ValueError):
        raise ValueError("quantity must be a positive integer")


@require_GET
def menu_search(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    raw_query = request.GET.get("q", "")
    dietary_tags = _dietary_filter(request)
    phrases = [phrase.strip() for phrase in raw_query.split(",") if phrase.strip()]
    if len(phrases) <= 1:
        query = phrases[0] if phrases else raw_query
        return JsonResponse(_search_result_payload(query, search_menu(query, tenant, dietary_tags), dietary_tags))

    return JsonResponse({
        "query": raw_query,
        "results": [
            {"query": phrase, **_search_result_payload(phrase, search_menu(phrase, tenant, dietary_tags), dietary_tags)}
            for phrase in phrases
        ],
    })


@require_GET
def menu_categories(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    query = request.GET.get("q", "")
    dietary_tags = _dietary_filter(request)
    items = search_menu_by_category(query, tenant, dietary_tags)
    return JsonResponse({
        "query": query,
        "items": [_item_payload(item, dietary_tags) for item in items],
    })


@require_GET
def menu_item_detail(request, tenant_slug, item_id):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    try:
        item = MenuItem.objects.get(id=item_id, tenant=tenant, is_active=True)
    except MenuItem.DoesNotExist:
        return JsonResponse({"error": "Menu item not found"}, status=404)
    return JsonResponse({"item": _item_payload(item, _dietary_filter(request))})


@csrf_exempt
@require_http_methods(["POST"])
def cart_items_create(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    session_id = payload.get("session_id")
    item_id = payload.get("item_id")
    if not session_id or not item_id:
        return JsonResponse({"error": "session_id and item_id are required"}, status=400)

    try:
        menu_item = MenuItem.objects.get(id=item_id, tenant=tenant, is_active=True)
    except (MenuItem.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"error": f"Unknown item_id {item_id!r}"}, status=404)

    selected_ids = [
        modifier.get("option_id")
        for modifier in (payload.get("modifiers") or [])
        if modifier.get("option_id") is not None
    ]
    try:
        unit_price, options_snapshot = validate_and_price(menu_item, selected_ids)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    _cleanup_stale_carts(tenant)
    cart, _ = AgentCallCart.objects.get_or_create(tenant=tenant, session_id=session_id)
    try:
        quantity = _quantity_from_payload(payload)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    item = AgentCallCartItem.objects.create(
        cart=cart,
        menu_item=menu_item,
        quantity=quantity,
        unit_price=unit_price,
        options=options_snapshot,
        special_instructions=payload.get("special_instructions", "") or "",
    )

    return JsonResponse({
        "success": True,
        "cart_item": _cart_item_payload(item),
        "cart": {"subtotal": str(cart.subtotal())},
    }, status=201)


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def cart_item_detail(request, tenant_slug, line_id):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    session_id = request.GET.get("session_id")
    if not session_id:
        return JsonResponse({"error": "session_id query param is required"}, status=400)

    pk = _parse_line_id(line_id)
    if pk is None:
        return JsonResponse({"error": "Invalid line_id"}, status=400)

    try:
        item = AgentCallCartItem.objects.select_related("cart", "menu_item").get(
            pk=pk,
            cart__tenant=tenant,
            cart__session_id=session_id,
        )
    except AgentCallCartItem.DoesNotExist:
        return JsonResponse({"error": "Cart line not found"}, status=404)

    cart = item.cart
    if request.method == "DELETE":
        item.delete()
        return JsonResponse({"success": True, "cart": {"subtotal": str(cart.subtotal())}})

    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    if "modifiers" in payload:
        selected_ids = [
            modifier.get("option_id")
            for modifier in (payload.get("modifiers") or [])
            if modifier.get("option_id") is not None
        ]
        try:
            item.unit_price, item.options = validate_and_price(item.menu_item, selected_ids)
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=400)

    if "quantity" in payload:
        try:
            item.quantity = _quantity_from_payload(payload)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    if "special_instructions" in payload:
        item.special_instructions = payload["special_instructions"] or ""
    item.save()

    return JsonResponse({
        "success": True,
        "cart_item": _cart_item_payload(item),
        "cart": {"subtotal": str(cart.subtotal())},
    })


@require_GET
def cart_detail(request, tenant_slug, session_id):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    try:
        cart = AgentCallCart.objects.get(tenant=tenant, session_id=session_id)
    except AgentCallCart.DoesNotExist:
        return JsonResponse({"session_id": session_id, "items": [], "cart": {"subtotal": "0.00"}})
    return JsonResponse(_cart_payload(cart))


@csrf_exempt
@require_http_methods(["POST"])
def order_summary_total(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    total, warnings = compute_total_from_summary(payload.get("order_summary", ""), tenant)
    whole_dollars = int(total.to_integral_value(rounding=ROUND_HALF_UP))
    return JsonResponse({
        "total_price": whole_dollars,
        "exact_total": str(total),
        "warnings": warnings,
    })


def _agent_checkout_token(tenant, session_id):
    return signing.dumps({"tenant_id": tenant.id, "session_id": session_id}, salt="agent-checkout")


def _save_resolved_agent_cart(tenant, session_id, resolved):
    cart, _ = AgentCallCart.objects.get_or_create(tenant=tenant, session_id=session_id)
    cart.items.all().delete()
    for line in resolved:
        AgentCallCartItem.objects.create(
            cart=cart,
            menu_item=line["item"],
            quantity=line["quantity"],
            unit_price=line["unit_price"],
            options=line["options"],
        )
    return cart


@csrf_exempt
@require_http_methods(["POST"])
def order_finalize_summary(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error
    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    session_id = payload.get("session_id")
    if not session_id or not payload.get("order_summary"):
        return JsonResponse({"error": "session_id and order_summary are required"}, status=400)

    resolved, warnings = resolve_order_summary(payload["order_summary"], tenant)
    cart = _save_resolved_agent_cart(tenant, session_id, resolved)
    checkout_url = request.build_absolute_uri(f"/api/{tenant.slug}/agent/checkout/{_agent_checkout_token(tenant, session_id)}/") if resolved else None
    if warnings:
        return JsonResponse({
            "success": False,
            "requires_review": True,
            "checkout_url": checkout_url,
            "resolved_summary": " ".join(
                f"{line['quantity']}x {line['item'].name};" for line in resolved
            ),
            "unresolved": warnings,
        }, status=422)

    request._body = json.dumps({
        "session_id": session_id,
        "pickup": payload.get("pickup", "asap"),
        "customer_name": payload.get("customer_name", ""),
        "customer_email": payload.get("customer_email", ""),
        "customer_phone": payload.get("customer_phone", ""),
    }).encode("utf-8")
    return order_finalize(request, tenant_slug)


@require_GET
def agent_checkout(request, tenant_slug, token):
    tenant = _tenant_or_404(request, tenant_slug)
    try:
        data = signing.loads(token, salt="agent-checkout", max_age=1800)
    except signing.BadSignature:
        return JsonResponse({"error": "This checkout link is invalid or expired."}, status=404)
    if data.get("tenant_id") != tenant.id:
        return JsonResponse({"error": "Checkout link does not belong to this restaurant."}, status=404)
    try:
        agent_cart = AgentCallCart.objects.get(tenant=tenant, session_id=data["session_id"])
    except (AgentCallCart.DoesNotExist, KeyError):
        return JsonResponse({"error": "This checkout cart is no longer available."}, status=404)
    website_cart = Cart(request)
    website_cart.lines = []
    for item in agent_cart.items.select_related("menu_item"):
        website_cart.lines.append({
            "key": uuid4().hex,
            "menu_item_id": item.menu_item_id,
            "name": item.menu_item.name,
            "unit_price": str(item.unit_price),
            "quantity": item.quantity,
            "options": item.options,
            "note": item.special_instructions,
        })
    website_cart.save()
    return redirect("checkout", tenant_slug=tenant.slug)


@csrf_exempt
@require_http_methods(["POST"])
def order_finalize(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    auth_error = _require_agent_auth(request, tenant)
    if auth_error:
        return auth_error

    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    session_id = payload.get("session_id")
    if not session_id:
        return JsonResponse({"error": "session_id is required"}, status=400)
    _cleanup_stale_carts(tenant)
    try:
        cart = AgentCallCart.objects.get(tenant=tenant, session_id=session_id)
    except AgentCallCart.DoesNotExist:
        return JsonResponse({"error": "Agent cart not found"}, status=404)

    try:
        pickup_at = OrderingHours(tenant).validate_pickup(payload.get("pickup", "asap"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc), "availability": OrderingHours(tenant).payload()}, status=400)

    stripe_secret_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    stripe_account_id = tenant.account.stripe_account_id
    if not stripe_secret_key or not stripe_account_id:
        return JsonResponse({"error": "Stripe is not configured for this restaurant."}, status=503)
    try:
        import stripe
    except ImportError:
        return JsonResponse({"error": "Stripe package is not installed."}, status=503)

    customer = {
        "name": payload.get("customer_name", ""),
        "email": payload.get("customer_email", ""),
        "phone": payload.get("customer_phone", ""),
    }
    try:
        order = create_order_from_agent_cart(cart, customer=customer, pickup_at=pickup_at)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    line_items = []
    for item in order.items.all():
        description = ", ".join(option["name"] for option in item.options_snapshot)
        if item.note:
            description = f"{description}; {item.note}" if description else item.note
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": item.name_snapshot, "description": description[:500] or "Carryout"},
                "unit_amount": int((item.unit_price * 100).quantize(Decimal("1"))),
            },
            "quantity": item.quantity,
        })
    metadata = {
        "tenant_id": str(tenant.id),
        "tenant_slug": tenant.slug,
        "order_id": str(order.id),
        "order_source": "agent",
        "order_summary": order.order_summary[:500],
        "pickup_at": pickup_at.isoformat(),
        "pickup_timezone": tenant.timezone,
    }
    payment_intent_data = {}
    fee_amount = _platform_fee_amount_cents(order.amount_paid, tenant.account)
    if fee_amount:
        payment_intent_data["application_fee_amount"] = fee_amount

    stripe.api_key = stripe_secret_key
    try:
        session_kwargs = {
            "mode": "payment",
            "line_items": line_items,
            "success_url": request.build_absolute_uri(reverse("checkout_success", kwargs={"tenant_slug": tenant.slug})) + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": request.build_absolute_uri(reverse("checkout", kwargs={"tenant_slug": tenant.slug})),
            "customer_email": customer["email"] or None,
            "phone_number_collection": {"enabled": True},
            "metadata": metadata,
            "stripe_account": stripe_account_id,
        }
        if payment_intent_data:
            session_kwargs["payment_intent_data"] = payment_intent_data
        session = stripe.checkout.Session.create(**session_kwargs)
    except Exception:
        log.exception("Could not create agent Stripe Checkout Session.")
        return JsonResponse({"error": "Could not create payment link."}, status=502)

    order.stripe_session_id = session.id
    order.save(update_fields=["stripe_session_id", "updated_at"])
    run_enabled_integrations(order)

    return JsonResponse({
        "order_id": order.id,
        "amount": str(order.amount_paid),
        "order_summary": order.order_summary,
        "payment_url": session.url,
    }, status=201)

@csrf_exempt
@require_http_methods(['POST'])
async def agent_pause_n_seconds(request, tenant_slug):
    try:
        body = json.loads(request.body)

        seconds = float(body.get("seconds", 2))
        if seconds < 10:

            await asyncio.sleep(seconds)

            return JsonResponse(
                {
                    "ok": True,

                },
                status=200,
            )
        else:
            return JsonResponse(
                {
                    'ok': False,
                    "message": "seconds must be lower than 10",
                },
                status=400
            )
        
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse(
            {
                'ok': False,
                'message': "Invalid Body value for seconds",
            },
            status=400,
        )

