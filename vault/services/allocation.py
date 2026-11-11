"""
Cross-Deck Physical Copy Allocation & Auto-Wishlist Engine (`vault/services/allocation.py`).

Core Rules:
1. A user owns `N` total physical copies of a card (`oracle_id`) across their `CollectionItem` rows.
2. Each `DeckCard` across the user's Commander decks requires `quantity` copies.
3. Copies are allocated deterministically in chronological deck/addition order (`deck__created_at`, `added_at`, `id`).
4. If a user owns 1 physical Sol Ring and uses it in "Dogmeat, Ever Loyal", that 1 copy is allocated to Dogmeat.
   When they add Sol Ring to a second deck, 0 unallocated copies remain:
   - The second deck's `DeckCard.allocated_quantity` is set to `0` (`missing_quantity = 1`).
   - A `WishlistItem(auto_added_from_deck=True, source_deck=second_deck)` is automatically created/updated.
5. When the user adds another physical copy of Sol Ring to their Vault, `reconcile_user_allocations`
   allocates the new copy to the waiting deck and automatically deletes the fulfilled auto-WishlistItem.
6. `add_deck_to_physical_collection(deck, mode="all" | "missing_only")` allows one-click onboarding of
   physical prebuilt Commander decks or newly acquired wishlist cards directly into the user's physical Vault.
"""
from collections import defaultdict
from typing import Iterable
from django.db import transaction
from django.db.models import Sum
from vault.models import (
    CollectionItem,
    Deck,
    DeckCard,
    ScryfallCard,
    User,
    WishlistItem,
)


@transaction.atomic
def reconcile_user_allocations(
    user: User, oracle_ids: Iterable[str] | None = None
) -> dict[str, int]:
    """
    Recompute `DeckCard.allocated_quantity` and synchronize automatic `WishlistItem` entries
    for the given user (scoped to `oracle_ids` if provided, or across all user cards).
    """
    if oracle_ids is not None:
        target_oracles = {oid for oid in oracle_ids if oid}
        if not target_oracles:
            return {"updated_deck_cards": 0, "auto_wishlisted": 0, "resolved_wishlists": 0}
        owned_qs = CollectionItem.objects.filter(user=user, oracle_id__in=target_oracles)
        deck_cards_qs = (
            DeckCard.objects.filter(deck__user=user, oracle_id__in=target_oracles)
            .select_related("deck", "card")
            .order_by("deck__created_at", "added_at", "id")
        )
    else:
        owned_qs = CollectionItem.objects.filter(user=user)
        deck_cards_qs = (
            DeckCard.objects.filter(deck__user=user)
            .select_related("deck", "card")
            .order_by("deck__created_at", "added_at", "id")
        )
        target_oracles = set(
            owned_qs.values_list("oracle_id", flat=True)
        ) | set(deck_cards_qs.values_list("oracle_id", flat=True))

    owned_totals: dict[str, int] = defaultdict(int)
    for row in owned_qs.values("oracle_id").annotate(total=Sum("quantity")):
        owned_totals[row["oracle_id"]] = int(row["total"] or 0)

    deck_cards_by_oracle: dict[str, list[DeckCard]] = defaultdict(list)
    for dc in deck_cards_qs:
        deck_cards_by_oracle[dc.oracle_id].append(dc)

    updated_deck_cards = 0
    auto_wishlisted = 0
    resolved_wishlists = 0

    for oid in target_oracles:
        remaining_owned = owned_totals.get(oid, 0)
        dcs = deck_cards_by_oracle.get(oid, [])
        active_deck_ids_needing_card: set[int] = set()

        for dc in dcs:
            alloc = min(dc.quantity, remaining_owned)
            remaining_owned = max(0, remaining_owned - alloc)
            if dc.allocated_quantity != alloc:
                dc.allocated_quantity = alloc
                dc.save(update_fields=["allocated_quantity"])
                updated_deck_cards += 1

            missing = dc.quantity - alloc
            if missing > 0:
                active_deck_ids_needing_card.add(dc.deck_id)
                wl, created = WishlistItem.objects.update_or_create(
                    user=user,
                    oracle_id=oid,
                    source_deck=dc.deck,
                    auto_added_from_deck=True,
                    defaults={
                        "card": dc.card,
                        "quantity_needed": missing,
                        "priority": WishlistItem.Priority.HIGH,
                        "is_public": user.wishlist_is_public,
                        "notes": f"Auto-added: Needed for deck '{dc.deck.name}'",
                    },
                )
                auto_wishlisted += 1
            else:
                deleted_count, _ = WishlistItem.objects.filter(
                    user=user,
                    oracle_id=oid,
                    source_deck=dc.deck,
                    auto_added_from_deck=True,
                ).delete()
                resolved_wishlists += deleted_count

        # Clean up any stale auto-wishlist rows for this oracle_id whose deck no longer needs it
        stale_deleted, _ = (
            WishlistItem.objects.filter(
                user=user, oracle_id=oid, auto_added_from_deck=True
            )
            .exclude(source_deck_id__in=active_deck_ids_needing_card)
            .delete()
        )
        resolved_wishlists += stale_deleted

    return {
        "updated_deck_cards": updated_deck_cards,
        "auto_wishlisted": auto_wishlisted,
        "resolved_wishlists": resolved_wishlists,
    }


