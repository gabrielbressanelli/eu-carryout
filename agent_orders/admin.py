from django.contrib import admin

from .models import AgentAccessToken, AgentCallCart, AgentCallCartItem


@admin.register(AgentAccessToken)
class AgentAccessTokenAdmin(admin.ModelAdmin):
    list_display = ("tenant", "name", "is_active", "created_at", "last_used_at")
    list_filter = ("tenant", "is_active")
    search_fields = ("tenant__name", "name")


class AgentCallCartItemInline(admin.TabularInline):
    model = AgentCallCartItem
    extra = 0
    readonly_fields = ("menu_item", "quantity", "unit_price", "options", "special_instructions", "created_at")
    can_delete = False


@admin.register(AgentCallCart)
class AgentCallCartAdmin(admin.ModelAdmin):
    list_display = ("tenant", "session_id", "created_at", "updated_at")
    list_filter = ("tenant",)
    search_fields = ("tenant__name", "session_id")
    inlines = [AgentCallCartItemInline]


@admin.register(AgentCallCartItem)
class AgentCallCartItemAdmin(admin.ModelAdmin):
    list_display = ("cart", "menu_item", "quantity", "unit_price", "created_at")
    list_filter = ("cart__tenant",)
    search_fields = ("cart__session_id", "menu_item__name")
