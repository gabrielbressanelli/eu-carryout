from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import get_object_or_404
from django.urls import reverse

from .models import Tenant


def accessible_tenants(user):
    if not user.is_authenticated or not user.is_active:
        return Tenant.objects.none()
    if user.is_superuser:
        return Tenant.objects.all()
    return Tenant.objects.filter(memberships__user=user)


def restaurant_access(view):
    @wraps(view)
    def wrapped(request, tenant_slug, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse("onboarding:restaurant_login", args=[tenant_slug]))
        tenant = get_object_or_404(accessible_tenants(request.user), slug=tenant_slug)
        return view(request, tenant, *args, **kwargs)

    return wrapped
