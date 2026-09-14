import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def create_accounts_for_existing_tenants(apps, schema_editor):
    Account = apps.get_model("tenants", "Account")
    Tenant = apps.get_model("tenants", "Tenant")
    db_alias = schema_editor.connection.alias
    for tenant in Tenant.objects.using(db_alias).all().iterator():
        account, _ = Account.objects.using(db_alias).get_or_create(
            slug=tenant.slug,
            defaults={
                "name": tenant.name,
                "created_at": tenant.created_at or django.utils.timezone.now(),
                "updated_at": tenant.updated_at or django.utils.timezone.now(),
            },
        )
        tenant.account_id = account.pk
        tenant.save(update_fields=["account"], using=db_alias)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tenants", "0004_tenant_closure_message_tenant_ordering_paused_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="Account",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("slug", models.SlugField(blank=True, max_length=80, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="tenant",
            name="account",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="restaurants", to="tenants.account"),
        ),
        migrations.RunPython(create_accounts_for_existing_tenants, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="tenant",
            name="account",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="restaurants", to="tenants.account"),
        ),
        migrations.CreateModel(
            name="AccountMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("owner", "Owner"), ("admin", "Admin")], default="admin", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to="tenants.account")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="account_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("user", "account"), name="unique_account_member")],
            },
        ),
    ]
