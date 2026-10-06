from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tickets.models import TicketDelivery
from tickets.services import deliver_ticket


class Command(BaseCommand):
    help = "Send queued ticket emails and retry transient delivery failures."

    def handle(self, *args, **options):
        deliveries = TicketDelivery.objects.filter(
            sent_at__isnull=True, order__status="paid"
        ).select_related("order", "order__event")
        sent = 0
        for delivery in deliveries.iterator():
            try:
                deliver_ticket(delivery)
            except Exception as error:
                delivery.attempts += 1
                delivery.last_error = str(error)[:2000]
                delivery.save(update_fields=("attempts", "last_error"))
                self.stderr.write(
                    self.style.ERROR(f"Delivery {delivery.pk} failed: {error}")
                )
            else:
                sent += 1
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} ticket email(s) at {timezone.now()}."))
