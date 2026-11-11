"""
Unit & Integration Tests for Arcane Vault (`vault/tests.py`).
Verifies:
1. Cross-Deck Physical Copy Allocation & Auto-Wishlist Engine (Dogmeat Sol Ring scenario)
2. Prebuilt / Physical Deck Collection Onboarding (`add_deck_to_physical_collection`)
3. Admin Account Creation Request Gate & Approval Workflow
4. Moxfield Parser & Exporter
5. Django Ninja AI API Key (`X-API-Key: arcv_...`) Endpoints
"""
from django.test import Client, TestCase
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
)
from vault.services.moxfield import export_deck_to_moxfield_text, parse_moxfield_text


class ArcaneVaultAllocationAndAuthTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="caleb",
            email="caleb@example.com",
            password="testpassword123",
            approval_status=User.ApprovalStatus.APPROVED,
        )
        self.sol_ring = ScryfallCard.objects.create(
            scryfall_id="sol-ring-pip-239",
            oracle_id="oracle-sol-ring",
            name="Sol Ring",
            mana_cost="{1}",
            cmc=1.0,
            type_line="Artifact",
            oracle_text="{T}: Add {C}{C}.",
            set_code="pip",
            collector_number="239",
            prices={"usd": "2.50"},
        )
        self.dogmeat = ScryfallCard.objects.create(
            scryfall_id="dogmeat-pip-2",
            oracle_id="oracle-dogmeat",
            name="Dogmeat, Ever Loyal",
            mana_cost="{1}{R}{G}{W}",
            cmc=4.0,
            type_line="Legendary Creature — Dog",
            set_code="pip",
            collector_number="2",
            prices={"usd": "5.00"},
        )

    def test_cross_deck_sol_ring_allocation_and_auto_wishlist(self):
        """
        Scenario requested by user:
        - User owns 1 physical Sol Ring.
        - User puts Sol Ring into 'Dogmeat, Ever Loyal' -> allocated_quantity=1, no wishlist.
        - User puts Sol Ring into a second deck 'Urza' -> Dogmeat already uses the 1 copy,
          so Urza gets allocated_quantity=0 and Sol Ring is automatically added to Wishlist!
        - User adds a 2nd physical Sol Ring to their Vault -> Urza gets allocated_quantity=1
          and the auto-Wishlist item is automatically removed!
        """
        add_card_to_collection(self.user, self.sol_ring, quantity=1)
        deck_dogmeat = Deck.objects.create(user=self.user, name="Dogmeat, Ever Loyal")
        dc1 = add_card_to_deck(deck_dogmeat, self.sol_ring, quantity=1)

        self.assertEqual(dc1.allocated_quantity, 1)
        self.assertEqual(dc1.missing_quantity, 0)
        self.assertEqual(WishlistItem.objects.filter(user=self.user).count(), 0)

        # Now add Sol Ring to a second deck while owning only 1 physical copy
        deck_urza = Deck.objects.create(user=self.user, name="Urza Artificer")
        dc2 = add_card_to_deck(deck_urza, self.sol_ring, quantity=1)

        self.assertEqual(dc2.allocated_quantity, 0)
        self.assertEqual(dc2.missing_quantity, 1)
        wl = WishlistItem.objects.filter(
            user=self.user, oracle_id=self.sol_ring.oracle_id, source_deck=deck_urza
        ).first()
        self.assertIsNotNone(wl)
        self.assertTrue(wl.auto_added_from_deck)
        self.assertEqual(wl.quantity_needed, 1)

        # Now user acquires a 2nd physical Sol Ring and adds it to their Vault
        add_card_to_collection(self.user, self.sol_ring, quantity=1)
        dc2.refresh_from_db()
        self.assertEqual(dc2.allocated_quantity, 1)
        self.assertEqual(dc2.missing_quantity, 0)
        self.assertEqual(
            WishlistItem.objects.filter(
                user=self.user, oracle_id=self.sol_ring.oracle_id
            ).count(),
            0,
        )

    def test_add_deck_to_physical_collection_precon_onboarding(self):
        """Verify adding an unowned deck's cards directly into the user's physical collection."""
        deck = Deck.objects.create(user=self.user, name="Scrappy Survivors Precon")
        add_card_to_deck(deck, self.dogmeat, quantity=1, is_commander=True)
        add_card_to_deck(deck, self.sol_ring, quantity=1)

        # Initially 0 owned in collection -> 2 auto-wishlist items created
        self.assertEqual(WishlistItem.objects.filter(user=self.user).count(), 2)

        # Onboard the deck into physical collection
        res = add_deck_to_physical_collection(deck, mode="all")
        self.assertEqual(res["cards_added"], 2)
        self.assertEqual(
            CollectionItem.objects.filter(user=self.user).count(), 2
        )
        # Auto-wishlist items should be completely resolved
        self.assertEqual(WishlistItem.objects.filter(user=self.user).count(), 0)
        for dc in DeckCard.objects.filter(deck=deck):
            self.assertEqual(dc.allocated_quantity, dc.quantity)

    def test_account_creation_request_and_admin_approval_gate(self):
        """Unapproved Google users are gated until an Admin approves their AccountRequest."""
        pending_user = User.objects.create_user(
            username="newplayer",
            email="newplayer@example.com",
            password="pass",
            approval_status=User.ApprovalStatus.PENDING,
        )
        acc_req = AccountRequest.objects.create(
            user=pending_user, note="Hey, it's me from the Friday pod!"
        )
        client = Client()
        client.force_login(pending_user)

        resp = client.get(reverse("my_vault"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("request_access"), resp.url)

        # Admin approves the request
        admin = User.objects.create_user(
            username="admin_user",
            email="admin@example.com",
            is_staff=True,
            is_superuser=True,
            approval_status=User.ApprovalStatus.APPROVED,
        )
        acc_req.approve(admin, reason="Welcome to the pod!")
        pending_user.refresh_from_db()
        self.assertTrue(pending_user.is_approved)

        resp2 = client.get(reverse("my_vault"))
        self.assertEqual(resp2.status_code, 200)

    def test_moxfield_parser_and_api_key_endpoints(self):
        """Verify Moxfield text parsing and Django Ninja API key authentication."""
        parsed = parse_moxfield_text(
            "COMMANDER:\n1 Dogmeat, Ever Loyal (PIP) 2 *F*\n\nDECK:\n1 Sol Ring (PIP) 239"
        )
        self.assertEqual(len(parsed), 2)
        self.assertTrue(parsed[0]["is_commander"])
        self.assertEqual(parsed[0]["finish"], "foil")
        self.assertEqual(parsed[1]["name"], "Sol Ring")

        _, raw_key = ApiKey.create_for_user(self.user, name="Antigravity Test Key")
        client = Client()
        resp = client.post(
            "/api/v1/collection/bulk-add",
            data='{"cards": [{"scryfall_id": "sol-ring-pip-239", "quantity": 3}]}',
            content_type="application/json",
            HTTP_X_API_KEY=raw_key,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["added_count"], 1)

        list_resp = client.get("/api/v1/collection", HTTP_X_API_KEY=raw_key)
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.json()["items"][0]["quantity"], 3)
