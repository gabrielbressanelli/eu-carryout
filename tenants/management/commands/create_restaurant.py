from django.core.management.base import BaseCommand, CommandError

from tenants.models import Tenant
from tenants.services import ensure_tenant_onboarding_defaults


class Command(BaseCommand):
    help = "Create a production tenant with disabled notification integrations."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Customer-facing restaurant slug, e.g. one-sixty-main.")
        parser.add_argument("--name", required=True, help="Restaurant display name.")
        parser.add_argument("--email", default="", help="Business email for notifications.")
        parser.add_argument("--phone", default="", help="Business phone for notifications.")
        parser.add_argument("--domain", default="", help="Optional primary custom domain.")

    def handle(self, *args, **options):
        slug = options["slug"].strip().lower()
        if slug in {"admin", "static", "media", "stripe", "api"}:
            raise CommandError(f"{slug!r} is reserved and cannot be used as a restaurant slug.")

        tenant, created = Tenant.objects.get_or_create(
            slug=slug,
            defaults={
                "name": options["name"],
                "business_email": options["email"],
                "business_phone": options["phone"],
                "primary_domain": options["domain"] or None,
            },
        )
        if not created:
            raise CommandError(f"Tenant slug {slug!r} already exists.")

        ensure_tenant_onboarding_defaults(tenant)

        self.stdout.write(self.style.SUCCESS(f"Created restaurant tenant {tenant.name} at /{tenant.slug}/."))
