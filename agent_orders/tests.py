import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from catalog.models import DietaryTag, MenuCategory, MenuCategoryModifierGroup, MenuItem, MenuItemAlias, MenuItemModifierGroup, ModifierGroup, ModifierOption, ModifierOptionAlias
from ordering.models import Order
from tenants.models import Tenant

from .models import AgentAccessToken


class AgentOrdersApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="160 Main", slug="one-sixty-main")
        self.category = MenuCategory.objects.create(
            tenant=self.tenant,
            name="Appetizers",
            slug="appetizers",
        )
        self.item = MenuItem.objects.create(
            tenant=self.tenant,
            category=self.category,
            name="Calamari",
            description="Lightly fried calamari with marinara.",
            price="16.00",
        )
        MenuItemAlias.objects.create(menu_item=self.item, alias="fried squid")
        self.group = ModifierGroup.objects.create(
            tenant=self.tenant,
            name="Sauce",
            required=False,
            max_choices=1,
        )
        self.option = ModifierOption.objects.create(
            group=self.group,
            name="Extra Marinara",
            price_delta="2.00",
        )
        ModifierOptionAlias.objects.create(modifier_option=self.option, alias="marinara")
        self.vegan = DietaryTag.objects.create(tenant=self.tenant, name="Vegan", slug="vegan")
        MenuItemModifierGroup.objects.create(menu_item=self.item, group=self.group)
        AgentAccessToken.objects.create(
            tenant=self.tenant,
            name="Test Agent",
            token="test-agent-token",
        )
        self.auth = {"HTTP_AUTHORIZATION": "Bearer test-agent-token"}

    def test_menu_search_can_filter_by_dietary_tag(self):
        self.item.dietary_tags.add(self.vegan)
        response = self.client.get(
            "/api/one-sixty-main/agent/menu/search?q=calamari&dietary=vegan",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["dietary_tags"][0]["slug"], "vegan")

        response = self.client.get(
            "/api/one-sixty-main/agent/menu/search?q=calamari&dietary=vegetarian",
            **self.auth,
        )
        self.assertEqual(response.json()["match_status"], "no_match")

    def test_menu_search_requires_agent_token(self):
        response = self.client.get("/api/one-sixty-main/agent/menu/search?q=calamari")

        self.assertEqual(response.status_code, 401)

    def test_menu_search_matches_alias(self):
        response = self.client.get(
            "/api/one-sixty-main/agent/menu/search?q=fried+squid",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["match_status"], "matched")
        self.assertEqual(payload["item"]["name"], "Calamari")

    def test_menu_search_includes_required_category_modifiers(self):
        category_group = ModifierGroup.objects.create(
            tenant=self.tenant,
            name="Category sauce",
            required=True,
            min_choices=1,
            max_choices=1,
        )
        category_option = ModifierOption.objects.create(
            group=category_group,
            name="Roasted garlic sauce",
            price_delta="1.50",
        )
        ModifierOptionAlias.objects.create(modifier_option=category_option, alias="garlic sauce")
        MenuCategoryModifierGroup.objects.create(category=self.category, group=category_group)

        response = self.client.get(
            "/api/one-sixty-main/agent/menu/search?q=calamari",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        self.assertEqual([group["name"] for group in payload["required_modifiers"]], ["Category sauce"])
        self.assertEqual(payload["required_modifiers"][0]["options"][0]["effective_price_adjustment"], "1.50")

        response = self.client.post(
            "/api/one-sixty-main/agent/order-summary/total",
            data=json.dumps({"order_summary": "1x Calamari; - Garlic sauce;"}),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["warnings"], [])
        self.assertEqual(response.json()["exact_total"], "17.50")

    def test_menu_search_surfaces_build_item_from_modifier_terms(self):
        build_item = MenuItem.objects.create(
            tenant=self.tenant,
            category=self.category,
            name="Build Your Own Pasta",
            price="12.00",
        )
        pasta_group = ModifierGroup.objects.create(tenant=self.tenant, name="Pasta type")
        sauce_group = ModifierGroup.objects.create(tenant=self.tenant, name="Build sauce")
        penne = ModifierOption.objects.create(group=pasta_group, name="Penne")
        marinara = ModifierOption.objects.create(group=sauce_group, name="Tomato Basil Sauce")
        ModifierOptionAlias.objects.create(modifier_option=marinara, alias="marinara sauce")
        MenuItemModifierGroup.objects.create(menu_item=build_item, group=pasta_group)
        MenuItemModifierGroup.objects.create(menu_item=build_item, group=sauce_group)

        response = self.client.get(
            "/api/one-sixty-main/agent/menu/search?q=penne+marinara+sauce",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["match_status"], "matched")
        self.assertEqual(response.json()["item"]["name"], "Build Your Own Pasta")

    def test_order_summary_matches_modifier_alias(self):
        response = self.client.post(
            "/api/one-sixty-main/agent/order-summary/total",
            data=json.dumps({"order_summary": "1x Calamari; - Marinara;"}),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["warnings"], [])
        self.assertEqual(response.json()["exact_total"], "18.00")

    def test_cart_lifecycle_prices_modifiers(self):
        response = self.client.post(
            "/api/one-sixty-main/agent/cart/items",
            data=json.dumps({
                "session_id": "call-1",
                "item_id": self.item.id,
                "quantity": 2,
                "modifiers": [{"option_id": self.option.id}],
                "special_instructions": "lightly fried",
            }),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["cart_item"]["unit_price"], "18.00")
        self.assertEqual(payload["cart"]["subtotal"], "36.00")

        line_id = payload["cart_item"]["line_id"]
        response = self.client.patch(
            f"/api/one-sixty-main/agent/cart/items/{line_id}?session_id=call-1",
            data=json.dumps({"quantity": 3}),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cart"]["subtotal"], "54.00")

        response = self.client.delete(
            f"/api/one-sixty-main/agent/cart/items/{line_id}?session_id=call-1",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cart"]["subtotal"], "0.00")

    def test_cart_rejects_modifier_from_another_item(self):
        other_category = MenuCategory.objects.create(
            tenant=self.tenant,
            name="Entrees",
            slug="entrees",
        )
        other_item = MenuItem.objects.create(
            tenant=self.tenant,
            category=other_category,
            name="Chicken Parm",
            price="24.00",
        )

        response = self.client.post(
            "/api/one-sixty-main/agent/cart/items",
            data=json.dumps({
                "session_id": "call-2",
                "item_id": other_item.id,
                "modifiers": [{"option_id": self.option.id}],
            }),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown modifier option", response.json()["error"])

    def test_order_summary_total(self):
        dessert = MenuItem.objects.create(
            tenant=self.tenant,
            category=self.category,
            name="Tiramisu",
            price="10.00",
        )

        response = self.client.post(
            "/api/one-sixty-main/agent/order-summary/total",
            data=json.dumps({"order_summary": f"2x {self.item.name}; 1x {dessert.name}"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["exact_total"], "42.00")
        self.assertEqual(response.json()["warnings"], [])

    @override_settings(STRIPE_SECRET_KEY="test-key")
    def test_finalize_summary_charges_resolved_lines_and_returns_warnings(self):
        self.tenant.account.stripe_account_id = "acct_test"
        self.tenant.account.save()
        stripe = SimpleNamespace(
            checkout=SimpleNamespace(
                Session=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        id="cs_test_agent",
                        url="https://checkout.stripe.test/agent",
                    )
                )
            )
        )
        with patch.dict("sys.modules", {"stripe": stripe}), patch(
            "agent_orders.views.OrderingHours.validate_pickup",
            return_value=timezone.now(),
        ):
            response = self.client.post(
                "/api/one-sixty-main/agent/orders/finalize-summary",
                data=json.dumps({
                    "session_id": "call-finalize",
                    "order_summary": "1x Calamari; - Unknown Sauce; 1x Missing Dish;",
                }),
                content_type="application/json",
                **self.auth,
            )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["amount"], "16.00")
        self.assertEqual(payload["payment_url"], "https://checkout.stripe.test/agent")
        self.assertEqual(len(payload["warnings"]), 2)
        self.assertIn("Unknown Sauce", " ".join(payload["warnings"]))
        self.assertIn("Missing Dish", " ".join(payload["warnings"]))
        self.assertEqual(str(Order.objects.get().amount_paid), "16.00")

    def test_finalize_summary_with_no_resolved_lines_returns_warnings_without_order(self):
        response = self.client.post(
            "/api/one-sixty-main/agent/orders/finalize-summary",
            data=json.dumps({
                "session_id": "call-empty",
                "order_summary": "1x Missing Dish;",
            }),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["warnings"], ["Could not price item: 'Missing Dish'"])
        self.assertFalse(Order.objects.exists())
