from django.urls import path
from . import staff_views

app_name = 'staff'

urlpatterns = [
    # Root
    path('',                         staff_views.staff_dashboard,          name='dashboard'),
    path('no-access/',               staff_views.no_access,                name='no_access'),

    # Role dashboards
    path('manager/',                 staff_views.manager_dashboard,        name='manager_dashboard'),
    path('kitchen/',                 staff_views.kitchen_dashboard,        name='kitchen_dashboard'),
    path('order-handler/',           staff_views.order_handler_dashboard,  name='order_handler_dashboard'),
    path('waiter/',                  staff_views.waiter_dashboard,         name='waiter_dashboard'),

    # Orders
    path('orders/',                  staff_views.staff_orders,             name='orders'),

    # Tables (Manager manage, others view)
    path('tables/',                  staff_views.staff_tables,             name='tables'),

    # Reservations
    path('table-reservations/',      staff_views.staff_table_reservations, name='table_reservations'),
    path('event-reservations/',      staff_views.staff_event_reservations, name='event_reservations'),

    # Billing (Manager only)
    path('billing/',                 staff_views.staff_billing,            name='billing'),

    # Customers (all, with limits)
    path('customers/',               staff_views.staff_customers,          name='customers'),

    # Notifications (all)
    path('notifications/',           staff_views.staff_notifications,      name='notifications'),

    # Manager-only
    path('menu-items/',              staff_views.staff_menu_items,         name='menu_items'),
    path('inventory/',               staff_views.staff_inventory,          name='inventory'),
    path('reports/',                 staff_views.staff_reports,            name='reports'),
    path('ngos/',                    staff_views.staff_ngos,               name='ngos'),

    # Staff management
    path('staff-list/',              staff_views.staff_staff_list,         name='staff_list'),
    path('staff-list/add/',          staff_views.staff_add_staff,          name='add_staff'),
    path('staff-list/<int:user_id>/delete/', staff_views.staff_delete_staff, name='delete_staff'),
    path('staff-list/<int:user_id>/update-role/', staff_views.staff_update_role, name='update_staff_role'),

    # Profile
    path('profile/',                 staff_views.staff_profile,            name='profile'),
]