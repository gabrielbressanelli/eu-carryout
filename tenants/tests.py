from io import BytesIO
from pathlib import Path
import os
from unittest.mock import patch

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalog.models import MenuCategory, MenuItem, MenuItemModifierGroup, ModifierGroup, ModifierOption
from ordering.models import Order, OrderItem
from .models import Account, AccountMembership, BusinessHour, Tenant, TenantIntegration, TenantMembership
from .services import ensure_tenant_onboarding_defaults
from .uploads import TARGET_IMAGE_BYTES


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class OnboardingFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("platform", "platform@example.test", "StrongPassphrase!873")
        cls.owner = get_user_model().objects.create_user("restaurant-owner", password="StrongPassphrase!873")
        cls.tenant = Tenant.objects.create(name="Blue Plate", slug="blue-plate", business_email="orders@blueplate.test")
        cls.other = Tenant.objects.create(name="Other Restaurant", slug="other-restaurant")
        TenantMembership.objects.create(user=cls.owner, tenant=cls.tenant)
        cls.category = MenuCategory.objects.create(tenant=cls.tenant, name="Mains", slug="mains")
        cls.item = MenuItem.objects.create(tenant=cls.tenant, category=cls.category, name="Grilled chicken", price="18.00")
        cls.group = ModifierGroup.objects.create(tenant=cls.tenant, name="Sauces")
        cls.option = ModifierOption.objects.create(group=cls.group, name="Garlic", price_delta="1.50")
        cls.link = MenuItemModifierGroup.objects.create(menu_item=cls.item, group=cls.group)
        ensure_tenant_onboarding_defaults(cls.tenant)
        ensure_tenant_onboarding_defaults(cls.other)

    def setUp(self):
        self.client.force_login(self.owner)

    def manage_url(self, tenant=None):
        return reverse("onboarding:restaurant_manage", args=[(tenant or self.tenant).slug])

    def editor_url(self, kind, record=None, delete=False, tenant=None):
        route = "catalog_delete" if delete else "catalog_edit" if record else "catalog_create"
        args = [(tenant or self.tenant).slug, kind]
        if record:
            args.append(record.pk)
        return reverse(f"onboarding:{route}", args=args)

    def image_upload(self, name="photo.jpg"):
        stream = BytesIO()
        Image.new("RGB", (120, 80), "#277653").save(stream, format="JPEG")
        return SimpleUploadedFile(name, stream.getvalue(), content_type="image/jpeg")

    def large_image_upload(self, name="large-photo.jpg"):
        stream = BytesIO()
        Image.effect_noise((1400, 1000), 90).convert("RGB").save(stream, format="JPEG", quality=95)
        content = stream.getvalue()
        self.assertGreater(len(content), TARGET_IMAGE_BYTES)
        return SimpleUploadedFile(name, content, content_type="image/jpeg")

    def business_data(self, **extra):
        return {
            "action": "business", "tenant-name": self.tenant.name, "tenant-slug": self.tenant.slug,
            "tenant-primary_domain": "", "tenant-business_email": "orders@blueplate.test",
            "tenant-business_phone": "+12485550100", "tenant-timezone": "America/Detroit",
            "tenant-is_active": "on", **extra,
        }

    def test_simple_and_branded_login(self):
        self.client.logout()
        response = self.client.get(self.manage_url())
        self.assertIn("/onboarding/blue-plate/login/", response["Location"])
        response = self.client.get(reverse("onboarding:restaurant_login", args=[self.tenant.slug]))
        self.assertContains(response, "Blue Plate")
        self.assertNotContains(response, self.other.name)
        self.assertNotContains(response, "/admin/")
        response = self.client.post(reverse("onboarding:login"), {"username": self.owner.username, "password": "StrongPassphrase!873", "next": "https://evil.test/"})
        self.assertRedirects(response, reverse("onboarding:restaurant_list"))
        self.assertEqual(self.client.get(reverse("onboarding:logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("onboarding:logout")).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_branded_login_rejects_unassigned_account(self):
        self.client.logout()
        response = self.client.post(reverse("onboarding:restaurant_login", args=[self.other.slug]), {"username": self.owner.username, "password": "StrongPassphrase!873"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(response.context["form"].errors)

    def test_location_isolation_including_staff_without_membership(self):
        response = self.client.get(reverse("onboarding:restaurant_list"))
        self.assertContains(response, self.tenant.name)
        self.assertNotContains(response, self.other.name)
        self.assertNotContains(response, "Add restaurant")
        self.assertEqual(self.client.get(self.manage_url(self.other)).status_code, 404)
        self.assertEqual(self.client.post(self.manage_url(self.other), self.business_data()).status_code, 404)
        self.assertEqual(self.client.get(reverse("onboarding:restaurant_create")).status_code, 403)
        self.assertEqual(self.client.get(self.manage_url() + "?section=access").status_code, 403)
        self.owner.is_staff = True
        self.owner.save()
        self.assertEqual(self.client.get(self.manage_url(self.other)).status_code, 404)

    def test_multiple_locations_and_revocation(self):
        membership = TenantMembership.objects.create(user=self.owner, tenant=self.other)
        self.assertContains(self.client.get(reverse("onboarding:restaurant_list")), self.other.name)
        self.assertEqual(self.client.get(self.manage_url(self.other)).status_code, 200)
        membership.delete()
        self.assertEqual(self.client.get(self.manage_url(self.other)).status_code, 404)

    def test_account_admin_sees_account_restaurants_and_manages_location_access(self):
        account = Account.objects.create(name="Restaurant Group", slug="restaurant-group")
        first = Tenant.objects.create(account=account, name="Group One", slug="group-one")
        second = Tenant.objects.create(account=account, name="Group Two", slug="group-two")
        account_admin = get_user_model().objects.create_user("group-admin", password="StrongPassphrase!873")
        location_user = get_user_model().objects.create_user("group-one-user", password="StrongPassphrase!873")
        AccountMembership.objects.create(user=account_admin, account=account)
        TenantMembership.objects.create(user=location_user, tenant=first)

        self.client.force_login(account_admin)
        response = self.client.get(reverse("onboarding:restaurant_list"))
        self.assertContains(response, "Restaurant Group")
        self.assertContains(response, first.name)
        self.assertContains(response, second.name)
        self.assertContains(response, "Add restaurant")
        self.assertEqual(self.client.get(self.manage_url(first) + "?section=access").status_code, 200)

        self.client.force_login(location_user)
        response = self.client.get(reverse("onboarding:restaurant_list"))
        self.assertContains(response, first.name)
        self.assertNotContains(response, second.name)
        self.assertNotContains(response, "Add restaurant")
        self.assertEqual(self.client.get(self.manage_url(second)).status_code, 404)
        self.assertEqual(self.client.get(self.manage_url(first) + "?section=access").status_code, 403)

    def test_account_admin_can_create_restaurant_under_their_account(self):
        account = Account.objects.create(name="Restaurant Group", slug="restaurant-group")
        account_admin = get_user_model().objects.create_user("group-admin", password="StrongPassphrase!873")
        AccountMembership.objects.create(user=account_admin, account=account)
        self.client.force_login(account_admin)
        data = self.business_data(**{
            "account-account": account.pk,
            "tenant-name": "Group Three",
            "tenant-slug": "group-three",
            "owner-username": "group-three-owner",
            "owner-email": "three@example.test",
            "owner-password": "NewStrongPassphrase!873",
            "owner-password_confirm": "NewStrongPassphrase!873",
        })
        response = self.client.post(reverse("onboarding:restaurant_create"), data)
        self.assertEqual(response.status_code, 302)
        tenant = Tenant.objects.get(slug="group-three")
        self.assertEqual(tenant.account, account)
        self.assertTrue(tenant.memberships.filter(user__username="group-three-owner").exists())

    def test_create_restaurant_with_login_and_defaults(self):
        self.client.force_login(self.admin)
        data = self.business_data(**{
            "account-account_name": "Third Group", "account-account_slug": "third-group",
            "tenant-name": "Third Location", "tenant-slug": "third-location",
            "owner-username": "new-owner", "owner-email": "new@example.test",
            "owner-password": "NewStrongPassphrase!873", "owner-password_confirm": "NewStrongPassphrase!873",
            "tenant-logo": self.image_upload(),
        })
        response = self.client.post(reverse("onboarding:restaurant_create"), data)
        self.assertEqual(response.status_code, 302)
        tenant = Tenant.objects.get(slug="third-location")
        account = get_user_model().objects.get(username="new-owner")
        self.assertTrue(account.check_password("NewStrongPassphrase!873"))
        self.assertFalse(account.is_staff)
        self.assertTrue(tenant.memberships.filter(user=account).exists())
        self.assertEqual(tenant.account.slug, "third-group")
        self.assertTrue(tenant.account.memberships.filter(user=account).exists())
        self.assertEqual(tenant.business_hours.count(), 7)
        self.assertEqual(tenant.integrations.count(), 3)
        self.assertTrue(tenant.logo.name.startswith("carryout/third-group/third-location/logo/"))
        self.assertLessEqual(tenant.logo.size, TARGET_IMAGE_BYTES)

    def test_existing_account_can_be_granted_another_location_without_password_reset(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.manage_url(self.other), {"action": "access", "owner-username": self.owner.username})
        self.assertEqual(response.status_code, 302)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("StrongPassphrase!873"))
        self.assertTrue(TenantMembership.objects.filter(user=self.owner, tenant=self.other).exists())

    def test_weak_password_and_reserved_slug_do_not_create_records(self):
        self.client.force_login(self.admin)
        data = self.business_data(**{"tenant-slug": "onboarding", "owner-username": "new-owner", "owner-password": "123", "owner-password_confirm": "123"})
        response = self.client.post(reverse("onboarding:restaurant_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertTrue(response.context["access_form"].errors)
        self.assertFalse(get_user_model().objects.filter(username="new-owner").exists())

    def test_catalog_create_edit_and_delete_separately(self):
        response = self.client.post(self.editor_url("group"), {"name": "Sides", "required": "on", "min_choices": 1, "max_choices": 2, "sort_order": 0})
        self.assertEqual(response.status_code, 302)
        group = ModifierGroup.objects.get(tenant=self.tenant, name="Sides")
        response = self.client.post(self.editor_url("option"), {"group": group.pk, "name": "Fries", "price_delta": "3.00", "sort_order": 0, "is_active": "on"})
        self.assertEqual(response.status_code, 302)
        option = ModifierOption.objects.get(group=group)
        response = self.client.post(self.editor_url("option", option), {"group": group.pk, "name": "Truffle fries", "price_delta": "5.00", "sort_order": 1, "is_active": "on"})
        self.assertEqual(response.status_code, 302)
        option.refresh_from_db()
        self.assertEqual(option.name, "Truffle fries")
        response = self.client.post(self.editor_url("link"), {"menu_item": self.item.pk, "group": group.pk, "required": "unknown", "min_choices": "", "max_choices": "", "sort_order": 0})
        self.assertEqual(response.status_code, 302)
        link = MenuItemModifierGroup.objects.get(menu_item=self.item, group=group)
        self.assertEqual(self.client.get(self.editor_url("link", link, delete=True)).status_code, 200)
        self.assertTrue(MenuItemModifierGroup.objects.filter(pk=link.pk).exists())
        self.assertEqual(self.client.post(self.editor_url("link", link, delete=True)).status_code, 302)
        self.assertTrue(ModifierOption.objects.filter(pk=option.pk).exists())
        self.assertEqual(self.client.post(self.editor_url("option", option, delete=True)).status_code, 302)
        self.assertTrue(ModifierGroup.objects.filter(pk=group.pk).exists())

    def test_edit_item_aliases_and_category(self):
        self.item.aliases.create(alias="old alias")
        response = self.client.post(self.editor_url("item", self.item), {
            "category": self.category.pk, "name": "Roasted chicken", "description": "Roasted with herbs",
            "price": "20.00", "sort_order": 0, "is_active": "on", "alias_text": "herb chicken",
        })
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, "Roasted chicken")
        self.assertEqual(list(self.item.aliases.values_list("alias", flat=True)), ["herb chicken"])
        self.assertEqual(self.client.post(self.editor_url("category", self.category), {"name": "Dinner", "slug": "dinner", "sort_order": 1, "is_active": "on"}).status_code, 302)

    def test_cross_tenant_object_and_relationship_tampering(self):
        other_group = ModifierGroup.objects.create(tenant=self.other, name="Private group")
        other_option = ModifierOption.objects.create(group=other_group, name="Private option")
        for kind, record in [("group", other_group), ("option", other_option)]:
            self.assertEqual(self.client.get(self.editor_url(kind, record)).status_code, 404)
            self.assertEqual(self.client.post(self.editor_url(kind, record, delete=True)).status_code, 404)
        response = self.client.post(self.editor_url("option"), {"group": other_group.pk, "name": "Intruder", "price_delta": "0", "sort_order": 0})
        self.assertEqual(response.status_code, 200)
        self.assertIn("group", response.context["form"].errors)
        response = self.client.post(self.editor_url("link"), {"menu_item": self.item.pk, "group": other_group.pk, "sort_order": 0})
        self.assertIn("group", response.context["form"].errors)

    def test_invalid_choice_ranges_and_duplicates(self):
        response = self.client.post(self.editor_url("group"), {"name": "Invalid", "required": "on", "min_choices": 3, "max_choices": 1, "sort_order": 0})
        self.assertTrue(response.context["form"].errors)
        response = self.client.post(self.editor_url("option"), {"group": self.group.pk, "name": self.option.name, "price_delta": "1", "sort_order": 0})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def test_protected_deletion_preserves_order_history(self):
        response = self.client.post(self.editor_url("category", self.category, delete=True))
        self.assertContains(response, "This record is in use")
        order = Order.objects.create(tenant=self.tenant, customer_email="guest@example.test")
        OrderItem.objects.create(order=order, menu_item=self.item, name_snapshot=self.item.name, quantity=1, unit_price=self.item.price)
        response = self.client.post(self.editor_url("item", self.item, delete=True))
        self.assertContains(response, "This record is in use")
        self.assertTrue(MenuItem.objects.filter(pk=self.item.pk).exists())

    def integration_data(self, tenant):
        data = {"action": "services", "integrations-TOTAL_FORMS": "3", "integrations-INITIAL_FORMS": "3"}
        for index, row in enumerate(tenant.integrations.order_by("kind")):
            data.update({f"integrations-{index}-id": row.pk, f"integrations-{index}-kind": row.kind, f"integrations-{index}-endpoint_url": "https://hook.make.com/orders", f"integrations-{index}-enabled": "on"})
        return data

    def test_service_toggles_and_tampered_formset(self):
        response = self.client.post(self.manage_url(), self.integration_data(self.tenant))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.tenant.integrations.filter(enabled=True).count(), 3)
        self.assertEqual(self.client.post(self.manage_url(), self.integration_data(self.other)).status_code, 403)
        self.assertEqual(self.other.integrations.filter(enabled=True).count(), 0)

    def test_hours_can_be_saved_but_not_reassigned(self):
        data = {"action": "hours", "hours-TOTAL_FORMS": "7", "hours-INITIAL_FORMS": "7"}
        for index, row in enumerate(self.tenant.business_hours.all()):
            data.update({f"hours-{index}-id": row.pk, f"hours-{index}-day_of_week": "6", f"hours-{index}-opens_at": "10:00", f"hours-{index}-closes_at": "20:00"})
        self.assertEqual(self.client.post(self.manage_url(), data).status_code, 302)
        self.assertEqual(set(self.tenant.business_hours.values_list("day_of_week", flat=True)), set(range(7)))
        data["hours-0-id"] = self.other.business_hours.first().pk
        self.assertEqual(self.client.post(self.manage_url(), data).status_code, 403)

    def test_uploads_use_location_folders_and_render_publicly(self):
        response = self.client.post(self.manage_url(), self.business_data(**{"tenant-logo": self.large_image_upload()}))
        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        logo_path = self.tenant.logo.name
        self.assertTrue(logo_path.startswith("carryout/blue-plate/blue-plate/logo/"))
        self.assertLessEqual(self.tenant.logo.size, TARGET_IMAGE_BYTES)
        response = self.client.post(self.editor_url("item", self.item), {
            "category": self.category.pk, "name": self.item.name, "price": "18.00", "sort_order": 0,
            "is_active": "on", "image": self.large_image_upload(),
        })
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertTrue(self.item.image.name.startswith("carryout/blue-plate/blue-plate/menu-items/grilled-chicken-"))
        self.assertLessEqual(self.item.image.size, TARGET_IMAGE_BYTES)
        response = self.client.get(f"/{self.tenant.slug}/")
        self.assertContains(response, self.tenant.logo.url)
        self.assertContains(response, self.item.image.url)
        self.tenant.slug = "renamed-location"
        self.tenant.save()
        self.assertEqual(self.tenant.logo.name, logo_path)
        self.assertNotEqual(self.tenant.media_key, self.other.media_key)

    def test_upload_rejects_fake_and_oversized_files(self):
        for upload in [
            SimpleUploadedFile("fake.jpg", b"not an image", content_type="image/jpeg"),
            SimpleUploadedFile("large.jpg", b"x" * (5 * 1024 * 1024 + 1), content_type="image/jpeg"),
        ]:
            response = self.client.post(self.manage_url(), self.business_data(**{"tenant-logo": upload}))
            self.assertEqual(response.status_code, 200)
            self.assertIn("logo", response.context["tenant_form"].errors)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.logo)

    def test_remove_logo_and_report_storage_failure(self):
        self.tenant.logo_url = "https://images.example.test/old-logo.jpg"
        self.tenant.save()
        response = self.client.post(self.manage_url(), self.business_data(**{"tenant-remove_logo": "True"}))
        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.logo_src, "")
        with patch("django.core.files.storage.memory.InMemoryStorage.save", side_effect=OSError("Storage unavailable")):
            response = self.client.post(self.manage_url(), self.business_data(**{"tenant-logo": self.image_upload()}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("logo", response.context["tenant_form"].errors)

    def test_assignment_prefill_is_scoped_and_group_edits_validate_overrides(self):
        response = self.client.get(self.editor_url("link") + f"?menu_item={self.item.pk}")
        self.assertEqual(response.context["form"].initial["menu_item"], self.item.pk)
        self.link.min_choices = 2
        self.link.save()
        response = self.client.post(self.editor_url("group", self.group), {"name": "Sauces", "min_choices": 0, "max_choices": 1, "sort_order": 0})
        self.assertTrue(response.context["form"].errors)
        response = self.client.post(self.editor_url("group", self.group), {"name": "Sauces", "min_choices": 0, "max_choices": 0, "sort_order": 0})
        self.assertEqual(response.status_code, 302)

    def test_csrf_required_for_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.editor_url("option", self.option, delete=True)).status_code, 403)

    def test_settings_and_editor_pages_render(self):
        pages = {"locations": reverse("onboarding:restaurant_list")}
        for section in ["overview", "hours", "menu", "categories", "modifiers", "options", "links", "services"]:
            pages[section] = self.manage_url() + "?section=" + section
        for kind in ["category", "item", "group", "option", "link"]:
            pages[f"new-{kind}"] = self.editor_url(kind)
        pages["edit-item"] = self.editor_url("item", self.item)
        for name, url in pages.items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, name)
            self.assertNotContains(response, self.other.name)
            self.assertNotContains(response, "/admin/")
            if os.environ.get("ONBOARDING_VISUAL_DIR"):
                directory = Path(os.environ["ONBOARDING_VISUAL_DIR"])
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{name}.html").write_bytes(response.content)
        self.client.logout()
        response = self.client.get(reverse("onboarding:restaurant_login", args=[self.tenant.slug]))
        if os.environ.get("ONBOARDING_VISUAL_DIR"):
            (directory / "login.html").write_bytes(response.content)
