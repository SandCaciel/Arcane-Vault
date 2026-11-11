"""
Moxfield Decklist & Collection Importer / Exporter (`vault/services/moxfield.py`).

Supports:
1. Standard Moxfield / MTGO / Arena text format:
   `1 Sol Ring (PIP) 239 *F*`
   `1x Dogmeat, Ever Loyal (PIP) 2 # !Commander`
   `1 Arcane Signet`
2. Moxfield public deck URL import (`https://www.moxfield.com/decks/<id>` or `https://moxfield.com/decks/<id>`)
3. Exporting any `Deck` or `CollectionItem` queryset to Moxfield text or CSV format
4. Optional `add_to_physical_collection=True` flag during import to immediately onboard physical prebuilt decks!
"""
import csv
import io
import re
from typing import Any
import httpx
from django.db import transaction
from vault.models import CollectionItem, Deck, DeckCard, ScryfallCard, User
from vault.services.allocation import (
    add_deck_to_physical_collection,
    reconcile_user_allocations,
)
from vault.services.scryfall import bulk_resolve_cards, resolve_single_card

MOXFIELD_LINE_RE = re.compile(
    r"^\s*(?:(?P<qty>\d+)x?\s+)?"
    r"(?P<name>[^(\n#*]+?)"
    r"(?:\s+\((?P<set>[A-Za-z0-9]{2,6})\)(?:\s+(?P<cn>[A-Za-z0-9\-★†]+))?)?"
    r"(?P<foil>\s+\*(?:F|E)\*)?"
    r"(?:\s+(?P<tags>#.*))?\s*$"
)


def parse_moxfield_text(raw_text: str) -> list[dict[str, Any]]:
    """Parse Moxfield / MTGO / Arena decklist lines into structured entries."""
    entries: list[dict[str, Any]] = []
    current_section = "mainboard"

    for raw_line in (raw_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        low = line.lower().rstrip(":")
        if low in ("commander", "commanders"):
            current_section = "commander"
            continue
        if low in ("deck", "mainboard", "main"):
            current_section = "mainboard"
            continue
        if low in ("sideboard", "maybeboard", "considering"):
            current_section = low
            continue

        m = MOXFIELD_LINE_RE.match(line)
        if not m:
            continue
        name = (m.group("name") or "").strip()
        if not name:
            continue
        qty = int(m.group("qty") or 1)
        set_code = (m.group("set") or "").strip().lower() or None
        cn = (m.group("cn") or "").strip() or None
        foil_marker = (m.group("foil") or "").strip().upper()
        tags_raw = (m.group("tags") or "").strip()

        finish = CollectionItem.Finish.NONFOIL
        if "*E*" in foil_marker:
            finish = CollectionItem.Finish.ETCHED
        elif "*F*" in foil_marker:
            finish = CollectionItem.Finish.FOIL

        is_commander = current_section == "commander" or "!commander" in tags_raw.lower()
        role_group = None
        if tags_raw:
            cleaned_tags = [
                t.strip().lstrip("!").strip()
                for t in tags_raw.split("#")
                if t.strip() and t.strip().lower() != "!commander"
            ]
            if cleaned_tags:
                role_group = cleaned_tags[0]

        if current_section in ("sideboard", "maybeboard", "considering"):
            continue

        entries.append(
            {
                "quantity": qty,
                "name": name,
                "set": set_code,
                "collector_number": cn,
                "finish": finish,
                "is_commander": is_commander,
                "role_group": role_group,
            }
        )
    return entries


def fetch_moxfield_url_decklist(moxfield_url: str) -> tuple[str | None, str]:
    """
    Attempt to fetch a public deck from a Moxfield URL (`https://moxfield.com/decks/<publicId>`).
    Returns `(deck_name, moxfield_text)`.
    """
    m = re.search(r"moxfield\.com/decks/([A-Za-z0-9_\-]+)", moxfield_url or "")
    if not m:
        raise ValueError("Invalid Moxfield deck URL.")
    public_id = m.group(1)
    api_url = f"https://api2.moxfield.com/v3/decks/all/{public_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ArcaneVault/1.0",
        "Accept": "application/json",
    }
    with httpx.Client(headers=headers, timeout=12.0) as client:
        resp = client.get(api_url)
        if resp.status_code != 200:
            raise ValueError(
                f"Moxfield API returned HTTP {resp.status_code}. Paste the Moxfield text export directly if Cloudflare blocks automated fetches."
            )
        data = resp.json()

    deck_name = data.get("name") or "Imported Moxfield Deck"
    lines: list[str] = []
    boards = data.get("boards", {})

    cmd_cards = boards.get("commanders", {}).get("cards", {})
    if cmd_cards:
        lines.append("COMMANDER:")
        for _, item in cmd_cards.items():
            c = item.get("card", {})
            qty = item.get("quantity", 1)
            cname = c.get("name", "")
            cset = (c.get("set") or "").upper()
            ccn = c.get("cn") or ""
            finish = " *F*" if item.get("finish") == "foil" else ""
            lines.append(f"{qty} {cname} ({cset}) {ccn}{finish} # !Commander")

    main_cards = boards.get("mainboard", {}).get("cards", {})
    if main_cards:
        lines.append("DECK:")
        for _, item in main_cards.items():
            c = item.get("card", {})
            qty = item.get("quantity", 1)
            cname = c.get("name", "")
            cset = (c.get("set") or "").upper()
            ccn = c.get("cn") or ""
            finish = " *F*" if item.get("finish") == "foil" else ""
            lines.append(f"{qty} {cname} ({cset}) {ccn}{finish}")

    return deck_name, "\n".join(lines)


