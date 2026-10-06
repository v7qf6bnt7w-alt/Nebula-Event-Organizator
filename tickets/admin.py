from django.contrib import admin

from .models import (
    DiscountCode,
    Event,
    IssuedTicket,
    Order,
    Seat,
    TicketDelivery,
    TicketType,
)


class TicketTypeInline(admin.TabularInline):
    model = TicketType
    extra = 1


class SeatInline(admin.TabularInline):
    model = Seat
    extra = 0


@admin.register(TicketType)
class TicketTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "price", "quantity", "active", "display_order")
    list_filter = ("event", "active")
    search_fields = ("name", "event__title")
    list_editable = ("price", "quantity", "active", "display_order")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "event_date", "venue", "status")
    list_filter = ("status", "event_date")
    search_fields = ("title", "venue")
    prepopulated_fields = {"slug": ("title",)}
    inlines = (TicketTypeInline, SeatInline)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "buyer_email", "total", "status", "created_at")
    list_filter = ("status", "event")
    search_fields = ("id", "buyer_email", "payment_id")
    readonly_fields = (
        "id",
        "event",
        "buyer_name",
        "buyer_email",
        "buyer_dni",
        "buyer_phone_area_code",
        "buyer_phone_number",
        "status",
        "total",
        "currency",
        "discount_code",
        "payment_id",
        "preference_id",
        "created_at",
        "paid_at",
    )


@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "event", "kind", "value", "uses", "active")
    list_filter = ("event", "active", "kind")


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("event", "label", "ticket_type", "reserved_order", "reserved_until")
    list_filter = ("event", "ticket_type")
    search_fields = ("label",)


@admin.register(TicketDelivery)
class TicketDeliveryAdmin(admin.ModelAdmin):
    list_display = ("order", "attempts", "queued_at", "sent_at")
    readonly_fields = ("order", "attempts", "last_error", "queued_at", "sent_at")


@admin.register(IssuedTicket)
class IssuedTicketAdmin(admin.ModelAdmin):
    list_display = ("id", "order_line", "checked_in_at", "created_at")
    list_filter = ("checked_in_at",)
    search_fields = ("id", "order_line__order__buyer_email")
    readonly_fields = ("id", "order_line", "qr_token", "checked_in_at", "created_at")


admin.site.site_header = "Nébula · Gestión de eventos"
admin.site.site_title = "Nébula Admin"
admin.site.index_title = "Panel de organización"
admin.site.index_template = "admin/index_custom.html"
