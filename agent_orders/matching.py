import re
from difflib import SequenceMatcher

from django.db.models import Q

from catalog.models import MenuCategory, MenuItem
from catalog.pricing import modifier_assignments

MATCH_SCORE_MIN = 0.82
AMBIGUOUS_SCORE_MIN = 0.62
MATCH_LEAD_MARGIN = 0.12
MAX_RESULTS = 4


def _normalize(value):
    return " ".join((value or "").lower().strip().split())


def _score(query, candidate):
    query = _normalize(query)
    candidate = _normalize(candidate)
    if not query or not candidate:
        return 0
    if query == candidate:
        return 1
    if query in candidate or candidate in query:
        shorter = min(len(query), len(candidate))
        longer = max(len(query), len(candidate))
        return max(0.78, shorter / longer)
    query_tokens = set(query.split())
    candidate_tokens = set(candidate.split())
    token_score = len(query_tokens & candidate_tokens) / max(len(query_tokens), 1)
    ratio_score = SequenceMatcher(None, query, candidate).ratio()
    return max(token_score, ratio_score)


def _tagged_items(tenant, dietary_tags):
    qs = MenuItem.objects.filter(tenant=tenant, is_active=True)
    for tag in dietary_tags:
        qs = qs.filter(dietary_tags__slug=tag)
    return qs.distinct()


def _item_score(query, item):
    candidates = [item.name, item.description] + [alias.alias for alias in item.aliases.all()]
    base_score = max(_score(query, candidate) for candidate in candidates)
    option_scores = []
    for link in modifier_assignments(item):
        for option in link.group.options.all():
            option_candidates = [option.name] + [alias.alias for alias in option.aliases.all()]
            option_scores.append(max(_score(query, candidate) for candidate in option_candidates))

    strong_options = [score for score in option_scores if score >= AMBIGUOUS_SCORE_MIN]
    if len(strong_options) >= 2:
        base_score = max(base_score, min(1, max(strong_options) + 0.14 * min(len(strong_options) - 1, 3)))
    elif strong_options:
        base_score = max(base_score, min(0.78, max(strong_options)))
    return base_score


def _explicit_item_match(query, items):
    """Find one item whose name or alias is explicitly present in the phrase."""
    matches = {}
    for item in items:
        candidates = [item.name] + [alias.alias for alias in item.aliases.all()]
        matched_lengths = []
        for candidate in candidates:
            candidate = _normalize(candidate)
            if candidate and re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", query):
                matched_lengths.append(len(candidate))
        if matched_lengths:
            matches[item.id] = max(matched_lengths)

    if not matches:
        return None
    longest = max(matches.values())
    best_ids = [item_id for item_id, length in matches.items() if length == longest]
    if len(best_ids) != 1:
        return None
    return next(item for item in items if item.id == best_ids[0])


def resolve_item_modifiers(query, item):
    """Resolve modifier names explicitly spoken in a menu search phrase."""
    selected = []
    missing_required_groups = []
    normalized_query = _normalize(query)

    for menu_group in modifier_assignments(item):
        option_matches = {}
        for option in menu_group.group.options.all():
            if not option.is_active:
                continue
            candidates = [option.name] + [alias.alias for alias in option.aliases.all()]
            for candidate in candidates:
                candidate = _normalize(candidate)
                if not candidate:
                    continue
                match = re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", normalized_query)
                if not match:
                    continue
                current = option_matches.get(option.id)
                entry = {"option": option, "span": match.span(), "length": len(candidate)}
                if not current or entry["length"] > current["length"]:
                    option_matches[option.id] = entry

        matches = sorted(
            option_matches.values(),
            key=lambda entry: (-entry["length"], entry["span"][0], entry["option"].sort_order, entry["option"].name),
        )
        chosen = []
        for entry in matches:
            if any(
                entry["span"][0] < chosen_entry["span"][1]
                and entry["span"][1] > chosen_entry["span"][0]
                for chosen_entry in chosen
            ):
                continue
            chosen.append(entry)
            if menu_group.effective_max() == 1:
                break

        for entry in chosen:
            option = entry["option"]
            selected.append({
                "group_id": menu_group.group_id,
                "group_name": menu_group.group.name,
                "option_id": option.id,
                "option_name": option.name,
            })

        minimum = max(1 if menu_group.effective_required() else 0, menu_group.effective_min())
        if len(chosen) < minimum:
            missing_required_groups.append({
                "group_id": menu_group.group_id,
                "group_name": menu_group.group.name,
                "min_choices": minimum,
            })

    return selected, missing_required_groups


def search_menu(query, tenant, dietary_tags=None):
    query = _normalize(query)
    if not query:
        return {"match_status": "no_match", "query": query}

    exact = _tagged_items(tenant, dietary_tags or []).filter(
        Q(name__iexact=query) | Q(aliases__alias__iexact=query)
    ).distinct()
    if exact.count() == 1:
        return {"match_status": "matched", "item": exact.first(), "confidence": 1}

    best_by_item = {}
    items = list(_tagged_items(tenant, dietary_tags or []).prefetch_related(
        "aliases", "category", "modifier_groups__group__options__aliases",
        "category__modifier_groups__group__options__aliases",
    ))
    explicit_item = _explicit_item_match(query, items)
    if explicit_item:
        return {"match_status": "matched", "item": explicit_item, "confidence": 1}
    for item in items:
        best_by_item[item.id] = (item, _item_score(query, item))

    ranked = sorted(best_by_item.values(), key=lambda row: row[1], reverse=True)
    if not ranked or ranked[0][1] < AMBIGUOUS_SCORE_MIN:
        return {"match_status": "no_match", "query": query}

    top_item, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    if top_score >= MATCH_SCORE_MIN and top_score - second_score >= MATCH_LEAD_MARGIN:
        return {"match_status": "matched", "item": top_item, "confidence": round(top_score, 2)}

    candidates = [
        {"item": item, "confidence": round(score, 2)}
        for item, score in ranked[:MAX_RESULTS]
        if score >= AMBIGUOUS_SCORE_MIN
    ]
    if len(candidates) == 1:
        return {
            "match_status": "matched",
            "item": candidates[0]["item"],
            "confidence": candidates[0]["confidence"],
        }
    return {"match_status": "ambiguous", "query": query, "candidates": candidates}


def search_menu_by_category(query, tenant, dietary_tags=None):
    query = _normalize(query)
    qs = _tagged_items(tenant, dietary_tags or []).select_related("category")
    if not query:
        return qs.order_by("category__sort_order", "sort_order", "name")

    category_ids = []
    for category in MenuCategory.objects.filter(tenant=tenant, is_active=True):
        if _score(query, category.name) >= AMBIGUOUS_SCORE_MIN or _score(query, category.slug) >= AMBIGUOUS_SCORE_MIN:
            category_ids.append(category.id)

    return qs.filter(
        Q(category_id__in=category_ids)
        | Q(name__icontains=query)
        | Q(description__icontains=query)
        | Q(aliases__alias__icontains=query)
    ).distinct().order_by("category__sort_order", "sort_order", "name")