@transaction.atomic
def import_moxfield_to_deck(
    user: User,
    deck_name: str,
    raw_text: str = "",
    moxfield_url: str = "",
    commander_name: str = "",
    add_to_physical_collection: bool = False,
    is_public: bool = True,
) -> tuple[Deck, dict[str, Any]]:
    """
    Create or update a Commander deck from Moxfield text or URL, bulk-resolving all cards
    via Scryfall, optionally adding all cards to the user's physical collection, and
    running cross-deck allocation & auto-wishlist reconciliation.
    """
    if moxfield_url and not raw_text.strip():
        fetched_name, raw_text = fetch_moxfield_url_decklist(moxfield_url)
        if not deck_name.strip():
            deck_name = fetched_name or "Imported Moxfield Deck"

    parsed = parse_moxfield_text(raw_text)
    if commander_name.strip():
        has_cmd = any(
            p["is_commander"] or p["name"].lower() == commander_name.strip().lower()
            for p in parsed
        )
        if not has_cmd:
            parsed.insert(
                0,
                {
                    "quantity": 1,
                    "name": commander_name.strip(),
                    "set": None,
                    "collector_number": None,
                    "finish": CollectionItem.Finish.NONFOIL,
                    "is_commander": True,
                    "role_group": "Commander",
                },
            )
        else:
            for p in parsed:
                if p["name"].lower() == commander_name.strip().lower():
                    p["is_commander"] = True
                    p["role_group"] = "Commander"

    # If no explicit commander was flagged and the first card is a Legendary Creature/Planeswalker, mark it later
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
    resolved_map = bulk_resolve_cards(identifiers)

    deck = Deck.objects.create(
        user=user,
        name=deck_name.strip() or "Commander Deck",
        is_public=is_public,
        moxfield_url=moxfield_url.strip(),
    )

    affected_oracles: set[str] = set()
    imported_cards = 0
    unresolved_names: list[str] = []
    commander_assigned = any(p["is_commander"] for p in parsed)

    for idx, entry in enumerate(parsed):
        card: ScryfallCard | None = None
        if entry["set"] and entry["collector_number"]:
            card = resolved_map.get(
                f"{entry['set'].lower()}:{str(entry['collector_number']).lower()}"
            )
        if not card and entry["set"]:
            card = resolved_map.get(f"{entry['name'].lower()}|{entry['set'].lower()}")
        if not card:
            card = resolved_map.get(entry["name"].lower())
        if not card and "//" in entry["name"]:
            card = resolved_map.get(entry["name"].split("//")[0].strip().lower())
        if not card:
            card = resolve_single_card(
                name=entry["name"],
                set_code=entry["set"],
                collector_number=entry["collector_number"],
            )

        if not card:
            unresolved_names.append(entry["name"])
            continue

        is_cmd = bool(entry["is_commander"])
        if not commander_assigned and idx == 0 and "legendary" in card.type_line.lower():
            is_cmd = True
            commander_assigned = True

        role_group = entry["role_group"] or card.infer_default_role_group(
            is_commander=is_cmd
        )
        dc, created = DeckCard.objects.get_or_create(
            deck=deck,
            card=card,
            is_commander=is_cmd,
            defaults={
                "oracle_id": card.oracle_id,
                "quantity": entry["quantity"],
                "finish": entry["finish"],
                "role_group": role_group,
            },
        )
        if not created:
            dc.quantity += entry["quantity"]
            dc.save(update_fields=["quantity"])

        imported_cards += entry["quantity"]
        affected_oracles.add(card.oracle_id)

    if add_to_physical_collection:
        add_deck_to_physical_collection(deck, mode="all")
    else:
        reconcile_user_allocations(user, affected_oracles)

    return deck, {
        "imported_cards": imported_cards,
        "unresolved_names": unresolved_names,
        "added_to_collection": add_to_physical_collection,
    }


def export_deck_to_moxfield_text(deck: Deck) -> str:
    """Format a Commander deck in standard Moxfield import/export text syntax."""
    lines: list[str] = []
    dcs = deck.cards.select_related("card").order_by(
        "-is_commander", "role_group", "card__name"
    )
    for dc in dcs:
        set_code = dc.card.set_code.upper()
        cn = dc.card.collector_number
        finish_tag = ""
        if dc.finish == CollectionItem.Finish.FOIL:
            finish_tag = " *F*"
        elif dc.finish == CollectionItem.Finish.ETCHED:
            finish_tag = " *E*"
        cmd_tag = " # !Commander" if dc.is_commander else ""
        if set_code and cn:
            lines.append(f"{dc.quantity} {dc.card.name} ({set_code}) {cn}{finish_tag}{cmd_tag}")
        else:
            lines.append(f"{dc.quantity} {dc.card.name}{finish_tag}{cmd_tag}")
    return "\n".join(lines)


def export_collection_to_moxfield_csv(user: User) -> str:
    """Export a user's physical collection in Moxfield-compatible CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Count",
            "Name",
            "Edition",
            "Condition",
            "Language",
            "Foil",
            "Collector Number",
            "Scryfall ID",
        ]
    )
    items = CollectionItem.objects.filter(user=user).select_related("card")
    for item in items:
        foil_val = (
            "foil"
            if item.finish == CollectionItem.Finish.FOIL
            else ("etched" if item.finish == CollectionItem.Finish.ETCHED else "")
        )
        writer.writerow(
            [
                item.quantity,
                item.card.name,
                item.card.set_code.upper(),
                item.condition,
                "English",
                foil_val,
                item.card.collector_number,
                item.card.scryfall_id,
            ]
        )
    return output.getvalue()
