from django.db import transaction
from django.db.models import Max
from django.core.exceptions import ObjectDoesNotExist, ValidationError

from catalog.models import MenuItem
from catalog.pricing import modifier_assignments, validate_and_price

from .matching import resolve_item_modifiers, search_menu
from .models import AgentCallCart, AgentCallCartItem, AgentOrderAction, AgentOrderSession, AgentPendingItem


def _positive_integer(value, field_name):
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a positive integer")
    if value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _session_id(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session_id must be a non-empty string")
    value = value.strip()
    if len(value) > 120:
        raise ValueError("session_id must be 120 characters or fewer")
    return value


def _special_instructions(value):
    value = "" if value is None else value
    if not isinstance(value, str):
        raise ValueError("special_instructions must be a string")
    if len(value) > 255:
        raise ValueError("special_instructions must be 255 characters or fewer")
    return value


def _action_id(value):
    if value is None:
        raise ValueError("Each action must include action_id")
    value = str(value).strip()
    if not value:
        raise ValueError("Each action must include action_id")
    if len(value) > 120:
        raise ValueError("action_id must be 120 characters or fewer")
    return value


def _external_pending_id(pending):
    return f"pending_{pending.pk}"


def _pending_pk(value):
    raw = str(value or "").removeprefix("pending_")
    if not raw.isdigit():
        raise ValueError("Invalid pending_id")
    return int(raw)


def _candidate_items(pending):
    return list(
        MenuItem.objects.filter(
            pk__in=pending.candidate_item_ids,
            tenant=pending.session.tenant,
            is_active=True,
        ).order_by("name", "pk")
    )


def _cart_item_payload(item):
    return {
        "line_id": f"line_{item.pk}",
        "item_id": item.menu_item_id,
        "item_name": item.menu_item.name,
        "quantity": item.quantity,
        "unit_price": str(item.unit_price),
        "line_total": str(item.line_total()),
        "modifiers": item.options,
        "special_instructions": item.special_instructions,
    }


def pending_item_payload(pending):
    return {
        "pending_id": _external_pending_id(pending),
        "item_id": pending.menu_item_id,
        "item_name": pending.menu_item.name if pending.menu_item else None,
        "candidate_item_ids": pending.candidate_item_ids,
        "original_phrase": pending.original_phrase,
        "quantity": pending.quantity,
        "selected_modifiers": pending.selected_modifiers,
        "missing_required_groups": pending.missing_groups,
        "special_instructions": pending.special_instructions,
        "status": pending.status,
    }


def active_pending_items(session):
    if not session:
        return []
    return [
        pending_item_payload(pending)
        for pending in session.pending_items.select_related("menu_item").filter(
            status__in=[AgentPendingItem.STATUS_NEEDS_ITEM, AgentPendingItem.STATUS_AMBIGUOUS, AgentPendingItem.STATUS_NEEDS_INPUT]
        )
    ]


def _next_sequence(session):
    return (session.pending_items.aggregate(max_sequence=Max("sequence"))["max_sequence"] or 0) + 1


def _missing_groups(item, selected_modifiers):
    selected_by_group = {}
    for modifier in selected_modifiers:
        selected_by_group.setdefault(modifier["group_id"], []).append(modifier["option_id"])

    missing = []
    for menu_group in modifier_assignments(item):
        minimum = max(1 if menu_group.effective_required() else 0, menu_group.effective_min())
        selected_count = len(selected_by_group.get(menu_group.group_id, []))
        if selected_count < minimum:
            missing.append({
                "group_id": menu_group.group_id,
                "group_name": menu_group.group.name,
                "min_choices": minimum,
            })
    return missing


def _group_for_item(item, group_id):
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        raise ValueError("group_id must be an integer")
    for menu_group in modifier_assignments(item):
        if menu_group.group_id == group_id:
            return menu_group
    raise ValueError("Modifier group is not available for this item")


def _resolve_modifier(item, action):
    group = _group_for_item(item, action.get("group_id"))
    options = [option for option in group.group.options.all() if option.is_active]
    option_id = action.get("option_id")
    if option_id is not None:
        option_id = _positive_integer(option_id, "option_id")
        option = next((option for option in options if option.pk == option_id), None)
        if not option:
            raise ValueError("Modifier option is not available for this item group")
        return group, option

    value = action.get("value")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("select_modifier requires value or option_id")
    selected, _missing = resolve_item_modifiers(value, item)
    matches = [modifier for modifier in selected if modifier["group_id"] == group.group_id]
    if not matches:
        raise ValueError(f"Could not resolve modifier {value!r} for {group.group.name}")
    option = next((option for option in options if option.pk == matches[0]["option_id"]), None)
    if not option:
        raise ValueError(f"Modifier {value!r} is not available for this item")
    return group, option


def _add_to_cart(session, item, quantity, selected_modifiers, special_instructions):
    option_ids = [modifier["option_id"] for modifier in selected_modifiers]
    unit_price, options_snapshot = validate_and_price(item, option_ids)
    cart, _created = AgentCallCart.objects.get_or_create(tenant=session.tenant, session_id=session.session_id)
    cart_item = AgentCallCartItem.objects.create(
        cart=cart,
        menu_item=item,
        quantity=quantity,
        unit_price=unit_price,
        options=options_snapshot,
        special_instructions=special_instructions,
    )
    return cart_item


def _create_pending(session, action, item=None, candidates=None, selected_modifiers=None, missing_groups=None, status=None):
    pending = AgentPendingItem.objects.create(
        session=session,
        sequence=_next_sequence(session),
        original_phrase=action["phrase"],
        menu_item=item,
        candidate_item_ids=candidates or [],
        quantity=action["quantity"],
        selected_modifiers=selected_modifiers or [],
        missing_groups=missing_groups or [],
        special_instructions=action["special_instructions"],
        status=status or AgentPendingItem.STATUS_NEEDS_INPUT,
    )
    return pending


def _pending_result(pending):
    status = "ambiguous" if pending.status in {
        AgentPendingItem.STATUS_AMBIGUOUS,
        AgentPendingItem.STATUS_NEEDS_ITEM,
    } else "needs_input"
    return {
        "success": True,
        "status": status,
        "pending_item": pending_item_payload(pending),
    }


def _added_result(pending=None, cart_item=None):
    return {
        "success": True,
        "status": "added_to_cart",
        "completed_item": _cart_item_payload(cart_item),
        "line_id": f"line_{cart_item.pk}",
        **({"pending_id": _external_pending_id(pending)} if pending else {}),
    }


def _process_add_item(session, action):
    phrase = action.get("phrase")
    if not isinstance(phrase, str) or not phrase.strip():
        return {"success": False, "error": "add_item requires a non-empty phrase"}
    action["phrase"] = phrase.strip()
    try:
        action["quantity"] = _positive_integer(action.get("quantity", 1), "quantity")
        action["special_instructions"] = _special_instructions(action.get("special_instructions", ""))
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    resolution = search_menu(action["phrase"], session.tenant)
    if resolution["match_status"] == "no_match":
        pending = _create_pending(session, action, status=AgentPendingItem.STATUS_NEEDS_ITEM, missing_groups=[{"type": "item"}])
        return _pending_result(pending)
    if resolution["match_status"] == "ambiguous":
        candidates = [row["item"].pk for row in resolution["candidates"]]
        pending = _create_pending(session, action, candidates=candidates, status=AgentPendingItem.STATUS_AMBIGUOUS, missing_groups=[{"type": "item"}])
        result = _pending_result(pending)
        result["candidates"] = [
            {"item_id": row["item"].pk, "item_name": row["item"].name}
            for row in resolution["candidates"]
        ]
        return result

    item = resolution["item"]
    selected_modifiers, missing_groups = resolve_item_modifiers(action["phrase"], item)
    if missing_groups:
        pending = _create_pending(session, action, item=item, selected_modifiers=selected_modifiers, missing_groups=missing_groups)
        return _pending_result(pending)
    try:
        cart_item = _add_to_cart(session, item, action["quantity"], selected_modifiers, action["special_instructions"])
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return _added_result(cart_item=cart_item)


def _load_pending(session, action):
    try:
        pending_id = _pending_pk(action.get("pending_id"))
    except ValueError as exc:
        return None, {"success": False, "error": str(exc)}
    try:
        pending = session.pending_items.select_for_update().select_related("menu_item").get(pk=pending_id)
    except AgentPendingItem.DoesNotExist:
        return None, {"success": False, "error": "Pending item not found"}
    if pending.status in {AgentPendingItem.STATUS_ADDED, AgentPendingItem.STATUS_CANCELLED}:
        return None, {"success": False, "error": "Pending item is no longer active"}
    return pending, None


def _process_select_item(session, action):
    pending, error = _load_pending(session, action)
    if error:
        return error
    if pending.status not in {AgentPendingItem.STATUS_AMBIGUOUS, AgentPendingItem.STATUS_NEEDS_ITEM}:
        return {"success": False, "error": "This pending item does not need an item selection"}
    item_id_value = action.get("item_id", action.get("candidate_item_id"))
    if item_id_value is None:
        candidate_name = action.get("value", action.get("item_name"))
        if isinstance(candidate_name, str) and candidate_name.strip():
            normalized_name = candidate_name.strip().casefold()
            name_matches = [
                item for item in _candidate_items(pending)
                if item.name.strip().casefold() == normalized_name
            ]
            if len(name_matches) == 1:
                item_id_value = name_matches[0].pk
            else:
                item_id_value = None
        else:
            candidate_name = None
    try:
        item_id = _positive_integer(item_id_value, "item_id")
    except ValueError as exc:
        candidates = _candidate_items(pending)
        return {
            "success": False,
            "status": "ambiguous",
            "pending_id": _external_pending_id(pending),
            "candidates": [
                {"item_id": item.pk, "item_name": item.name}
                for item in candidates
            ],
            "error": "select_item requires the exact candidate item_id or item_name",
        }
    if item_id not in pending.candidate_item_ids:
        return {"success": False, "error": "Selected item is not one of the pending candidates"}
    try:
        item = MenuItem.objects.get(pk=item_id, tenant=session.tenant, is_active=True)
    except MenuItem.DoesNotExist:
        return {"success": False, "error": "Selected menu item is no longer available"}
    selected_modifiers, missing_groups = resolve_item_modifiers(pending.original_phrase, item)
    pending.menu_item = item
    pending.candidate_item_ids = []
    pending.selected_modifiers = selected_modifiers
    pending.missing_groups = missing_groups
    if missing_groups:
        pending.status = AgentPendingItem.STATUS_NEEDS_INPUT
        pending.save()
        return _pending_result(pending)
    try:
        cart_item = _add_to_cart(session, item, pending.quantity, selected_modifiers, pending.special_instructions)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    pending.status = AgentPendingItem.STATUS_ADDED
    pending.cart_item = cart_item
    pending.save()
    return _added_result(pending=pending, cart_item=cart_item)


def _process_select_modifier(session, action):
    pending, error = _load_pending(session, action)
    if error:
        return error
    if not pending.menu_item:
        return {"success": False, "error": "Resolve the menu item before selecting modifiers"}
    try:
        group, option = _resolve_modifier(pending.menu_item, action)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    selected = [modifier for modifier in pending.selected_modifiers if modifier["option_id"] != option.pk]
    if group.effective_max() == 1:
        selected = [modifier for modifier in selected if modifier["group_id"] != group.group_id]
    selected.append({
        "group_id": group.group_id,
        "group_name": group.group.name,
        "option_id": option.pk,
        "option_name": option.name,
    })
    if group.effective_max() and len([modifier for modifier in selected if modifier["group_id"] == group.group_id]) > group.effective_max():
        return {"success": False, "error": f"Too many selections for {group.group.name}"}

    missing_groups = _missing_groups(pending.menu_item, selected)
    pending.selected_modifiers = selected
    pending.missing_groups = missing_groups
    if missing_groups:
        pending.status = AgentPendingItem.STATUS_NEEDS_INPUT
        pending.save()
        return _pending_result(pending)
    try:
        cart_item = _add_to_cart(session, pending.menu_item, pending.quantity, selected, pending.special_instructions)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    pending.status = AgentPendingItem.STATUS_ADDED
    pending.cart_item = cart_item
    pending.save()
    return _added_result(pending=pending, cart_item=cart_item)


def _process_action(session, action):
    action_type = action.get("type")
    if action_type == "add_item":
        return _process_add_item(session, action)
    if action_type == "select_item":
        return _process_select_item(session, action)
    if action_type == "select_modifier":
        return _process_select_modifier(session, action)
    return {"success": False, "error": f"Unsupported action type: {action_type or 'missing'}"}


def _order_stage(session, pending_count):
    if pending_count:
        return "needs_input"
    if AgentCallCart.objects.filter(tenant=session.tenant, session_id=session.session_id, items__isnull=False).exists():
        return "review_ready"
    return "collecting"


def next_question(session):
    pending = session.pending_items.select_related("menu_item").filter(
        status__in=[AgentPendingItem.STATUS_NEEDS_ITEM, AgentPendingItem.STATUS_AMBIGUOUS, AgentPendingItem.STATUS_NEEDS_INPUT]
    ).first()
    if not pending:
        return None
    if pending.status in {AgentPendingItem.STATUS_NEEDS_ITEM, AgentPendingItem.STATUS_AMBIGUOUS}:
        candidates = list(MenuItem.objects.filter(pk__in=pending.candidate_item_ids, tenant=session.tenant, is_active=True))
        names = [item.name for item in candidates]
        question = (
            "Which menu item did you mean? " + ", ".join(names)
            if names
            else f"I could not find {pending.original_phrase!r}. Which menu item did you mean?"
        )
        return {
            "pending_id": _external_pending_id(pending),
            "item_name": None,
            "question": question,
            "candidate_item_ids": [item.pk for item in candidates],
            "candidates": [{"item_id": item.pk, "item_name": item.name} for item in candidates],
        }
    missing = pending.missing_groups[0]
    options = []
    for menu_group in modifier_assignments(pending.menu_item):
        if menu_group.group_id == missing["group_id"]:
            options = [
                {"option_id": option.pk, "name": option.name, "option_name": option.name}
                for option in menu_group.group.options.filter(is_active=True).order_by("sort_order", "name")
            ]
            break
    item_name = pending.menu_item.name
    return {
        "pending_id": _external_pending_id(pending),
        "item_name": item_name,
        "group_id": missing["group_id"],
        "modifier_group": missing["group_name"],
        "options": options,
        "question": f"Which {missing['group_name'].lower()} would you like for the {item_name.lower()}?",
    }


def process_order_turn(tenant, payload):
    session_id = _session_id(payload.get("session_id"))
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty array")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("Each action must be an object")
        _action_id(action.get("action_id"))

    with transaction.atomic():
        session, _created = AgentOrderSession.objects.get_or_create(tenant=tenant, session_id=session_id)
        session = AgentOrderSession.objects.select_for_update().get(pk=session.pk)
        results = []
        for action in actions:
            action_id = _action_id(action["action_id"])
            stored = AgentOrderAction.objects.filter(session=session, action_id=action_id).first()
            if stored:
                results.append(stored.result)
                continue
            try:
                with transaction.atomic():
                    action_result = _process_action(session, action.copy())
                    if not action_result.get("success") and "status" not in action_result:
                        action_result["status"] = "error"
                    result = {"action_id": action_id, **action_result}
                    if result["success"]:
                        AgentOrderAction.objects.create(
                            session=session,
                            action_id=action_id,
                            action_type=str(action.get("type", "")),
                            result=result,
                        )
            except (ObjectDoesNotExist, TypeError, KeyError, ValueError, ValidationError) as exc:
                result = {
                    "action_id": action_id,
                    "success": False,
                    "status": "error",
                    "error": str(exc) or "Invalid order action",
                }
            results.append(result)
        session.save(update_fields=["updated_at"])
        pending_count = session.pending_items.filter(
            status__in=[AgentPendingItem.STATUS_NEEDS_ITEM, AgentPendingItem.STATUS_AMBIGUOUS, AgentPendingItem.STATUS_NEEDS_INPUT]
        ).count()
        return {
            "success": all(result.get("success", False) for result in results),
            "session_id": session_id,
            "results": results,
            "pending_count": pending_count,
            "next_question": next_question(session),
            "order_stage": _order_stage(session, pending_count),
        }
