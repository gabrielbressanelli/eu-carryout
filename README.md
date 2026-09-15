# Carryout System

Multi-tenant Django ordering service for restaurant carryout websites.

## Current Scope

- Slug-based access for restaurant-specific ordering sites, for example `/<restaurant-slug>/`.
- Tenant-scoped menu categories, menu items, modifier groups, and modifier options.
- Session carts isolated per tenant.
- Stripe Checkout session creation through account-level Stripe Connect direct charges, with tenant and draft order metadata.
- Paid orders tracked by tenant and customer email for future account linking.
- Tenant integration toggles for kitchen printing, email notification, and SMS notification.
- Post-payment workflow dispatches enabled integrations through configured HTTP endpoints.
- Integration attempts are logged so Stripe webhook retries do not duplicate sent notifications.
- Tenant-scoped agent ordering API for menu search, call carts, and order-summary totals.
- Account-scoped restaurant groups, with account admins able to add locations and restaurant users scoped to assigned locations.
- Restaurant login with explicit access to assigned locations, plus separate editors for business settings and menu configuration.
- Logo and menu photo uploads, with local storage or Cloudflare R2.
- Public product landing page at `/` with Eusocial branding, signup placeholder CTAs, and an autoplay Carryout product tour.
- HostHub integration section explaining the future connection between Eusocial voice agents, restaurant locations, Carryout menus, and order workflows.

## Local Setup

```bash
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py createsuperuser
./.venv/bin/python manage.py create_restaurant one-sixty-main --name "160 Main" --account one-sixty-group --account-name "One Sixty Group"
./.venv/bin/python manage.py create_agent_token one-sixty-main --name "Local Voice Agent"
./.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/one-sixty-main/`.

The product landing page is at `http://127.0.0.1:8000/`. Its signup buttons currently scroll to the signup section as a placeholder; connect them to the Eusocial signup URL when that destination is ready.

Restaurant settings start at `/onboarding/`, with a simple login at `/onboarding/login/`. A platform superuser creates accounts and the first restaurant/location for each account. Account admins can add more restaurants under accounts they manage, while restaurant users only see the locations assigned to them. Ordinary restaurant accounts do not require Django staff permissions.

## Environment

```bash
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_APPLICATION_FEE_PERCENT=0
STRIPE_APPLICATION_FEE_FIXED_CENTS=0
```

Stripe payments use Connect direct charges. Configure each restaurant group's connected account in Django Admin under `Accounts` by setting `stripe_account_id` to the connected account id, for example `acct_...`. Optional application fees are controlled globally by `STRIPE_APPLICATION_FEE_PERCENT` and `STRIPE_APPLICATION_FEE_FIXED_CENTS`; per-account fee fields override the global defaults when set, including explicit zero-fee accounts.

## Tenant Integrations

Configure these in Django Admin under `Tenant integrations`.

- `Kitchen Printing`: sends the paid order payload to the tenant's print endpoint.
- `Email Notification`: sends the paid order payload to a Make.com webhook.
- `SMS Notification`: sends the paid order payload to a Make.com webhook.

Each payload includes tenant id, tenant slug, customer email/phone, business email/phone, order summary, and amount paid.

## Restaurant Onboarding

Use `/onboarding/` to create and configure restaurant tenants. The setup page supports:

- restaurant identity and customer slug
- seven-day business hours
- printing, email, and SMS service endpoints
- menu categories and menu items
- modifier groups and modifier options
- linking modifier groups to menu items

Each settings area has its own view. Categories, items, groups, options, and assignments have separate create/edit pages and explicit delete confirmation. Protected records used by orders cannot be deleted; hide the item instead. Editing a group shows its options, and editing an item shows its assigned groups.

New restaurant creation also creates or assigns a login. Each restaurant belongs to an account, and the location list groups restaurants by account. Under a location's **Login access** tab, account admins and platform superusers can grant an existing username access to that location or revoke access. `/onboarding/<restaurant-slug>/login/` shows that restaurant's identity. After login, users choose from only their assigned locations, unless they are account admins for the parent account. Account memberships and restaurant memberships can also be managed in Django Admin. Existing staff accounts need a membership unless they are superusers.

## Image Storage

Restaurant logos and menu photos accept JPG, PNG, and WebP uploads through drag-and-drop or a file picker. Original uploads are verified with a 5 MB safety cap and a 25-million-pixel limit, then optimized server-side before storage. The saved delivery file targets 150 KB or less for faster menu loading. The public navbar and menu use uploaded files, falling back to existing image URLs where present.

Local files, and R2 objects when R2 is enabled, go under `carryout/<account-slug>/<restaurant-slug>/logo/` or `carryout/<account-slug>/<restaurant-slug>/menu-items/`. Each file receives a random suffix, so duplicate filenames do not overwrite images. A remove-image action clears the reference on Save; old objects are retained and should eventually be cleaned by an unreferenced-object retention job.

To use Cloudflare R2, export these environment variables before starting Django (a `.env` file is not loaded automatically):

```text
R2_BUCKET_NAME=your-bucket
R2_ACCOUNT_ID=your-account-id
R2_ACCESS_KEY_ID=your-access-key
R2_SECRET_ACCESS_KEY=your-secret-key
R2_PUBLIC_DOMAIN=images.yourdomain.com
```

`R2_PUBLIC_DOMAIN` is an optional hostname without a scheme or path. Configure that custom domain on the R2 bucket for public image delivery. Without it, storage generates signed URLs. Credentials remain server-side; files are uploaded through authenticated, location-authorized Django forms.

The storage configuration follows the [django-storages R2 backend documentation](https://django-storages.readthedocs.io/en/latest/backends/s3_compatible/cloudflare-r2.html). A live bucket upload has not been verified without R2 credentials.

## Agent Orders

Agent endpoints live under `/api/<tenant-slug>/agent/` and require `Authorization: Bearer <tenant-agent-token>`.

Current foundation:

- fuzzy menu search with menu item aliases
- item detail with required/optional modifiers
- tenant/session-scoped call carts
- add, update, remove, and read cart lines
- order summary total estimation
