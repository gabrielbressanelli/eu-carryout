from django.conf import settings
from django.db import models
from django.utils.text import slugify
from uuid import uuid4
from django.core.validators import MinValueValidator, MaxValueValidator

from .uploads import logo_upload_path


class Account(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:80]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Tenant(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="restaurants")
    ordering_paused = models.BooleanField(default=False)
    closure_message = models.CharField(max_length=250, blank=True, default="")
    preparation_minutes = models.PositiveIntegerField(default=20, validators=[MinValueValidator(1), MaxValueValidator(240)])
    scheduled_pickup_enabled = models.BooleanField(default=False)
    scheduling_days = models.PositiveIntegerField(default=7, validators=[MinValueValidator(1), MaxValueValidator(30)])
    pickup_interval_minutes = models.PositiveIntegerField(default=15, validators=[MinValueValidator(5), MaxValueValidator(60)])
    media_key = models.UUIDField(default=uuid4, editable=False, unique=True)
    logo = models.ImageField(upload_to=logo_upload_path, blank=True, max_length=500)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True, blank=True)
    primary_domain = models.CharField(max_length=255, unique=True, blank=True, null=True)
    logo_url = models.URLField(max_length=500, blank=True, default="")
    business_email = models.EmailField(blank=True, default="")
    business_phone = models.CharField(max_length=32, blank=True, default="")
    timezone = models.CharField(max_length=64, default="America/Detroit")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:80]
        if not self.account_id:
            account, _ = Account.objects.get_or_create(
                slug=self.slug,
                defaults={"name": self.name},
            )
            self.account = account
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def logo_src(self):
        return self.logo.url if self.logo else self.logo_url


class TenantMembership(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="restaurant_memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "tenant"], name="unique_restaurant_member")]

    def __str__(self):
        return f"{self.user} - {self.tenant}"


class AccountMembership(models.Model):
    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_CHOICES = [
        (ROLE_OWNER, "Owner"),
        (ROLE_ADMIN, "Admin"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account_memberships")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_ADMIN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "account"], name="unique_account_member")]

    def __str__(self):
        return f"{self.user} - {self.account} ({self.get_role_display()})"


class TenantDomain(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="domains")
    domain = models.CharField(max_length=255, unique=True)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["domain"]

    def __str__(self):
        return f"{self.domain} -> {self.tenant}"


class BusinessHour(models.Model):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6
    DAY_CHOICES = [
        (MONDAY, "Monday"),
        (TUESDAY, "Tuesday"),
        (WEDNESDAY, "Wednesday"),
        (THURSDAY, "Thursday"),
        (FRIDAY, "Friday"),
        (SATURDAY, "Saturday"),
        (SUNDAY, "Sunday"),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="business_hours")
    day_of_week = models.PositiveSmallIntegerField(choices=DAY_CHOICES)
    opens_at = models.TimeField(null=True, blank=True)
    closes_at = models.TimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)

    class Meta:
        unique_together = ("tenant", "day_of_week")
        ordering = ["day_of_week"]

    def __str__(self):
        if self.is_closed:
            return f"{self.tenant} {self.get_day_of_week_display()}: closed"
        return f"{self.tenant} {self.get_day_of_week_display()}: {self.opens_at} - {self.closes_at}"


class HoursOverride(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="hours_overrides")
    date = models.DateField()
    is_closed = models.BooleanField(default=True)
    opens_at = models.TimeField(null=True, blank=True)
    closes_at = models.TimeField(null=True, blank=True)
    note = models.CharField(max_length=250, blank=True, default="")

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["tenant", "date"], name="unique_tenant_hours_override")]

    def __str__(self):
        return f"{self.date}: {self.note or ('Closed' if self.is_closed else 'Special hours')}"


class TenantIntegration(models.Model):
    KIND_PRINT = "print"
    KIND_EMAIL = "email"
    KIND_SMS = "sms"
    KIND_CHOICES = [
        (KIND_PRINT, "Kitchen Printing"),
        (KIND_EMAIL, "Email Notification"),
        (KIND_SMS, "SMS Notification"),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="integrations")
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    enabled = models.BooleanField(default=False)
    endpoint_url = models.URLField(max_length=500, blank=True, default="")
    auth_token = models.CharField(max_length=255, blank=True, default="")
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("tenant", "kind")
        ordering = ["tenant", "kind"]

    def __str__(self):
        return f"{self.tenant} {self.get_kind_display()}"


class IntegrationEvent(models.Model):
    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
        (STATUS_SKIPPED, "Skipped"),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="integration_events")
    order = models.ForeignKey("ordering.Order", on_delete=models.CASCADE, related_name="integration_events")
    kind = models.CharField(max_length=24, choices=TenantIntegration.KIND_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveIntegerField(default=0)
    request_payload = models.JSONField(default=dict, blank=True)
    response_status = models.PositiveIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("order", "kind")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order_id} {self.kind} {self.status}"
