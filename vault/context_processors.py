"""Template context processor injecting global Vault stats & admin notification badges."""
from decimal import Decimal
from .models import AccountRequest, CollectionItem, Deck, WishlistItem


def vault_header_stats(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_approved:
        return {
            "header_total_cards": 0,
            "header_est_value": Decimal("0.00"),
            "header_deck_count": 0,
            "header_wishlist_count": 0,
            "header_pending_requests": 0,
            "user_decks_nav": [],
        }

    items = CollectionItem.objects.filter(user=user).select_related("card")
    total_cards = 0
    est_value = Decimal("0.00")
    for item in items:
        total_cards += item.quantity
        est_value += item.total_price

    decks = Deck.objects.filter(user=user).order_by("-updated_at")
    deck_count = decks.count()
    wishlist_count = sum(
        w.quantity_needed for w in WishlistItem.objects.filter(user=user)
    )
    pending_requests = 0
    if user.is_staff or user.is_superuser:
        pending_requests = AccountRequest.objects.filter(
            status=AccountRequest.Status.PENDING
        ).count()

    return {
        "header_total_cards": total_cards,
        "header_est_value": est_value,
        "header_deck_count": deck_count,
        "header_wishlist_count": wishlist_count,
        "header_pending_requests": pending_requests,
        "user_decks_nav": list(decks[:8]),
    }
