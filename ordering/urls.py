from django.urls import path

from . import views

urlpatterns = [
    path("<slug:tenant_slug>/items/<int:item_id>/options/", views.item_options, name="item_options"),
    path("<slug:tenant_slug>/items/<int:item_id>/quote/", views.item_quote, name="item_quote"),
    path("<slug:tenant_slug>/pickup-times/", views.pickup_availability, name="pickup_availability"),
    path("<slug:tenant_slug>/", views.menu, name="menu"),
    path("<slug:tenant_slug>/cart/add/", views.cart_add, name="cart_add"),
    path("<slug:tenant_slug>/cart/update/", views.cart_update, name="cart_update"),
    path("<slug:tenant_slug>/cart/remove/", views.cart_remove, name="cart_remove"),
    path("<slug:tenant_slug>/cart/summary/", views.cart_summary, name="cart_summary"),
    path("<slug:tenant_slug>/cart/count/", views.cart_count, name="cart_count"),
    path("<slug:tenant_slug>/checkout/", views.checkout, name="checkout"),
    path("<slug:tenant_slug>/checkout/create-session/", views.create_checkout_session, name="create_checkout_session"),
    path("<slug:tenant_slug>/checkout/success/", views.checkout_success, name="checkout_success"),
    path("<slug:tenant_slug>/orders/operations/", views.order_operations, name="order_operations"),
    path("stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
    path("<slug:tenant_slug>/orders/", views.orders_by_email, name="orders_by_email"),
]
