"""
Views for Arcane Vault (Commander's Ledger):
- Google OIDC authentication, Account Creation Request gate, and Admin Approval Dashboard
- Card Explorer (Scryfall syntax search & quick-add)
- My Vault & Wishlist (physical inventory, cross-deck "In Deck" badges, bulk import, CSV export)
- Commander Deck Workshop (100-card categorized ledger, cross-deck allocation vs. wishlist, Moxfield import/export, "Add Deck to Physical Collection" onboarding)
- Deck Analysis & Playtest (mana curve, color identity breakdown, interactive Goldfish opening hand simulator)
- Profile & AI API Key Management + Public Shareable Profiles & Decks
"""
from collections import defaultdict
from decimal import Decimal
import random
import secrets
from urllib.parse import urlencode
import httpx
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from vault.models import (
    AccountRequest,
    ApiKey,
    CollectionItem,
    Deck,
    DeckCard,
    ScryfallCard,
    User,
    WishlistItem,
)
from vault.services.allocation import (
    add_card_to_collection,
    add_card_to_deck,
    add_deck_to_physical_collection,
    get_oracle_deck_usage_map,
    reconcile_user_allocations,
)
from vault.services.moxfield import (
    export_collection_to_moxfield_csv,
    export_deck_to_moxfield_text,
    import_moxfield_to_deck,
    parse_moxfield_text,
)
from vault.services.scryfall import (
    apply_local_scryfall_syntax,
    bulk_resolve_cards,
    resolve_single_card,
    search_scryfall,
)

ROLE_GROUP_ORDER = [
    "Commander",
    "Mana Ramp & Rocks",
    "Card Draw & Value Engines",
    "Removal & Interaction",
    "Creatures & Threats",
    "Artifacts & Enchantments",
    "Instants & Sorceries",
    "Lands & Mana Base",
]


