"""URL configuration for Arcane Vault."""
from django.contrib import admin
from django.urls import path
from vault import views
from vault.api import api

urlpatterns = [
    path("", views.home_redirect, name="home"),
    path("admin/", admin.site.urls),
    # Authentication & Account Request Approval
    path("auth/login/", views.login_view, name="login"),
    path("auth/google/login/", views.google_login_start, name="google_login"),
    path("auth/google/callback/", views.google_login_callback, name="google_callback"),
    path("auth/dev-login/", views.dev_quick_login, name="dev_quick_login"),
    path("auth/logout/", views.logout_view, name="logout"),
    path("auth/request-access/", views.request_access_view, name="request_access"),
    path(
        "admin-portal/requests/",
        views.admin_requests_dashboard,
        name="admin_requests_dashboard",
    ),
    # Core Stitch UI Views
    path("explorer/", views.card_explorer_view, name="card_explorer"),
    path("cards/quick-action/", views.quick_card_action, name="quick_card_action"),
    path("vault/", views.my_vault_view, name="my_vault"),
    path(
        "vault/export.csv",
        views.export_collection_csv_view,
        name="export_collection_csv",
    ),
    path("decks/", views.decks_index_view, name="decks_index"),
    path("decks/<slug:slug>/", views.deck_workshop_view, name="deck_workshop"),
    path(
        "decks/<slug:slug>/analysis/",
        views.deck_analysis_playtest_view,
        name="deck_analysis",
    ),
    path(
        "analysis/",
        views.deck_analysis_playtest_view,
        name="deck_analysis_default",
    ),
    # Profile Settings, AI API Keys & Public Share Links
    path("settings/", views.profile_settings_view, name="profile_settings"),
    path("u/<str:username>/", views.public_profile_view, name="public_profile"),
    path(
        "u/<str:username>/decks/<slug:slug>/",
        views.public_deck_view,
        name="public_deck",
    ),
    # Django Ninja REST API (/api/v1/ + /api/v1/docs)
    path("api/v1/", api.urls),
]
