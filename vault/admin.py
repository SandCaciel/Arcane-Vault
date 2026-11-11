"""Django Admin registration for Arcane Vault models."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import (
    AccountRequest,
    ApiKey,
    CollectionItem,
    Deck,
    DeckCard,
    ScryfallCard,
    User,
    WishlistItem,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "username",
        "email",
        "display_name",
        "approval_status",
        "is_staff",
        "collection_is_public",
        "wishlist_is_public",
    )
    list_filter = ("approval_status", "is_staff", "is_superuser")
    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "Arcane Vault Profile",
            {
                "fields": (
                    "display_name",
                    "avatar_url",
                    "google_sub",
                    "approval_status",
                    "bio",
                    "collection_is_public",
                    "wishlist_is_public",
                )
            },
        ),
    )


@admin.register(AccountRequest)
class AccountRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "status", "created_at", "reviewed_by", "reviewed_at")
    list_filter = ("status",)
    search_fields = ("user__email", "user__username", "note")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "key_prefix", "is_active", "last_used_at", "created_at")
    list_filter = ("is_active",)


@admin.register(ScryfallCard)
class ScryfallCardAdmin(admin.ModelAdmin):
    list_display = ("name", "set_code", "collector_number", "cmc", "type_line", "rarity")
    search_fields = ("name", "oracle_id", "scryfall_id", "type_line")


@admin.register(CollectionItem)
class CollectionItemAdmin(admin.ModelAdmin):
    list_display = ("user", "card", "quantity", "finish", "condition", "is_public")
    list_filter = ("finish", "condition", "is_public")
    search_fields = ("card__name", "user__username")


@admin.register(Deck)
class DeckAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "slug", "format", "is_public", "updated_at")
    list_filter = ("is_public", "format")
    search_fields = ("name", "user__username")


@admin.register(DeckCard)
class DeckCardAdmin(admin.ModelAdmin):
    list_display = ("deck", "card", "quantity", "allocated_quantity", "role_group", "is_commander")
    list_filter = ("is_commander", "role_group")


@admin.register(WishlistItem)
class WishlistItemAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "card",
        "quantity_needed",
        "auto_added_from_deck",
        "source_deck",
        "priority",
    )
    list_filter = ("auto_added_from_deck", "priority", "is_public")