def home_redirect(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        if not request.user.is_approved:
            return redirect("request_access")
        return redirect("my_vault")
    return redirect("card_explorer")


# ---------------------------------------------------------------------------
# Authentication & Admin Account Approval Flow
# ---------------------------------------------------------------------------
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("my_vault" if request.user.is_approved else "request_access")
    google_configured = bool(
        settings.GOOGLE_OIDC_CLIENT_ID and settings.GOOGLE_OIDC_CLIENT_SECRET
    )
    return render(
        request,
        "vault/login.html",
        {
            "google_configured": google_configured,
            "debug_mode": settings.DEBUG,
        },
    )


def google_login_start(request: HttpRequest) -> HttpResponse:
    """Redirect the user to Google's OAuth 2.0 / OIDC authorization endpoint."""
    if not (settings.GOOGLE_OIDC_CLIENT_ID and settings.GOOGLE_OIDC_CLIENT_SECRET):
        messages.error(
            request,
            "Google OIDC credentials are not configured in .env yet.",
        )
        return redirect("login")

    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    callback_url = f"{settings.BASE_URL}{reverse('google_callback')}"
    params = {
        "client_id": settings.GOOGLE_OIDC_CLIENT_ID,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


def google_login_callback(request: HttpRequest) -> HttpResponse:
    """Handle Google OIDC callback, auto-promote admin email, or queue Account Creation Request."""
    state = request.GET.get("state")
    code = request.GET.get("code")
    expected_state = request.session.pop("google_oauth_state", None)
    if not code or (expected_state and state != expected_state):
        messages.error(request, "Invalid or expired Google OAuth state. Please try again.")
        return redirect("login")

    callback_url = f"{settings.BASE_URL}{reverse('google_callback')}"
    try:
        with httpx.Client(timeout=12.0) as client:
            token_resp = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_OIDC_CLIENT_ID,
                    "client_secret": settings.GOOGLE_OIDC_CLIENT_SECRET,
                    "redirect_uri": callback_url,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()
            userinfo_resp = client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            userinfo_resp.raise_for_status()
            profile = userinfo_resp.json()
    except Exception as exc:
        messages.error(request, f"Google authentication failed: {exc}")
        return redirect("login")

    email = (profile.get("email") or "").strip().lower()
    if not email:
        messages.error(request, "Google account did not return an email address.")
        return redirect("login")

    sub = profile.get("sub") or ""
    name = profile.get("name") or email.split("@")[0]
    picture = profile.get("picture") or ""

    is_designated_admin = bool(
        settings.ARCANE_ADMIN_EMAIL and email == settings.ARCANE_ADMIN_EMAIL
    )

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        base_username = email.split("@")[0].replace(".", "_")[:24] or "planeswalker"
        username = base_username
        idx = 2
        while User.objects.filter(username__iexact=username).exists():
            username = f"{base_username}{idx}"
            idx += 1

        user = User.objects.create(
            username=username,
            email=email,
            display_name=name,
            avatar_url=picture,
            google_sub=sub,
            is_staff=is_designated_admin,
            is_superuser=is_designated_admin,
            approval_status=(
                User.ApprovalStatus.APPROVED
                if is_designated_admin
                else User.ApprovalStatus.PENDING
            ),
        )
        if not is_designated_admin:
            AccountRequest.objects.get_or_create(
                user=user,
                defaults={"status": AccountRequest.Status.PENDING},
            )
    else:
        updated_fields = []
        if picture and user.avatar_url != picture:
            user.avatar_url = picture
            updated_fields.append("avatar_url")
        if sub and user.google_sub != sub:
            user.google_sub = sub
            updated_fields.append("google_sub")
        if is_designated_admin and (not user.is_superuser or not user.is_approved):
            user.is_staff = True
            user.is_superuser = True
            user.approval_status = User.ApprovalStatus.APPROVED
            updated_fields.extend(["is_staff", "is_superuser", "approval_status"])
        if updated_fields:
            user.save(update_fields=updated_fields)

    login(request, user)
    if user.is_approved:
        messages.success(request, f"Welcome back to the Arcane Vault, {user.get_display_name()}.")
        return redirect("my_vault")
    return redirect("request_access")


def dev_quick_login(request: HttpRequest) -> HttpResponse:
    """Local development helper (active only when DEBUG=True) to sign in for local testing."""
    if not settings.DEBUG or request.method != "POST":
        return HttpResponseForbidden("Only available in local DEBUG mode.")
    email = (request.POST.get("email") or "archmage@localhost").strip().lower()
    role = request.POST.get("role", "admin")
    is_admin = role == "admin" or (
        settings.ARCANE_ADMIN_EMAIL and email == settings.ARCANE_ADMIN_EMAIL
    )
    user, created = User.objects.get_or_create(
        email=email,
        defaults={
            "username": email.split("@")[0][:24],
            "display_name": email.split("@")[0].title(),
            "is_staff": is_admin,
            "is_superuser": is_admin,
            "approval_status": (
                User.ApprovalStatus.APPROVED
                if is_admin
                else User.ApprovalStatus.PENDING
            ),
        },
    )
    if not is_admin and created:
        AccountRequest.objects.get_or_create(user=user)
    login(request, user)
    return redirect("my_vault" if user.is_approved else "request_access")


def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    messages.info(request, "You have been signed out of the Arcane Vault.")
    return redirect("card_explorer")


@login_required
def request_access_view(request: HttpRequest) -> HttpResponse:
    """Screen shown to unapproved users where they can submit/update their Account Creation Request."""
    if request.user.is_approved:
        return redirect("my_vault")

    acc_req, _ = AccountRequest.objects.get_or_create(user=request.user)
    if request.method == "POST":
        note = (request.POST.get("note") or "").strip()
        acc_req.note = note
        if acc_req.status == AccountRequest.Status.REJECTED:
            acc_req.status = AccountRequest.Status.PENDING
            request.user.approval_status = User.ApprovalStatus.PENDING
            request.user.save(update_fields=["approval_status"])
        acc_req.save(update_fields=["note", "status"])
        messages.success(
            request,
            "Your account creation request has been submitted to the Vault Administrators.",
        )
        return redirect("request_access")

    return render(
        request,
        "vault/request_access.html",
        {"account_request": acc_req},
    )


@login_required
def admin_requests_dashboard(request: HttpRequest) -> HttpResponse:
    """Admin Dashboard to approve or reject user Account Creation Requests and pre-approve emails."""
    if not (request.user.is_staff or request.user.is_superuser):
        return HttpResponseForbidden("Administrator privileges required.")

    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("approve", "reject"):
            req_id = request.POST.get("request_id")
            acc_req = get_object_or_404(AccountRequest, id=req_id)
            reason = (request.POST.get("admin_reason") or "").strip()
            if action == "approve":
                acc_req.approve(request.user, reason=reason)
                messages.success(
                    request,
                    f"Approved account access for {acc_req.user.email}.",
                )
            else:
                acc_req.reject(request.user, reason=reason)
                messages.warning(
                    request,
                    f"Rejected account request for {acc_req.user.email}.",
                )
        elif action == "preapprove_email":
            email = (request.POST.get("email") or "").strip().lower()
            if email:
                base_username = email.split("@")[0].replace(".", "_")[:24]
                username = base_username
                idx = 2
                while User.objects.filter(username__iexact=username).exists():
                    username = f"{base_username}{idx}"
                    idx += 1
                user, created = User.objects.get_or_create(
                    email=email,
                    defaults={
                        "username": username,
                        "display_name": base_username.title(),
                        "approval_status": User.ApprovalStatus.APPROVED,
                    },
                )
                if not created:
                    user.approval_status = User.ApprovalStatus.APPROVED
                    user.save(update_fields=["approval_status"])
                AccountRequest.objects.filter(user=user).update(
                    status=AccountRequest.Status.APPROVED
                )
                messages.success(
                    request,
                    f"Pre-approved {email}. They will have immediate access upon Google sign-in.",
                )
        return redirect("admin_requests_dashboard")

    pending_requests = AccountRequest.objects.filter(
        status=AccountRequest.Status.PENDING
    ).select_related("user")
    reviewed_requests = (
        AccountRequest.objects.exclude(status=AccountRequest.Status.PENDING)
        .select_related("user", "reviewed_by")[:30]
    )
    all_users = User.objects.all().order_by("-created_at")

    return render(
        request,
        "vault/admin_requests.html",
        {
            "pending_requests": pending_requests,
            "reviewed_requests": reviewed_requests,
            "all_users": all_users,
        },
    )


# ---------------------------------------------------------------------------
# View 1: Card Explorer (Scryfall Syntax Search)
# ---------------------------------------------------------------------------
def card_explorer_view(request: HttpRequest) -> HttpResponse:
    query = (request.GET.get("q") or "").strip()
    page = max(1, int(request.GET.get("page") or 1))
    order = request.GET.get("order") or "edhrec"

    search_result = search_scryfall(query or "f:commander", page=page, order=order)
    cards: list[ScryfallCard] = search_result["cards"]

    owned_by_oracle: dict[str, int] = defaultdict(int)
    usage_map: dict[str, list[dict]] = {}
    user_decks: list[Deck] = []

    if request.user.is_authenticated and request.user.is_approved:
        user_decks = list(Deck.objects.filter(user=request.user).order_by("-updated_at"))
        usage_map = get_oracle_deck_usage_map(request.user)
        oracles = [c.oracle_id for c in cards]
        for item in CollectionItem.objects.filter(
            user=request.user, oracle_id__in=oracles
        ):
            owned_by_oracle[item.oracle_id] += item.quantity

    enriched_cards = []
    for c in cards:
        owned_qty = owned_by_oracle.get(c.oracle_id, 0)
        decks_using = [
            d for d in usage_map.get(c.oracle_id, []) if d["allocated"] > 0
        ]
        enriched_cards.append(
            {
                "card": c,
                "owned_qty": owned_qty,
                "decks_using": decks_using,
                "price_usd": c.get_unit_price("nonfoil"),
                "price_foil": c.get_unit_price("foil"),
            }
        )

    return render(
        request,
        "vault/card_explorer.html",
        {
            "query": query,
            "search_result": search_result,
            "enriched_cards": enriched_cards,
            "user_decks": user_decks,
            "order": order,
            "active_nav": "explorer",
        },
    )


@login_required
def quick_card_action(request: HttpRequest) -> HttpResponse:
    """Handle 1-click Add to Vault, Add to Deck, or Add to Wishlist from any screen."""
    if request.method != "POST":
        return redirect("card_explorer")

    scryfall_id = request.POST.get("scryfall_id")
    card_name = request.POST.get("card_name")
    action_type = request.POST.get("action_type", "add_vault")
    quantity = max(1, int(request.POST.get("quantity") or 1))
    finish = request.POST.get("finish") or CollectionItem.Finish.NONFOIL
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"

    card = resolve_single_card(scryfall_id=scryfall_id, name=card_name)
    if not card:
        messages.error(request, "Could not resolve that card from Scryfall.")
        return redirect(next_url)

    if action_type == "add_vault":
        add_card_to_collection(
            user=request.user,
            card=card,
            quantity=quantity,
            finish=finish,
        )
        messages.success(
            request,
            f"Added {quantity}x {card.name} ({card.set_code.upper()}) to your Vault and updated deck allocations.",
        )
    elif action_type == "add_deck":
        deck_id = request.POST.get("deck_id")
        deck = get_object_or_404(Deck, id=deck_id, user=request.user)
        is_commander = request.POST.get("is_commander") == "true"
        role_group = request.POST.get("role_group") or None
        dc = add_card_to_deck(
            deck=deck,
            card=card,
            quantity=quantity,
            role_group=role_group,
            is_commander=is_commander,
            finish=finish,
        )
        if dc.missing_quantity > 0:
            messages.warning(
                request,
                f"Added {card.name} to '{deck.name}'. No unallocated physical copy was available in your Vault, so {dc.missing_quantity}x was automatically added to your Wishlist!",
            )
        else:
            messages.success(
                request,
                f"Added {card.name} to '{deck.name}' (allocated from your physical Vault).",
            )
    elif action_type == "add_wishlist":
        wl, created = WishlistItem.objects.get_or_create(
            user=request.user,
            oracle_id=card.oracle_id,
            auto_added_from_deck=False,
            source_deck=None,
            defaults={
                "card": card,
                "quantity_needed": quantity,
                "is_public": request.user.wishlist_is_public,
            },
        )
        if not created:
            wl.quantity_needed += quantity
            wl.save(update_fields=["quantity_needed"])
        messages.info(request, f"Added {quantity}x {card.name} to your Wishlist.")

    return redirect(next_url)


# ---------------------------------------------------------------------------
# View 2: My Vault & Wishlist
# ---------------------------------------------------------------------------
@login_required
def my_vault_view(request: HttpRequest) -> HttpResponse:
    tab = request.GET.get("tab", "collection")
    q = (request.GET.get("q") or "").strip()
    filter_mode = request.GET.get("filter", "all")  # all | in_deck | unallocated | foil

    if request.method == "POST":
        form_action = request.POST.get("form_action")
        if form_action == "bulk_import_collection":
            raw_text = request.POST.get("moxfield_text") or ""
            parsed = parse_moxfield_text(raw_text)
            identifiers = [
                {
                    k: v
                    for k, v in {
                        "name": p["name"],
                        "set": p["set"],
                        "collector_number": p["collector_number"],
                    }.items()
                    if v
                }
                for p in parsed
            ]
            resolved = bulk_resolve_cards(identifiers)
            added_total = 0
            for p in parsed:
                card = None
                if p["set"] and p["collector_number"]:
                    card = resolved.get(
                        f"{p['set'].lower()}:{str(p['collector_number']).lower()}"
                    )
                if not card:
                    card = resolved.get(p["name"].lower()) or resolve_single_card(
                        name=p["name"],
                        set_code=p["set"],
                        collector_number=p["collector_number"],
                    )
                if card:
                    add_card_to_collection(
                        user=request.user,
                        card=card,
                        quantity=p["quantity"],
                        finish=p["finish"],
                    )
                    added_total += p["quantity"]
            messages.success(
                request,
                f"Bulk-imported {added_total} physical cards into your Vault and reconciled all Commander deck allocations!",
            )
            return redirect("my_vault")

        elif form_action == "update_collection_qty":
            item_id = request.POST.get("item_id")
            new_qty = int(request.POST.get("quantity") or 0)
            item = get_object_or_404(CollectionItem, id=item_id, user=request.user)
            oid = item.oracle_id
            if new_qty <= 0:
                item.delete()
                messages.info(request, f"Removed {item.card.name} from your Vault.")
            else:
                item.quantity = new_qty
                item.save(update_fields=["quantity", "updated_at"])
            reconcile_user_allocations(request.user, [oid])
            return redirect("my_vault")

        elif form_action == "acquire_wishlist_item":
            wl_id = request.POST.get("wishlist_id")
            wl = get_object_or_404(WishlistItem, id=wl_id, user=request.user)
            qty = wl.quantity_needed
            card = wl.card
            if not wl.auto_added_from_deck:
                wl.delete()
            add_card_to_collection(user=request.user, card=card, quantity=qty)
            messages.success(
                request,
                f"Acquired {qty}x {card.name}! Added to your physical Vault and allocated to your deck.",
            )
            return redirect(f"{reverse('my_vault')}?tab=wishlist")

        elif form_action == "delete_wishlist_item":
            wl_id = request.POST.get("wishlist_id")
            wl = get_object_or_404(WishlistItem, id=wl_id, user=request.user)
            wl.delete()
            messages.info(request, "Removed item from your Wishlist.")
            return redirect(f"{reverse('my_vault')}?tab=wishlist")

    items_qs = CollectionItem.objects.filter(user=request.user).select_related("card")
    if q:
        items_qs = apply_local_scryfall_syntax(items_qs, q, card_prefix="card__")
    if filter_mode == "foil":
        items_qs = items_qs.filter(finish__in=["foil", "etched"])

    usage_map = get_oracle_deck_usage_map(request.user)

    vault_entries = []
    allocated_card_copies = 0
    for item in items_qs:
        deck_usages = [
            u for u in usage_map.get(item.oracle_id, []) if u["allocated"] > 0
        ]
        total_allocated_for_oracle = sum(u["allocated"] for u in deck_usages)
        if filter_mode == "in_deck" and not deck_usages:
            continue
        if filter_mode == "unallocated" and deck_usages:
            continue
        allocated_card_copies += min(item.quantity, total_allocated_for_oracle)
        vault_entries.append(
            {
                "item": item,
                "card": item.card,
                "deck_usages": deck_usages,
                "total_allocated": total_allocated_for_oracle,
                "unit_price": item.card.get_unit_price(item.finish),
                "total_price": item.total_price,
            }
        )

    wishlist_qs = WishlistItem.objects.filter(user=request.user).select_related(
        "card", "source_deck"
    )
    if q:
        wishlist_qs = apply_local_scryfall_syntax(wishlist_qs, q, card_prefix="card__")
    wishlist_total_cost = sum(
        (w.estimated_cost for w in wishlist_qs), Decimal("0.00")
    )

    return render(
        request,
        "vault/my_vault.html",
        {
            "tab": tab,
            "q": q,
            "filter_mode": filter_mode,
            "vault_entries": vault_entries,
            "allocated_card_copies": allocated_card_copies,
            "wishlist_items": wishlist_qs,
            "wishlist_total_cost": wishlist_total_cost,
            "active_nav": "vault",
        },
    )


@login_required
def export_collection_csv_view(request: HttpRequest) -> HttpResponse:
    csv_data = export_collection_to_moxfield_csv(request.user)
    response = HttpResponse(csv_data, content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="{request.user.username}_arcane_vault_collection.csv"'
    )
    return response


# ---------------------------------------------------------------------------
# View 3: Commander Deck Workshop
# ---------------------------------------------------------------------------
@login_required
def decks_index_view(request: HttpRequest) -> HttpResponse:
    """Redirect to the user's most recently updated deck or render the Deck Workshop hub."""
    if request.method == "POST":
        return _handle_deck_workshop_post(request, current_deck=None)

    latest_deck = Deck.objects.filter(user=request.user).order_by("-updated_at").first()
    if latest_deck and request.GET.get("list") != "1":
        return redirect("deck_workshop", slug=latest_deck.slug)

    decks = Deck.objects.filter(user=request.user).prefetch_related("cards__card")
    deck_summaries = []
    for d in decks:
        total_cards = sum(dc.quantity for dc in d.cards.all())
        owned_alloc = sum(dc.allocated_quantity for dc in d.cards.all())
        total_val = sum(
            (dc.card.get_unit_price(dc.finish) * Decimal(dc.quantity) for dc in d.cards.all()),
            Decimal("0.00"),
        )
        deck_summaries.append(
            {
                "deck": d,
                "commander": d.primary_commander,
                "total_cards": total_cards,
                "owned_alloc": owned_alloc,
                "wishlist_left": max(0, total_cards - owned_alloc),
                "total_val": total_val,
            }
        )
    return render(
        request,
        "vault/decks_list.html",
        {
            "deck_summaries": deck_summaries,
            "active_nav": "workshop",
        },
    )


@login_required
def deck_workshop_view(request: HttpRequest, slug: str) -> HttpResponse:
    deck = get_object_or_404(Deck, user=request.user, slug=slug)
    if request.method == "POST":
        return _handle_deck_workshop_post(request, current_deck=deck)

    q = (request.GET.get("q") or "").strip()
    view_mode = request.GET.get("view", "groups")  # groups | types | visual

    dcs_qs = deck.cards.select_related("card").all()
    if q:
        dcs_qs = apply_local_scryfall_syntax(dcs_qs, q, card_prefix="card__")

    usage_map = get_oracle_deck_usage_map(request.user)
    all_deck_cards = list(deck.cards.select_related("card").all())

    total_count = sum(dc.quantity for dc in all_deck_cards)
    allocated_count = sum(dc.allocated_quantity for dc in all_deck_cards)
    wishlist_left = max(0, total_count - allocated_count)

    total_value = Decimal("0.00")
    vault_owned_value = Decimal("0.00")
    missing_wishlist_value = Decimal("0.00")
    cmc_sum_all = 0.0
    cmc_sum_nonland = 0.0
    nonland_count = 0

    for dc in all_deck_cards:
        unit_p = dc.card.get_unit_price(dc.finish)
        total_value += unit_p * Decimal(dc.quantity)
        vault_owned_value += unit_p * Decimal(dc.allocated_quantity)
        missing_wishlist_value += unit_p * Decimal(dc.missing_quantity)
        cmc_sum_all += dc.card.cmc * dc.quantity
        if "land" not in (dc.card.type_line or "").lower():
            cmc_sum_nonland += dc.card.cmc * dc.quantity
            nonland_count += dc.quantity

    avg_cmc_all = (cmc_sum_all / total_count) if total_count else 0.0
    avg_cmc_nonland = (cmc_sum_nonland / nonland_count) if nonland_count else 0.0

    grouped_cards: dict[str, dict] = {}
    for dc in dcs_qs:
        group_name = (
            dc.role_group
            if view_mode != "types"
            else (dc.card.type_line.split("—")[0].strip() or "Other")
        )
        if group_name not in grouped_cards:
            grouped_cards[group_name] = {
                "name": group_name,
                "cards": [],
                "total_qty": 0,
                "owned_qty": 0,
                "wishlist_qty": 0,
                "est_value": Decimal("0.00"),
            }
        other_decks_using = [
            u["deck_name"]
            for u in usage_map.get(dc.oracle_id, [])
            if u["deck_id"] != deck.id and u["allocated"] > 0
        ]
        unit_price = dc.card.get_unit_price(dc.finish)
        grouped_cards[group_name]["cards"].append(
            {
                "dc": dc,
                "card": dc.card,
                "unit_price": unit_price,
                "line_price": unit_price * Decimal(dc.quantity),
                "other_decks_using": other_decks_using,
            }
        )
        grouped_cards[group_name]["total_qty"] += dc.quantity
        grouped_cards[group_name]["owned_qty"] += dc.allocated_quantity
        grouped_cards[group_name]["wishlist_qty"] += dc.missing_quantity
        grouped_cards[group_name]["est_value"] += unit_price * Decimal(dc.quantity)

    ordered_groups = sorted(
        grouped_cards.values(),
        key=lambda g: (
            ROLE_GROUP_ORDER.index(g["name"])
            if g["name"] in ROLE_GROUP_ORDER
            else 99,
            g["name"],
        ),
    )

    moxfield_export_text = export_deck_to_moxfield_text(deck)
    user_decks = Deck.objects.filter(user=request.user).order_by("-updated_at")

    return render(
        request,
        "vault/deck_workshop.html",
        {
            "deck": deck,
            "commander": deck.primary_commander,
            "user_decks": user_decks,
            "ordered_groups": ordered_groups,
            "total_count": total_count,
            "allocated_count": allocated_count,
            "wishlist_left": wishlist_left,
            "total_value": total_value,
            "vault_owned_value": vault_owned_value,
            "missing_wishlist_value": missing_wishlist_value,
            "avg_cmc_all": avg_cmc_all,
            "avg_cmc_nonland": avg_cmc_nonland,
            "moxfield_export_text": moxfield_export_text,
            "role_group_choices": ROLE_GROUP_ORDER,
            "q": q,
            "view_mode": view_mode,
            "active_nav": "workshop",
        },
    )


def _handle_deck_workshop_post(
    request: HttpRequest, current_deck: Deck | None
) -> HttpResponse:
    form_action = request.POST.get("form_action")

    if form_action == "create_or_import_deck":
        name = (request.POST.get("name") or "New Commander Deck").strip()
        commander_name = (request.POST.get("commander_name") or "").strip()
        moxfield_text = request.POST.get("moxfield_text") or ""
        moxfield_url = (request.POST.get("moxfield_url") or "").strip()
        add_to_collection = request.POST.get("add_to_physical_collection") == "on"
        is_public = request.POST.get("is_public", "on") == "on"

        try:
            deck, meta = import_moxfield_to_deck(
                user=request.user,
                deck_name=name,
                raw_text=moxfield_text,
                moxfield_url=moxfield_url,
                commander_name=commander_name,
                add_to_physical_collection=add_to_collection,
                is_public=is_public,
            )
            if add_to_collection:
                messages.success(
                    request,
                    f"Created deck '{deck.name}' with {meta['imported_cards']} cards and added all cards directly into your physical Vault collection!",
                )
            else:
                messages.success(
                    request,
                    f"Created deck '{deck.name}' ({meta['imported_cards']} cards). Unowned or already-in-use cards were automatically added to your Wishlist.",
                )
            return redirect("deck_workshop", slug=deck.slug)
        except Exception as exc:
            messages.error(request, f"Could not import deck: {exc}")
            return redirect("decks_index")

    if not current_deck:
        return redirect("decks_index")

    if form_action == "add_card_to_deck":
        card_query = (request.POST.get("card_name") or "").strip()
        role_group = request.POST.get("role_group") or None
        is_commander = request.POST.get("is_commander") == "on"
        quantity = max(1, int(request.POST.get("quantity") or 1))
        card = resolve_single_card(name=card_query)
        if not card:
            messages.error(request, f"Could not find card '{card_query}' on Scryfall.")
        else:
            dc = add_card_to_deck(
                deck=current_deck,
                card=card,
                quantity=quantity,
                role_group=role_group,
                is_commander=is_commander,
            )
            if dc.missing_quantity > 0:
                messages.warning(
                    request,
                    f"Added {card.name} to '{current_deck.name}'. Because all owned physical copies are either 0 or already in use in another deck, {dc.missing_quantity}x was automatically added to your Wishlist!",
                )
            else:
                messages.success(
                    request,
                    f"Added {card.name} to '{current_deck.name}' (allocated from your physical Vault).",
                )

    elif form_action == "add_deck_to_collection":
        mode = request.POST.get("mode", "missing_only")  # "all" or "missing_only"
        res = add_deck_to_physical_collection(current_deck, mode=mode)
        if mode == "all":
            messages.success(
                request,
                f"Added all {res['cards_added']} physical cards from '{current_deck.name}' into your Vault collection and allocated them to this deck!",
            )
        else:
            messages.success(
                request,
                f"Acquired {res['cards_added']} missing wishlist cards into your physical Vault collection for '{current_deck.name}'!",
            )

    elif form_action == "remove_deck_card":
        dc_id = request.POST.get("deck_card_id")
        dc = get_object_or_404(DeckCard, id=dc_id, deck=current_deck)
        oid = dc.oracle_id
        cname = dc.card.name
        dc.delete()
        reconcile_user_allocations(request.user, [oid])
        messages.info(
            request,
            f"Removed {cname} from '{current_deck.name}' and updated cross-deck allocations & wishlist.",
        )

    elif form_action == "set_commander":
        dc_id = request.POST.get("deck_card_id")
        dc = get_object_or_404(DeckCard, id=dc_id, deck=current_deck)
        dc.is_commander = not dc.is_commander
        if dc.is_commander:
            dc.role_group = "Commander"
        dc.save(update_fields=["is_commander", "role_group"])
        messages.success(request, f"Updated Commander status for {dc.card.name}.")

    elif form_action == "delete_deck":
        oracles = list(current_deck.cards.values_list("oracle_id", flat=True))
        dname = current_deck.name
        current_deck.delete()
        reconcile_user_allocations(request.user, oracles)
        messages.info(request, f"Deleted deck '{dname}' and freed its allocated cards.")
        return redirect(f"{reverse('decks_index')}?list=1")

    return redirect("deck_workshop", slug=current_deck.slug)


# ---------------------------------------------------------------------------
# View 4: Deck Analysis & Playtest (Goldfish Simulator)
# ---------------------------------------------------------------------------
@login_required
def deck_analysis_playtest_view(
    request: HttpRequest, slug: str | None = None
) -> HttpResponse:
    if slug:
        deck = get_object_or_404(Deck, user=request.user, slug=slug)
    else:
        deck = Deck.objects.filter(user=request.user).order_by("-updated_at").first()
        if not deck:
            messages.info(request, "Create or import a Commander deck first to run analysis & playtest.")
            return redirect("decks_index")

    dcs = list(deck.cards.select_related("card").all())
    curve_buckets = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0}
    color_counts = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
    type_counts: dict[str, int] = defaultdict(int)
    library_pool: list[ScryfallCard] = []

    for dc in dcs:
        tl = (dc.card.type_line or "").lower()
        if not dc.is_commander:
            for _ in range(dc.quantity):
                library_pool.append(dc.card)

        if "land" not in tl:
            bucket = min(7, max(0, int(dc.card.cmc or 0)))
            curve_buckets[bucket] += dc.quantity

        c_id = dc.card.color_identity or []
        if not c_id and "land" not in tl:
            color_counts["C"] += dc.quantity
        else:
            for col in c_id:
                if col in color_counts:
                    color_counts[col] += dc.quantity

        for tname in ("Creature", "Artifact", "Enchantment", "Instant", "Sorcery", "Planeswalker", "Land"):
            if tname.lower() in tl:
                type_counts[tname] += dc.quantity
                break

    max_curve = max(curve_buckets.values()) if any(curve_buckets.values()) else 1
    curve_bars = [
        {
            "label": "7+" if k == 7 else str(k),
            "count": v,
            "height_pct": int((v / max_curve) * 100) if max_curve else 0,
        }
        for k, v in curve_buckets.items()
    ]

    random.shuffle(library_pool)
    opening_hand = library_pool[:7]
    next_draws = library_pool[7:15]
    user_decks = Deck.objects.filter(user=request.user).order_by("-updated_at")

    return render(
        request,
        "vault/deck_analysis_playtest.html",
        {
            "deck": deck,
            "commander": deck.primary_commander,
            "user_decks": user_decks,
            "curve_bars": curve_bars,
            "color_counts": color_counts,
            "type_counts": dict(type_counts),
            "opening_hand": opening_hand,
            "next_draws": next_draws,
            "active_nav": "analysis",
        },
    )


# ---------------------------------------------------------------------------
# View 5: User Settings, Public Visibility & AI API Keys
# ---------------------------------------------------------------------------
@login_required
def profile_settings_view(request: HttpRequest) -> HttpResponse:
    newly_created_key = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "update_profile":
            request.user.display_name = (request.POST.get("display_name") or "").strip()
            request.user.bio = (request.POST.get("bio") or "").strip()
            request.user.collection_is_public = (
                request.POST.get("collection_is_public") == "on"
            )
            request.user.wishlist_is_public = (
                request.POST.get("wishlist_is_public") == "on"
            )
            request.user.save(
                update_fields=[
                    "display_name",
                    "bio",
                    "collection_is_public",
                    "wishlist_is_public",
                ]
            )
            messages.success(request, "Updated your profile and public visibility settings.")
            return redirect("profile_settings")

        elif action == "create_api_key":
            key_name = (request.POST.get("key_name") or "AI Assistant Key").strip()
            _, raw_key = ApiKey.create_for_user(request.user, name=key_name)
            newly_created_key = raw_key
            messages.success(
                request,
                "Generated new AI API Key! Copy it below now — it will only be shown in full once.",
            )

        elif action == "revoke_api_key":
            key_id = request.POST.get("key_id")
            ApiKey.objects.filter(id=key_id, user=request.user).delete()
            messages.info(request, "Revoked API key.")
            return redirect("profile_settings")

    api_keys = ApiKey.objects.filter(user=request.user)
    return render(
        request,
        "vault/profile_settings.html",
        {
            "api_keys": api_keys,
            "newly_created_key": newly_created_key,
            "base_url": settings.BASE_URL,
            "active_nav": "settings",
        },
    )


# ---------------------------------------------------------------------------
# View 6: Public User Profile & Public Deck Share Views
# ---------------------------------------------------------------------------
def public_profile_view(request: HttpRequest, username: str) -> HttpResponse:
    profile_user = get_object_or_404(User, username__iexact=username)
    is_owner = request.user.is_authenticated and request.user.id == profile_user.id

    public_decks = Deck.objects.filter(user=profile_user)
    if not is_owner:
        public_decks = public_decks.filter(is_public=True)

    collection_items = []
    if profile_user.collection_is_public or is_owner:
        qs = CollectionItem.objects.filter(user=profile_user).select_related("card")
        if not is_owner:
            qs = qs.filter(is_public=True)
        collection_items = list(qs[:100])

    wishlist_items = []
    if profile_user.wishlist_is_public or is_owner:
        wqs = WishlistItem.objects.filter(user=profile_user).select_related(
            "card", "source_deck"
        )
        if not is_owner:
            wqs = wqs.filter(is_public=True)
        wishlist_items = list(wqs[:100])

    return render(
        request,
        "vault/public_profile.html",
        {
            "profile_user": profile_user,
            "is_owner": is_owner,
            "public_decks": public_decks,
            "collection_items": collection_items,
            "wishlist_items": wishlist_items,
        },
    )


def public_deck_view(request: HttpRequest, username: str, slug: str) -> HttpResponse:
    profile_user = get_object_or_404(User, username__iexact=username)
    deck = get_object_or_404(Deck, user=profile_user, slug=slug)
    is_owner = request.user.is_authenticated and request.user.id == profile_user.id
    if not deck.is_public and not is_owner:
        return HttpResponseForbidden("This Commander deck is private.")

    dcs = list(deck.cards.select_related("card").all())
    moxfield_text = export_deck_to_moxfield_text(deck)
    return render(
        request,
        "vault/public_deck.html",
        {
            "profile_user": profile_user,
            "deck": deck,
            "commander": deck.primary_commander,
            "deck_cards": dcs,
            "total_cards": sum(dc.quantity for dc in dcs),
            "moxfield_text": moxfield_text,
        },
    )
