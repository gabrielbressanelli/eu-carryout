from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify the configured default file storage by writing, reading, and deleting a small test object."

    def handle(self, *args, **options):
        default_config = settings.STORAGES.get("default", {})
        storage_options = default_config.get("OPTIONS", {})
        test_name = "carryout/storage-smoke-test.txt"

        self.stdout.write("Default storage backend: " + default_config.get("BACKEND", "<missing>"))
        for key in ("bucket_name", "endpoint_url", "custom_domain", "querystring_auth"):
            if key in storage_options:
                self.stdout.write(f"{key}: {storage_options[key]}")
        for key in ("access_key", "secret_key"):
            value = storage_options.get(key)
            self.stdout.write(f"{key}: {'set' if value else 'missing'}")

        try:
            saved_name = default_storage.save(test_name, ContentFile(b"carryout-r2-ok"))
            self.stdout.write(f"saved: {saved_name}")
            exists = default_storage.exists(saved_name)
            self.stdout.write(f"exists: {exists}")
            with default_storage.open(saved_name, "rb") as uploaded:
                content = uploaded.read()
            self.stdout.write(f"read: {content!r}")
            self.stdout.write(f"url: {default_storage.url(saved_name)}")
            default_storage.delete(saved_name)
            self.stdout.write(self.style.SUCCESS("Storage check passed."))
        except Exception as exc:
            raise CommandError(f"Storage check failed: {exc.__class__.__name__}: {exc}") from exc
