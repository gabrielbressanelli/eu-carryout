from django.contrib import admin

from .models import BusinessHour, IntegrationEvent, Tenant, TenantDomain, TenantIntegration, TenantMembership
from .models import HoursOverride

@admin.register(HoursOverride)
class HoursOverrideAdmin(admin.ModelAdmin):
    list_display = ("tenant", "date", "is_closed", "opens_at", "closes_at")
    list_filter = ("tenant", "is_closed")


@admin.register(TenantMembership)
class TenantMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "created_at")
    search_fields = ("user__username", "tenant__name")
    autocomplete_fields = ("user", "tenant")


class TenantDomainInline(admin.TabularInline):
    model = TenantDomain
    extra = 1


class TenantIntegrationInline(admin.TabularInline):
    model = TenantIntegration
    extra = 0


class BusinessHourInline(admin.TabularInline):
    model = BusinessHour
    extra = 0


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "primary_domain", "business_email", "business_phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "primary_domain", "business_email", "business_phone")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [TenantDomainInline, BusinessHourInline, TenantIntegrationInline]


@admin.register(TenantDomain)
class TenantDomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "tenant", "is_primary")
    list_filter = ("is_primary",)
    search_fields = ("domain", "tenant__name", "tenant__slug")


@admin.register(TenantIntegration)
class TenantIntegrationAdmin(admin.ModelAdmin):
    list_display = ("tenant", "kind", "enabled", "endpoint_url")
    list_filter = ("kind", "enabled", "tenant")
    search_fields = ("tenant__name", "endpoint_url")


@admin.register(BusinessHour)
class BusinessHourAdmin(admin.ModelAdmin):
    list_display = ("tenant", "day_of_week", "opens_at", "closes_at", "is_closed")
    list_filter = ("tenant", "is_closed")
    search_fields = ("tenant__name", "tenant__slug")


@admin.register(IntegrationEvent)
class IntegrationEventAdmin(admin.ModelAdmin):
    list_display = ("order", "tenant", "kind", "status", "attempts", "response_status", "updated_at")
    list_filter = ("tenant", "kind", "status")
    search_fields = ("order__customer_email", "order__customer_name", "tenant__name")
    readonly_fields = (
        "tenant",
        "order",
        "kind",
        "status",
        "attempts",
        "request_payload",
        "response_status",
        "response_body",
        "last_error",
        "created_at",
        "updated_at",
        "sent_at",
    )
