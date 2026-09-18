import logging

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from botocore.exceptions import BotoCoreError, ClientError

from catalog.models import MenuCategory, MenuItem, MenuItemModifierGroup, ModifierGroup, ModifierOption

from .access import accessible_tenants, can_create_restaurants, can_manage_tenant_access, restaurant_access
from .forms import (
    BusinessHourFormSet, MenuCategoryForm, MenuItemForm, MenuItemModifierGroupForm,
    ModifierGroupForm, ModifierOptionEditorForm, RestaurantAccessForm,
    RestaurantAccountForm, RestaurantCreateAccessForm, RestaurantLoginForm, TenantIntegrationFormSet, TenantOnboardingForm,
    OrderingSettingsForm, HoursOverrideForm,
)
from .models import BusinessHour, Tenant, TenantIntegration, HoursOverride
from .services import ensure_tenant_onboarding_defaults


log = logging.getLogger(__name__)

SECTIONS = [("overview", "Business"), ("orders", "Orders"), ("hours", "Business hours"), ("menu", "Menu items"),
            ("categories", "Categories"), ("modifiers", "Modifier groups"),
            ("options", "Modifier options"), ("links", "Item assignments"), ("services", "Services")]
EDITORS = {
    "hours_override": (HoursOverride, HoursOverrideForm, "hours", "special hours"),
    "category": (MenuCategory, MenuCategoryForm, "categories", "category"),
    "item": (MenuItem, MenuItemForm, "menu", "menu item"),
    "group": (ModifierGroup, ModifierGroupForm, "modifiers", "modifier group"),
    "option": (ModifierOption, ModifierOptionEditorForm, "options", "modifier option"),
    "link": (MenuItemModifierGroup, MenuItemModifierGroupForm, "links", "item assignment"),
}


def _manage_url(tenant, section="overview"):
    return reverse("onboarding:restaurant_manage", args=[tenant.slug]) + "?section=" + section


def _context(tenant, section):
    return {"tenant": tenant, "section": section, "sections": SECTIONS}


def restaurant_login(request, tenant_slug=None):
    if request.user.is_authenticated:
        return redirect("onboarding:restaurant_list")
    tenant = get_object_or_404(Tenant, slug=tenant_slug) if tenant_slug else None
    form = RestaurantLoginForm(request, data=request.POST or None, tenant=tenant)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        target = request.POST.get("next", "")
        if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
            return redirect(target)
        return redirect("onboarding:restaurant_list")
    return render(request, "onboarding/login.html", {"form": form, "tenant": tenant, "login_page": True, "next": request.GET.get("next", request.POST.get("next", ""))})


@require_POST
def restaurant_logout(request):
    logout(request)
    return redirect("onboarding:login")


@login_required(login_url="onboarding:login")
def restaurant_list(request):
    tenants = list(
        accessible_tenants(request.user)
        .select_related("account")
        .order_by("account__name", "name")
        .prefetch_related("menu_items", "integrations")
    )
    account_groups = []
    for tenant in tenants:
        if not account_groups or account_groups[-1]["account"] != tenant.account:
            account_groups.append({"account": tenant.account, "tenants": []})
        account_groups[-1]["tenants"].append(tenant)
    return render(
        request,
        "onboarding/index.html",
        {"tenants": tenants, "account_groups": account_groups, "can_create_restaurants": can_create_restaurants(request.user)},
    )


