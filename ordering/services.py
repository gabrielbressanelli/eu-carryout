from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from catalog.models import MenuItem
from catalog.pricing import validate_and_price
from tenants.models import Tenant
from tenants.services import run_post_payment_workflow

from .models import Order, OrderItem


def stripe_payload(value):
    """Convert stripe-python resource objects to the mapping used by our services."""
    if isinstance(value, dict):
        return value
    converter = getattr(value, "to_dict_recursive", None)
    if callable(converter):
        return converter()
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return converter()
    return dict(value)


def create_order_from_cart(cart, stripe_session_id="", customer=None, status=Order.STATUS_DRAFT, pickup_at=None):
    customer = customer or {}
    _, changes, errors = cart.review()
    if not cart.lines or errors or changes:
        raise ValueError("Review your cart before paying. Menu availability or pricing has changed.")
    with transaction.atomic():
        order = Order.objects.create(
            tenant=cart.tenant,
            pickup_at=pickup_at,
            pickup_timezone=cart.tenant.timezone,
            customer_email=customer.get("email") or "",
            customer_name=customer.get("name") or "",
            customer_phone=customer.get("phone") or "",
            status=status,
            amount_paid=cart.total(),
            order_summary=cart.order_summary(),
            stripe_session_id=stripe_session_id or None,
            paid_at=timezone.now() if status == Order.STATUS_PAID else None,
        )
        item_map = {
            item.id: item
            for item in MenuItem.objects.filter(
                id__in=[line["menu_item_id"] for line in cart.lines],
                tenant=cart.tenant,
            )
        }
        for line in cart.lines:
            item = item_map.get(line["menu_item_id"])
            if not item:
                continue
            OrderItem.objects.create(
                order=order,
                menu_item=item,
                name_snapshot=line["name"],
                quantity=line["quantity"],
                unit_price=Decimal(str(line["unit_price"])),
                options_snapshot=line.get("options", []),
                note=line.get("note", ""),
            )
    return order


def create_order_from_agent_cart(agent_cart, customer=None, pickup_at=None):
    customer = customer or {}
    cart_items = list(agent_cart.items.select_related("menu_item", "menu_item__category").order_by("created_at", "id"))
    if not cart_items:
        raise ValueError("Agent cart is empty.")

    validated_items = []
    total = Decimal("0.00")
    summary_parts = []
    for cart_item in cart_items:
        option_ids = [option.get("id") for option in cart_item.options if option.get("id") is not None]
        unit_price, options = validate_and_price(cart_item.menu_item, option_ids)
        total += unit_price * cart_item.quantity
        summary_parts.append(f"{cart_item.quantity}x {cart_item.menu_item.name};")
        summary_parts.extend(f"- {option['name']};" for option in options)
        if cart_item.special_instructions:
            summary_parts.append(f"- {cart_item.special_instructions};")
        validated_items.append((cart_item, unit_price, options))

    with transaction.atomic():
        order = Order.objects.create(
            tenant=agent_cart.tenant,
            pickup_at=pickup_at,
            pickup_timezone=agent_cart.tenant.timezone,
            customer_email=customer.get("email") or "",
            customer_name=customer.get("name") or "",
            customer_phone=customer.get("phone") or "",
            amount_paid=total.quantize(Decimal("0.01")),
            order_summary=" ".join(summary_parts),
        )
        for cart_item, unit_price, options in validated_items:
            OrderItem.objects.create(
                order=order,
                menu_item=cart_item.menu_item,
                name_snapshot=cart_item.menu_item.name,
                quantity=cart_item.quantity,
                unit_price=unit_price,
                options_snapshot=options,
                note=cart_item.special_instructions,
            )
    return order


def mark_stripe_order_paid(session):
    session = stripe_payload(session)
    metadata = session.get("metadata") or {}
    tenant_id = metadata.get("tenant_id")
    if not tenant_id:
        raise ValueError("Stripe session is missing tenant_id metadata.")

    tenant = Tenant.objects.filter(id=tenant_id, is_active=True).first()
    if not tenant:
        raise ValueError(f"Stripe session has unknown tenant_id {tenant_id!r}.")
    customer_details = session.get("customer_details") or {}
    amount_paid = (Decimal(session.get("amount_total") or 0) / Decimal("100")).quantize(Decimal("0.01"))
    stripe_session_id = session.get("id")

    order = None
    order_id = metadata.get("order_id")
    if order_id:
        order = Order.objects.filter(id=order_id, tenant=tenant).first()

    if order:
        order.stripe_session_id = stripe_session_id
        order.tenant = tenant
        order.customer_email = customer_details.get("email") or order.customer_email
        order.customer_name = customer_details.get("name") or order.customer_name or "Guest"
        order.customer_phone = customer_details.get("phone") or order.customer_phone
        order.status = Order.STATUS_PAID
        order.amount_paid = amount_paid
        order.stripe_payment_intent_id = session.get("payment_intent") or ""
        order.paid_at = timezone.now()
        order.save()
    else:
        order, _created = Order.objects.update_or_create(
            stripe_session_id=stripe_session_id,
            defaults={
                "tenant": tenant,
                "customer_email": customer_details.get("email") or metadata.get("customer_email") or "",
                "customer_name": customer_details.get("name") or metadata.get("customer_name") or "Guest",
                "customer_phone": customer_details.get("phone") or metadata.get("customer_phone") or "",
                "status": Order.STATUS_PAID,
                "amount_paid": amount_paid,
                "order_summary": metadata.get("order_summary", ""),
                "stripe_payment_intent_id": session.get("payment_intent") or "",
                "paid_at": timezone.now(),
            },
        )
    if metadata.get("order_source") != "agent":
        run_post_payment_workflow(order)
    return order
