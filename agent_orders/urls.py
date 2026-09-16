from django.urls import path

from . import views

app_name = "agent_orders"

urlpatterns = [
    path("menu/search", views.menu_search, name="menu_search"),
    path("menu/categories", views.menu_categories, name="menu_categories"),
    path("menu/items/<int:item_id>/", views.menu_item_detail, name="menu_item_detail"),
    path("cart/items", views.cart_items_create, name="cart_items_create"),
    path("cart/items/<str:line_id>", views.cart_item_detail, name="cart_item_detail"),
    path("cart/<str:session_id>", views.cart_detail, name="cart_detail"),
    path("order-summary/total", views.order_summary_total, name="order_summary_total"),
    path("orders/finalize-summary", views.order_finalize_summary, name="order_finalize_summary"),
    path("orders/finalize", views.order_finalize, name="order_finalize"),
    path("checkout/<str:token>/", views.agent_checkout, name="agent_checkout"),
]