@login_required(login_url="onboarding:login")
def restaurant_create(request):
    if not can_create_restaurants(request.user):
        raise PermissionDenied
    form = TenantOnboardingForm(request.POST or None, request.FILES or None, prefix="tenant", initial={"timezone": "America/Detroit", "is_active": True})
    account_form = RestaurantAccountForm(request.POST or None, prefix="account", user=request.user)
    login_required = not account_form.fields["account"].queryset.exists()
    if request.method == "POST":
        valid = form.is_valid()
        valid = account_form.is_valid() and valid
        login_required = not bool(account_form.cleaned_data.get("account")) if account_form.is_valid() else True
    access_form = RestaurantCreateAccessForm(request.POST or None, prefix="owner", login_required=login_required)
    if request.method == "POST":
        valid = access_form.is_valid() and valid
        if valid:
            try:
                with transaction.atomic():
                    account = account_form.save()
                    tenant = form.save(commit=False)
                    tenant.account = account
                    tenant.save()
                    ensure_tenant_onboarding_defaults(tenant)
                    owner = access_form.save(tenant)
                    account_form.grant_owner_if_created(owner, account)
            except IntegrityError:
                form.add_error(None, "This restaurant or login already exists. Check the details and try again.")
            except (BotoCoreError, ClientError, OSError):
                log.exception("Restaurant logo upload failed while creating a restaurant.")
                form.add_error("logo", "The image could not be uploaded. Please choose the file and try again.")
            else:
                messages.success(request, "Restaurant created.")
                return redirect(_manage_url(tenant))
    return render(request, "onboarding/create.html", {"form": form, "account_form": account_form, "access_form": access_form})


def _validate_formset_identity(data, queryset, prefix):
    # Formset management fields and row IDs are untrusted, even on an authorized location.
    expected = {str(pk) for pk in queryset.values_list("pk", flat=True)}
    if data.get(f"{prefix}-INITIAL_FORMS") != str(len(expected)) or data.get(f"{prefix}-TOTAL_FORMS") != str(len(expected)):
        raise PermissionDenied
    submitted = {data.get(f"{prefix}-{index}-id") for index in range(len(expected))}
    if submitted != expected:
        raise PermissionDenied


@restaurant_access
def restaurant_manage(request, tenant):
    ensure_tenant_onboarding_defaults(tenant)
    section = request.GET.get("section", "overview")
    if section not in dict(SECTIONS) and section != "access":
        return redirect(_manage_url(tenant))
    can_manage_access = can_manage_tenant_access(request.user, tenant)
    if section == "access" and not can_manage_access:
        raise PermissionDenied
    context = _context(tenant, section)
    context["can_manage_access"] = can_manage_access
    action = request.POST.get("action")
    if request.method == "POST" and action not in {"business", "hours", "services", "access", "revoke_access", "ordering"}:
        raise PermissionDenied
    context["tenant_form"] = TenantOnboardingForm(instance=tenant, prefix="tenant")
    context["access_form"] = RestaurantAccessForm(prefix="owner")
    context["ordering_form"] = OrderingSettingsForm(instance=tenant, prefix="ordering")
    if request.method == "POST" and action == "ordering":
        form = OrderingSettingsForm(request.POST, instance=tenant, prefix="ordering")
        context.update(ordering_form=form, section="hours")
        if form.is_valid():
            form.save()
            messages.success(request, "Ordering availability saved.")
            return redirect(_manage_url(tenant, "hours"))
    if request.method == "POST" and action == "business":
        form = TenantOnboardingForm(request.POST, request.FILES, instance=tenant, prefix="tenant")
        context.update(tenant_form=form, section="overview")
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
            except IntegrityError:
                form.add_error(None, "This restaurant URL or domain is already in use.")
            except (BotoCoreError, ClientError, OSError):
                log.exception("Restaurant logo upload failed.", extra={"tenant_id": tenant.pk, "tenant_slug": tenant.slug})
                form.add_error("logo", "The image could not be uploaded. Please choose the file and try again.")
            else:
                messages.success(request, "Business profile saved.")
                return redirect(_manage_url(tenant))
    for key, model, factory, prefix, ordering in [
        ("hours", BusinessHour, BusinessHourFormSet, "hours", "day_of_week"),
        ("services", TenantIntegration, TenantIntegrationFormSet, "integrations", "kind"),
    ]:
        queryset = model.objects.filter(tenant=tenant).order_by(ordering)
        bound = request.method == "POST" and action == key
        if bound:
            _validate_formset_identity(request.POST, queryset, prefix)
        formset = factory(request.POST if bound else None, queryset=queryset, prefix=prefix)
        context["hours_formset" if key == "hours" else "integration_formset"] = formset
        if bound:
            context["section"] = key
            if formset.is_valid():
                with transaction.atomic():
                    formset.save()
                messages.success(request, f"{key.capitalize()} saved.")
                return redirect(_manage_url(tenant, key))
    if request.method == "POST" and action in {"access", "revoke_access"}:
        if not can_manage_access:
            raise PermissionDenied
        context["section"] = "access"
        if action == "revoke_access":
            membership = get_object_or_404(tenant.memberships, pk=request.POST.get("membership_id"))
            membership.delete()
            messages.success(request, "Location access removed.")
            return redirect(_manage_url(tenant, "access"))
        form = RestaurantAccessForm(request.POST, prefix="owner")
        context["access_form"] = form
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save(tenant)
            except IntegrityError:
                form.add_error(None, "This login changed while saving. Please try again.")
            else:
                messages.success(request, "Location access granted.")
                return redirect(_manage_url(tenant, "access"))
    context.update(
        hours_overrides=tenant.hours_overrides.all(),
        categories=MenuCategory.objects.filter(tenant=tenant).prefetch_related("items"),
        menu_items=MenuItem.objects.filter(tenant=tenant).select_related("category").prefetch_related("modifier_groups__group"),
        modifier_groups=ModifierGroup.objects.filter(tenant=tenant).prefetch_related("options", "menuitemmodifiergroup_set"),
        options=ModifierOption.objects.filter(group__tenant=tenant).select_related("group"),
        item_links=MenuItemModifierGroup.objects.filter(menu_item__tenant=tenant, group__tenant=tenant).select_related("menu_item", "group"),
        memberships=tenant.memberships.select_related("user") if can_manage_access else [],
    )
    return render(request, "onboarding/manage.html", context)


