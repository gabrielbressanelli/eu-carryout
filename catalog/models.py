from decimal import Decimal

from django.db import models
from django.core.validators import MinValueValidator
from tenants.uploads import menu_upload_path


class MenuCategory(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="menu_categories")
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("tenant", "slug")
        ordering = ["sort_order", "name"]

    def __str__(self):
        return f"{self.tenant} - {self.name}"


class DietaryTag(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="dietary_tags")
    name = models.CharField(max_length=40)
    slug = models.SlugField(max_length=40)

    class Meta:
        unique_together = ("tenant", "slug")
        ordering = ["name"]

    def __str__(self):
        return self.name


class MenuItem(models.Model):
    image = models.ImageField(upload_to=menu_upload_path, blank=True, max_length=500)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="menu_items")
    category = models.ForeignKey(MenuCategory, on_delete=models.PROTECT, related_name="items")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    image_url = models.URLField(max_length=500, blank=True, default="")
    dietary_tags = models.ManyToManyField(DietaryTag, blank=True, related_name="menu_items")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("tenant", "name")
        ordering = ["category__sort_order", "sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def image_src(self):
        return self.image.url if self.image else self.image_url


class MenuItemAlias(models.Model):
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(max_length=120)

    class Meta:
        unique_together = ("menu_item", "alias")
        ordering = ["alias"]

    def __str__(self):
        return f"{self.alias} -> {self.menu_item}"


class ModifierGroup(models.Model):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="modifier_groups")
    name = models.CharField(max_length=80)
    required = models.BooleanField(default=False)
    min_choices = models.PositiveIntegerField(default=0)
    max_choices = models.PositiveIntegerField(default=1)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("tenant", "name")
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class ModifierOption(models.Model):
    group = models.ForeignKey(ModifierGroup, on_delete=models.CASCADE, related_name="options")
    name = models.CharField(max_length=80)
    price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    price_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"), validators=[MinValueValidator(Decimal("0.00"))])
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    dietary_tags = models.ManyToManyField(DietaryTag, blank=True, related_name="modifier_options")

    class Meta:
        unique_together = ("group", "name")
        ordering = ["group__sort_order", "sort_order", "name"]

    def __str__(self):
        return self.name


class ModifierOptionAlias(models.Model):
    modifier_option = models.ForeignKey(ModifierOption, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(max_length=80)

    class Meta:
        unique_together = ("modifier_option", "alias")
        ordering = ["alias"]

    def __str__(self):
        return f"{self.alias} -> {self.modifier_option}"


class MenuItemModifierGroup(models.Model):
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="modifier_groups")
    group = models.ForeignKey(ModifierGroup, on_delete=models.CASCADE)
    required = models.BooleanField(null=True, blank=True)
    min_choices = models.PositiveIntegerField(null=True, blank=True)
    max_choices = models.PositiveIntegerField(null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("menu_item", "group")
        ordering = ["sort_order", "id"]

    def effective_required(self):
        return self.required if self.required is not None else self.group.required

    def effective_min(self):
        return self.min_choices if self.min_choices is not None else self.group.min_choices

    def effective_max(self):
        return self.max_choices if self.max_choices is not None else self.group.max_choices

    def __str__(self):
        return f"{self.menu_item} - {self.group}"
