import json
from datetime import timedelta
from decimal import ROUND_HALF_UP

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from catalog.models import MenuItem
from catalog.pricing import validate_and_price, effective_option_delta
from tenants.services import get_tenant_by_slug

from .auth import authenticate_agent_request
from .matching import search_menu, search_menu_by_category
from .models import AgentCallCart, AgentCallCartItem
from .order_summary import compute_total_from_summary

STALE_CART_MAX_AGE = timedelta(hours=4)


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


def _modifier_group_payload(menu_group):
    group = menu_group.group
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
            }
            for option in group.options.filter(is_active=True).order_by("sort_order", "name")
        ],
    }


def _item_payload(item):
    groups = (
        item.modifier_groups
        .select_related("group")
        .prefetch_related("group__options")
        .order_by("sort_order")
    )
    modifier_payloads = [_modifier_group_payload(group) for group in groups]
    return {
        "id": item.id,
        "name": item.name,
        "category": item.category.name,
        "base_price": str(item.price),
        "description": item.description,
        "aliases": [alias.alias for alias in item.aliases.all()],
        "required_modifiers": [payload for payload in modifier_payloads if payload["required"]],
        "optional_modifiers": [payload for payload in modifier_payloads if not payload["required"]],
    }


def _search_result_payload(query, result):
    if result["match_status"] == "matched":
        return {
            "match_status": "matched",
            "confidence": result.get("confidence"),
            "item": _item_payload(result["item"]),
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
    phrases = [phrase.strip() for phrase in raw_query.split(",") if phrase.strip()]
    if len(phrases) <= 1:
        query = phrases[0] if phrases else raw_query
        return JsonResponse(_search_result_payload(query, search_menu(query, tenant)))

    return JsonResponse({
        "query": raw_query,
        "results": [
            {"query": phrase, **_search_result_payload(phrase, search_menu(phrase, tenant))}
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
    items = search_menu_by_category(query, tenant)
    return JsonResponse({
        "query": query,
        "items": [_item_payload(item) for item in items],
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
    return JsonResponse({"item": _item_payload(item)})


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
