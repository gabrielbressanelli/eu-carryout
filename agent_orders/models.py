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


class AgentOrderSession(models.Model):
    STATUS_COLLECTING = "collecting"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_COLLECTING, "Collecting"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="agent_order_sessions")
    session_id = models.CharField(max_length=120)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_COLLECTING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "session_id"], name="unique_agent_order_session"),
        ]

    def __str__(self):
        return f"{self.tenant} order session {self.session_id}"


class AgentPendingItem(models.Model):
    STATUS_NEEDS_ITEM = "needs_item"
    STATUS_AMBIGUOUS = "ambiguous"
    STATUS_NEEDS_INPUT = "needs_input"
    STATUS_ADDED = "added"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_NEEDS_ITEM, "Needs item"),
        (STATUS_AMBIGUOUS, "Ambiguous"),
        (STATUS_NEEDS_INPUT, "Needs input"),
        (STATUS_ADDED, "Added"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    session = models.ForeignKey(AgentOrderSession, on_delete=models.CASCADE, related_name="pending_items")
    sequence = models.PositiveIntegerField()
    original_phrase = models.TextField()
    menu_item = models.ForeignKey("catalog.MenuItem", null=True, blank=True, on_delete=models.PROTECT, related_name="agent_pending_items")
    candidate_item_ids = models.JSONField(default=list, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    selected_modifiers = models.JSONField(default=list, blank=True)
    missing_groups = models.JSONField(default=list, blank=True)
    special_instructions = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_NEEDS_INPUT)
    cart_item = models.ForeignKey(AgentCallCartItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="pending_source")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(fields=["session", "sequence"], name="unique_pending_item_sequence"),
        ]

    def __str__(self):
        return f"{self.session.session_id} pending {self.sequence}: {self.original_phrase}"


class AgentOrderAction(models.Model):
    session = models.ForeignKey(AgentOrderSession, on_delete=models.CASCADE, related_name="actions")
    action_id = models.CharField(max_length=120)
    action_type = models.CharField(max_length=40)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["session", "action_id"], name="unique_agent_order_action"),
        ]

    def __str__(self):
        return f"{self.session.session_id} action {self.action_id}"
