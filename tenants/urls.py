from django.urls import path

from . import views

app_name = "onboarding"

urlpatterns = [
    path("", views.restaurant_list, name="restaurant_list"),
    path("login/", views.restaurant_login, name="login"),
    path("logout/", views.restaurant_logout, name="logout"),
    path("restaurants/new/", views.restaurant_create, name="restaurant_create"),
    path("<slug:tenant_slug>/login/", views.restaurant_login, name="restaurant_login"),
    path("<slug:tenant_slug>/menu/<str:kind>/new/", views.catalog_edit, name="catalog_create"),
    path("<slug:tenant_slug>/menu/<str:kind>/<int:object_id>/", views.catalog_edit, name="catalog_edit"),
    path("<slug:tenant_slug>/menu/<str:kind>/<int:object_id>/delete/", views.catalog_delete, name="catalog_delete"),
    path("<slug:tenant_slug>/", views.restaurant_manage, name="restaurant_manage"),
]
