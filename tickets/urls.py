from django.urls import path

from . import views

urlpatterns = [
    path("", views.event_list, name="event_list"),
    path("organizer/", views.organizer_dashboard, name="organizer_dashboard"),
    path("events/<slug:slug>/", views.event_detail, name="event_detail"),
    path("orders/<uuid:order_id>/", views.order_status, name="order_status"),
    path("retrieve-ticket/", views.retrieve_ticket, name="retrieve_ticket"),
    path("api/webhooks/mercadopago", views.mercadopago_webhook, name="mercadopago_webhook"),
    path("gate/", views.gatekeeper, name="gatekeeper"),
    path("api/check-in/", views.check_in, name="check_in"),
]
