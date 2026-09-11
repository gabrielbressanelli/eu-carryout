from decimal import Decimal, ROUND_HALF_UP

from .models import MenuItem, ModifierOption


def validate_and_price(menu_item: MenuItem, selected_option_ids: list[int] | None = None):
    if not menu_item.is_active or not menu_item.category.is_active or menu_item.category.tenant_id != menu_item.tenant_id:
        raise ValueError("This menu item is no longer available.")
    selected_option_ids = selected_option_ids or []
    try:
        selected_option_ids = [int(option_id) for option_id in selected_option_ids]
    except (TypeError, ValueError):
        raise ValueError("Modifier option IDs must be integers")
    if len(selected_option_ids) != len(set(selected_option_ids)):
        raise ValueError("Duplicate modifier option IDs are not allowed")

    menu_groups = list(
        menu_item.modifier_groups
        .select_related("group")
        .prefetch_related("group__options")
        .order_by("sort_order")
    )
    allowed_group_ids = [menu_group.group_id for menu_group in menu_groups]
    selected = list(
        ModifierOption.objects.filter(
            id__in=selected_option_ids,
            group_id__in=allowed_group_ids,
            group__tenant=menu_item.tenant,
            is_active=True,
        ).select_related("group")
    )
    selected_ids = {option.id for option in selected}
    unknown_ids = set(selected_option_ids) - selected_ids
    if unknown_ids:
        formatted_ids = ", ".join(str(option_id) for option_id in sorted(unknown_ids))
        raise ValueError(f"Unknown modifier option ID(s) for this item: {formatted_ids}")

    selected_by_group = {}
    for option in selected:
        selected_by_group.setdefault(option.group_id, []).append(option)

    for menu_group in menu_groups:
        chosen = selected_by_group.get(menu_group.group_id, [])
        required = menu_group.effective_required()
        minimum = menu_group.effective_min()
        maximum = menu_group.effective_max()

        if required and not chosen:
            raise ValueError(f"Missing required selection: {menu_group.group.name}")
        if minimum and len(chosen) < minimum:
            raise ValueError(f"Select at least {minimum} option(s) for {menu_group.group.name}")
        if maximum and len(chosen) > maximum:
            raise ValueError(f"Select at most {maximum} option(s) for {menu_group.group.name}")

    multiplier = Decimal("1.00")
    for option in selected:
        multiplier *= option.price_multiplier
    unit_price = menu_item.price * multiplier + sum((option.price_delta for option in selected), start=Decimal("0.00"))
    if unit_price < 0 or unit_price > Decimal("99999999.99"):
        raise ValueError("These selections produce an invalid price. Please contact the restaurant.")
    options_snapshot = [
        {
            "id": option.id,
            "name": option.name,
            "group": option.group.name,
            "price_delta": str(option.price_delta),
            "price_multiplier": str(option.price_multiplier),
            "effective_delta": str(effective_option_delta(menu_item, option)),
        }
        for option in selected
    ]
    return unit_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), options_snapshot


def effective_option_delta(menu_item, option):
    return (menu_item.price * (option.price_multiplier - 1) + option.price_delta).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def modifier_payload(menu_item):
    groups = []
    for link in menu_item.modifier_groups.select_related("group").prefetch_related("group__options"):
        if link.group.tenant_id != menu_item.tenant_id:
            continue
        groups.append({
            "id": link.group_id, "name": link.group.name,
            "required": link.effective_required(), "min_choices": link.effective_min(), "max_choices": link.effective_max(),
            "options": [{"id": option.pk, "name": option.name, "price_delta": str(option.price_delta),
                         "price_multiplier": str(option.price_multiplier), "effective_delta": str(effective_option_delta(menu_item, option)),
                         "is_default": option.is_default} for option in link.group.options.all() if option.is_active],
        })
    return groups
