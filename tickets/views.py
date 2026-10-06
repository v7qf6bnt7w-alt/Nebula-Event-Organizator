import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .models import Event, IssuedTicket, Order, OrderLine, Seat, TicketDelivery
from .services import (
    CheckoutError,
    create_checkout_order,
    process_mercadopago_payment,
    start_mercadopago_checkout,
    verify_mercadopago_signature,
    verify_ticket_token,
)

logger = logging.getLogger(__name__)


def event_list(request):
    events = (
        Event.objects.filter(status=Event.Status.PUBLISHED, event_date__gte=timezone.now())
        .annotate(ticket_count=Count("ticket_types", filter=Q(ticket_types__active=True)))
        .prefetch_related("ticket_types")
    )
    return render(
        request,
        "tickets/event_list.html",
        {
            "events": events,
            "event_months": sorted(
                {
                    (event.event_date.year, event.event_date.month)
                    for event in events
                }
            ),
        },
    )


@staff_member_required
def organizer_dashboard(request):
    now = timezone.now()
    recent_orders = Order.objects.select_related("event").order_by("-created_at")[:7]
    paid_orders = Order.objects.filter(status=Order.Status.PAID)
    paid_totals = paid_orders.aggregate(revenue=Sum("total"), orders=Count("id"))
    ticket_count = OrderLine.objects.filter(
        order__status=Order.Status.PAID
    ).aggregate(total=Sum("quantity"))["total"] or 0
    sales_by_day = {
        row["day"]: row["revenue"]
        for row in paid_orders.filter(
            paid_at__date__gte=now.date() - timedelta(days=6),
            paid_at__date__lte=now.date(),
        )
        .annotate(day=TruncDate("paid_at"))
        .values("day")
        .annotate(revenue=Sum("total"))
    }
    weekday_labels = ("LUN", "MAR", "MIÉ", "JUE", "VIE", "SÁB", "DOM")
    sales_week = []
    for offset in range(6, -1, -1):
        day = now.date() - timedelta(days=offset)
        sales_week.append(
            {
                "label": weekday_labels[day.weekday()],
                "amount": sales_by_day.get(day, Decimal("0.00")),
            }
        )
    max_daily_revenue = max((point["amount"] for point in sales_week), default=0)
    for point in sales_week:
        point["height"] = (
            max(8, int(point["amount"] / max_daily_revenue * 100))
            if max_daily_revenue
            else 8
        )
    context = {
        "published_events": Event.objects.filter(
            status=Event.Status.PUBLISHED, event_date__gte=now
        ).count(),
        "total_events": Event.objects.count(),
        "pending_orders": Order.objects.filter(status=Order.Status.PENDING).count(),
        "approved_orders": paid_totals["orders"],
        "revenue": paid_totals["revenue"] or Decimal("0.00"),
        "tickets_sold": ticket_count,
        "recent_orders": recent_orders,
        "sales_week": sales_week,
    }
    return render(request, "tickets/organizer_dashboard.html", context)


def event_detail(request, slug):
    event = get_object_or_404(Event, slug=slug, status=Event.Status.PUBLISHED)
    if request.method == "POST":
        try:
            quantities = {
                key.removeprefix("quantity_"): value
                for key, value in request.POST.items()
                if key.startswith("quantity_")
            }
            seat_ids = request.POST.getlist("seats")
            order = create_checkout_order(
                event=event,
                buyer_name=request.POST.get("buyer_name", ""),
                buyer_email=request.POST.get("buyer_email", ""),
                buyer_email_confirmation=request.POST.get(
                    "buyer_email_confirmation", ""
                ),
                buyer_dni=request.POST.get("buyer_dni", ""),
                buyer_phone_area_code=request.POST.get(
                    "buyer_phone_area_code", ""
                ),
                buyer_phone_number=request.POST.get("buyer_phone_number", ""),
                quantities=quantities,
                seat_ids=seat_ids,
                coupon_code=request.POST.get("coupon_code", ""),
            )
            checkout_url = start_mercadopago_checkout(order)
        except CheckoutError as error:
            if "order" in locals():
                Order.objects.filter(pk=order.pk, status=Order.Status.PENDING).update(
                    status=Order.Status.CANCELLED
                )
                Seat.objects.filter(reserved_order=order.pk).update(
                    reserved_order=None, reserved_until=None
                )
            messages.error(request, str(error))
            return redirect("event_detail", slug=event.slug)
        except Exception:
            logger.exception("Mercado Pago checkout failed for event %s", event.pk)
            if "order" in locals():
                Order.objects.filter(pk=order.pk, status=Order.Status.PENDING).update(
                    status=Order.Status.CANCELLED
                )
                Seat.objects.filter(reserved_order=order.pk).update(
                    reserved_order=None, reserved_until=None
                )
            messages.error(request, "No pudimos iniciar el pago. Intentá nuevamente.")
            return redirect("event_detail", slug=event.slug)
        return redirect(checkout_url)

    now = timezone.now()
    seats = event.seats.select_related("ticket_type").filter(
        Q(reserved_order__isnull=True) | Q(reserved_until__lte=now)
    )
    ticket_types = [
        ticket
        for ticket in event.ticket_types.filter(active=True)
        if ticket.is_on_sale() and ticket.available_quantity() > 0
    ]
    return render(
        request,
        "tickets/event_detail.html",
        {"event": event, "ticket_types": ticket_types, "seats": seats},
    )


