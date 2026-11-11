"""
Scryfall API client & local SQLite cache manager.
Supports:
1. Full Scryfall syntax search (`q=c:wubrg cmc<=3 o:draw`, etc.)
2. Single card lookup by Scryfall ID, (set_code, collector_number), or exact/fuzzy name
3. Bulk card resolution via `POST https://api.scryfall.com/cards/collection` (up to 75 identifiers per batch)
4. Local SQLite Scryfall syntax filtering for Collection and Deck views
"""
import re
import time
from typing import Any
import httpx
from django.db.models import Q, QuerySet
from vault.models import ScryfallCard

SCRYFALL_BASE_URL = "https://api.scryfall.com"
SCRYFALL_HEADERS = {
    "User-Agent": "ArcaneVault/1.0 (https://github.com/SandCaciel/Arcane-Vault)",
    "Accept": "application/json",
}
_LAST_REQUEST_TS = 0.0


def _throttle():
    """Respect Scryfall's 10 requests/second rate limit guideline (~100ms spacing)."""
    global _LAST_REQUEST_TS
    now = time.monotonic()
    elapsed = now - _LAST_REQUEST_TS
    if elapsed < 0.095:
        time.sleep(0.095 - elapsed)
    _LAST_REQUEST_TS = time.monotonic()


def upsert_scryfall_card(data: dict[str, Any]) -> ScryfallCard:
    """Hydrate or update a ScryfallCard row in SQLite from a Scryfall API card JSON payload."""
    scryfall_id = data.get("id")
    if not scryfall_id:
        raise ValueError("Scryfall card payload missing 'id'")

    oracle_id = data.get("oracle_id") or (
        data.get("card_faces", [{}])[0].get("oracle_id", "")
        if data.get("card_faces")
        else ""
    ) or scryfall_id

    mana_cost = data.get("mana_cost") or ""
    oracle_text = data.get("oracle_text") or ""
    image_uris = data.get("image_uris") or {}
    card_faces = data.get("card_faces") or []

    if not mana_cost and card_faces and isinstance(card_faces, list):
        mana_cost = " // ".join(
            f.get("mana_cost", "") for f in card_faces if f.get("mana_cost")
        )
    if not oracle_text and card_faces and isinstance(card_faces, list):
        oracle_text = "\n//\n".join(
            f.get("oracle_text", "") for f in card_faces if f.get("oracle_text")
        )
    if not image_uris and card_faces and isinstance(card_faces, list):
        first_face_imgs = card_faces[0].get("image_uris")
        if first_face_imgs:
            image_uris = first_face_imgs

    defaults = {
        "oracle_id": oracle_id,
        "name": data.get("name", "Unknown Card"),
        "mana_cost": mana_cost,
        "cmc": float(data.get("cmc") or 0.0),
        "type_line": data.get("type_line") or "",
        "oracle_text": oracle_text,
        "colors": data.get("colors") or [],
        "color_identity": data.get("color_identity") or [],
        "keywords": data.get("keywords") or [],
        "set_code": (data.get("set") or "").lower(),
        "set_name": data.get("set_name") or "",
        "collector_number": str(data.get("collector_number") or ""),
        "rarity": data.get("rarity") or "common",
        "image_uris": image_uris,
        "card_faces": card_faces,
        "prices": data.get("prices") or {},
        "legalities": data.get("legalities") or {},
        "scryfall_uri": data.get("scryfall_uri") or "",
        "edhrank": data.get("edhrec_rank"),
    }
    card_obj, _ = ScryfallCard.objects.update_or_create(
        scryfall_id=scryfall_id, defaults=defaults
    )
    return card_obj


