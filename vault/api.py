"""
Django Ninja REST API (`/api/v1/`) for Arcane Vault.
Provides OpenAPI 3.1 documentation at `/api/docs` for AI Agents (Antigravity, Roko, custom scripts)
and supports authentication via `X-API-Key: arcv_...`, `Authorization: Bearer arcv_...`, or session cookies.
"""
import subprocess
from typing import Any
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import File, NinjaAPI, Schema
from ninja.files import UploadedFile
from ninja.security import APIKeyHeader, HttpBearer, django_auth
from vault.models import (
    ApiKey,
    CollectionItem,
    Deck,
    DeckCard,
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
from vault.services.vision_ai import identify_cards_from_image_bytes


def _authenticate_raw_key(raw_key: str) -> User | None:
    if not raw_key or not raw_key.startswith("arcv_"):
        return None
    hashed = ApiKey.hash_raw_key(raw_key.strip())
    api_key = (
        ApiKey.objects.select_related("user")
        .filter(key_hash=hashed, is_active=True)
        .first()
    )
    if not api_key:
        return None
    if api_key.expires_at and api_key.expires_at < timezone.now():
        return None
    if not api_key.user.is_approved:
        return None
    api_key.last_used_at = timezone.now()
    api_key.save(update_fields=["last_used_at"])
    return api_key.user


class HeaderApiKeyAuth(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request: HttpRequest, key: str | None):
        if key:
            user = _authenticate_raw_key(key)
            if user:
                request.user = user
                return user
        if getattr(request, "user", None) and request.user.is_authenticated and request.user.is_approved:
            return request.user
        return None


class BearerApiKeyAuth(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str):
        user = _authenticate_raw_key(token)
        if user:
            request.user = user
            return user
        return None


api = NinjaAPI(
    title="Arcane Vault API",
    version="1.0.0",
    description=(
        "AI-Ready REST API for managing Magic: The Gathering physical collections, "
        "Commander (EDH) decks, cross-deck card allocations, wishlists, Moxfield imports/exports, "
        "and Scryfall syntax queries."
    ),
    auth=[HeaderApiKeyAuth(), BearerApiKeyAuth(), django_auth],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class BulkCardEntrySchema(Schema):
    name: str | None = None
    scryfall_id: str | None = None
    set_code: str | None = None
    collector_number: str | None = None
    quantity: int = 1
    finish: str = "nonfoil"
    condition: str = "NM"
    notes: str = ""


class BulkCollectionAddRequest(Schema):
    cards: list[BulkCardEntrySchema] = []
    moxfield_text: str = ""


class CreateDeckRequest(Schema):
    name: str
    commander_name: str = ""
    description: str = ""
    is_public: bool = True
    moxfield_text: str = ""
    moxfield_url: str = ""
    add_to_physical_collection: bool = False


class DeckCardInputSchema(Schema):
    name: str | None = None
    scryfall_id: str | None = None
    set_code: str | None = None
    collector_number: str | None = None
    quantity: int = 1
    role_group: str | None = None
    is_commander: bool = False
    finish: str = "nonfoil"


class DeckCardsMutationRequest(Schema):
    add: list[DeckCardInputSchema] = []
    remove_scryfall_ids: list[str] = []


class AddDeckToCollectionRequest(Schema):
    mode: str = "all"  # "all" or "missing_only"


class WishlistAddRequest(Schema):
    name: str | None = None
    scryfall_id: str | None = None
    set_code: str | None = None
    collector_number: str | None = None
    quantity_needed: int = 1
    priority: str = "high"
    notes: str = ""


# ---------------------------------------------------------------------------
# Scryfall Endpoints
# ---------------------------------------------------------------------------
@api.get("/scryfall/search", tags=["Scryfall"], auth=None)
def api_scryfall_search(
    request: HttpRequest, q: str = "f:commander", page: int = 1
) -> dict[str, Any]:
    """Search Scryfall using full Scryfall syntax (`c:wubrg cmc<=3 o:draw`, etc.)."""
    res = search_scryfall(q, page=page)
    return {
        "query": res["query"],
        "total_cards": res["total_cards"],
        "has_more": res["has_more"],
        "page": res["page"],
        "error": res["error"],
        "cards": [
            {
                "scryfall_id": c.scryfall_id,
                "oracle_id": c.oracle_id,
                "name": c.name,
                "mana_cost": c.mana_cost,
                "cmc": c.cmc,
                "type_line": c.type_line,
                "oracle_text": c.oracle_text,
                "color_identity": c.color_identity,
                "set_code": c.set_code.upper(),
                "set_name": c.set_name,
                "collector_number": c.collector_number,
                "rarity": c.rarity,
                "image_normal": c.image_normal,
                "price_usd": str(c.get_unit_price("nonfoil")),
                "price_foil": str(c.get_unit_price("foil")),
            }
            for c in res["cards"]
        ],
    }


# ---------------------------------------------------------------------------
# Collection Endpoints
# ---------------------------------------------------------------------------
@api.get("/collection", tags=["Collection"])
def api_list_collection(
    request: HttpRequest, q: str = "", in_deck_only: bool = False
) -> dict[str, Any]:
    """List the authenticated user's physical card collection with live deck allocation badges."""
    user: User = request.auth
    qs = CollectionItem.objects.filter(user=user).select_related("card")
    if q:
        qs = apply_local_scryfall_syntax(qs, q, card_prefix="card__")

    usage_map = get_oracle_deck_usage_map(user)
    items_out = []
    for item in qs:
        decks_using = [
            d for d in usage_map.get(item.oracle_id, []) if d["allocated"] > 0
        ]
        if in_deck_only and not decks_using:
            continue
        items_out.append(
            {
                "id": item.id,
                "scryfall_id": item.card.scryfall_id,
                "oracle_id": item.oracle_id,
                "name": item.card.name,
                "set_code": item.card.set_code.upper(),
                "collector_number": item.card.collector_number,
                "type_line": item.card.type_line,
                "mana_cost": item.card.mana_cost,
                "cmc": item.card.cmc,
                "quantity": item.quantity,
                "finish": item.finish,
                "condition": item.condition,
                "unit_price_usd": str(item.card.get_unit_price(item.finish)),
                "total_price_usd": str(item.total_price),
                "image_normal": item.card.image_normal,
                "allocated_in_decks": decks_using,
            }
        )
    return {"count": len(items_out), "items": items_out}


@api.post("/collection/bulk-add", tags=["Collection"])
def api_bulk_add_collection(
    request: HttpRequest, payload: BulkCollectionAddRequest
) -> dict[str, Any]:
    """
    Bulk-add cards to the user's physical Vault collection (ideal for AI agents looking up cards
    from photos of a physical collection) and automatically reconcile deck allocations & wishlists.
    """
    user: User = request.auth
    entries: list[dict[str, Any]] = []

    for c in payload.cards:
        entries.append(
            {
                "name": c.name,
                "scryfall_id": c.scryfall_id,
                "set": c.set_code,
                "collector_number": c.collector_number,
                "quantity": c.quantity,
                "finish": c.finish,
                "condition": c.condition,
                "notes": c.notes,
            }
        )

    if payload.moxfield_text.strip():
        for parsed in parse_moxfield_text(payload.moxfield_text):
            entries.append(
                {
                    "name": parsed["name"],
                    "scryfall_id": None,
                    "set": parsed["set"],
                    "collector_number": parsed["collector_number"],
                    "quantity": parsed["quantity"],
                    "finish": parsed["finish"],
                    "condition": "NM",
                    "notes": "Bulk import",
                }
            )

    identifiers = [
        {
            k: v
            for k, v in {
                "id": e.get("scryfall_id"),
                "name": e.get("name"),
                "set": e.get("set"),
                "collector_number": e.get("collector_number"),
            }.items()
            if v
        }
        for e in entries
    ]
    resolved_map = bulk_resolve_cards(identifiers)

    added_items = []
    unresolved = []
    for e in entries:
        card = None
        if e.get("scryfall_id"):
            card = resolved_map.get(e["scryfall_id"])
        if not card and e.get("set") and e.get("collector_number"):
            card = resolved_map.get(
                f"{e['set'].lower()}:{str(e['collector_number']).lower()}"
            )
        if not card and e.get("name"):
            card = resolved_map.get(e["name"].lower())
        if not card:
            card = resolve_single_card(
                scryfall_id=e.get("scryfall_id"),
                name=e.get("name"),
                set_code=e.get("set"),
                collector_number=e.get("collector_number"),
            )
        if not card:
            unresolved.append(e.get("name") or e.get("scryfall_id") or "unknown")
            continue

        item = add_card_to_collection(
            user=user,
            card=card,
            quantity=e.get("quantity", 1),
            finish=e.get("finish") or "nonfoil",
            condition=e.get("condition") or "NM",
            notes=e.get("notes") or "",
        )
        added_items.append(
            {
                "name": card.name,
                "scryfall_id": card.scryfall_id,
                "new_total_quantity": item.quantity,
                "finish": item.finish,
            }
        )

    return {
        "added_count": len(added_items),
        "added": added_items,
        "unresolved": unresolved,
    }


@api.post("/collection/vision-scan", tags=["Collection", "AI Vision"])
def api_vision_scan_collection(
    request: HttpRequest,
    image: UploadedFile = File(...),
    commit_to_collection: bool = True,
) -> dict[str, Any]:
    """
    Upload a photo of physical Magic: The Gathering cards to automatically detect card names/sets
    via Gemini Vision, resolve them against Scryfall, and optionally add them to the collection.
    """
    user: User = request.auth
    img_bytes = image.read()
    mime_type = image.content_type or "image/jpeg"
    detected = identify_cards_from_image_bytes(img_bytes, mime_type=mime_type)

    results = []
    for d in detected:
        card = resolve_single_card(
            name=d.get("name"),
            set_code=d.get("set_code"),
            collector_number=d.get("collector_number"),
        )
        if card:
            qty = int(d.get("quantity") or 1)
            finish = d.get("finish") or "nonfoil"
            if commit_to_collection:
                add_card_to_collection(user=user, card=card, quantity=qty, finish=finish)
            results.append(
                {
                    "name": card.name,
                    "scryfall_id": card.scryfall_id,
                    "set_code": card.set_code.upper(),
                    "quantity": qty,
                    "finish": finish,
                    "committed": commit_to_collection,
                }
            )
    return {"detected_count": len(results), "cards": results}


# ---------------------------------------------------------------------------
# Decks Endpoints
# ---------------------------------------------------------------------------
@api.get("/decks", tags=["Decks"])
def api_list_decks(request: HttpRequest) -> dict[str, Any]:
    """List all Commander decks belonging to the authenticated user."""
    user: User = request.auth
    decks = Deck.objects.filter(user=user).prefetch_related("cards__card")
    out = []
    for d in decks:
        total_qty = sum(dc.quantity for dc in d.cards.all())
        alloc_qty = sum(dc.allocated_quantity for dc in d.cards.all())
        cmd = d.primary_commander
        out.append(
            {
                "id": d.id,
                "name": d.name,
                "slug": d.slug,
                "format": d.format,
                "is_public": d.is_public,
                "commander": cmd.name if cmd else None,
                "total_cards": total_qty,
                "allocated_owned_cards": alloc_qty,
                "missing_wishlist_cards": max(0, total_qty - alloc_qty),
                "updated_at": d.updated_at.isoformat(),
            }
        )
    return {"count": len(out), "decks": out}


@api.post("/decks", tags=["Decks"])
def api_create_deck(request: HttpRequest, payload: CreateDeckRequest) -> dict[str, Any]:
    """
    Create a new Commander deck (optionally importing a Moxfield decklist or URL and
    optionally adding all cards directly to the user's physical collection).
    """
    user: User = request.auth
    deck, meta = import_moxfield_to_deck(
        user=user,
        deck_name=payload.name,
        raw_text=payload.moxfield_text,
        moxfield_url=payload.moxfield_url,
        commander_name=payload.commander_name,
        add_to_physical_collection=payload.add_to_physical_collection,
        is_public=payload.is_public,
    )
    if payload.description:
        deck.description = payload.description
        deck.save(update_fields=["description"])

    return {
        "id": deck.id,
        "name": deck.name,
        "slug": deck.slug,
        "imported_cards": meta["imported_cards"],
        "unresolved_names": meta["unresolved_names"],
        "added_to_collection": meta["added_to_collection"],
    }


@api.get("/decks/{deck_id}", tags=["Decks"])
def api_get_deck(request: HttpRequest, deck_id: int) -> dict[str, Any]:
    """Get full Commander deck details including per-card ownership allocation and wishlist status."""
    user: User = request.auth
    deck = get_object_or_404(Deck, id=deck_id, user=user)
    usage_map = get_oracle_deck_usage_map(user)
    cards_out = []
    for dc in deck.cards.select_related("card").all():
        other_decks = [
            u["deck_name"]
            for u in usage_map.get(dc.oracle_id, [])
            if u["deck_id"] != deck.id and u["allocated"] > 0
        ]
        cards_out.append(
            {
                "scryfall_id": dc.card.scryfall_id,
                "oracle_id": dc.oracle_id,
                "name": dc.card.name,
                "mana_cost": dc.card.mana_cost,
                "cmc": dc.card.cmc,
                "type_line": dc.card.type_line,
                "role_group": dc.role_group,
                "is_commander": dc.is_commander,
                "quantity": dc.quantity,
                "allocated_quantity": dc.allocated_quantity,
                "missing_quantity": dc.missing_quantity,
                "status": "OWNED_ALLOCATED" if dc.is_fully_allocated else "WISHLIST_NEEDED",
                "copies_used_in_other_decks": other_decks,
                "price_usd": str(dc.card.get_unit_price(dc.finish)),
            }
        )
    return {
        "id": deck.id,
        "name": deck.name,
        "slug": deck.slug,
        "is_public": deck.is_public,
        "total_cards": sum(c["quantity"] for c in cards_out),
        "allocated_owned_cards": sum(c["allocated_quantity"] for c in cards_out),
        "cards": cards_out,
    }


@api.post("/decks/{deck_id}/cards", tags=["Decks"])
def api_mutate_deck_cards(
    request: HttpRequest, deck_id: int, payload: DeckCardsMutationRequest
) -> dict[str, Any]:
    """
    Add or remove cards in a Commander deck.
    Automatically checks if physical copies are available or already used in other decks,
    and updates the user's Wishlist accordingly.
    """
    user: User = request.auth
    deck = get_object_or_404(Deck, id=deck_id, user=user)
    affected_oracles: set[str] = set()

    if payload.remove_scryfall_ids:
        to_remove = DeckCard.objects.filter(
            deck=deck, card_id__in=payload.remove_scryfall_ids
        )
        for dc in to_remove:
            affected_oracles.add(dc.oracle_id)
        to_remove.delete()
        reconcile_user_allocations(user, affected_oracles)

    added = []
    for entry in payload.add:
        card = resolve_single_card(
            scryfall_id=entry.scryfall_id,
            name=entry.name,
            set_code=entry.set_code,
            collector_number=entry.collector_number,
        )
        if not card:
            continue
        dc = add_card_to_deck(
            deck=deck,
            card=card,
            quantity=entry.quantity,
            role_group=entry.role_group,
            is_commander=entry.is_commander,
            finish=entry.finish,
        )
        added.append(
            {
                "name": card.name,
                "quantity": dc.quantity,
                "allocated_quantity": dc.allocated_quantity,
                "auto_wishlisted": dc.missing_quantity > 0,
            }
        )

    return {"deck_id": deck.id, "added": added}


@api.post("/decks/{deck_id}/add-to-collection", tags=["Decks", "Collection"])
def api_add_deck_to_collection(
    request: HttpRequest, deck_id: int, payload: AddDeckToCollectionRequest
) -> dict[str, Any]:
    """
    Add cards from a Commander deck directly into the user's physical Vault collection.
    - `mode="all"`: Adds all cards in the deck to the physical collection (ideal for prebuilt decks).
    - `mode="missing_only"`: Adds only the unallocated/wishlist cards to the physical collection.
    """
    user: User = request.auth
    deck = get_object_or_404(Deck, id=deck_id, user=user)
    result = add_deck_to_physical_collection(deck, mode=payload.mode)
    return {"deck_id": deck.id, "mode": payload.mode, **result}


@api.get("/decks/{deck_id}/export-moxfield", tags=["Decks"])
def api_export_deck_moxfield(request: HttpRequest, deck_id: int) -> dict[str, Any]:
    """Export a Commander deck in standard Moxfield text format."""
    user: User = request.auth
    deck = get_object_or_404(Deck, id=deck_id, user=user)
    return {
        "deck_id": deck.id,
        "name": deck.name,
        "moxfield_text": export_deck_to_moxfield_text(deck),
    }


# ---------------------------------------------------------------------------
# Wishlist Endpoints
# ---------------------------------------------------------------------------
@api.get("/wishlist", tags=["Wishlist"])
def api_list_wishlist(request: HttpRequest) -> dict[str, Any]:
    """List all manual and deck-triggered wishlist cards for the authenticated user."""
    user: User = request.auth
    items = WishlistItem.objects.filter(user=user).select_related("card", "source_deck")
    return {
        "count": items.count(),
        "items": [
            {
                "id": w.id,
                "scryfall_id": w.card.scryfall_id,
                "oracle_id": w.oracle_id,
                "name": w.card.name,
                "set_code": w.card.set_code.upper(),
                "quantity_needed": w.quantity_needed,
                "auto_added_from_deck": w.auto_added_from_deck,
                "source_deck_name": w.source_deck.name if w.source_deck else None,
                "priority": w.priority,
                "estimated_cost_usd": str(w.estimated_cost),
            }
            for w in items
        ],
    }


@api.post("/wishlist", tags=["Wishlist"])
def api_add_wishlist(request: HttpRequest, payload: WishlistAddRequest) -> dict[str, Any]:
    """Manually add a card to the user's Wishlist."""
    user: User = request.auth
    card = resolve_single_card(
        scryfall_id=payload.scryfall_id,
        name=payload.name,
        set_code=payload.set_code,
        collector_number=payload.collector_number,
    )
    if not card:
        return {"error": "Card not found on Scryfall"}
    wl, _ = WishlistItem.objects.update_or_create(
        user=user,
        oracle_id=card.oracle_id,
        auto_added_from_deck=False,
        source_deck=None,
        defaults={
            "card": card,
            "quantity_needed": max(1, payload.quantity_needed),
            "priority": payload.priority,
            "is_public": user.wishlist_is_public,
            "notes": payload.notes,
        },
    )
    return {"id": wl.id, "name": card.name, "quantity_needed": wl.quantity_needed}


# ---------------------------------------------------------------------------
# System / Git-Sync Webhook Endpoint
# ---------------------------------------------------------------------------
@api.post("/system/git-sync", tags=["System"])
def api_trigger_git_sync(request: HttpRequest) -> dict[str, Any]:
    """Trigger an immediate `git pull` inside the container (Staff/Admin only)."""
    user: User = request.auth
    if not (user.is_staff or user.is_superuser):
        return {"error": "Admin privileges required"}
    try:
        res = subprocess.run(
            ["git", "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {
            "status": "ok" if res.returncode == 0 else "error",
            "stdout": res.stdout.strip(),
            "stderr": res.stderr.strip(),
        }
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
