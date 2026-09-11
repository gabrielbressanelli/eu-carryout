import secrets

from django.utils import timezone

from .models import AgentAccessToken


def authenticate_agent_request(request, tenant):
    auth_header = request.headers.get("Authorization", "")
    try:
        scheme, token = auth_header.split(maxsplit=1)
    except ValueError:
        return False

    if scheme.lower() != "bearer":
        return False

    for token_record in AgentAccessToken.objects.filter(tenant=tenant, is_active=True):
        if secrets.compare_digest(token_record.token, token):
            token_record.last_used_at = timezone.now()
            token_record.save(update_fields=["last_used_at"])
            return True
    return False
