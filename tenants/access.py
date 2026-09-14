from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.db import models
from django.shortcuts import get_object_or_404
from django.urls import reverse

from .models import Account, Tenant


def accessible_tenants(user):
    if not user.is_authenticated or not user.is_active:
        return Tenant.objects.none()
    if user.is_superuser:
        return Tenant.objects.all()
    return Tenant.objects.filter(
        models.Q(memberships__user=user) | models.Q(account__memberships__user=user)
    ).distinct()


def manageable_accounts(user):
    if not user.is_authenticated or not user.is_active:
        return Account.objects.none()
    if user.is_superuser:
        return Account.objects.all()
    return Account.objects.filter(memberships__user=user).distinct()


def can_create_restaurants(user):
    return user.is_authenticated and user.is_active and (user.is_superuser or manageable_accounts(user).exists())


def can_manage_account(user, account):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return account.memberships.filter(user=user).exists()


def can_manage_tenant_access(user, tenant):
    return can_manage_account(user, tenant.account)


def restaurant_access(view):
    @wraps(view)
    def wrapped(request, tenant_slug, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse("onboarding:restaurant_login", args=[tenant_slug]))
        tenant = get_object_or_404(accessible_tenants(request.user), slug=tenant_slug)
        return view(request, tenant, *args, **kwargs)

    return wrapped