def _editor_queryset(model, tenant):
    if model == ModifierOption:
        return model.objects.filter(group__tenant=tenant)
    if model == MenuItemModifierGroup:
        return model.objects.filter(menu_item__tenant=tenant, group__tenant=tenant)
    return model.objects.filter(tenant=tenant)


@restaurant_access
def catalog_edit(request, tenant, kind, object_id=None):
    if kind not in EDITORS:
        raise PermissionDenied
    model, form_class, section, label = EDITORS[kind]
    instance = get_object_or_404(_editor_queryset(model, tenant), pk=object_id) if object_id else model()
    if kind in {"category", "item", "group", "hours_override"}:
        instance.tenant = tenant
    kwargs = {"tenant": tenant} if kind in {"item", "option", "link"} else {}
    initial = {}
    for field, related_model in [("menu_item", MenuItem), ("group", ModifierGroup), ("category", MenuCategory)]:
        value = request.GET.get(field)
        if value and value.isdecimal() and field in form_class.base_fields:
            related = related_model.objects.filter(tenant=tenant, pk=value).first()
            if related:
                initial[field] = related.pk
    form = form_class(request.POST or None, request.FILES or None, instance=instance, initial=initial, **kwargs)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except IntegrityError:
            form.add_error(None, "A record with these details already exists in this location.")
        except (BotoCoreError, ClientError, OSError):
            log.exception(
                "Catalog image upload failed.",
                extra={"tenant_id": tenant.pk, "tenant_slug": tenant.slug, "catalog_kind": kind, "object_id": object_id},
            )
            form.add_error(None, "The image could not be uploaded. Please choose the file and try again.")
        else:
            messages.success(request, f"{label.capitalize()} saved.")
            return redirect(_manage_url(tenant, section))
    context = _context(tenant, section)
    context.update(form=form, label=label, kind=kind, record=instance, editing=bool(object_id), back_url=_manage_url(tenant, section))
    return render(request, "onboarding/editor.html", context)


@restaurant_access
def catalog_delete(request, tenant, kind, object_id):
    if kind not in EDITORS:
        raise PermissionDenied
    model, _, section, label = EDITORS[kind]
    instance = get_object_or_404(_editor_queryset(model, tenant), pk=object_id)
    error = ""
    if request.method == "POST":
        try:
            with transaction.atomic():
                instance.delete()
        except ProtectedError:
            error = "This record is in use. Move its menu items or mark it inactive to preserve existing orders."
        else:
            messages.success(request, f"{label.capitalize()} removed.")
            return redirect(_manage_url(tenant, section))
    context = _context(tenant, section)
    context.update(object=instance, label=label, kind=kind, error=error, back_url=_manage_url(tenant, section))
    return render(request, "onboarding/delete.html", context)
