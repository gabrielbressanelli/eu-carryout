from django.contrib import admin

from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("menu_item", "name_snapshot", "quantity", "unit_price", "options_snapshot", "note")
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "customer_email", "customer_name", "status", "fulfillment_status", "amount_paid", "created_at")
    list_filter = ("tenant", "status", "fulfillment_status")
    search_fields = ("customer_email", "customer_name", "customer_phone", "stripe_session_id", "tenant__name")
    readonly_fields = ("created_at", "updated_at", "paid_at")
    inlines = [OrderItemInline]


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "name_snapshot", "quantity", "unit_price")
    list_filter = ("order__tenant",)
    search_fields = ("name_snapshot", "order__customer_email")
