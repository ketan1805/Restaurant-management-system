from .models import Order, Reservation


def admin_badges(request):
    """Provide real counts for sidebar badges on every admin page."""
    if not request.user.is_authenticated:
        return {}

    if not (request.user.is_staff or request.user.is_superuser):
        return {}

    return {
        'pending_orders_count': Order.objects.filter(status='pending').count(),
        'pending_reservations_count': Reservation.objects.filter(is_deleted=False, status='pending').count(),
    }