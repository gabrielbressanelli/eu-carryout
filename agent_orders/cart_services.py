from django.core.exceptions import ObjectDoesNotExist

from catalog.pricing import validate_and_price

from .models import AgentCallCartItem


class InvalidCartLineId(ValueError):
    pass


class CartLineNotFound(ObjectDoesNotExist):
    pass


UNSET = object()


def parse_line_id(line_id):
    raw = str(line_id or "").removeprefix("line_")
    if not raw.isdigit():
        raise InvalidCartLineId("Invalid line_id")
    return int(raw)


def get_cart_line(tenant, session_id, line_id, *, for_update=False):
    line_pk = parse_line_id(line_id)
    queryset = AgentCallCartItem.objects.select_related("cart", "menu_item").filter(
        pk=line_pk,
        cart__tenant=tenant,
        cart__session_id=session_id,
    )
    if for_update:
        queryset = queryset.select_for_update()
    try:
        return queryset.get()
    except AgentCallCartItem.DoesNotExist as exc:
        raise CartLineNotFound("Cart line not found") from exc


def _positive_integer(value, field_name):
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def update_cart_line(item, *, quantity=UNSET, modifier_ids=UNSET, special_instructions=UNSET):
    if quantity is UNSET and modifier_ids is UNSET and special_instructions is UNSET:
        raise ValueError("At least one cart line change is required")

    if quantity is not UNSET:
        item.quantity = _positive_integer(quantity, "quantity")
    if modifier_ids is not UNSET:
        item.unit_price, item.options = validate_and_price(item.menu_item, modifier_ids)
    if special_instructions is not UNSET:
        if not isinstance(special_instructions, str):
            raise ValueError("special_instructions must be a string")
        if len(special_instructions) > 255:
            raise ValueError("special_instructions must be 255 characters or fewer")
        item.special_instructions = special_instructions
    item.save()
    return item


def remove_cart_line(item):
    cart = item.cart
    item.delete()
    return cart
