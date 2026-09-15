import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from catalog.models import MenuItem
from catalog.pricing import validate_and_price
from tenants.services import get_request_tenant


class Cart:
    SESSION_PREFIX = "cart"

    def __init__(self, request):
        self.tenant = get_request_tenant(request)
        self.session = request.session
        self.session_key = f"cart:{self.tenant.id}"
        self.lines = self.session.get(self.session_key) or []
        if not isinstance(self.lines, list):
            self.lines = []
        changed = False
        for line in self.lines:
            if not line.get("key"):
                line["key"] = uuid4().hex
                changed = True
        if changed:
            self.save()

    def save(self):
        self.session[self.session_key] = self.lines
        self.session.modified = True

    def __len__(self):
        return sum(line["quantity"] for line in self.lines)

    @staticmethod
    def quantity(value):
        try:
            if isinstance(value, bool) or str(int(value)) != str(value).strip():
                raise ValueError
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("Quantity must be a whole number.")
        if not 1 <= value <= 99:
            raise ValueError("Choose a quantity from 1 to 99.")
        return value

    def find(self, key=None, index=None):
        if key:
            line = next((line for line in self.lines if line["key"] == key), None)
            if line is not None:
                return line
        elif index is not None:
            try:
                index = int(index)
                if 0 <= index < len(self.lines):
                    return self.lines[index]
            except (ValueError, TypeError):
                pass
        raise ValueError("This cart item has changed. Refresh your cart.")

    def add(self, item, quantity=1, selected_option_ids=None, note="", replace_key=None):
        if item.tenant_id != self.tenant.id:
            raise ValueError("This item does not belong to this restaurant.")
        quantity = self.quantity(quantity)
        note = (note or "").strip()
        if len(note) > 255:
            raise ValueError("Special instructions must be 255 characters or fewer.")
        price, options = validate_and_price(item, selected_option_ids)
        replacing = self.find(key=replace_key) if replace_key else None
        ids = sorted(option["id"] for option in options)
        match = next((line for line in self.lines if line is not replacing and line["menu_item_id"] == item.pk
                      and sorted(option["id"] for option in line.get("options", [])) == ids
                      and line.get("note", "") == note), None)
        if match:
            quantity = self.quantity(match["quantity"] + quantity)
        if replacing:
            self.lines.remove(replacing)
        if match:
            match.update(quantity=quantity, unit_price=str(price), options=options, name=item.name)
        else:
            self.lines.append({"key": replace_key or uuid4().hex, "menu_item_id": item.pk, "name": item.name,
                               "unit_price": str(price), "quantity": quantity, "options": options, "note": note})
        self.save()

    def update(self, line_index=None, quantity=1, key=None):
        line = self.find(key=key, index=line_index)
        if str(quantity) == "0":
            self.lines.remove(line)
        else:
            line["quantity"] = self.quantity(quantity)
        self.save()

    def delete(self, line_index=None, key=None):
        self.lines.remove(self.find(key=key, index=line_index))
        self.save()

    def clear(self):
        self.lines = []
        self.save()

    @property
    def revision(self):
        return hashlib.sha256(json.dumps(self.lines, sort_keys=True).encode()).hexdigest()

    def review(self):
        items = {item.pk: item for item in MenuItem.objects.filter(
            tenant=self.tenant, pk__in=[line["menu_item_id"] for line in self.lines]).select_related("category")}
        changes, errors, rows = [], [], []
        for index, line in enumerate(self.lines):
            item = items.get(line["menu_item_id"])
            old_price = line["unit_price"]
            line.pop("error", None)
            try:
                if not item:
                    raise ValueError("This menu item is no longer available.")
                self.quantity(line["quantity"])
                price, options = validate_and_price(item, [option["id"] for option in line.get("options", [])])
                line.update(name=item.name, unit_price=str(price), options=options)
                if old_price != str(price):
                    changes.append(f"{item.name}: price updated to ${price}.")
            except ValueError as exc:
                line["error"] = str(exc)
                errors.append(f"{line['name']}: {exc}")
            rows.append({**line, "index": index, "item": item, "unit_price": Decimal(line["unit_price"]),
                         "line_total": Decimal(line["unit_price"]) * line["quantity"]})
        self.save()
        return rows, changes, errors

    def total(self):
        return sum((Decimal(line["unit_price"]) * line["quantity"] for line in self.lines if not line.get("error")), Decimal("0.00")).quantize(Decimal("0.01"))

    def hydrated_lines(self):
        return self.review()[0]

    def order_summary(self):
        parts = []
        for line in self.lines:
            parts.append(f"{line['quantity']}x {line['name']};")
            parts.extend(f"- {option['name']};" for option in line.get("options", []))
            if line.get("note"):
                parts.append(f"- {line['note']};")
        return " ".join(parts)
