# Nébula — Event ticketing

Django event management and ticketing starter for Argentina. Includes a public
event catalog, ticket tiers and seat selection, Mercado Pago Checkout Pro,
payment webhooks, signed QR tickets, staff check-in, and ticket email delivery.
Development uses SQLite; the Django ORM can use PostgreSQL in production.

The public experience is at `/`; organizers use the staff-only activity
dashboard at `/organizer/` and the fully-featured management interface at
`/admin/`. The event catalog includes live text and month filters, while the
organizer dashboard summarizes sales, tickets, pending payments, and recent
orders.

## Run locally

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Manage events, ticket types, venue seats, promo codes, and orders at
`/admin/`. Publish an event and add one or more active ticket types before
opening its public event page. Upload flyers to an image host and set the
event's `flyer_url`.

At checkout, buyers must provide their full name, matching email and
confirmation, Argentine DNI, phone area code, and phone number. A pending order
with its selected ticket types and seats is saved before the Mercado Pago
preference is created; those verified order details are sent in its `payer`
object (`name`, `surname`, phone, and DNI identification).

## Mercado Pago

Set `MERCADOPAGO_ACCESS_TOKEN` to a Mercado Pago Argentina access token and set
`SITE_URL` to the public HTTPS origin of this installation. Register
`https://your-domain/api/webhooks/mercadopago` for payment notifications and set
`MERCADOPAGO_WEBHOOK_SECRET` to the webhook secret from Mercado Pago. Payment
status is always confirmed by fetching the payment through the official Python
SDK; the browser return URL is not treated as proof of payment.

The webhook validates the `X-Signature` HMAC and `X-Request-Id`, uses the order
UUID as `external_reference`, and safely ignores repeat approvals. Orders hold
inventory for 15 minutes; the preference is configured to expire at the same
deadline. Stale seat reservations are released the next time that event's
inventory is reserved.

## Email and gate check-in

Configure Django's SMTP environment variables for delivery. During local
development, the configured console backend prints messages to the server log.
Ticket emails are placed in the database-backed delivery queue after payment
approval; run the worker periodically:

```sh
python manage.py send_ticket_emails
```

Use a process supervisor or scheduled worker in production. Staff log in at
`/admin/`, then open `/gate/` for camera or manual QR validation. Camera access
requires HTTPS (except localhost) and browser permission.
Each purchased admission receives its own signed QR code, so a multi-ticket
order supports one-time check-in for each attendee independently.

## Production notes

- Generate a strong unique `DJANGO_SECRET_KEY`; set `DJANGO_DEBUG=False` and
  configure `DJANGO_ALLOWED_HOSTS`, HTTPS, SMTP, and `CORS_ALLOWED_ORIGINS`.
- Switch `DATABASES["default"]` to PostgreSQL for production workloads.
- Run `python manage.py check --deploy` and `python manage.py collectstatic`.
- Serve uploaded flyer assets from a managed media/object-storage service.
- The ticket QR contains a Django-signed token; keep the signing key private.
- SQLite serializes writes. Use PostgreSQL for concurrent high-volume ticket
  sales and validate load and payment-provider limits before launch.
