# AutoForge AI — Local Business Website Agent

A phone-friendly Flask MVP combining business discovery and draft website creation.

## Included
- Search an area using OpenStreetMap Nominatim and Overpass.
- Save named business listings where no website tag is recorded.
- Save leads in SQLite and avoid duplicate OSM records.
- Create a private storefront draft from a lead.
- Edit shop details and add products with prices and image URLs.
- Publish a simple storefront only after owner approval.
- Prepare email drafts from publicly listed business email addresses. Emails are never sent automatically.

## Important limitations
- A missing OpenStreetMap website tag does not prove the business has no website. Verify manually.
- Map data may be incomplete or outdated.
- This is an MVP with one admin workspace, not yet a production multi-tenant service with separate shop-owner accounts.
- Checkout, online payments, order management, owner login, AI copy generation and custom domains are future features.
- Do not publish a website or use a business's name, logo, photos or product information without permission.
- Identify yourself honestly in outreach, use public business contact details responsibly and honour opt-outs.
- Follow OpenStreetMap attribution and usage policies.

## Run
Python 3.10+:

    pip install -r requirements.txt

Set secrets, then start:

    export SECRET_KEY="a-long-random-secret"
    export ADMIN_PASSWORD="your-strong-password"
    python app.py

On Android, use a browser-based Python environment or deploy to a Python host; the phone is the control device, not the server.

Open http://127.0.0.1:5000.

## Deploy
Set `SECRET_KEY`, `ADMIN_PASSWORD`, and optionally `DATABASE_PATH` on your host. Start command:

    gunicorn app:app

Use persistent storage for SQLite. A host's temporary filesystem can be erased on restart. Never commit secrets or customer data.

## Data sources
OpenStreetMap data via Nominatim and Overpass. Respect their usage policies and include required attribution in a production deployment.

## Next development phase
Separate owner accounts and consent, AI-assisted site copy, cart and checkout, orders, verified contact settings, custom domains, audit logs, rate limiting and production security review.
