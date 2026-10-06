import hashlib
import hmac
import io
import json
import re
from datetime import timedelta
from decimal import Decimal

import mercadopago
import qrcode
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.core.signing import BadSignature, dumps, loads
from django.db import models, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import (
    DiscountCode,
    IssuedTicket,
    Order,
    OrderLine,
    Seat,
    TicketDelivery,
    TicketType,
)

HOLD_MINUTES = 15
TICKET_SALT = "tickets.ticket"


class CheckoutError(Exception):
    pass


def verify_mercadopago_signature(signature_header, request_id, payment_id, secret):
    if not signature_header or not request_id or not payment_id or not secret:
        return False
    parts = {}
    for part in signature_header.split(","):
        key, separator, value = part.strip().partition("=")
        if separator:
            parts[key] = value
    timestamp = parts.get("ts")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False
    manifest = f"id:{str(payment_id).lower()};request-id:{request_id};ts:{timestamp};"
    expected = hmac.new(
        secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_ticket_token(token):
    try:
        payload = loads(token, salt=TICKET_SALT, max_age=None)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _valid_discount(event, code):
    if not code:
        return None
    now = timezone.now()
    try:
        discount = DiscountCode.objects.select_for_update().get(
            event=event, code__iexact=code.strip(), active=True
        )
    except DiscountCode.DoesNotExist as error:
        raise CheckoutError("El código de descuento no es válido.") from error
    if discount.value <= 0 or (
        discount.kind == DiscountCode.Kind.PERCENT
        and discount.value > Decimal("100")
    ):
        raise CheckoutError("El código de descuento tiene un valor inválido.")
    if discount.expires_at and discount.expires_at <= now:
        raise CheckoutError("El código de descuento venció.")
    active_reservations = Order.objects.filter(
        discount_code=discount,
        status=Order.Status.PENDING,
        created_at__gte=now - timedelta(minutes=HOLD_MINUTES),
    ).count()
    if (
        discount.max_uses is not None
        and discount.uses + active_reservations >= discount.max_uses
    ):
        raise CheckoutError("El código de descuento ya alcanzó su límite de uso.")
    return discount


def _discounted_total(subtotal, discount):
    if not discount:
        return subtotal
    if discount.kind == DiscountCode.Kind.PERCENT:
        amount = subtotal * discount.value / Decimal("100")
    else:
        amount = discount.value
    return max(subtotal - amount, Decimal("0.00")).quantize(Decimal("0.01"))


def create_checkout_order(
    event,
    buyer_name,
    buyer_email,
    quantities,
    seat_ids,
    coupon_code,
    buyer_email_confirmation,
    buyer_dni,
    buyer_phone_area_code,
    buyer_phone_number,
):
    if not quantities and not seat_ids:
        raise CheckoutError("Seleccioná al menos una entrada.")
    normalized_name = " ".join(buyer_name.split())
    if len(normalized_name.split()) < 2:
        raise CheckoutError("Ingresá tu nombre y apellido.")
    normalized_email = buyer_email.strip().lower()
    if not normalized_email or not buyer_email_confirmation.strip():
        raise CheckoutError("Completá y confirmá tu correo electrónico.")
    if normalized_email != buyer_email_confirmation.strip().lower():
        raise CheckoutError("Los correos electrónicos no coinciden.")
    try:
        validate_email(normalized_email)
    except ValidationError as error:
        raise CheckoutError("Ingresá un correo electrónico válido.") from error
    if not re.fullmatch(r"[\d.\s-]+", buyer_dni.strip()):
        raise CheckoutError("Ingresá un DNI válido de 7 u 8 dígitos.")
    dni = re.sub(r"\D", "", buyer_dni)
    if not re.fullmatch(r"\d{7,8}", dni):
        raise CheckoutError("Ingresá un DNI válido de 7 u 8 dígitos.")
    if not re.fullmatch(r"[+()\d\s-]+", buyer_phone_area_code.strip()) or not re.fullmatch(
        r"[+()\d\s-]+", buyer_phone_number.strip()
    ):
        raise CheckoutError("Ingresá un código de área y teléfono válidos.")
    area_code = re.sub(r"\D", "", buyer_phone_area_code)
    phone_number = re.sub(r"\D", "", buyer_phone_number)
    if not re.fullmatch(r"\d{2,4}", area_code) or not re.fullmatch(
        r"\d{6,10}", phone_number
    ):
        raise CheckoutError("Ingresá un código de área y teléfono válidos.")
    with transaction.atomic():
        now = timezone.now()
        stale_order_ids = Order.objects.filter(
            event=event,
            status=Order.Status.PENDING,
            created_at__lt=now - timedelta(minutes=HOLD_MINUTES),
        ).values_list("id", flat=True)
        Seat.objects.filter(
            reserved_order__in=stale_order_ids,
            reserved_until__lt=now,
        ).update(reserved_order=None, reserved_until=None)

        requested = {}
        for raw_id, raw_qty in quantities.items():
            try:
                ticket_id, qty = int(raw_id), int(raw_qty)
            except (TypeError, ValueError) as error:
                raise CheckoutError("La selección de entradas no es válida.") from error
            if qty < 0 or qty > 10:
                raise CheckoutError("Podés comprar hasta 10 entradas por tipo.")
            if qty:
                requested[ticket_id] = qty
        selected_seats = list(
            Seat.objects.select_for_update()
            .filter(pk__in=seat_ids, event=event)
            .select_related("ticket_type")
        )
        if len(selected_seats) != len(set(seat_ids)):
            raise CheckoutError("Uno de los lugares seleccionados no está disponible.")
        ticket_ids = set(requested)
        ticket_ids.update(seat.ticket_type_id for seat in selected_seats)
        ticket_types = {
            item.pk: item
            for item in TicketType.objects.select_for_update().filter(
                event=event, active=True, pk__in=ticket_ids
            )
        }
        if any(ticket_id not in ticket_types for ticket_id in requested):
            raise CheckoutError("Uno de los tipos de entrada no está disponible.")
        for seat in selected_seats:
            if seat.ticket_type_id not in ticket_types:
                raise CheckoutError("El lugar no corresponde a una entrada activa.")
            if seat.reserved_order and (
                seat.reserved_until is None or seat.reserved_until > now
            ):
                raise CheckoutError("Uno de los lugares ya está reservado.")
            requested[seat.ticket_type_id] = requested.get(seat.ticket_type_id, 0) + 1

        for ticket_id, qty in requested.items():
            if qty > 10:
                raise CheckoutError("Podés comprar hasta 10 entradas por tipo.")
            ticket = ticket_types[ticket_id]
            if not ticket.is_on_sale():
                raise CheckoutError(f"La venta de {ticket.name} no está activa.")
            paid = sum(
                ticket.order_lines.filter(order__status=Order.Status.PAID)
                .values_list("quantity", flat=True)
            )
            active_holds = sum(
                ticket.order_lines.filter(
                    order__status=Order.Status.PENDING,
                    order__created_at__gte=now - timedelta(minutes=HOLD_MINUTES),
                ).values_list("quantity", flat=True)
            )
            if paid + active_holds + qty > ticket.quantity:
                raise CheckoutError(f"No quedan suficientes entradas de {ticket.name}.")

        discount = _valid_discount(event, coupon_code)
        subtotal = sum(
            (ticket_types[ticket_id].price * qty for ticket_id, qty in requested.items()),
            Decimal("0.00"),
        )
        total = _discounted_total(subtotal, discount)
        order = Order.objects.create(
            event=event,
            buyer_name=normalized_name,
            buyer_email=normalized_email,
            buyer_dni=dni,
            buyer_phone_area_code=area_code,
            buyer_phone_number=phone_number,
            total=total,
            discount_code=discount,
        )
        seat_by_type = {}
        for seat in selected_seats:
            seat_by_type.setdefault(seat.ticket_type_id, []).append(seat)
        for ticket_id, qty in requested.items():
            ticket = ticket_types[ticket_id]
            seats_for_type = seat_by_type.get(ticket_id, [])
            if seats_for_type:
                for seat in seats_for_type:
                    OrderLine.objects.create(
                        order=order,
                        ticket_type=ticket,
                        quantity=1,
                        unit_price=ticket.price,
                        seat=seat,
                    )
                qty -= len(seats_for_type)
            if qty:
                OrderLine.objects.create(
                    order=order, ticket_type=ticket, quantity=qty, unit_price=ticket.price
                )
        Seat.objects.filter(pk__in=[seat.pk for seat in selected_seats]).update(
            reserved_order=order.pk, reserved_until=now + timedelta(minutes=HOLD_MINUTES)
        )
        return order


def start_mercadopago_checkout(order):
    if not settings.MERCADOPAGO_ACCESS_TOKEN:
        raise CheckoutError("Mercado Pago no está configurado. Agregá MERCADOPAGO_ACCESS_TOKEN.")
    sdk = mercadopago.SDK(settings.MERCADOPAGO_ACCESS_TOKEN)
    ticket_names = ", ".join(
        f"{line.quantity} × {line.ticket_type.name}"
        for line in order.lines.select_related("ticket_type")
    )
    name_parts = order.buyer_name.split()
    first_name = name_parts[0]
    surname = " ".join(name_parts[1:])
    items = [
        {
            "id": str(order.pk),
            "title": f"{order.event.title} · {ticket_names}",
            "quantity": 1,
            "unit_price": float(order.total),
            "currency_id": order.currency,
        }
    ]
    preference = {
        "items": items,
        "payer": {
            "email": order.buyer_email,
            "name": first_name,
            "surname": surname,
            "phone": {
                "area_code": order.buyer_phone_area_code,
                "number": order.buyer_phone_number,
            },
            "identification": {"type": "DNI", "number": order.buyer_dni},
        },
        "external_reference": str(order.pk),
        "notification_url": f"{settings.SITE_URL}/api/webhooks/mercadopago",
        "back_urls": {
            "success": f"{settings.SITE_URL}/orders/{order.pk}/?result=success",
            "failure": f"{settings.SITE_URL}/orders/{order.pk}/?result=failure",
            "pending": f"{settings.SITE_URL}/orders/{order.pk}/?result=pending",
        },
        "expires": True,
        "expiration_date_from": timezone.now().isoformat(),
        "expiration_date_to": (order.created_at + timedelta(minutes=HOLD_MINUTES)).isoformat(),
        "statement_descriptor": "EVENTOS",
    }
    result = sdk.preference().create(preference)
    response = result.get("response", {})
    if result.get("status") not in (200, 201) or not response.get("init_point"):
        raise CheckoutError("Mercado Pago no pudo crear el pago. Intentá nuevamente.")
    order.preference_id = response.get("id", "")
    order.save(update_fields=("preference_id",))
    return response["init_point"]


def process_mercadopago_payment(payment_id):
    if not settings.MERCADOPAGO_ACCESS_TOKEN:
        raise CheckoutError("Mercado Pago no está configurado.")
    sdk = mercadopago.SDK(settings.MERCADOPAGO_ACCESS_TOKEN)
    result = sdk.payment().get(payment_id)
    if result.get("status") != 200:
        raise CheckoutError("No se pudo verificar el pago con Mercado Pago.")
    payment = result.get("response", {})
    order_id = payment.get("external_reference")
    if not order_id:
        raise CheckoutError("El pago verificado no tiene una orden asociada.")
    payment_status = payment.get("status")
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(pk=order_id)
        except (Order.DoesNotExist, ValueError) as error:
            raise CheckoutError("No se encontró la orden del pago.") from error
        order.payment_id = str(payment_id)
        if payment_status == "approved":
            if order.status == Order.Status.PENDING:
                order.status = Order.Status.PAID
                order.paid_at = timezone.now()
                order.save(update_fields=("payment_id", "status", "paid_at"))
                Seat.objects.filter(reserved_order=order.pk).update(reserved_until=None)
                for line in order.lines.all():
                    for _ in range(line.quantity):
                        issued_ticket = IssuedTicket(order_line=line)
                        issued_ticket.qr_token = dumps(
                            {
                                "ticket_id": str(issued_ticket.pk),
                                "event_id": order.event_id,
                            },
                            salt=TICKET_SALT,
                        )
                        issued_ticket.save()
                TicketDelivery.objects.get_or_create(order=order)
                if order.discount_code_id:
                    DiscountCode.objects.filter(pk=order.discount_code_id).update(
                        uses=models.F("uses") + 1
                    )
            elif order.status == Order.Status.PAID:
                order.save(update_fields=("payment_id",))
        elif payment_status in ("refunded", "charged_back") and order.status != Order.Status.REFUNDED:
            order.status = Order.Status.REFUNDED
            order.save(update_fields=("payment_id", "status"))
            Seat.objects.filter(reserved_order=order.pk).update(
                reserved_order=None, reserved_until=None
            )
        elif payment_status in ("rejected", "cancelled"):
            if order.status == Order.Status.PENDING:
                order.status = Order.Status.CANCELLED
                order.save(update_fields=("payment_id", "status"))
                Seat.objects.filter(reserved_order=order.pk).update(
                    reserved_order=None, reserved_until=None
                )
        elif payment_status != "pending":
            order.save(update_fields=("payment_id",))
        return order


def deliver_ticket(delivery):
    order = delivery.order
    tickets = list(
        IssuedTicket.objects.filter(order_line__order=order)
        .select_related("order_line__ticket_type")
        .order_by("created_at")
    )
    html = render_to_string(
        "tickets/email_ticket.html", {"order": order, "tickets": tickets}
    )
    message = EmailMultiAlternatives(
        subject=f"Tu entrada para {order.event.title}",
        body=f"Tu compra está confirmada. Número de orden: {order.pk}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[order.buyer_email],
    )
    message.attach_alternative(html, "text/html")
    for index, issued_ticket in enumerate(tickets, start=1):
        qr = qrcode.make(issued_ticket.qr_token)
        image = io.BytesIO()
        qr.save(image, format="PNG")
        message.attach(f"entrada-{index:02d}.png", image.getvalue(), "image/png")
    message.send(fail_silently=False)
    delivery.sent_at = timezone.now()
    delivery.attempts += 1
    delivery.last_error = ""
    delivery.save(update_fields=("sent_at", "attempts", "last_error"))
