import hashlib
import hmac
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Event, IssuedTicket, Order, Seat, TicketDelivery, TicketType
from .services import (
    CheckoutError,
    create_checkout_order,
    process_mercadopago_payment,
    verify_mercadopago_signature,
    verify_ticket_token,
)


class PlatformTests(TestCase):
    def setUp(self):
        self.event = Event.objects.create(
            title="Noche Prisma",
            slug="noche-prisma",
            summary="Música y luces",
            event_date=timezone.now() + timedelta(days=10),
            venue="Sala Uno",
            status=Event.Status.PUBLISHED,
        )
        self.ticket = TicketType.objects.create(
            event=self.event, name="General", price=Decimal("12500.00"), quantity=5
        )

    def create_order(self, quantities, seat_ids=None, coupon_code=""):
        return create_checkout_order(
            self.event,
            "Ada Lovelace",
            "ada@example.com",
            quantities,
            seat_ids or [],
            coupon_code,
            "ada@example.com",
            "12345678",
            "11",
            "12345678",
        )

    def test_create_order_places_hold_and_applies_discount(self):
        from .models import DiscountCode

        DiscountCode.objects.create(
            event=self.event, code="PRISMA10", kind="percent", value=10
        )
        order = create_checkout_order(
            self.event,
            "Ada Lovelace",
            "ADA@example.com",
            {str(self.ticket.pk): "2"},
            [],
            "prisma10",
            "ada@example.com",
            "12345678",
            "11",
            "12345678",
        )
        self.assertEqual(order.total, Decimal("22500.00"))
        self.assertEqual(order.buyer_email, "ada@example.com")
        self.assertEqual(order.buyer_dni, "12345678")
        self.assertEqual(order.buyer_phone_area_code, "11")
        self.assertEqual(order.buyer_phone_number, "12345678")
        self.assertEqual(self.ticket.available_quantity(), 3)

    def test_checkout_cannot_oversell(self):
        self.create_order({str(self.ticket.pk): "4"})
        with self.assertRaises(CheckoutError):
            self.create_order({str(self.ticket.pk): "2"})

    def test_expired_order_hold_is_released(self):
        order = self.create_order({str(self.ticket.pk): "5"})
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(minutes=16)
        )
        self.assertEqual(self.ticket.available_quantity(), 5)
        next_order = self.create_order({str(self.ticket.pk): "5"})
        self.assertEqual(next_order.status, Order.Status.PENDING)

    def test_seat_is_reserved_for_hold_duration(self):
        seat = Seat.objects.create(event=self.event, ticket_type=self.ticket, label="A-01")
        order = self.create_order({}, [seat.pk])
        seat.refresh_from_db()
        self.assertEqual(seat.reserved_order, order.pk)
        self.assertGreater(seat.reserved_until, timezone.now())

    def test_signature_verification(self):
        secret, request_id, timestamp, payment_id = "webhook-secret", "req-42", "1720000000", "123456"
        manifest = f"id:{payment_id};request-id:{request_id};ts:{timestamp};"
        signature = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(
            verify_mercadopago_signature(
                f"ts={timestamp},v1={signature}", request_id, payment_id, secret
            )
        )
        self.assertFalse(
            verify_mercadopago_signature("ts=1,v1=bad", request_id, payment_id, secret)
        )

    @patch("tickets.services.mercadopago.SDK")
    def test_approved_webhook_is_idempotent(self, sdk_class):
        order = self.create_order({str(self.ticket.pk): "1"})
        sdk = sdk_class.return_value
        sdk.payment.return_value.get.return_value = {
            "status": 200,
            "response": {"external_reference": str(order.pk), "status": "approved"},
        }
        with self.settings(MERCADOPAGO_ACCESS_TOKEN="test-token"):
            process_mercadopago_payment("payment-123")
            process_mercadopago_payment("payment-123")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        ticket = IssuedTicket.objects.get(order_line__order=order)
        self.assertTrue(verify_ticket_token(ticket.qr_token))
        self.assertEqual(TicketDelivery.objects.filter(order=order).count(), 1)

    @patch("tickets.services.mercadopago.SDK")
    def test_each_ticket_can_only_be_checked_in_once(self, sdk_class):
        order = self.create_order({str(self.ticket.pk): "2"})
        sdk = sdk_class.return_value
        sdk.payment.return_value.get.return_value = {
            "status": 200,
            "response": {"external_reference": str(order.pk), "status": "approved"},
        }
        with self.settings(MERCADOPAGO_ACCESS_TOKEN="test-token"):
            process_mercadopago_payment("payment-456")
        tickets = list(IssuedTicket.objects.filter(order_line__order=order))
        self.assertEqual(len(tickets), 2)

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="gatekeeper", password="long-test-password", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("check_in"),
            data=f'{{"token":"{tickets[0].qr_token}"}}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        duplicate = self.client.post(
            reverse("check_in"),
            data=f'{{"token":"{tickets[0].qr_token}"}}',
            content_type="application/json",
        )
        self.assertEqual(duplicate.status_code, 409)
        unused = self.client.post(
            reverse("check_in"),
            data=f'{{"token":"{tickets[1].qr_token}"}}',
            content_type="application/json",
        )
        self.assertEqual(unused.status_code, 200)

    def test_email_confirmation_is_required_to_match(self):
        with self.assertRaisesRegex(CheckoutError, "no coinciden"):
            create_checkout_order(
                self.event,
                "Ada Lovelace",
                "ada@example.com",
                {str(self.ticket.pk): "1"},
                [],
                "",
                "different@example.com",
                "12345678",
                "11",
                "12345678",
            )
        self.assertFalse(Order.objects.exists())

    @patch("tickets.services.mercadopago.SDK")
    def test_checkout_saves_pending_order_and_maps_payer_details(self, sdk_class):
        sdk = sdk_class.return_value
        sdk.preference.return_value.create.return_value = {
            "status": 201,
            "response": {"id": "pref-123", "init_point": "https://pay.example/checkout"},
        }
        with self.settings(MERCADOPAGO_ACCESS_TOKEN="test-token"):
            response = self.client.post(
                reverse("event_detail", args=(self.event.slug,)),
                {
                    f"quantity_{self.ticket.pk}": "1",
                    "buyer_name": "Ada Marie Lovelace",
                    "buyer_email": "ada@example.com",
                    "buyer_email_confirmation": "ada@example.com",
                    "buyer_dni": "12.345.678",
                    "buyer_phone_area_code": "11",
                    "buyer_phone_number": "15 1234-5678",
                },
            )
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.lines.get().quantity, 1)
        self.assertEqual(order.buyer_name, "Ada Marie Lovelace")
        self.assertEqual(order.buyer_dni, "12345678")
        self.assertEqual(order.buyer_phone_number, "1512345678")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://pay.example/checkout")
        preference = sdk.preference.return_value.create.call_args.args[0]
        self.assertEqual(
            preference["payer"],
            {
                "email": "ada@example.com",
                "name": "Ada",
                "surname": "Marie Lovelace",
                "phone": {"area_code": "11", "number": "1512345678"},
                "identification": {"type": "DNI", "number": "12345678"},
            },
        )

    def test_public_event_and_retrieve_pages(self):
        self.assertEqual(self.client.get(reverse("event_list")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("event_detail", args=(self.event.slug,))).status_code, 200
        )
        self.assertEqual(self.client.get(reverse("retrieve_ticket")).status_code, 200)
        self.assertContains(self.client.get(reverse("event_list")), 'id="event-search"')

    def test_organizer_dashboard_requires_staff_login(self):
        url = reverse("organizer_dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        from django.contrib.auth import get_user_model

        staff = get_user_model().objects.create_user(
            username="organizer", password="long-test-password", is_staff=True
        )
        self.client.force_login(staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ritmo de ventas")
        self.assertContains(response, "Control de acceso")

    def test_custom_admin_index_renders_branded_actions(self):
        from django.contrib.auth import get_user_model

        admin = get_user_model().objects.create_superuser(
            username="site-admin", email="admin@example.com", password="long-test-password"
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CENTRO DE OPERACIONES")
        self.assertContains(response, "Abrir resumen de actividad")
