import json

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.forms import modelformset_factory
from django.utils.text import slugify
from zoneinfo import available_timezones

from catalog.models import DietaryTag, MenuCategory, MenuItem, MenuItemAlias, MenuItemModifierGroup, ModifierGroup, ModifierOption

from .access import can_manage_account, manageable_accounts
from .models import Account, AccountMembership, BusinessHour, Tenant, TenantIntegration, TenantMembership, HoursOverride
from .uploads import RestaurantImageField

RESERVED_SLUGS = {"admin", "api", "onboarding", "static", "media", "stripe", "restaurants", "login", "logout"}


def _timezone_choices():
    """Return IANA timezone choices without legacy aliases or fixed offsets."""
    zones = sorted(
        zone for zone in available_timezones()
        if "/" in zone and not zone.startswith(("Etc/", "posix/", "right/"))
    )
    if not zones:
        zones = [
            "America/Anchorage", "America/Chicago", "America/Denver", "America/Detroit",
            "America/Los_Angeles", "America/New_York", "America/Phoenix", "America/Toronto",
            "America/Vancouver", "Asia/Tokyo", "Australia/Sydney", "Europe/Berlin",
            "Europe/London", "Pacific/Auckland",
        ]
    return [(zone, zone.replace("_", " ")) for zone in zones]


class RestaurantLoginForm(AuthenticationForm):
    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["username"].widget.attrs["autocomplete"] = "username"
        self.fields["password"].widget.attrs["autocomplete"] = "current-password"

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if (
            self.tenant
            and not user.is_superuser
            and not user.restaurant_memberships.filter(tenant=self.tenant).exists()
            and not user.account_memberships.filter(account=self.tenant.account).exists()
        ):
            raise self.get_invalid_login_error()


class RestaurantAccessForm(forms.Form):
    username = forms.CharField(max_length=150, label="Login username", widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    email = forms.EmailField(required=False, widget=forms.EmailInput(attrs={"class": "form-control"}))
    password = forms.CharField(required=False, label="Password for new account", widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}))
    password_confirm = forms.CharField(required=False, label="Confirm password", widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}))

    def clean(self):
        cleaned = super().clean()
        username = cleaned.get("username")
        if not username:
            return cleaned
        self.account = get_user_model().objects.filter(username=username).first()
        password = cleaned.get("password")
        if self.account:
            if password or cleaned.get("password_confirm"):
                self.add_error("password", "This account already exists. Leave passwords empty to grant access without changing its login.")
            if not self.account.is_active:
                self.add_error("username", "This account is inactive.")
        else:
            self.account = get_user_model()(username=username, email=cleaned.get("email", ""))
            if not password:
                self.add_error("password", "A password is required for a new account.")
            elif password != cleaned.get("password_confirm"):
                self.add_error("password_confirm", "Passwords do not match.")
            else:
                try:
                    password_validation.validate_password(password, self.account)
                    self.account.full_clean(exclude=["password"])
                except forms.ValidationError as error:
                    self.add_error(None, error.messages)
        return cleaned

    def save(self, tenant):
        if not self.account.pk:
            self.account.set_password(self.cleaned_data["password"])
            self.account.save()
        TenantMembership.objects.get_or_create(user=self.account, tenant=tenant)
        return self.account


