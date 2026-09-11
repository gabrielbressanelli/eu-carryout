from decimal import Decimal

from django.db import models


class AgentAccessToken(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="agent_tokens")
    name = models.CharField(max_length=80, default="Voice Agent")
    token = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["tenant", "name"]

    def __str__(self):
        return f"{self.tenant} - {self.name}"


class AgentCallCart(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="agent_call_carts")
    session_id = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("tenant", "session_id")
        ordering = ["-updated_at"]

    def subtotal(self):
        total = Decimal("0.00")
        for item in self.items.select_related("menu_item").all():
            total += item.unit_price * item.quantity
        return total.quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.tenant} cart {self.session_id}"


class AgentCallCartItem(models.Model):
    cart = models.ForeignKey(AgentCallCart, on_delete=models.CASCADE, related_name="items")
    menu_item = models.ForeignKey("catalog.MenuItem", on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    options = models.JSONField(default=list, blank=True)
    special_instructions = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def line_total(self):
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.quantity}x {self.menu_item.name}"
