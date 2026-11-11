"""
Core SQLite models for Arcane Vault (Commander's Ledger):
- User (with Google OIDC subject, approval status, and public/private profile flags)
- AccountRequest (admin approval queue for new Google sign-ins)
- ApiKey (scoped SHA-256 hashed bearer tokens for AI agents & automation)
- ScryfallCard (local cache of Scryfall card metadata & pricing)
- CollectionItem (physical card inventory per user)
- Deck & DeckCard (Commander decks with physical copy allocation tracking)
- WishlistItem (manual + automatic cross-deck unallocated card wishlist)
"""
import hashlib
import secrets
from decimal import Decimal
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class User(AbstractUser):
    """Custom user supporting Google OIDC and Admin Account Approval workflow."""

    class ApprovalStatus(models.TextChoices):
        PENDING = "PENDING", "Pending Admin Approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=120, blank=True)
    avatar_url = models.URLField(max_length=500, blank=True)
    google_sub = models.CharField(max_length=255, blank=True, db_index=True)
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
        db_index=True,
    )
    bio = models.TextField(blank=True)
    collection_is_public = models.BooleanField(default=False)
    wishlist_is_public = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_approved(self) -> bool:
        return (
            self.is_superuser
            or self.is_staff
            or self.approval_status == self.ApprovalStatus.APPROVED
        )

    def get_display_name(self) -> str:
        return self.display_name or self.username or self.email.split("@")[0]

    def __str__(self) -> str:
        return f"{self.username} ({self.email})"