def search_scryfall(
    query: str,
    page: int = 1,
    unique: str = "cards",
    order: str = "edhrec",
) -> dict[str, Any]:
    """
    Search Scryfall using full Scryfall syntax (`q`) and cache the returned page in SQLite.
    Falls back to local SQLite search if Scryfall returns 404 or is unreachable.
    """
    q = (query or "").strip()
    if not q:
        q = "f:commander"

    _throttle()
    params = {
        "q": q,
        "page": max(1, int(page)),
        "unique": unique,
        "order": order,
    }
    try:
        with httpx.Client(headers=SCRYFALL_HEADERS, timeout=12.0) as client:
            resp = client.get(f"{SCRYFALL_BASE_URL}/cards/search", params=params)
            if resp.status_code == 200:
                payload = resp.json()
                raw_cards = payload.get("data", [])
                cards = [upsert_scryfall_card(c) for c in raw_cards[:36]]
                return {
                    "cards": cards,
                    "total_cards": payload.get("total_cards", len(cards)),
                    "has_more": bool(payload.get("has_more", False)),
                    "page": page,
                    "query": q,
                    "error": None,
                }
            elif resp.status_code == 404:
                return {
                    "cards": [],
                    "total_cards": 0,
                    "has_more": False,
                    "page": page,
                    "query": q,
                    "error": "No cards matched your Scryfall syntax query.",
                }
            else:
                err_msg = resp.json().get("details", f"Scryfall HTTP {resp.status_code}")
                return {
                    "cards": [],
                    "total_cards": 0,
                    "has_more": False,
                    "page": page,
                    "query": q,
                    "error": err_msg,
                }
    except Exception as exc:
        local_qs = apply_local_scryfall_syntax(
            ScryfallCard.objects.all(), q, card_prefix=""
        )[:36]
        cards = list(local_qs)
        return {
            "cards": cards,
            "total_cards": len(cards),
            "has_more": False,
            "page": 1,
            "query": q,
            "error": f"Offline cache mode ({exc})",
        }


def resolve_single_card(
    *,
    scryfall_id: str | None = None,
    name: str | None = None,
    set_code: str | None = None,
    collector_number: str | None = None,
) -> ScryfallCard | None:
    """Resolve a single card from local SQLite cache first, then fallback to Scryfall API."""
    if scryfall_id:
        cached = ScryfallCard.objects.filter(scryfall_id=scryfall_id).first()
        if cached:
            return cached
        _throttle()
        try:
            with httpx.Client(headers=SCRYFALL_HEADERS, timeout=10.0) as client:
                r = client.get(f"{SCRYFALL_BASE_URL}/cards/{scryfall_id}")
                if r.status_code == 200:
                    return upsert_scryfall_card(r.json())
        except Exception:
            pass

    if set_code and collector_number:
        cached = ScryfallCard.objects.filter(
            set_code=set_code.lower(), collector_number=str(collector_number)
        ).first()
        if cached:
            return cached
        _throttle()
        try:
            with httpx.Client(headers=SCRYFALL_HEADERS, timeout=10.0) as client:
                r = client.get(
                    f"{SCRYFALL_BASE_URL}/cards/{set_code.lower()}/{collector_number}"
                )
                if r.status_code == 200:
                    return upsert_scryfall_card(r.json())
        except Exception:
            pass

    if name:
        clean_name = name.strip()
        qs = ScryfallCard.objects.filter(name__iexact=clean_name)
        if set_code:
            qs = qs.filter(set_code=set_code.lower())
        cached = qs.first()
        if cached:
            return cached

        _throttle()
        params = {"fuzzy": clean_name}
        if set_code:
            params["set"] = set_code.lower()
        try:
            with httpx.Client(headers=SCRYFALL_HEADERS, timeout=10.0) as client:
                r = client.get(f"{SCRYFALL_BASE_URL}/cards/named", params=params)
                if r.status_code == 200:
                    return upsert_scryfall_card(r.json())
        except Exception:
            pass

    return None