def order_status(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    result = request.GET.get("result", "")
    return render(
        request,
        "tickets/order_status.html",
        {
            "order": order,
            "result": result,
            "hold_until": order.created_at + timedelta(minutes=15),
        },
    )


def retrieve_ticket(request):
    if request.method != "POST":
        return render(request, "tickets/retrieve_ticket.html")
    email = request.POST.get("email", "").strip().lower()
    order_id = request.POST.get("order_id", "").strip()
    orders = Order.objects.filter(
        buyer_email__iexact=email,
        status=Order.Status.PAID,
        created_at__gte=timezone.now() - timedelta(days=365),
    )
    if order_id:
        try:
            order_id = uuid.UUID(order_id)
        except ValueError:
            orders = orders.none()
        else:
            orders = orders.filter(pk=order_id)
    for order in orders:
        TicketDelivery.objects.get_or_create(order=order, defaults={"attempts": 0})
        delivery = order.delivery
        if delivery.sent_at:
            delivery.sent_at = None
            delivery.save(update_fields=("sent_at",))
    messages.success(
        request, "Si encontramos una entrada con esos datos, la enviaremos a tu correo."
    )
    return redirect("retrieve_ticket")

@csrf_exempt
def mercadopago_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid_payload"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid_payload"}, status=400)
    payment_data = payload.get("data") or {}
    if not isinstance(payment_data, dict):
        return JsonResponse({"error": "invalid_payload"}, status=400)
    payment_id = payment_data.get("id") or request.GET.get("data.id")
    payment_id = str(payment_id) if payment_id else ""
    if not verify_mercadopago_signature(
        request.headers.get("X-Signature", ""),
        request.headers.get("X-Request-Id", ""),
        payment_id,
        settings.MERCADOPAGO_WEBHOOK_SECRET,
    ):
        return JsonResponse({"error": "invalid_signature"}, status=401)
    try:
        order = process_mercadopago_payment(payment_id)
    except CheckoutError as error:
        logger.warning("Mercado Pago webhook could not be processed: %s", error)
        return JsonResponse({"error": "payment_verification_failed"}, status=502)
    except Exception:
        logger.exception("Unexpected Mercado Pago webhook failure")
        return JsonResponse({"error": "internal_error"}, status=500)
    return JsonResponse({"received": True, "order": str(order.pk)})


@staff_member_required
@ensure_csrf_cookie
def gatekeeper(request):
    return render(request, "tickets/gatekeeper.html")


@staff_member_required
@require_POST
def check_in(request):
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid_payload"}, status=400)
    token = body.get("token", "")
    if not isinstance(token, str) or not token:
        return JsonResponse({"error": "invalid_ticket"}, status=400)
    payload = verify_ticket_token(token)
    if not payload:
        return JsonResponse({"error": "invalid_ticket"}, status=400)
    try:
        ticket = IssuedTicket.objects.select_related(
            "order_line__order__event"
        ).get(
            pk=payload.get("ticket_id"),
            order_line__order__event_id=payload.get("event_id"),
            order_line__order__status=Order.Status.PAID,
        )
    except (IssuedTicket.DoesNotExist, ValueError):
        return JsonResponse({"error": "ticket_not_found"}, status=404)
    checked_in = IssuedTicket.objects.filter(
        pk=ticket.pk, checked_in_at__isnull=True
    ).update(checked_in_at=timezone.now())
    if not checked_in:
        return JsonResponse(
            {"error": "already_used", "event": ticket.order_line.order.event.title},
            status=409,
        )
    return JsonResponse(
        {
            "valid": True,
            "buyer": ticket.order_line.order.buyer_name,
            "event": ticket.order_line.order.event.title,
            "ticket": str(ticket.pk),
        }
    )