class AccountRequest(models.Model):
    """Account creation request submitted by a user after authenticating via Google OIDC."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="account_request"
    )
    note = models.TextField(
        blank=True,
        help_text="Optional note from the user (e.g., Discord handle or reason for joining).",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_requests",
    )
    admin_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def approve(self, admin_user: User, reason: str = ""):
        self.status = self.Status.APPROVED
        self.reviewed_by = admin_user
        self.admin_reason = reason
        self.reviewed_at = timezone.now()
        self.save()
        self.user.approval_status = User.ApprovalStatus.APPROVED
        self.user.is_active = True
        self.user.save(update_fields=["approval_status", "is_active"])

    def reject(self, admin_user: User, reason: str = ""):
        self.status = self.Status.REJECTED
        self.reviewed_by = admin_user
        self.admin_reason = reason
        self.reviewed_at = timezone.now()
        self.save()
        self.user.approval_status = User.ApprovalStatus.REJECTED
        self.user.save(update_fields=["approval_status"])

    def __str__(self) -> str:
        return f"Request({self.user.email} - {self.status})"


class ApiKey(models.Model):
    """Personal API key enabling AI agents (Antigravity, Roko, etc.) to manage a user's Vault & Decks."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100)
    key_prefix = models.CharField(max_length=20, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scopes = models.JSONField(
        default=list,
        help_text="List of allowed scopes: collection:read, collection:write, decks:read, decks:write, wishlist:write",
    )
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @staticmethod
    def hash_raw_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @classmethod
    def create_for_user(cls, user: User, name: str, scopes: list[str] | None = None):
        if scopes is None:
            scopes = [
                "collection:read",
                "collection:write",
                "decks:read",
                "decks:write",
                "wishlist:read",
                "wishlist:write",
            ]
        raw_secret = secrets.token_urlsafe(24)
        raw_key = f"arcv_{raw_secret}"
        instance = cls.objects.create(
            user=user,
            name=name,
            key_prefix=raw_key[:12],
            key_hash=cls.hash_raw_key(raw_key),
            scopes=scopes,
        )
        return instance, raw_key

    def __str__(self) -> str:
        return f"{self.name} ({self.key_prefix}...)"


class ScryfallCard(models.Model):
    """Cached Magic: The Gathering card printing metadata hydrated from the Scryfall REST API."""

    scryfall_id = models.CharField(max_length=64, primary_key=True)
    oracle_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=255, db_index=True)
    mana_cost = models.CharField(max_length=100, blank=True)
    cmc = models.FloatField(default=0.0, db_index=True)
    type_line = models.CharField(max_length=255, blank=True, db_index=True)
    oracle_text = models.TextField(blank=True)
    colors = models.JSONField(default=list)
    color_identity = models.JSONField(default=list)
    keywords = models.JSONField(default=list)
    set_code = models.CharField(max_length=20, db_index=True)
    set_name = models.CharField(max_length=150, blank=True)
    collector_number = models.CharField(max_length=30, blank=True)
    rarity = models.CharField(max_length=30, blank=True)
    image_uris = models.JSONField(default=dict)
    card_faces = models.JSONField(default=list)
    prices = models.JSONField(default=dict)
    legalities = models.JSONField(default=dict)
    scryfall_uri = models.URLField(max_length=500, blank=True)
    edhrank = models.IntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["oracle_id", "set_code"]),
            models.Index(fields=["name", "set_code"]),
        ]

    @property
    def image_normal(self) -> str:
        if self.image_uris and self.image_uris.get("normal"):
            return self.image_uris["normal"]
        if self.card_faces and isinstance(self.card_faces, list):
            first = self.card_faces[0]
            if isinstance(first, dict) and first.get("image_uris", {}).get("normal"):
                return first["image_uris"]["normal"]
        return "https://cards.scryfall.io/large/front/0/c/0c082aa8-bf7f-47f2-baf8-43ad253fd7d7.jpg"

    @property
    def image_art_crop(self) -> str:
        if self.image_uris and self.image_uris.get("art_crop"):
            return self.image_uris["art_crop"]
        if self.card_faces and isinstance(self.card_faces, list):
            first = self.card_faces[0]
            if isinstance(first, dict) and first.get("image_uris", {}).get("art_crop"):
                return first["image_uris"]["art_crop"]
        return self.image_normal

    def get_unit_price(self, finish: str = "nonfoil") -> Decimal:
        if not self.prices:
            return Decimal("0.00")
        raw = None
        if finish == "foil":
            raw = self.prices.get("usd_foil") or self.prices.get("usd")
        elif finish == "etched":
            raw = (
                self.prices.get("usd_etched")
                or self.prices.get("usd_foil")
                or self.prices.get("usd")
            )
        else:
            raw = self.prices.get("usd") or self.prices.get("usd_foil")
        try:
            return Decimal(str(raw)) if raw is not None else Decimal("0.00")
        except Exception:
            return Decimal("0.00")

    def infer_default_role_group(self, is_commander: bool = False) -> str:
        if is_commander:
            return "Commander"
        tl = (self.type_line or "").lower()
        ot = (self.oracle_text or "").lower()
        if "land" in tl:
            return "Lands & Mana Base"
        if ("add {" in ot or "search your library for a" in ot and "land" in ot) and self.cmc <= 4:
            return "Mana Ramp & Rocks"
        if "draw a card" in ot or "draw cards" in ot or "draw two" in ot or "scry" in ot and "draw" in ot:
            return "Card Draw & Value Engines"
        if "destroy target" in ot or "exile target" in ot or "counter target" in ot or "destroy all" in ot:
            return "Removal & Interaction"
        if "creature" in tl:
            return "Creatures & Threats"
        if "artifact" in tl or "enchantment" in tl:
            return "Artifacts & Enchantments"
        return "Instants & Sorceries"

    def __str__(self) -> str:
        return f"{self.name} ({self.set_code.upper()} #{self.collector_number})"


class CollectionItem(models.Model):
    """Physical card copies owned by a user in their Vault."""

    class Finish(models.TextChoices):
        NONFOIL = "nonfoil", "Non-Foil"
        FOIL = "foil", "Foil"
        ETCHED = "etched", "Etched Foil"

    class Condition(models.TextChoices):
        NM = "NM", "Near Mint"
        LP = "LP", "Lightly Played"
        MP = "MP", "Moderately Played"
        HP = "HP", "Heavily Played"
        DMG = "DMG", "Damaged"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="collection_items"
    )
    card = models.ForeignKey(
        ScryfallCard, on_delete=models.CASCADE, related_name="collection_entries"
    )
    oracle_id = models.CharField(max_length=64, db_index=True)
    quantity = models.PositiveIntegerField(default=1)
    finish = models.CharField(
        max_length=20, choices=Finish.choices, default=Finish.NONFOIL
    )
    condition = models.CharField(
        max_length=10, choices=Condition.choices, default=Condition.NM
    )
    is_public = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("user", "card", "finish", "condition")]
        ordering = ["card__name"]

    @property
    def total_price(self) -> Decimal:
        return self.card.get_unit_price(self.finish) * Decimal(self.quantity)

    def save(self, *args, **kwargs):
        if self.card_id and not self.oracle_id:
            self.oracle_id = self.card.oracle_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.quantity}x {self.card.name} [{self.finish}] ({self.user.username})"


