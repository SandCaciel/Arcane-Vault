# Arcane Vault — Commander's Ledger

**Arcane Vault** is a multi-user Magic: The Gathering (MTG) physical collection manager, wishlist tracker, and Commander (EDH) deckbuilding platform built with **Django 5**, **Django Ninja**, and **SQLite (WAL mode)**.

## Core Features
- **Scryfall Syntax Explorer**: Search Magic cards using full Scryfall query syntax (`c:wubrg cmc<=3 o:draw`, `is:commander`, etc.) with local SQLite caching.
- **Cross-Deck Physical Copy Allocation & Auto-Wishlist**:
  - Tracks how many physical copies of a card you own across your Commander decks.
  - Shows **"In Deck"** badges directly on cards in your Vault.
  - Automatically adds cards to your **Wishlist** if you add a card to a deck when you own 0 copies *or* when all of your owned physical copies are already allocated to other Commander decks.
  - Automatically resolves and removes Wishlist items when new physical copies are added to your Vault.
- **Physical Prebuilt Deck Onboarding**: One-click option to add all cards (or only missing cards) from a Commander deck directly into your physical Vault collection.
- **Moxfield Import & Export**: Import decks or collections from Moxfield text/URLs and export to Moxfield text or CSV formats.
- **Google OIDC + Admin Account Request Approval Dashboard**: Supports Google Single Sign-On with an admin approval queue for new users.
- **AI-Ready OpenAPI & Vision Bulk Import (`/api/v1/docs`)**: Generate personal `arcv_...` API keys so AI agents can query your collection, bulk-add cards from binder photos, and construct Commander decks.

## Quick Start (Local Development)

1. Copy `.env.example` to `.env` and configure your environment variables:
   ```bash
   cp .env.example .env
   ```
2. Install dependencies and run migrations:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Or .\.venv\Scripts\activate on Windows
   pip install -r requirements.txt
   python manage.py migrate
   python manage.py runserver
   ```
3. Open `http://localhost:8000` (and `/api/v1/docs` for the interactive OpenAPI documentation).