class RestaurantCreateAccessForm(RestaurantAccessForm):
    add_login = forms.BooleanField(required=False, label="Add a location-specific login")

    def __init__(self, *args, login_required=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.login_required = login_required
        self.account = None
        if login_required:
            self.fields.pop("add_login")
        else:
            add_login = self.fields.pop("add_login")
            self.fields = {"add_login": add_login, **self.fields}
            self.fields["username"].required = False
            self.fields["username"].help_text = "Leave blank when the account's admins should manage this location."

    def clean(self):
        if self.login_required:
            return super().clean()
        cleaned = forms.Form.clean(self)
        login_values = [cleaned.get(name) for name in ("username", "email", "password", "password_confirm")]
        wants_login = cleaned.get("add_login") or any(login_values)
        if not wants_login:
            self.account = None
            return cleaned
        if not cleaned.get("username"):
            self.add_error("username", "Enter a username for the location-specific login.")
            return cleaned
        return RestaurantAccessForm.clean(self)

    def save(self, tenant):
        if self.account is None:
            return None
        return super().save(tenant)


class RestaurantAccountForm(forms.Form):
    account = forms.ModelChoiceField(required=False, queryset=Account.objects.none(), label="Account", widget=forms.Select(attrs={"class": "form-select"}))
    account_name = forms.CharField(required=False, max_length=120, label="New account name", widget=forms.TextInput(attrs={"class": "form-control"}))
    account_slug = forms.SlugField(required=False, max_length=80, label="New account URL key", widget=forms.TextInput(attrs={"class": "form-control"}))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.created_account = False
        accounts = manageable_accounts(user).order_by("name") if user else Account.objects.none()
        self.fields["account"].queryset = accounts
        if not user or not user.is_superuser:
            self.fields["account_name"].disabled = True
            self.fields["account_slug"].disabled = True
            self.fields["account_name"].help_text = "Only platform admins can create new accounts."
            self.fields["account_slug"].help_text = "Only platform admins can create new accounts."
        if accounts.count() == 1:
            self.fields["account"].initial = accounts.first()

    def clean(self):
        cleaned = super().clean()
        account = cleaned.get("account")
        account_name = (cleaned.get("account_name") or "").strip()
        account_slug = (cleaned.get("account_slug") or "").strip().lower()
        if account and account_name:
            self.add_error("account_name", "Choose an existing account or create a new one, not both.")
        if account:
            if not can_manage_account(self.user, account):
                self.add_error("account", "Choose an account you can manage.")
            return cleaned
        if not getattr(self.user, "is_superuser", False):
            self.add_error("account", "Choose the account this restaurant belongs to.")
            return cleaned
        if not account_name:
            self.add_error("account_name", "Enter an account name.")
            return cleaned
        slug = account_slug or slugify(account_name)[:80]
        if not slug or slug in RESERVED_SLUGS:
            self.add_error("account_slug", "Choose another account URL key.")
        elif Account.objects.filter(slug=slug).exists():
            self.add_error("account_slug", "This account URL key is already in use.")
        cleaned["account_slug"] = slug
        return cleaned

    def save(self):
        account = self.cleaned_data.get("account")
        if account:
            self.created_account = False
            return account
        self.created_account = True
        return Account.objects.create(
            name=self.cleaned_data["account_name"].strip(),
            slug=self.cleaned_data["account_slug"],
        )

    def grant_owner_if_created(self, user, account):
        if self.created_account and user:
            AccountMembership.objects.get_or_create(
                user=user,
                account=account,
                defaults={"role": AccountMembership.ROLE_OWNER},
            )


class TenantOnboardingForm(forms.ModelForm):
    logo = RestaurantImageField(label="Restaurant logo", kind="logo")
    remove_logo = forms.BooleanField(required=False, widget=forms.HiddenInput(attrs={"data-remove-image-value": ""}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        timezone_choices = _timezone_choices()
        self.fields["timezone"].choices = timezone_choices
        self.fields["timezone"].widget.choices = timezone_choices
        self.fields["timezone"].help_text = "Used for business hours, pickup times, and order confirmations."

    def save(self, commit=True):
        tenant = super().save(commit=False)
        if self.cleaned_data.get("remove_logo") and self.add_prefix("logo") not in self.files:
            tenant.logo = ""
            tenant.logo_url = ""
        if commit:
            tenant.save()
        return tenant

    def clean_slug(self):
        slug = (self.cleaned_data.get("slug") or slugify(self.cleaned_data.get("name", ""))).lower()
        if not slug or slug in RESERVED_SLUGS:
            raise forms.ValidationError("Choose another restaurant URL.")
        if Tenant.objects.filter(slug=slug).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This restaurant URL is already in use.")
        return slug

    def clean_timezone(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        value = self.cleaned_data["timezone"]
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise forms.ValidationError("Enter a valid timezone, such as America/Detroit.")
        return value

    class Meta:
        model = Tenant
        fields = [
            "name",
            "slug",
            "primary_domain",
            "logo",
            "business_email",
            "business_phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "timezone",
            "is_active",
        ]
        labels = {"slug": "Ordering URL", "is_active": "Ordering site active", "primary_domain": "Custom domain"}
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "primary_domain": forms.TextInput(attrs={"class": "form-control", "placeholder": "orders.restaurant.com"}),
            "business_email": forms.EmailInput(attrs={"class": "form-control"}),
            "business_phone": forms.TextInput(attrs={"class": "form-control"}),
            "address_line1": forms.TextInput(attrs={"class": "form-control"}),
            "address_line2": forms.TextInput(attrs={"class": "form-control"}),
            "city": forms.TextInput(attrs={"class": "form-control"}),
            "state": forms.TextInput(attrs={"class": "form-control"}),
            "postal_code": forms.TextInput(attrs={"class": "form-control"}),
            "timezone": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class BusinessHourForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["day_of_week"].disabled = True

    class Meta:
        model = BusinessHour
        fields = ["day_of_week", "opens_at", "closes_at", "is_closed"]
        widgets = {
            "day_of_week": forms.HiddenInput(),
            "opens_at": forms.TimeInput(attrs={"class": "form-control", "type": "time"}),
            "closes_at": forms.TimeInput(attrs={"class": "form-control", "type": "time"}),
            "is_closed": forms.CheckboxInput(attrs={"class": "form-check-input", "data-hours-closed": "1"}),
        }

    def clean(self):
        cleaned = super().clean()
        is_closed = cleaned.get("is_closed")
        opens_at = cleaned.get("opens_at")
        closes_at = cleaned.get("closes_at")
        if is_closed:
            cleaned["opens_at"] = None
            cleaned["closes_at"] = None
            return cleaned
        if not opens_at or not closes_at:
            raise forms.ValidationError("Open and close times are required unless the restaurant is closed.")
        if opens_at >= closes_at:
            raise forms.ValidationError("Close time must be after open time.")
        return cleaned


BusinessHourFormSet = modelformset_factory(
    BusinessHour,
    form=BusinessHourForm,
    extra=0,
    can_delete=False,
)


class TenantIntegrationForm(forms.ModelForm):
    request_body = forms.CharField(
        required=False,
        label="JSON request body",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 6, "placeholder": '{\n  "type": "Website Carryout",\n  "customerName": "{{ customer_name }}",\n  "order_summary": "{{ order_summary }}",\n  "orderNumber": "{{ order_ref }}"\n}'}),
        help_text="Optional JSON template. Use {{ customer_name }}, {{ order_summary }}, {{ order_ref }}, {{ pickup_at }}, and other order fields.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].disabled = True
        self.fields["enabled"].widget.attrs["aria-label"] = f"Enable {self.instance.get_kind_display()}"
        request_body = (self.instance.config or {}).get("request_body")
        if request_body:
            self.initial["request_body"] = json.dumps(request_body, indent=2)

    class Meta:
        model = TenantIntegration
        fields = ["kind", "enabled", "endpoint_url", "auth_token"]
        widgets = {
            "kind": forms.HiddenInput(),
            "enabled": forms.CheckboxInput(attrs={"class": "form-check-input", "data-service-enabled": "1"}),
            "endpoint_url": forms.URLInput(attrs={"class": "form-control", "placeholder": "https://hook.make.com/..."}),
            "auth_token": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional bearer token"}),
        }

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("enabled") and not cleaned.get("endpoint_url"):
            raise forms.ValidationError("Enabled services need an endpoint URL.")
        return cleaned

    def clean_request_body(self):
        raw = self.cleaned_data.get("request_body", "").strip()
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Enter valid JSON (line {exc.lineno}, column {exc.colno}).")
        if not isinstance(value, dict):
            raise forms.ValidationError("The JSON request body must be an object.")
        return value

    def save(self, commit=True):
        integration = super().save(commit=False)
        config = dict(integration.config or {})
        request_body = self.cleaned_data.get("request_body")
        if request_body is None:
            config.pop("request_body", None)
        else:
            config["request_body"] = request_body
        integration.config = config
        if commit:
            integration.save()
        return integration


TenantIntegrationFormSet = modelformset_factory(
    TenantIntegration,
    form=TenantIntegrationForm,
    extra=0,
    can_delete=False,
)


class MenuCategoryForm(forms.ModelForm):
    class Meta:
        model = MenuCategory
        fields = ["name", "slug", "sort_order", "is_active"]
        labels = {"slug": "Category URL", "sort_order": "Display order", "is_active": "Visible on menu"}
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class MenuItemForm(forms.ModelForm):
    image = RestaurantImageField(label="Menu photo", kind="menu_item")
    remove_image = forms.BooleanField(required=False, widget=forms.HiddenInput(attrs={"data-remove-image-value": ""}))
    alias_text = forms.CharField(
        required=False,
        label="Voice/search aliases",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "fried squid, kids pasta"}),
    )

    class Meta:
        model = MenuItem
        fields = ["category", "name", "description", "price", "image", "dietary_tags", "sort_order", "is_active"]
        widgets = {
            "category": forms.Select(attrs={"class": "form-select"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "price": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "dietary_tags": forms.SelectMultiple(attrs={"class": "form-select", "size": 4}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        if tenant:
            self.fields["category"].queryset = MenuCategory.objects.filter(tenant=tenant).order_by("sort_order", "name")
            self.fields["dietary_tags"].queryset = DietaryTag.objects.filter(tenant=tenant)
        self.fields["category"].label_from_instance = lambda category: category.name
        self.fields["sort_order"].label = "Display order"
        self.fields["is_active"].label = "Available"
        if self.instance.pk:
            self.fields["alias_text"].initial = ", ".join(self.instance.aliases.values_list("alias", flat=True))

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price < 0:
            raise forms.ValidationError("Price cannot be negative.")
        return price

    def clean_alias_text(self):
        value = self.cleaned_data["alias_text"]
        if any(len(alias.strip()) > 120 for alias in value.split(",")):
            raise forms.ValidationError("Each alias must be 120 characters or fewer.")
        return value

    def save(self, commit=True):
        item = super().save(commit=False)
        if self.tenant:
            item.tenant = self.tenant
        if self.cleaned_data.get("remove_image") and "image" not in self.files:
            item.image = ""
            item.image_url = ""
        if commit:
            item.save()
            self.save_m2m()
            self.save_aliases(item)
        return item

    def save_aliases(self, item):
        aliases = [value.strip() for value in self.cleaned_data.get("alias_text", "").split(",") if value.strip()]
        item.aliases.exclude(alias__in=aliases).delete()
        for alias in aliases:
            MenuItemAlias.objects.get_or_create(menu_item=item, alias=alias)


class ModifierGroupForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        minimum, maximum = cleaned.get("min_choices"), cleaned.get("max_choices")
        if minimum is not None and maximum and minimum > maximum:
            raise forms.ValidationError("Maximum selections must cover the minimum, or be 0 for unlimited.")
        if self.instance.pk and minimum is not None and maximum is not None:
            for link in self.instance.menuitemmodifiergroup_set.all():
                effective_min = minimum if link.min_choices is None else link.min_choices
                effective_max = maximum if link.max_choices is None else link.max_choices
                if effective_max and effective_min > effective_max:
                    raise forms.ValidationError("These defaults conflict with an item's selection limits. Update that assignment first.")
        return cleaned

    class Meta:
        model = ModifierGroup
        fields = ["name", "required", "min_choices", "max_choices", "sort_order"]
        labels = {"required": "Selection required", "min_choices": "Minimum selections", "max_choices": "Maximum selections (0 = unlimited)", "sort_order": "Display order"}
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "min_choices": forms.NumberInput(attrs={"class": "form-control"}),
            "max_choices": forms.NumberInput(attrs={"class": "form-control"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control"}),
        }


class ModifierOptionForm(forms.ModelForm):
    def clean_price_multiplier(self):
        value = self.cleaned_data.get("price_multiplier")
        return value if value is not None else 1

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tenant:
            self.fields["dietary_tags"].queryset = DietaryTag.objects.filter(tenant=tenant)
        self.fields["price_multiplier"].required = False
        self.fields["price_multiplier"].label = "Base price multiplier (0.50 = half portion)"
        self.fields["is_default"].label = "Selected by default"
        self.fields["dietary_tags"].widget.attrs.update({"class": "form-select", "size": 4})

    class Meta:
        model = ModifierOption
        fields = ["name", "price_delta", "price_multiplier", "is_default", "dietary_tags", "sort_order", "is_active"]
        labels = {"price_delta": "Price adjustment", "sort_order": "Display order", "is_active": "Available"}
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Extra cheese"}),
            "price_delta": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "price_multiplier": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "is_default": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class OrderingSettingsForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["ordering_paused", "closure_message", "preparation_minutes", "scheduled_pickup_enabled", "scheduling_days", "pickup_interval_minutes"]
        labels = {"ordering_paused": "Pause new orders", "closure_message": "Closure message", "preparation_minutes": "Preparation time (minutes)", "scheduled_pickup_enabled": "Allow scheduled pickup", "scheduling_days": "Days ahead available", "pickup_interval_minutes": "Pickup interval (minutes)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"


class HoursOverrideForm(forms.ModelForm):
    class Meta:
        model = HoursOverride
        fields = ["date", "is_closed", "opens_at", "closes_at", "note"]
        widgets = {"date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
                   "opens_at": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
                   "closes_at": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
                   "is_closed": forms.CheckboxInput(attrs={"class": "form-check-input"}),
                   "note": forms.TextInput(attrs={"class": "form-control"})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_closed"):
            cleaned["opens_at"] = cleaned["closes_at"] = None
        elif not cleaned.get("opens_at") or not cleaned.get("closes_at") or cleaned["opens_at"] >= cleaned["closes_at"]:
            raise forms.ValidationError("Set an opening and closing time on the same day, or mark the date closed.")
        return cleaned


class ModifierOptionEditorForm(ModifierOptionForm):
    class Meta(ModifierOptionForm.Meta):
        fields = ["group", *ModifierOptionForm.Meta.fields]
        widgets = {**ModifierOptionForm.Meta.widgets, "group": forms.Select(attrs={"class": "form-select"})}

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, tenant=tenant, **kwargs)
        self.fields["group"].queryset = ModifierGroup.objects.filter(tenant=tenant)


class MenuItemModifierGroupForm(forms.ModelForm):
    class Meta:
        model = MenuItemModifierGroup
        fields = ["menu_item", "group", "required", "min_choices", "max_choices", "sort_order"]
        labels = {"required": "Selection requirement", "min_choices": "Minimum selections", "max_choices": "Maximum selections (0 = unlimited)", "sort_order": "Display order"}
        widgets = {
            "menu_item": forms.Select(attrs={"class": "form-select"}),
            "group": forms.Select(attrs={"class": "form-select"}),
            "required": forms.NullBooleanSelect(attrs={"class": "form-select"}),
            "min_choices": forms.NumberInput(attrs={"class": "form-control"}),
            "max_choices": forms.NumberInput(attrs={"class": "form-control"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        self.fields["required"].widget.choices = [("unknown", "Use group setting"), ("true", "Required"), ("false", "Optional")]
        for name in ("min_choices", "max_choices"):
            self.fields[name].widget.attrs["placeholder"] = "Use group setting"
        if tenant:
            self.fields["menu_item"].queryset = MenuItem.objects.filter(tenant=tenant).order_by("category__sort_order", "name")
            self.fields["group"].queryset = ModifierGroup.objects.filter(tenant=tenant).order_by("sort_order", "name")

    def clean(self):
        cleaned = super().clean()
        menu_item = cleaned.get("menu_item")
        group = cleaned.get("group")
        if menu_item and group and self.tenant:
            if menu_item.tenant_id != self.tenant.id or group.tenant_id != self.tenant.id:
                raise forms.ValidationError("Menu item and modifier group must belong to this restaurant.")
            existing = MenuItemModifierGroup.objects.filter(menu_item=menu_item, group=group)
            if self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError("This modifier group is already linked to that menu item.")
            minimum = cleaned.get("min_choices")
            maximum = cleaned.get("max_choices")
            minimum = group.min_choices if minimum is None else minimum
            maximum = group.max_choices if maximum is None else maximum
            if maximum and minimum > maximum:
                raise forms.ValidationError("Maximum selections must cover the minimum, or be 0 for unlimited.")
        return cleaned
