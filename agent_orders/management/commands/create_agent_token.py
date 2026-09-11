import secrets

from django.core.management.base import BaseCommand, CommandError

from agent_orders.models import AgentAccessToken
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Create a bearer token for an ordering agent integration."

    def add_arguments(self, parser):
        parser.add_argument("tenant_slug")
        parser.add_argument("--name", default="Voice Agent")
        parser.add_argument("--token", default="")

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(slug=options["tenant_slug"])
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Unknown tenant slug: {options['tenant_slug']}") from exc

        raw_token = options["token"] or secrets.token_urlsafe(32)
        token, created = AgentAccessToken.objects.get_or_create(
            tenant=tenant,
            token=raw_token,
            defaults={"name": options["name"]},
        )
        if not created:
            token.name = options["name"]
            token.is_active = True
            token.save(update_fields=["name", "is_active"])

        self.stdout.write(self.style.SUCCESS(f"Created agent token for {tenant.slug}:"))
        self.stdout.write(raw_token)