class Deck(models.Model):
    """Commander (EDH) deck built by a user."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="decks")
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, db_index=True)
    subtitle = models.CharField(max_length=200, blank=True)
    format = models.CharField(max_length=40, default="commander")
    tier_badge = models.CharField(max_length=60, default="Commander EDH")
    description = models.TextField(blank=True)
    is_public = models.BooleanField(default=True)
    moxfield_url = models.URLField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("user", "slug")]
        ordering = ["-updated_at"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name) or "commander-deck"
            slug = base_slug
            counter = 2
            while Deck.objects.filter(user=self.user, slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def primary_commander(self):
        cmd = self.cards.filter(is_commander=True).select_related("card").first()
        if cmd:
            return cmd.card
        first_card = self.cards.select_related("card").first()
        return first_card.card if first_card else None

    def __str__(self) -> str:
        return f"{self.name} ({self.user.username})"


class DeckCard(models.Model):
    """
    A card slot inside a Commander deck.
    Tracks both `quantity` required by the deck and `allocated_quantity`
    (how many physical copies owned by the user are actively allocated to THIS deck).
    """

    deck = models.ForeignKey(Deck, on_delete=models.CASCADE, related_name="cards")
    card = models.ForeignKey(
        ScryfallCard, on_delete=models.CASCADE, related_name="deck_appearances"
    )
    oracle_id = models.CharField(max_length=64, db_index=True)
    quantity = models.PositiveIntegerField(default=1)
    allocated_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Number of physical copies from the user's Vault allocated to this deck.",
    )
    finish = models.CharField(
        max_length=20,
        choices=CollectionItem.Finish.choices,
        default=CollectionItem.Finish.NONFOIL,
    )
    role_group = models.CharField(max_length=80, default="Creatures & Threats")
    is_commander = models.BooleanField(default=False)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("deck", "card", "is_commander")]
        ordering = ["-is_commander", "card__cmc", "card__name"]

    @property
    def missing_quantity(self) -> int:
        return max(0, self.quantity - self.allocated_quantity)

    @property
    def is_fully_allocated(self) -> bool:
        return self.allocated_quantity >= self.quantity

    def save(self, *args, **kwargs):
        if self.card_id and not self.oracle_id:
            self.oracle_id = self.card.oracle_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.quantity}x {self.card.name} in {self.deck.name} (alloc={self.allocated_quantity})"


class WishlistItem(models.Model):
    """
    Cards a user wants to acquire.
    Can be added manually or automatically when a deck requires a card whose
    owned copies are 0 or already allocated in other decks.
    """

    class Priority(models.TextChoices):
        HIGH = "high", "High Priority"
        MEDIUM = "medium", "Medium Priority"
        LOW = "low", "Low Priority"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="wishlist_items"
    )
    card = models.ForeignKey(
        ScryfallCard, on_delete=models.CASCADE, related_name="wishlist_entries"
    )
    oracle_id = models.CharField(max_length=64, db_index=True)
    quantity_needed = models.PositiveIntegerField(default=1)
    auto_added_from_deck = models.BooleanField(default=False)
    source_deck = models.ForeignKey(
        Deck,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="wishlist_items",
    )
    priority = models.CharField(
        max_length=20, choices=Priority.choices, default=Priority.HIGH
    )
    is_public = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-auto_added_from_deck", "card__name"]

    @property
    def estimated_cost(self) -> Decimal:
        return self.card.get_unit_price("nonfoil") * Decimal(self.quantity_needed)

    def save(self, *args, **kwargs):
        if self.card_id and not self.oracle_id:
            self.oracle_id = self.card.oracle_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Wishlist: {self.quantity_needed}x {self.card.name} ({self.user.username})"
