import re
from decimal import Decimal
from difflib import SequenceMatcher

from catalog.models import MenuItem, ModifierOption

ITEM_BOUNDARY_RE = re.compile(r"(\d+)\s*x\s+", re.IGNORECASE)
MIN_ITEM_SCORE = 0.74
MIN_OPTION_SCORE = 0.72


def _score(a, b):
    a = " ".join((a or "").lower().split())
    b = " ".join((b or "").lower().split())
    if not a or not b:
        return 0
    if a == b:
        return 1
    if a in b or b in a:
        return 0.88
    return SequenceMatcher(None, a, b).ratio()


def _split_chunks(summary):
    matches = list(ITEM_BOUNDARY_RE.finditer(summary or ""))
    chunks = []
    for index, match in enumerate(matches):
        qty = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(summary or "")
        chunks.append((qty, (summary or "")[start:end].strip()))
    return chunks


def _match_item(name, tenant):
    best_item = None
    best_score = 0
    for item in MenuItem.objects.filter(tenant=tenant, is_active=True).prefetch_related("aliases"):
        candidates = [item.name, item.description] + [alias.alias for alias in item.aliases.all()]
        score = max(_score(name, candidate) for candidate in candidates)
        if score > best_score:
            best_item = item
            best_score = score
    return best_item if best_score >= MIN_ITEM_SCORE else None


def _match_option(menu_item, value):
    options = ModifierOption.objects.filter(
        group__tenant=menu_item.tenant,
        group__menuitemmodifiergroup__menu_item=menu_item,
        is_active=True,
    )
    best_option = None
    best_score = 0
    for option in options:
        score = _score(value, option.name)
        if score > best_score:
            best_option = option
            best_score = score
    return best_option if best_score >= MIN_OPTION_SCORE else None


def compute_total_from_summary(order_summary, tenant):
    total = Decimal("0.00")
    warnings = []
    chunks = _split_chunks(order_summary)
    if not chunks:
        warnings.append("Could not find any 'Nx Item' entries in order_summary.")
        return total, warnings

    for quantity, chunk in chunks:
        segments = [segment.strip().lstrip("-").strip() for segment in chunk.split(";") if segment.strip()]
        if not segments:
            warnings.append("Found a quantity with no item name.")
            continue

        item = _match_item(segments[0], tenant)
        if not item:
            warnings.append(f"Could not match item: {segments[0]!r}")
            continue

        unit_price = item.price
        for modifier_text in segments[1:]:
            option = _match_option(item, modifier_text)
            if not option:
                warnings.append(f"Could not match modifier {modifier_text!r} for item {item.name!r}")
                continue
            unit_price += option.price_delta
        total += unit_price * quantity

    return total.quantize(Decimal("0.01")), warnings
