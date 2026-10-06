import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class Event(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Borrador"
        PUBLISHED = "published", "Publicado"
        ARCHIVED = "archived", "Archivado"

    title = models.CharField(max_length=180)
    slug = models.SlugField(max_length=200, unique=True)
    summary = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    event_date = models.DateTimeField()
    venue = models.CharField(max_length=180)
    address = models.CharField(max_length=250, blank=True)
    flyer_url = models.URLField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("event_date",)

    def __str__(self):
        return self.title


class TicketType(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="ticket_types")
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=250, blank=True)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    sale_starts_at = models.DateTimeField(null=True, blank=True)
    sale_ends_at = models.DateTimeField(null=True, blank=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("display_order", "price")

    def __str__(self):
        return f"{self.event}: {self.name}"

    def available_quantity(self):
        now = timezone.now()
        sold = self.order_lines.filter(
            models.Q(order__status=Order.Status.PAID)
            | models.Q(
                order__status=Order.Status.PENDING,
                order__created_at__gte=now - timedelta(minutes=15),
            )
        ).aggregate(total=models.Sum("quantity"))["total"] or 0
        return max(self.quantity - sold, 0)

    def is_on_sale(self):
        now = timezone.now()
        return self.active and not (
            (self.sale_starts_at and now < self.sale_starts_at)
            or (self.sale_ends_at and now > self.sale_ends_at)
        )


class Seat(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seats")
    ticket_type = models.ForeignKey(
        TicketType, on_delete=models.CASCADE, related_name="seats"
    )
    label = models.CharField(max_length=60)
    reserved_order = models.UUIDField(null=True, blank=True, db_index=True)
    reserved_until = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ("label",)
        constraints = [
            models.UniqueConstraint(fields=("event", "label"), name="unique_event_seat")
        ]

    def __str__(self):
        return f"{self.event}: {self.label}"


class DiscountCode(models.Model):
    class Kind(models.TextChoices):
        PERCENT = "percent", "Porcentaje"
        FIXED = "fixed", "Importe fijo"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="discount_codes")
    code = models.CharField(max_length=40)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    uses = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "code"), name="unique_event_discount_code"
            )
        ]

    def __str__(self):
        return f"{self.event}: {self.code}"

    def clean(self):
        super().clean()
        if self.kind == self.Kind.PERCENT and self.value > Decimal("100"):
            raise ValidationError({"value": "El descuento porcentual no puede superar el 100%."})

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        PAID = "paid", "Aprobado"
        REFUNDED = "refunded", "Reembolsado"
        CANCELLED = "cancelled", "Cancelado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="orders")
    buyer_name = models.CharField(max_length=160)
    buyer_email = models.EmailField()
    buyer_dni = models.CharField(max_length=12, default="")
    buyer_phone_area_code = models.CharField(max_length=5, default="")
    buyer_phone_number = models.CharField(max_length=12, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="ARS")
    discount_code = models.ForeignKey(
        DiscountCode, on_delete=models.SET_NULL, null=True, blank=True
    )
    payment_id = models.CharField(max_length=80, blank=True, db_index=True)
    preference_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.event} — {self.id}"


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    ticket_type = models.ForeignKey(
        TicketType, on_delete=models.PROTECT, related_name="order_lines"
    )
    quantity = models.PositiveSmallIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    seat = models.OneToOneField(Seat, on_delete=models.PROTECT, null=True, blank=True)


class IssuedTicket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_line = models.ForeignKey(
        OrderLine, on_delete=models.CASCADE, related_name="issued_tickets"
    )
    qr_token = models.TextField(unique=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)


class TicketDelivery(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="delivery")
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
