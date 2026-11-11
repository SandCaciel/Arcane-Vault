"""WSGI config for arcane_vault project."""
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "arcane_vault.settings")
application = get_wsgi_application()
