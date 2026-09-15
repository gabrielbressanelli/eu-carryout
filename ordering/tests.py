from datetime import datetime, time, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalog.models import MenuCategory, MenuItem, MenuItemModifierGroup, ModifierGroup, ModifierOption
from catalog.pricing import validate_and_price
from tenants.hours import OrderingHours
from tenants.models import BusinessHour, HoursOverride, Tenant, TenantMembership
from tenants.services import build_order_event_payload
from .models import Order, OrderItem


class LandingPageTests(TestCase):
    def test_landing_page_is_public_and_has_signup_tour(self):
        response = self.client.get(reverse("landing"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your restaurant's online ordering storefront")
        self.assertContains(response, "data-tour")
        self.assertContains(response, "signup-placeholder")
        self.assertContains(response, "/onboarding/login/")
        self.assertContains(response, "Log in")
        self.assertContains(response, "Half portion")
        self.assertContains(response, "Your Systems")
        self.assertContains(response, "HostHub")
        self.assertContains(response, "Voice AI agent")


class CustomerOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(
            name="Kitchen",
            slug="kitchen",
            preparation_minutes=20,
            address_line1="160 Main St",
            city="Northville",
            state="MI",
            postal_code="48167",
            business_phone="+12485550100",
        )
        cls.tenant.account.stripe_account_id = "acct_kitchen"
        cls.tenant.account.stripe_charges_enabled = True
        cls.tenant.account.stripe_onboarding_complete = True
        cls.tenant.account.save()
        cls.category = MenuCategory.objects.create(tenant=cls.tenant, name="Mains", slug="mains")
        cls.item = MenuItem.objects.create(tenant=cls.tenant, category=cls.category, name="Pasta", price=Decimal("20.00"))
        cls.portion = ModifierGroup.objects.create(tenant=cls.tenant, name="Portion", required=True)
        cls.full = ModifierOption.objects.create(group=cls.portion, name="Full portion", is_default=True)
        cls.half = ModifierOption.objects.create(group=cls.portion, name="Half portion", price_multiplier=Decimal("0.50"))
        cls.extras = ModifierGroup.objects.create(tenant=cls.tenant, name="Extras", max_choices=2)
        cls.cheese = ModifierOption.objects.create(group=cls.extras, name="Cheese", price_delta=Decimal("3.00"))
        MenuItemModifierGroup.objects.create(menu_item=cls.item, group=cls.portion)
        MenuItemModifierGroup.objects.create(menu_item=cls.item, group=cls.extras)
        for day in range(7):
            BusinessHour.objects.create(tenant=cls.tenant, day_of_week=day, opens_at=time(11), closes_at=time(21))

    def add(self, options=None, quantity=1, note=""):
        return self.client.post("/kitchen/cart/add/", {"item_id": self.item.pk, "option_ids": options if options is not None else [self.full.pk], "quantity": quantity, "note": note}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def summary(self):
        return self.client.get("/kitchen/cart/summary/", HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()

    def checkout(self, revision=None, pickup="asap"):
        return self.client.post("/kitchen/checkout/create-session/", {"cart_revision": revision or self.summary()["cart_revision"], "pickup": pickup})

    def test_half_portion_affects_base_only_and_multiplies_before_addons(self):
        price, options = validate_and_price(self.item, [self.half.pk, self.cheese.pk])
        self.assertEqual(price, Decimal("13.00"))
        self.assertEqual(next(option for option in options if option["id"] == self.half.pk)["price_multiplier"], "0.50")
        second = ModifierOption.objects.create(group=self.extras, name="Double base", price_multiplier=Decimal("2"))
        price, _ = validate_and_price(self.item, [self.half.pk, self.cheese.pk, second.pk])
        self.assertEqual(price, Decimal("23.00"))
        self.item.price = Decimal("20.01")
        self.assertEqual(validate_and_price(self.item, [self.half.pk])[0], Decimal("10.01"))

    def test_required_duplicate_unavailable_and_out_of_scope_options_rejected(self):
        for ids in [[], [self.full.pk, self.half.pk], [self.full.pk, self.full.pk], [999999]]:
            self.assertEqual(self.add(ids).status_code, 400)
        self.half.is_active = False
        self.half.save()
        self.assertEqual(self.add([self.half.pk]).status_code, 400)
        other = Tenant.objects.create(name="Other", slug="other")
        group = ModifierGroup.objects.create(tenant=other, name="Private")
        option = ModifierOption.objects.create(group=group, name="Private option")
        self.assertEqual(self.add([self.full.pk, option.pk]).status_code, 400)

    def test_cart_merges_only_identical_options_and_notes(self):
        self.add([self.half.pk], quantity=2, note="No salt")
        self.add([self.half.pk], note="No salt")
        self.add([self.full.pk], note="No salt")
        self.add([self.half.pk], note="Extra salt")
        lines = self.client.session[f"cart:{self.tenant.pk}"]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["quantity"], 3)
        self.assertEqual(self.summary()["cart_total"], "60.00")
        self.assertIn("No salt", self.summary()["cart_html"])

    def test_edit_selection_and_stable_line_key_after_removal(self):
        self.add([self.full.pk])
        self.add([self.half.pk])
        first, second = self.client.session[f"cart:{self.tenant.pk}"]
        self.client.post("/kitchen/cart/remove/", {"line_key": first["key"]})
        response = self.client.post("/kitchen/cart/update/", {"line_key": second["key"], "configure": "1", "quantity": 2, "option_ids": [self.half.pk, self.cheese.pk], "note": "Light sauce"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cart_total"], "26.00")
        self.assertEqual(self.client.session[f"cart:{self.tenant.pk}"][0]["note"], "Light sauce")

    def test_quote_is_authoritative_and_detail_is_scoped(self):
        response = self.client.post(f"/kitchen/items/{self.item.pk}/quote/", {"option_ids": [self.half.pk, self.cheese.pk], "quantity": 2, "price": "0.01"})
        self.assertEqual(response.json()["total"], "26.00")
        data = self.client.get(f"/kitchen/items/{self.item.pk}/options/").json()
        self.assertTrue(data["item"]["groups"][0]["options"][0]["is_default"])
        Tenant.objects.create(name="Other", slug="other")
        self.assertEqual(self.client.get(f"/other/items/{self.item.pk}/options/").status_code, 404)

    def test_invalid_quantities_and_notes(self):
        for quantity in [0, -1, 100, "1.5", "bad"]:
            self.assertEqual(self.add(quantity=quantity).status_code, 400)
        self.assertEqual(self.add(note="x" * 256).status_code, 400)

    def test_price_changes_require_confirmation_without_creating_order(self):
        self.add()
        revision = self.summary()["cart_revision"]
        self.item.price = Decimal("25.00")
        self.item.save()
        response = self.checkout(revision)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["cart_total"], "25.00")
        self.assertFalse(Order.objects.exists())

    def test_unavailable_cart_item_stays_visible_but_blocks_checkout(self):
        self.add()
        self.category.is_active = False
        self.category.save()
        response = self.checkout()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["cart_valid"])
        self.assertIn("no longer available", response.json()["cart_html"])
        self.assertEqual(response.json()["cart_total"], "0.00")

    def test_checkout_is_post_and_csrf_protected(self):
        self.assertEqual(self.client.get("/kitchen/checkout/create-session/").status_code, 405)
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/kitchen/checkout/create-session/").status_code, 403)

    @override_settings(STRIPE_SECRET_KEY="test-key", STRIPE_APPLICATION_FEE_PERCENT="10", STRIPE_APPLICATION_FEE_FIXED_CENTS="30")
    def test_checkout_snapshots_modifiers_note_and_pickup(self):
        self.add([self.half.pk, self.cheese.pk], quantity=2, note="No salt")
        session = SimpleNamespace(id="cs_test_order", url="https://checkout.stripe.test/session")
        created = {}

        def create_session(**kwargs):
            created.update(kwargs)
            return session

        stripe = SimpleNamespace(api_key=None, checkout=SimpleNamespace(Session=SimpleNamespace(create=create_session)))
        now = datetime(2026, 9, 10, 16, tzinfo=dt_timezone.utc)
        with patch.dict("sys.modules", {"stripe": stripe}), patch("tenants.hours.timezone.now", return_value=now):
            response = self.checkout()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(created["stripe_account"], "acct_kitchen")
        self.assertEqual(created["payment_intent_data"]["application_fee_amount"], 290)
        self.assertEqual(created["metadata"]["stripe_account_id"], "acct_kitchen")
        order = Order.objects.get()
        self.assertEqual(order.amount_paid, Decimal("26.00"))
        self.assertEqual(order.pickup_at, datetime(2026, 9, 10, 16, 20, tzinfo=dt_timezone.utc))
        self.assertEqual({option["name"] for option in order.items.get().options_snapshot}, {"Half portion", "Cheese"})
        self.assertIn("No salt", order.order_summary)
        self.assertEqual(build_order_event_payload(order)["items"][0]["note"], "No salt")

    @override_settings(STRIPE_SECRET_KEY="test-key")
    def test_checkout_requires_connected_stripe_account(self):
        self.add()
        self.tenant.account.stripe_account_id = ""
        self.tenant.account.save()
        response = self.checkout()
        self.assertEqual(response.status_code, 503)
        self.assertIn("restaurant", response.json()["error"])
        self.assertFalse(Order.objects.exists())

    @override_settings(STRIPE_SECRET_KEY="test-key", STRIPE_APPLICATION_FEE_PERCENT="10", STRIPE_APPLICATION_FEE_FIXED_CENTS="30")
    def test_account_fee_override_can_disable_global_fee(self):
        self.add()
        self.tenant.account.stripe_application_fee_percent = Decimal("0")
        self.tenant.account.stripe_application_fee_fixed_cents = 0
        self.tenant.account.save()
        session = SimpleNamespace(id="cs_test_no_fee", url="https://checkout.stripe.test/session")
        created = {}

        def create_session(**kwargs):
            created.update(kwargs)
            return session

        stripe = SimpleNamespace(api_key=None, checkout=SimpleNamespace(Session=SimpleNamespace(create=create_session)))
        with patch.dict("sys.modules", {"stripe": stripe}), patch("tenants.hours.timezone.now", return_value=datetime(2026, 9, 10, 16, tzinfo=dt_timezone.utc)):
            response = self.checkout()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("payment_intent_data", created)

    @override_settings(STRIPE_SECRET_KEY="test-key")
    def test_checkout_success_retrieves_connected_session_and_marks_paid(self):
        self.add()
        order = Order.objects.create(
            tenant=self.tenant,
            customer_email="",
            status=Order.STATUS_DRAFT,
            amount_paid=Decimal("20.00"),
            stripe_session_id="cs_test_success",
        )
        session = {
            "id": "cs_test_success",
            "metadata": {"tenant_id": str(self.tenant.pk), "order_id": str(order.pk)},
            "customer_details": {"email": "guest@example.test", "name": "Guest", "phone": "+12485550123"},
            "amount_total": 2000,
            "payment_intent": "pi_test",
        }
        retrieved = {}

        def retrieve_session(session_id, **kwargs):
            retrieved["session_id"] = session_id
            retrieved.update(kwargs)
            return session

        stripe = SimpleNamespace(api_key=None, checkout=SimpleNamespace(Session=SimpleNamespace(retrieve=retrieve_session)))
        with patch.dict("sys.modules", {"stripe": stripe}):
            response = self.client.get("/kitchen/checkout/success/?session_id=cs_test_success")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(retrieved["stripe_account"], "acct_kitchen")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PAID)
        self.assertEqual(order.customer_email, "guest@example.test")
        self.assertContains(response, "160 Main St")
        self.assertContains(response, "Open in Maps")

    @override_settings(STRIPE_SECRET_KEY="test-key")
    def test_checkout_success_still_shows_pickup_details_when_confirmation_is_pending(self):
        order = Order.objects.create(
            tenant=self.tenant,
            customer_email="guest@example.test",
            status=Order.STATUS_DRAFT,
            amount_paid=Decimal("20.00"),
            order_summary="1x Pasta",
            pickup_at=datetime(2026, 9, 10, 16, 20, tzinfo=dt_timezone.utc),
            pickup_timezone=self.tenant.timezone,
            stripe_session_id="cs_test_pending",
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.item,
            name_snapshot="Pasta",
            quantity=1,
            unit_price=Decimal("20.00"),
        )

        def retrieve_session(*args, **kwargs):
            raise RuntimeError("Stripe is temporarily unavailable")

        stripe = SimpleNamespace(api_key=None, checkout=SimpleNamespace(Session=SimpleNamespace(retrieve=retrieve_session)))
        with patch.dict("sys.modules", {"stripe": stripe}):
            response = self.client.get("/kitchen/checkout/success/?session_id=cs_test_pending")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your payment went through at Stripe")
        self.assertContains(response, "Pickup time")
        self.assertContains(response, "160 Main St")
        self.assertContains(response, "Pasta")

    def test_closed_or_paused_store_cannot_start_payment(self):
        self.add()
        with patch("tenants.hours.timezone.now", return_value=datetime(2026, 9, 10, 5, tzinfo=dt_timezone.utc)):
            self.assertEqual(self.checkout().status_code, 400)
        self.tenant.ordering_paused = True
        self.tenant.save()
        self.assertEqual(self.checkout().status_code, 400)
        self.assertFalse(Order.objects.exists())


class OrderingHoursTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Kitchen", slug="kitchen", scheduled_pickup_enabled=True, preparation_minutes=20)
        for day in range(7):
            BusinessHour.objects.create(tenant=self.tenant, day_of_week=day, opens_at=time(11), closes_at=time(21))
        self.zone = ZoneInfo("America/Detroit")

    def hours(self, hour, minute=0):
        return OrderingHours(self.tenant, datetime(2026, 9, 10, hour, minute, tzinfo=self.zone))

    def test_open_close_and_preparation_cutoff(self):
        self.assertFalse(self.hours(10, 59).is_open)
        self.assertTrue(self.hours(11).is_open)
        self.assertFalse(self.hours(21).is_open)
        self.assertIsNotNone(self.hours(20, 39).asap)
        self.assertIsNone(self.hours(20, 40).asap)

    def test_timezone_is_per_restaurant(self):
        instant = datetime(2026, 9, 10, 15, tzinfo=dt_timezone.utc)
        self.assertTrue(OrderingHours(self.tenant, instant).is_open)
        self.tenant.timezone = "America/Los_Angeles"
        self.assertFalse(OrderingHours(self.tenant, instant).is_open)

    def test_overrides_replace_weekly_hours_and_pause_wins(self):
        override = HoursOverride.objects.create(tenant=self.tenant, date="2026-09-10", is_closed=True)
        self.assertFalse(self.hours(12).is_open)
        override.is_closed = False
        override.opens_at, override.closes_at = time(13), time(16)
        override.save()
        self.assertFalse(self.hours(12).is_open)
        self.assertTrue(self.hours(14).is_open)
        self.tenant.ordering_paused = True
        self.assertFalse(self.hours(14).is_open)
        self.assertEqual(self.hours(14).slots(), [])

    def test_scheduling_requires_valid_slot_lead_time_and_enabled_setting(self):
        hours = self.hours(10)
        first = hours.slots()[0]
        self.assertEqual((first.hour, first.minute), (11, 20))
        self.assertEqual(hours.validate_pickup(first.isoformat()), first)
        for value in ["asap", "2026-09-10T11:00:00-04:00", "2026-09-10T22:00:00-04:00", "2027-09-10T11:20:00-04:00", "2026-09-10T11:20:00"]:
            with self.assertRaises(ValueError):
                hours.validate_pickup(value)
        self.tenant.scheduled_pickup_enabled = False
        with self.assertRaises(ValueError):
            self.hours(10).validate_pickup(first.isoformat())

    def test_override_is_tenant_scoped_and_no_hours_fails_closed(self):
        other = Tenant.objects.create(name="Other", slug="other")
        HoursOverride.objects.create(tenant=other, date="2026-09-10", is_closed=True)
        self.assertTrue(self.hours(12).is_open)
        self.assertFalse(OrderingHours(other).is_open)

    def test_nonexistent_daylight_saving_slots_are_excluded(self):
        row = self.tenant.business_hours.get(day_of_week=6)
        row.opens_at, row.closes_at = time(1), time(4)
        row.save()
        hours = OrderingHours(self.tenant, datetime(2026, 3, 8, 0, tzinfo=self.zone))
        slots = [slot for slot in hours.slots() if slot.date() == hours.now.date()]
        self.assertFalse(any(slot.hour == 2 for slot in slots))

    def test_restaurant_can_manage_overrides_but_not_another_locations(self):
        user = get_user_model().objects.create_user(username="owner")
        TenantMembership.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)
        response = self.client.post(reverse("onboarding:catalog_create", args=["kitchen", "hours_override"]), {"date": "2026-12-25", "is_closed": "on", "note": "Holiday"})
        self.assertEqual(response.status_code, 302)
        override = self.tenant.hours_overrides.get()
        self.assertEqual(self.client.post(reverse("onboarding:catalog_delete", args=["kitchen", "hours_override", override.pk])).status_code, 302)
        response = self.client.post(reverse("onboarding:catalog_create", args=["kitchen", "hours_override"]), {"date": "2026-12-25", "opens_at": "21:00", "closes_at": "11:00"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.tenant.hours_overrides.exists())
