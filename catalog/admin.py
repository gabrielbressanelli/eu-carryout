from django.contrib import admin

from .models import DietaryTag, MenuCategory, MenuItem, MenuItemAlias, MenuItemModifierGroup, ModifierGroup, ModifierOption


class ModifierOptionInline(admin.TabularInline):
    model = ModifierOption
    extra = 1


class MenuItemModifierGroupInline(admin.TabularInline):
    model = MenuItemModifierGroup
    extra = 1


class MenuItemAliasInline(admin.TabularInline):
    model = MenuItemAlias
    extra = 1


@admin.register(MenuCategory)
class MenuCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "sort_order", "is_active")
    list_filter = ("tenant", "is_active")
    search_fields = ("name", "tenant__name")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(DietaryTag)
class DietaryTagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "tenant")
    list_filter = ("tenant",)
    search_fields = ("name", "slug", "tenant__name")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "category", "price", "is_active")
    list_filter = ("tenant", "category", "is_active")
    search_fields = ("name", "description", "tenant__name")
    inlines = [MenuItemAliasInline, MenuItemModifierGroupInline]


@admin.register(ModifierGroup)
class ModifierGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "required", "min_choices", "max_choices")
    list_filter = ("tenant", "required")
    search_fields = ("name", "tenant__name")
    inlines = [ModifierOptionInline]


@admin.register(ModifierOption)
class ModifierOptionAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "price_delta", "price_multiplier", "is_default", "is_active")
    list_filter = ("group__tenant", "group", "is_active")
    search_fields = ("name", "group__name")


admin.site.register(MenuItemModifierGroup)
admin.site.register(MenuItemAlias)
