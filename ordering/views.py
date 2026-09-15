import json
import logging
from decimal import Decimal

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from catalog.models import MenuCategory, MenuItem
from catalog.pricing import modifier_payload, validate_and_price
from tenants.services import get_tenant_by_slug
from tenants.hours import OrderingHours

from .cart import Cart
from .models import Order
from .services import create_order_from_cart, mark_stripe_order_paid

log = logging.getLogger(__name__)


def _platform_fee_amount_cents(total, account):
    percent = (
        account.stripe_application_fee_percent
        if account.stripe_application_fee_percent is not None
        else Decimal(str(getattr(settings, "STRIPE_APPLICATION_FEE_PERCENT", "0") or "0"))
    )
    fixed_cents = (
        account.stripe_application_fee_fixed_cents
        if account.stripe_application_fee_fixed_cents is not None
        else int(getattr(settings, "STRIPE_APPLICATION_FEE_FIXED_CENTS", "0") or 0)
    )
    total_cents = int((total * 100).quantize(Decimal("1")))
    fee_cents = fixed_cents + int((Decimal(total_cents) * percent / Decimal("100")).quantize(Decimal("1")))
    return fee_cents if 0 < fee_cents < total_cents else 0


def landing(request):
    return render(request, "landing/index.html")


def _tenant_or_404(request, tenant_slug):
    tenant = get_tenant_by_slug(tenant_slug)
    request.tenant = tenant
    return tenant