def bulk_resolve_cards(identifiers: list[dict[str, str]]) -> dict[str, ScryfallCard]:
    """
    Resolve up to N cards in batches of 75 using Scryfall's `POST /cards/collection`.
    Each identifier dict can contain `id`, `name`, `set`, `collector_number`.
    Returns a lookup dictionary mapping normalized keys (id, lower(name), set:cn) -> ScryfallCard.
    """
    resolved: dict[str, ScryfallCard] = {}
    pending_api: list[dict[str, str]] = []

    for ident in identifiers:
        card = None
        if ident.get("id"):
            card = ScryfallCard.objects.filter(scryfall_id=ident["id"]).first()
        elif ident.get("set") and ident.get("collector_number"):
            card = ScryfallCard.objects.filter(
                set_code=ident["set"].lower(),
                collector_number=str(ident["collector_number"]),
            ).first()
        elif ident.get("name"):
            qs = ScryfallCard.objects.filter(name__iexact=ident["name"].strip())
            if ident.get("set"):
                qs = qs.filter(set_code=ident["set"].lower())
            card = qs.first()

        if card:
            _index_resolved_card(resolved, card)
        else:
            api_ident: dict[str, str] = {}
            if ident.get("id"):
                api_ident["id"] = ident["id"]
            elif ident.get("set") and ident.get("collector_number"):
                api_ident["set"] = ident["set"].lower()
                api_ident["collector_number"] = str(ident["collector_number"])
            elif ident.get("name"):
                api_ident["name"] = ident["name"].strip()
                if ident.get("set"):
                    api_ident["set"] = ident["set"].lower()
            if api_ident:
                pending_api.append(api_ident)

    # Batch into chunks of 75 per Scryfall's /cards/collection limit
    for idx in range(0, len(pending_api), 75):
        batch = pending_api[idx : idx + 75]
        _throttle()
        try:
            with httpx.Client(headers=SCRYFALL_HEADERS, timeout=15.0) as client:
                r = client.post(
                    f"{SCRYFALL_BASE_URL}/cards/collection",
                    json={"identifiers": batch},
                )
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    for raw in data:
                        card_obj = upsert_scryfall_card(raw)
                        _index_resolved_card(resolved, card_obj)
        except Exception:
            pass

    return resolved


def _index_resolved_card(index: dict[str, ScryfallCard], card: ScryfallCard):
    index[card.scryfall_id] = card
    index[card.name.lower()] = card
    if "//" in card.name:
        front_face = card.name.split("//")[0].strip().lower()
        index[front_face] = card
    index[f"{card.set_code.lower()}:{card.collector_number.lower()}"] = card
    index[f"{card.name.lower()}|{card.set_code.lower()}"] = card


def apply_local_scryfall_syntax(
    qs: QuerySet, query: str, card_prefix: str = "card__"
) -> QuerySet:
    """
    Translate Scryfall syntax tokens (`t:`, `o:`, `c:`, `id:`, `cmc<=`, `set:`, `r:`, `is:foil`)
    into Django ORM filters over `CollectionItem`, `DeckCard`, or `ScryfallCard` querysets.
    """
    q = (query or "").strip()
    if not q:
        return qs

    tokens = re.findall(r'(?:[^\s"]+|"[^"]*")+', q)
    for token in tokens:
        low = token.lower()
        if low.startswith(("t:", "type:")):
            val = token.split(":", 1)[1].strip('"')
            qs = qs.filter(**{f"{card_prefix}type_line__icontains": val})
        elif low.startswith(("o:", "oracle:")):
            val = token.split(":", 1)[1].strip('"')
            qs = qs.filter(**{f"{card_prefix}oracle_text__icontains": val})
        elif low.startswith(("s:", "set:", "e:")):
            val = token.split(":", 1)[1].strip('"').lower()
            qs = qs.filter(**{f"{card_prefix}set_code": val})
        elif low.startswith(("r:", "rarity:")):
            val = token.split(":", 1)[1].strip('"').lower()
            qs = qs.filter(**{f"{card_prefix}rarity__iexact": val})
        elif low.startswith(("c:", "color:", "id:", "commander:")):
            colors_str = token.split(":", 1)[1].strip('"').upper()
            for ch in colors_str:
                if ch in ("W", "U", "B", "R", "G"):
                    qs = qs.filter(**{f"{card_prefix}color_identity__icontains": ch})
        elif re.match(r"^(cmc|mv)(<=|>=|<|>|=)(\d+(\.\d+)?)$", low):
            m = re.match(r"^(?:cmc|mv)(<=|>=|<|>|=)(\d+(?:\.\d+)?)$", low)
            if m:
                op, num_str = m.group(1), m.group(2)
                num = float(num_str)
                lookup = {
                    "=": "",
                    "<": "__lt",
                    "<=": "__lte",
                    ">": "__gt",
                    ">=": "__gte",
                }[op]
                qs = qs.filter(**{f"{card_prefix}cmc{lookup}": num})
        elif low == "is:foil" and card_prefix == "card__":
            qs = qs.filter(finish__in=["foil", "etched"])
        else:
            clean = token.strip('"')
            qs = qs.filter(
                Q(**{f"{card_prefix}name__icontains": clean})
                | Q(**{f"{card_prefix}type_line__icontains": clean})
                | Q(**{f"{card_prefix}oracle_text__icontains": clean})
            )
    return qs
