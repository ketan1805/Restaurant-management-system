"""
TableServe — Database Data Viewer Utility
Run with: python view_database.py
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'restaurant_project.settings')
django.setup()

from django.contrib.auth.models import User, Group
from restaurant.models import (
    Table, MenuCategory, MenuItem, Order, OrderItem,
    Reservation, InventoryItem, StockTransaction, NGO, EventTheme
)
from restaurant.kitchen_models import KitchenStation, ChefStatus, KitchenTicket


def separator(title):
    print("\n" + "=" * 70)
    print(f"  {title.upper()}")
    print("=" * 70)


def show_users():
    separator("1. Users & Staff Accounts")
    users = User.objects.all().order_by('id')
    print(f"{'ID':<4} | {'Username':<18} | {'Full Name':<18} | {'Role':<14} | {'Email':<30}")
    print("-" * 90)
    for u in users:
        groups = list(u.groups.values_list('name', flat=True))
        role = 'Superuser' if u.is_superuser else (groups[0] if groups else ('Staff' if u.is_staff else 'Customer'))
        full_name = u.get_full_name() or '-'
        email = u.email or '-'
        print(f"{u.id:<4} | {u.username:<18} | {full_name:<18} | {role:<14} | {email:<30}")


def show_menu():
    separator("2. Menu Categories & Items")
    print(f"{'ID':<4} | {'Item Name':<30} | {'Category':<18} | {'Price':<10} | {'Available'}")
    print("-" * 75)
    for m in MenuItem.objects.all().order_by('category__name', 'name'):
        cat = m.category.name if m.category else 'Uncategorized'
        print(f"{m.id:<4} | {m.name:<30} | {cat:<18} | Rs. {m.price:<6} | {m.is_available}")


def show_tables():
    separator("3. Dining Tables")
    print(f"{'ID':<4} | {'Table No':<10} | {'Seats':<8} | {'Location':<12} | {'Status':<10} | {'Available'}")
    print("-" * 65)
    for t in Table.objects.all().order_by('id'):
        print(f"{t.id:<4} | {t.number:<10} | {t.seats:<8} | {t.location:<12} | {t.status:<10} | {t.is_available}")


def show_orders():
    separator("4. Orders & Items")
    orders = Order.objects.all().order_by('-id')
    if not orders.exists():
        print("No orders found.")
        return
    for o in orders:
        cust = o.user.username if o.user else 'Guest'
        tbl = f"Table {o.table.number}" if o.table else "Delivery/Online"
        items = [f"{i.menu_item.name} (x{i.quantity})" for i in o.items.all()]
        items_str = ", ".join(items) if items else "No items"
        print(f"Order #{o.id:<3} | Type: {o.order_type:<10} | {tbl:<15} | Customer: {cust:<12} | Total: Rs. {o.total:<7} | Status: {o.status}")
        print(f"   Items: {items_str}")


def show_reservations():
    separator("5. Table Reservations")
    res = Reservation.objects.all().order_by('-date', '-time')
    print(f"{'ID':<4} | {'Customer Name':<18} | {'Date & Time':<20} | {'Guests':<8} | {'Table':<10} | {'Status'}")
    print("-" * 75)
    for r in res:
        tbl = f"Table {r.table.number}" if r.table else 'Unassigned'
        dt = f"{r.date} {r.time}"
        print(f"{r.id:<4} | {r.name:<18} | {dt:<20} | {r.guests:<8} | {tbl:<10} | {r.status}")


def show_inventory():
    separator("6. Inventory Stock")
    print(f"{'ID':<4} | {'Item Name':<18} | {'Category':<14} | {'Stock Qty':<12} | {'Min Stock':<10} | {'Supplier'}")
    print("-" * 80)
    for i in InventoryItem.objects.all().order_by('category', 'name'):
        qty = f"{i.quantity} {i.unit}"
        sup = i.supplier or '-'
        print(f"{i.id:<4} | {i.name:<18} | {i.category:<14} | {qty:<12} | {i.min_stock:<10} | {sup}")


def show_kitchen():
    separator("7. Kitchen Stations & Chefs")
    print("Stations:")
    for s in KitchenStation.objects.all():
        print(f"  * [{s.code}] {s.name} - {s.description}")

    print("\nChef Status:")
    for cs in ChefStatus.objects.all():
        st = cs.current_station.name if cs.current_station else 'Unassigned'
        print(f"  * {cs.chef.username:<15} | Station: {st:<22} | Available: {cs.is_available} | Active Tickets: {cs.active_tickets_count}/{cs.max_simultaneous_orders}")


def show_ngos_and_themes():
    separator("8. NGOs & Event Themes")
    print("NGO Partners:")
    for n in NGO.objects.all():
        print(f"  * #{n.id} {n.name:<25} | Contact: {n.contact_person:<15} | Phone: {n.phone:<16} | Status: {n.status}")
    print("\nEvent Themes:")
    for et in EventTheme.objects.all():
        print(f"  * #{et.id} {et.name:<25} | Image: {et.image}")


def main():
    print("\n======================================================================")
    print("            TABLESERVE DATABASE DATA SNAPSHOT                         ")
    print("======================================================================")
    show_users()
    show_menu()
    show_tables()
    show_orders()
    show_reservations()
    show_inventory()
    show_kitchen()
    show_ngos_and_themes()
    print("\n" + "=" * 70)
    print("  To view or edit interactively, log in to:")
    print("  - Django Admin: http://127.0.0.1:8000/django-admin/")
    print("  - Admin Dashboard: http://127.0.0.1:8000/admin-login/")
    print("=" * 70 + "\n")


if __name__ == '__main__':
    main()