def menu(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    categories = (
        MenuCategory.objects.filter(tenant=tenant, is_active=True)
        .prefetch_related("items")
        .order_by("sort_order", "name")
    )
    cart = Cart(request)
    cart_context = _cart_context(cart)
    return render(
        request,
        "catalog/menu.html",
        {
            "tenant": tenant,
            "categories": categories,
            **cart_context,
            "availability": OrderingHours(tenant).payload(),
        },
    )


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _cart_context(cart):
    lines, changes, errors = cart.review()
    total = cart.total()
    return {
        "tenant": cart.tenant,
        "cart": cart,
        "cart_lines": lines,
        "cart_total": total,
        "lines": lines,
        "total": total,
        "cart_changes": changes,
        "cart_errors": errors,
        "cart_revision": cart.revision,
    }


def _cart_fragment_response(request, cart, error=None, status=200):
    context = _cart_context(cart)
    return JsonResponse(
        {
            "ok": error is None,
            "error": error,
            "cart_revision": context["cart_revision"],
            "cart_valid": not context["cart_errors"],
            "cart_qty": len(cart),
            "cart_total": str(context["cart_total"]),
            "cart_html": render_to_string(
                "ordering/partials/cart_summary.html",
                context,
                request=request,
            ),
            "checkout_html": render_to_string(
                "ordering/partials/checkout_summary.html",
                context,
                request=request,
            ),
        }, status=status,
    )


@require_POST
def cart_add(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    cart = Cart(request)
    item = get_object_or_404(MenuItem, id=request.POST.get("item_id"), tenant=cart.tenant, is_active=True, category__is_active=True)
    try:
        cart.add(item, request.POST.get("quantity", 1), request.POST.getlist("option_ids"), request.POST.get("note", ""))
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if _is_ajax(request):
        return _cart_fragment_response(request, cart)
    return redirect("menu", tenant_slug=cart.tenant.slug)


@require_POST
def cart_update(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    cart = Cart(request)
    try:
        if request.POST.get("configure") == "1":
            line = cart.find(key=request.POST.get("line_key"))
            item = get_object_or_404(MenuItem, pk=line["menu_item_id"], tenant=cart.tenant)
            cart.add(item, request.POST.get("quantity", 1), request.POST.getlist("option_ids"), request.POST.get("note", ""), replace_key=line["key"])
        else:
            cart.update(request.POST.get("line_index"), request.POST.get("quantity", 1), key=request.POST.get("line_key"))
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if _is_ajax(request):
        return _cart_fragment_response(request, cart)
    return redirect("checkout", tenant_slug=cart.tenant.slug)


@require_POST
def cart_remove(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    cart = Cart(request)
    try:
        cart.delete(request.POST.get("line_index"), key=request.POST.get("line_key"))
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    if _is_ajax(request):
        return _cart_fragment_response(request, cart)
    return redirect("checkout", tenant_slug=cart.tenant.slug)


def cart_summary(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    cart = Cart(request)
    if _is_ajax(request):
        return _cart_fragment_response(request, cart)
    return render(request, "ordering/partials/cart_summary.html", _cart_context(cart))


def cart_count(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    cart = Cart(request)
    return JsonResponse({"cart_qty": len(cart), "cart_total": str(cart.total())})


def item_options(request, tenant_slug, item_id):
    tenant = _tenant_or_404(request, tenant_slug)
    item = get_object_or_404(MenuItem, pk=item_id, tenant=tenant, is_active=True, category__is_active=True)
    line = None
    if request.GET.get("line_key"):
        try:
            line = Cart(request).find(key=request.GET["line_key"])
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=404)
        if line["menu_item_id"] != item.pk:
            return JsonResponse({"error": "Cart item not found."}, status=404)
    return JsonResponse({"item": {"id": item.pk, "name": item.name, "description": item.description,
                                  "base_price": str(item.price), "image": item.image_src, "groups": modifier_payload(item)},
                         "line": line, "quote_url": reverse("item_quote", args=[tenant.slug, item.pk]),
                         "action_url": reverse("cart_update" if line else "cart_add", args=[tenant.slug])})


@require_POST
def item_quote(request, tenant_slug, item_id):
    tenant = _tenant_or_404(request, tenant_slug)
    item = get_object_or_404(MenuItem, pk=item_id, tenant=tenant, is_active=True, category__is_active=True)
    try:
        price, _ = validate_and_price(item, request.POST.getlist("option_ids"))
        quantity = Cart.quantity(request.POST.get("quantity", 1))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"unit_price": str(price), "total": str(price * quantity)})


def pickup_availability(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    return JsonResponse(OrderingHours(tenant).payload(), headers={"Cache-Control": "no-store"})


def checkout(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    cart = Cart(request)
    return render(
        request,
        "ordering/checkout.html",
        {**_cart_context(cart), "availability": OrderingHours(cart.tenant).payload()},
    )


@require_POST
def create_checkout_session(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    cart = Cart(request)
    if len(cart) == 0:
        return JsonResponse({"error": "Cart is empty."}, status=400)

    _, changes, errors = cart.review()
    if errors or changes or request.POST.get("cart_revision") != cart.revision:
        return _cart_fragment_response(request, cart, "Your cart changed. Review the updated items and total before paying.", status=409)
    try:
        pickup_at = OrderingHours(cart.tenant).validate_pickup(request.POST.get("pickup", "asap"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc), "availability": OrderingHours(cart.tenant).payload()}, status=400)

    stripe_secret_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    if not stripe_secret_key:
        return JsonResponse({"error": "Stripe is not configured."}, status=503)
    stripe_account_id = cart.tenant.account.stripe_account_id
    if not stripe_account_id:
        return JsonResponse({"error": "Stripe is not configured for this restaurant."}, status=503)

    try:
        import stripe
    except ImportError:
        return JsonResponse({"error": "Stripe package is not installed."}, status=503)

    stripe.api_key = stripe_secret_key
    line_items = []
    for line in cart.lines:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": line["name"], "description": (", ".join(option["name"] for option in line.get("options", [])) + ("; " + line["note"] if line.get("note") else ""))[:500] or "Carryout"},
                    "unit_amount": int((Decimal(str(line["unit_price"])) * 100).quantize(Decimal("1"))),
                },
                "quantity": int(line["quantity"]),
            }
        )

    try:
        order = create_order_from_cart(cart, pickup_at=pickup_at)
    except ValueError as exc:
        return _cart_fragment_response(request, cart, str(exc), status=409)
    metadata = {
        "tenant_id": str(cart.tenant.id),
        "tenant_slug": cart.tenant.slug,
        "order_id": str(order.id),
        "order_source": "website",
        "order_summary": order.order_summary[:500],
        "pickup_at": pickup_at.isoformat(),
        "pickup_timezone": cart.tenant.timezone,
        "stripe_account_id": stripe_account_id,
    }
    payment_intent_data = {}
    application_fee_amount = _platform_fee_amount_cents(cart.total(), cart.tenant.account)
    if application_fee_amount:
        payment_intent_data["application_fee_amount"] = application_fee_amount

    try:
        session_kwargs = {
            "mode": "payment",
            "line_items": line_items,
            "success_url": request.build_absolute_uri(
                reverse("checkout_success", kwargs={"tenant_slug": cart.tenant.slug})
            ) + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": request.build_absolute_uri(
                reverse("checkout", kwargs={"tenant_slug": cart.tenant.slug})
            ),
            "customer_email": request.POST.get("email") or None,
            "phone_number_collection": {"enabled": True},
            "metadata": metadata,
            "stripe_account": stripe_account_id,
        }
        if payment_intent_data:
            session_kwargs["payment_intent_data"] = payment_intent_data
        session = stripe.checkout.Session.create(**session_kwargs)
    except Exception:
        log.exception("Could not create Stripe Checkout Session.")
        return JsonResponse({"error": "Could not start payment."}, status=502)

    order.stripe_session_id = session.id
    order.save(update_fields=["stripe_session_id", "updated_at"])

    return JsonResponse({"checkout_url": session.url})


def checkout_success(request, tenant_slug):
    _tenant_or_404(request, tenant_slug)
    session_id = request.GET.get("session_id", "")
    if not session_id.startswith("cs_"):
        return redirect("menu", tenant_slug=tenant_slug)

    stripe_secret_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    if not stripe_secret_key:
        return HttpResponse("Payment received. Stripe confirmation is not configured locally.", status=200)

    try:
        import stripe
    except ImportError:
        return HttpResponse("Payment received. Stripe package is not installed locally.", status=200)

    stripe.api_key = stripe_secret_key
    try:
        tenant = _tenant_or_404(request, tenant_slug)
        order = Order.objects.filter(stripe_session_id=session_id, tenant=tenant).select_related("tenant__account").first()
        stripe_account_id = (order.tenant.account.stripe_account_id if order else tenant.account.stripe_account_id)
        if not stripe_account_id:
            return HttpResponse("Thanks. We are confirming your payment.", status=200)
        session = stripe.checkout.Session.retrieve(session_id, expand=["customer_details"], stripe_account=stripe_account_id)
    except Exception:
        log.exception("Could not retrieve Stripe Checkout Session.")
        return HttpResponse("Thanks. We are confirming your payment.", status=200)

    order = mark_stripe_order_paid(session)
    Cart(request).clear()
    return render(request, "ordering/success.html", {"tenant": order.tenant, "order": order})


@csrf_exempt
def stripe_webhook(request):
    webhook_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        return HttpResponse(status=503)

    try:
        import stripe
    except ImportError:
        return HttpResponse(status=503)

    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = stripe.Webhook.construct_event(request.body, sig, webhook_secret)
    except Exception:
        log.warning("Stripe webhook signature validation failed.")
        return HttpResponse(status=400)

    if event.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        if session.get("payment_status") == "paid":
            try:
                mark_stripe_order_paid(session)
            except ValueError as exc:
                log.warning("Stripe webhook ignored: %s", exc)
                return HttpResponse(status=400)
    return HttpResponse(status=200)


def orders_by_email(request, tenant_slug):
    tenant = _tenant_or_404(request, tenant_slug)
    email = (request.GET.get("email") or "").strip().lower()
    orders = []
    if email:
        orders = Order.objects.filter(tenant=tenant, customer_email__iexact=email, status=Order.STATUS_PAID)
    return render(request, "ordering/orders_by_email.html", {"tenant": tenant, "email": email, "orders": orders})
