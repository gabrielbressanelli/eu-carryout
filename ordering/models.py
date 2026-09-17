from django.conf import settings
from django.db import models


class Order(models.Model):
    pickup_at = models.DateTimeField(null=True, blank=True)
    pickup_timezone = models.CharField(max_length=64, blank=True, default="")
    STATUS_DRAFT = "draft"
    STATUS_PAID = "paid"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PAID, "Paid"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    FULFILLMENT_PENDING = "pending"
    FULFILLMENT_PICKED_UP = "picked_up"
    FULFILLMENT_CANCELLED = "cancelled"
    FULFILLMENT_CHOICES = [
        (FULFILLMENT_PENDING, "Pending"),
        (FULFILLMENT_PICKED_UP, "Picked up"),
        (FULFILLMENT_CANCELLED, "Cancelled"),
    ]

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="orders")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    customer_email = models.EmailField(db_index=True)
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_phone = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    fulfillment_status = models.CharField(max_length=24, choices=FULFILLMENT_CHOICES, default=FULFILLMENT_PENDING)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    order_summary = models.TextField(blank=True, default="")
    stripe_session_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "customer_email"]),
            models.Index(fields=["tenant", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.tenant} order {self.pk or 'new'}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    menu_item = models.ForeignKey("catalog.MenuItem", on_delete=models.PROTECT)
    name_snapshot = models.CharField(max_length=120)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    options_snapshot = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["id"]

    @property
    def line_total(self):
        return self.unit_price * self.quantity

    def __str__(self):
        return f"{self.quantity}x {self.name_snapshot}"