@transaction.atomic
def add_card_to_collection(
    user: User,
    card: ScryfallCard,
    quantity: int = 1,
    finish: str = CollectionItem.Finish.NONFOIL,
    condition: str = CollectionItem.Condition.NM,
    notes: str = "",
) -> CollectionItem:
    """
    Add or increment physical copies of a card in the user's Vault and automatically
    reconcile deck allocations & wishlist items.
    """
    qty = max(1, int(quantity))
    item, created = CollectionItem.objects.get_or_create(
        user=user,
        card=card,
        finish=finish,
        condition=condition,
        defaults={
            "oracle_id": card.oracle_id,
            "quantity": qty,
            "is_public": user.collection_is_public,
            "notes": notes,
        },
    )
    if not created:
        item.quantity += qty
        if notes:
            item.notes = notes
        item.save(update_fields=["quantity", "notes", "updated_at"])

    reconcile_user_allocations(user, [card.oracle_id])
    return item


@transaction.atomic
def add_card_to_deck(
    deck: Deck,
    card: ScryfallCard,
    quantity: int = 1,
    role_group: str | None = None,
    is_commander: bool = False,
    finish: str = CollectionItem.Finish.NONFOIL,
) -> DeckCard:
    """
    Add a card to a Commander deck and run cross-deck allocation reconciliation.
    If all owned physical copies are already allocated in another deck (or 0 are owned),
    this automatically creates a WishlistItem linked to `deck`.
    """
    qty = max(1, int(quantity))
    group = role_group or card.infer_default_role_group(is_commander=is_commander)
    dc, created = DeckCard.objects.get_or_create(
        deck=deck,
        card=card,
        is_commander=is_commander,
        defaults={
            "oracle_id": card.oracle_id,
            "quantity": qty,
            "role_group": group,
            "finish": finish,
        },
    )
    if not created:
        dc.quantity += qty
        if role_group:
            dc.role_group = role_group
        dc.save(update_fields=["quantity", "role_group"])

    reconcile_user_allocations(deck.user, [card.oracle_id])
    dc.refresh_from_db()
    return dc


@transaction.atomic
def add_deck_to_physical_collection(
    deck: Deck, mode: str = "all"
) -> dict[str, int]:
    """
    Add cards from a Commander deck directly into the user's physical Vault collection.
    - `mode="all"`: Adds the full decklist (e.g. 100 cards of a newly bought physical Commander precon)
      into `CollectionItem` and allocates them.
    - `mode="missing_only"`: Adds only the unallocated/wishlisted cards (`missing_quantity > 0`)
      from this deck into `CollectionItem`, fulfilling and clearing their wishlist entries.
    """
    user = deck.user
    deck_cards = list(deck.cards.select_related("card").all())
    affected_oracles: set[str] = set()
    cards_added = 0
    distinct_updated = 0

    for dc in deck_cards:
        add_qty = dc.quantity if mode == "all" else dc.missing_quantity
        if add_qty <= 0:
            continue
        item, created = CollectionItem.objects.get_or_create(
            user=user,
            card=dc.card,
            finish=dc.finish or CollectionItem.Finish.NONFOIL,
            condition=CollectionItem.Condition.NM,
            defaults={
                "oracle_id": dc.card.oracle_id,
                "quantity": add_qty,
                "is_public": user.collection_is_public,
                "notes": f"Added from deck: {deck.name}",
            },
        )
        if not created:
            item.quantity += add_qty
            item.save(update_fields=["quantity", "updated_at"])
        cards_added += add_qty
        distinct_updated += 1
        affected_oracles.add(dc.card.oracle_id)

    reconcile_user_allocations(user, affected_oracles)
    return {
        "cards_added": cards_added,
        "distinct_cards": distinct_updated,
    }


def get_oracle_deck_usage_map(user: User) -> dict[str, list[dict]]:
    """
    Return a mapping of `oracle_id -> [{"deck_id", "deck_name", "deck_slug", "allocated", "quantity"}]`
    so both My Vault and Deck Workshop can display which decks are actively using each physical copy.
    """
    usage: dict[str, list[dict]] = defaultdict(list)
    dcs = (
        DeckCard.objects.filter(deck__user=user)
        .select_related("deck")
        .order_by("deck__name")
    )
    for dc in dcs:
        usage[dc.oracle_id].append(
            {
                "deck_id": dc.deck_id,
                "deck_name": dc.deck.name,
                "deck_slug": dc.deck.slug,
                "allocated": dc.allocated_quantity,
                "quantity": dc.quantity,
            }
        )
    return usage
