"""AppConfig for Vault with automatic SQLite WAL pragma initialization."""
from django.apps import AppConfig
from django.db.backends.signals import connection_created


def enable_sqlite_wal(sender, connection, **kwargs):
    """Enable Write-Ahead Logging and foreign keys for SQLite connections."""
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")


class VaultConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vault"
    verbose_name = "Arcane Vault"

    def ready(self):
        connection_created.connect(enable_sqlite_wal)
