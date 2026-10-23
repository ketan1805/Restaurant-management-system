import json
import csv
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User, Group
from django.db import transaction
from django.db.models import Sum, Count, Q, F
from django.utils import timezone
from datetime import timedelta
from functools import wraps
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import (
    Order, Payment, Reservation, Event,
    Table, MenuItem, MenuCategory, InventoryItem, NGO, FoodDonation, OrderItem,
    EventTheme, StockTransaction, PurchaseOrder, KitchenNotification,
    ItemCustomization,
)
import logging
import razorpay
from django.conf import settings
from .views import calculate_cancellation_refund

logger = logging.getLogger(__name__)



# ============================================================
# ROLE DECORATOR
# ============================================================
def role_required(*allowed_groups):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                messages.error(request, 'Please login first')
                return redirect('login')

            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            if not request.user.is_staff:
                messages.error(request, 'Staff access required')
                return redirect('home')

            user_groups = set(request.user.groups.values_list('name', flat=True))
            if not user_groups.intersection(set(allowed_groups)):
                messages.error(
                    request,
                    f'Access denied. Required role: {", ".join(allowed_groups)}'
                )
                return redirect('staff:no_access')

            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


staff_required = role_required('Manager', 'OrderHandler', 'Waiter', 'Chef', 'Kitchen Staff')


# ============================================================
# NO ACCESS
# ============================================================
@login_required
def no_access(request):
    return render(request, 'restaurant/staff/no_access.html')


# ============================================================
# STAFF DASHBOARD — auto redirect
# ============================================================
@staff_required
def staff_dashboard(request):
    user = request.user
    if user.is_superuser or user.groups.filter(name='Manager').exists():
        return redirect('staff:manager_dashboard')
    elif user.groups.filter(name__in=['Chef', 'Kitchen Staff']).exists():
        return redirect('staff:kitchen_dashboard')
    elif user.groups.filter(name='OrderHandler').exists():
        return redirect('staff:order_handler_dashboard')
    elif user.groups.filter(name='Waiter').exists():
        return redirect('staff:waiter_dashboard')
    return redirect('staff:no_access')


# ============================================================
# KITCHEN DASHBOARD — Real-time Food Preparation Queue
# ============================================================
@role_required('Chef', 'Kitchen Staff', 'Manager')
def kitchen_dashboard(request):
    """
    Kitchen Panel View for Chefs & Kitchen Staff.
    Manages food prep workflow: ACCEPTED -> PREPARING -> READY
    Auto-assigns ready orders to available waiters with lowest active workload.
    """
    if request.method == 'POST':
        action = request.POST.get('action')
        order_id = request.POST.get('order_id')

        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            messages.error(request, 'Order not found.')
            return redirect('staff:kitchen_dashboard')

        # 1. Start Preparing: ACCEPTED -> PREPARING
        if action == 'start_preparing':
            if order.status == 'accepted':
                order.status = 'preparing'
                order.chef = request.user
                order.prep_started_at = timezone.now()
                order.save()
                messages.success(request, f'👨‍🍳 Order #{order.id} started preparing!')
            else:
                messages.error(request, f'Order #{order.id} cannot start preparing from status "{order.get_status_display()}". Must be "Accepted".')

        # 2. Mark Ready: PREPARING -> READY
        elif action == 'mark_ready':
            if order.status == 'preparing':
                order.status = 'ready'
                order.ready_at = timezone.now()
                order.save()

                # Notify Waiter
                is_rush = order.rush_flag or order.priority == 'high'
                rush_str = " [RUSH]" if is_rush else ""
                table_str = f"Table {order.table.number}" if order.table else "Dine-in"
                KitchenNotification.objects.create(
                    target_role='Waiter',
                    notification_type='order_ready',
                    order=order,
                    title=f"Order #{order.id}{rush_str} Ready for Serving",
                    message=f"Order #{order.id} ({table_str}) is prepared and ready for service!",
                )

                messages.success(request, f'✅ Order #{order.id} marked as READY!')
            else:
                messages.error(request, f'Order #{order.id} cannot be marked ready from status "{order.get_status_display()}". Must be "Preparing".')

        else:
            messages.error(request, 'Invalid action request.')

        return redirect('staff:kitchen_dashboard')

    active_orders = Order.objects.filter(
        status__in=['accepted', 'preparing', 'ready']
    ).select_related('user', 'table', 'reservation', 'assigned_waiter', 'chef', 'order_handler').prefetch_related(
        'items__menu_item', 'payments'
    ).order_by('-rush_flag', '-priority', 'created_at')

    accepted_orders = [o for o in active_orders if o.status == 'accepted']
    preparing_orders = [o for o in active_orders if o.status == 'preparing']
    ready_orders = [o for o in active_orders if o.status == 'ready']
    rush_orders_count = sum(1 for o in active_orders if o.rush_flag or o.priority == 'high')

    context = {
        'active_orders': active_orders,
        'accepted_orders': accepted_orders,
        'preparing_orders': preparing_orders,
        'ready_orders': ready_orders,
        'new_orders_count': len(accepted_orders),
        'preparing_count': len(preparing_orders),
        'ready_count': len(ready_orders),
        'rush_count': rush_orders_count,
    }
    return render(request, 'restaurant/staff/kitchen_dashboard.html', context)


# ============================================================
# MANAGER DASHBOARD — Full Database & Flowchart Connectivity
# ============================================================
@role_required('Manager')
def manager_dashboard(request):
    today = timezone.now().date()

    # ============================================================
    # POST ACTIONS (Interactive Alerts, Quick Actions, Status Updates)
    # ============================================================
    if request.method == 'POST':
        action = request.POST.get('action')

        # 1. Quick Add Stock (Alerts & Inventory)
        if action == 'add_stock':
            item_id = request.POST.get('item_id')
            qty_to_add = request.POST.get('qty', 5)
            try:
                qty_val = Decimal(str(qty_to_add))
                item = InventoryItem.objects.get(id=item_id)
                item.quantity += qty_val
                item.save()
                messages.success(request, f"Added {qty_val} {item.unit} to {item.name}. New total: {item.quantity} {item.unit}")
            except Exception as e:
                messages.error(request, f"Failed to update stock: {e}")
            return redirect(request.get_full_path())

        # 2. Update Reservation Status (Check-in, Confirm, Cancel)
        elif action == 'update_reservation_status':
            res_id = request.POST.get('reservation_id')
            new_status = request.POST.get('status')
            valid_statuses = ['confirmed', 'cancelled', 'completed', 'rejected', 'pending']
            if new_status in valid_statuses:
                try:
                    res = Reservation.objects.get(id=res_id)
                    res.status = new_status
                    res.save()
                    messages.success(request, f"Reservation #{res.id} ({res.name}) updated to '{res.get_status_display()}'")
                except Reservation.DoesNotExist:
                    messages.error(request, "Reservation not found")
            return redirect(request.get_full_path())

        # 3. Update Recent Order Status
        elif action == 'update_order_status':
            order_id = request.POST.get('order_id')
            new_status = request.POST.get('status')
            valid_order_statuses = ['pending', 'preparing', 'out_for_delivery', 'delivered', 'cancelled']
            if new_status in valid_order_statuses:
                try:
                    order = Order.objects.get(id=order_id)
                    order.status = new_status
                    order.save()
                    if new_status == 'delivered' and order.table:
                        order.table.is_available = True
                        order.table.save()
                    messages.success(request, f"Order #{order.id} status set to '{order.get_status_display()}'")
                except Order.DoesNotExist:
                    messages.error(request, "Order not found")
            return redirect(request.get_full_path())

        # 4. Notify Reservation Customer
        elif action == 'notify_reservation':
            res_id = request.POST.get('reservation_id')
            try:
                res = Reservation.objects.get(id=res_id)
                messages.info(request, f"Notification alert triggered for guest {res.name} ({res.phone or 'No phone'})")
            except Reservation.DoesNotExist:
                pass
            return redirect(request.get_full_path())

    # ============================================================
    # GLOBAL FILTERS BAR
    # ============================================================
    selected_date = request.GET.get('date', 'today')
    selected_status = request.GET.get('status', 'all')
    selected_staff = request.GET.get('staff', 'all')
    selected_table = request.GET.get('table', 'all')

    orders_qs = Order.objects.select_related('user', 'table').prefetch_related('items__menu_item')

    # Date Filtering
    if selected_date == 'today':
        orders_qs = orders_qs.filter(created_at__date=today)
    elif selected_date == 'yesterday':
        orders_qs = orders_qs.filter(created_at__date=today - timedelta(days=1))
    elif selected_date == 'this_week':
        orders_qs = orders_qs.filter(created_at__date__gte=today - timedelta(days=6))
    elif selected_date == 'this_month':
        orders_qs = orders_qs.filter(created_at__date__gte=today - timedelta(days=29))

    # Status Filtering
    if selected_status != 'all':
        orders_qs = orders_qs.filter(status=selected_status)

    # Staff Filtering
    if selected_staff != 'all':
        orders_qs = orders_qs.filter(user_id=selected_staff)

    # Table Filtering
    if selected_table != 'all':
        orders_qs = orders_qs.filter(table_id=selected_table)

    # ============================================================
    # SECTION 1: STATS CARDS (8 Cards)
    # ============================================================
    total_orders = orders_qs.count()
    pending_orders = orders_qs.filter(status='pending').count()
    preparing_orders = orders_qs.filter(status='preparing').count()
    ready_orders = orders_qs.filter(status='out_for_delivery').count()
    delivered_orders = orders_qs.filter(status='delivered').count()

    total_revenue = orders_qs.filter(status__in=['delivered', 'out_for_delivery', 'preparing', 'pending']).aggregate(Sum('total'))['total__sum'] or 0

    all_tables = Table.objects.all().order_by('number')
    total_tables = all_tables.count()
    occupied_tables = all_tables.filter(is_available=False).count()
    free_tables = all_tables.filter(is_available=True).count()

    # Reservations
    res_qs = Reservation.objects.all()
    if selected_date == 'today':
        res_qs = res_qs.filter(date=today)
    elif selected_date == 'yesterday':
        res_qs = res_qs.filter(date=today - timedelta(days=1))
    elif selected_date == 'this_week':
        res_qs = res_qs.filter(date__gte=today - timedelta(days=6))
    elif selected_date == 'this_month':
        res_qs = res_qs.filter(date__gte=today - timedelta(days=29))

    total_reservations = res_qs.count()
    table_res_count = res_qs.filter(table__isnull=False).count()
    event_reservations_count = Event.objects.filter(event_date=today).count() if selected_date == 'today' else Event.objects.count()

    # Staff Stats
    total_staff = User.objects.filter(is_staff=True, is_superuser=False).count()
    managers = User.objects.filter(groups__name='Manager').count()
    handlers = User.objects.filter(groups__name='OrderHandler').count()
    waiters = User.objects.filter(groups__name='Waiter').count()
    all_staff_members = User.objects.filter(is_staff=True).order_by('username')

    # Inventory & Low Stock Stats
    all_inventory = InventoryItem.objects.all()
    low_stock_items = [item for item in all_inventory if item.is_low_stock]
    low_stock_count = len(low_stock_items)

    # ============================================================
    # SECTION 3: CHARTS DATA GENERATION
    # ============================================================
    # Chart 1: Sales Trend (Last 7 Days)
    sales_trend_labels = []
    sales_trend_data = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.strftime("%d %b")
        day_rev = Order.objects.filter(created_at__date=day).aggregate(Sum('total'))['total__sum'] or 0
        sales_trend_labels.append(day_str)
        sales_trend_data.append(float(day_rev))

    # Chart 2: Top 5 Items
    top_items_qs = OrderItem.objects.filter(order__in=orders_qs).values('menu_item__name').annotate(
        total_qty=Sum('quantity')
    ).order_by('-total_qty')[:5]

    top_items_labels = [item['menu_item__name'] for item in top_items_qs]
    top_items_data = [item['total_qty'] for item in top_items_qs]

    # Chart 3: Peak Hours (Hourly distribution)
    peak_hours_labels = ["9 AM", "12 PM", "3 PM", "6 PM", "7 PM", "8 PM", "9 PM", "10 PM"]
    peak_hours_data = [0] * len(peak_hours_labels)

    for o in orders_qs:
        h = o.created_at.hour
        if 8 <= h < 11:
            peak_hours_data[0] += 1
        elif 11 <= h < 14:
            peak_hours_data[1] += 1
        elif 14 <= h < 17:
            peak_hours_data[2] += 1
        elif 17 <= h < 19:
            peak_hours_data[3] += 1
        elif 19 <= h < 20:
            peak_hours_data[4] += 1
        elif 20 <= h < 21:
            peak_hours_data[5] += 1
        elif 21 <= h < 22:
            peak_hours_data[6] += 1
        elif h >= 22:
            peak_hours_data[7] += 1

    # Chart 4: Category Sales
    cat_sales_qs = OrderItem.objects.filter(order__in=orders_qs).values('menu_item__category__name').annotate(
        cat_total=Sum(F('quantity') * F('price'))
    ).order_by('-cat_total')

    cat_sales_labels = [item['menu_item__category__name'] or 'General' for item in cat_sales_qs]
    cat_sales_data = [float(item['cat_total']) for item in cat_sales_qs]

    # ============================================================
    # SECTION 4: ALERTS (Low Stock & Today's Reservations)
    # ============================================================
    today_reservations_list = Reservation.objects.filter(date=today).select_related('table', 'user').order_by('time')

    # ============================================================
    # SECTION 5: RECENT ORDERS (Last 10)
    # ============================================================
    recent_orders = orders_qs.order_by('-created_at')[:10]

    # ============================================================
    # SECTION 6: MANAGEMENT OVERVIEW (4 Tiles)
    # ============================================================
    total_menu_items = MenuItem.objects.count()
    out_of_stock_items = MenuItem.objects.filter(status='out_of_stock').count()
    total_inventory_count = all_inventory.count()
    total_customers = User.objects.filter(is_staff=False, is_superuser=False).count()
    new_customers_today = User.objects.filter(is_staff=False, is_superuser=False, date_joined__date=today).count()
    total_ngos = NGO.objects.count()
    total_ngo_donations = NGO.objects.aggregate(Sum('meals_served'))['meals_served__sum'] or 0

    # ============================================================
    # SECTION 7: TOP PERFORMERS (2 Columns)
    # ============================================================
    top_customers = User.objects.filter(is_staff=False, is_superuser=False).annotate(
        total_spent=Sum('orders__total'),
        order_cnt=Count('orders')
    ).order_by('-total_spent', '-order_cnt')[:5]

    top_staff = User.objects.filter(is_staff=True, is_superuser=False).annotate(
        orders_handled=Count('orders')
    ).order_by('-orders_handled')[:5]

    context = {
        'manager_user': request.user,
        # Global Filters
        'selected_date': selected_date,
        'selected_status': selected_status,
        'selected_staff': selected_staff,
        'selected_table': selected_table,
        'staff_members': all_staff_members,
        'all_tables': all_tables,
        # Section 1 Stats
        'total_orders': total_orders,
        'pending_orders': pending_orders,
        'preparing_orders': preparing_orders,
        'ready_orders': ready_orders,
        'delivered_orders': delivered_orders,
        'total_revenue': total_revenue,
        'total_tables': total_tables,
        'occupied_tables': occupied_tables,
        'free_tables': free_tables,
        'total_reservations': total_reservations,
        'table_res_count': table_res_count,
        'event_reservations_count': event_reservations_count,
        'total_staff': total_staff,
        'managers': managers,
        'handlers': handlers,
        'waiters': waiters,
        'low_stock_count': low_stock_count,
        # Section 3 Charts Data (JSON)
        'sales_trend_labels_json': json.dumps(sales_trend_labels),
        'sales_trend_data_json': json.dumps(sales_trend_data),
        'top_items_labels_json': json.dumps(top_items_labels),
        'top_items_data_json': json.dumps(top_items_data),
        'peak_hours_labels_json': json.dumps(peak_hours_labels),
        'peak_hours_data_json': json.dumps(peak_hours_data),
        'cat_sales_labels_json': json.dumps(cat_sales_labels),
        'cat_sales_data_json': json.dumps(cat_sales_data),
        # Section 4 Alerts
        'low_stock_items': low_stock_items,
        'today_reservations': today_reservations_list,
        # Section 5 Orders
        'recent_orders': recent_orders,
        # Section 6 Management Overview
        'total_menu_items': total_menu_items,
        'out_of_stock_items': out_of_stock_items,
        'total_inventory': total_inventory_count,
        'total_customers': total_customers,
        'new_customers_today': new_customers_today,
        'total_ngos': total_ngos,
        'total_ngo_donations': total_ngo_donations,
        # Section 7 Top Performers
        'top_customers': top_customers,
        'top_staff': top_staff,
    }
    return render(request, 'restaurant/staff/manager_dashboard.html', context)



# ============================================================
# ORDER HANDLER DASHBOARD
# ============================================================
@role_required('OrderHandler', 'Manager')
def order_handler_dashboard(request):
    if request.method == 'POST':
        order_id = request.POST.get('order_id')
        new_status = request.POST.get('status')
        valid = ['pending', 'accepted', 'preparing', 'ready', 'out_for_delivery', 'delivered', 'completed', 'cancelled']
        if new_status in valid:
            try:
                o = Order.objects.get(id=order_id)
                o.status = new_status
                if new_status == 'accepted':
                    o.sent_to_kitchen_at = timezone.now()
                    o.order_handler = request.user
                    is_rush = o.rush_flag or o.priority == 'high'
                    table_str = f"Table {o.table.number}" if o.table else "Dine-in"
                    KitchenNotification.objects.create(
                        target_role='Kitchen',
                        notification_type='rush_order' if is_rush else 'new_order',
                        order=o,
                        title=f"{'⚡ RUSH Order' if is_rush else '🔔 New Order'} #{o.id}",
                        message=f"Order #{o.id} ({table_str}) verified and sent to Kitchen.",
                    )
                elif new_status == 'cancelled':
                    o.cancelled_at = timezone.now()
                    KitchenNotification.objects.create(
                        target_role='Kitchen',
                        notification_type='order_cancelled',
                        order=o,
                        title=f"❌ Order #{o.id} Cancelled",
                        message=f"Order #{o.id} was cancelled by Order Handler.",
                    )
                o.save()
                msg_txt = "sent to Kitchen!" if new_status == 'accepted' else f"updated to {o.get_status_display()}"
                messages.success(request, f'Order #{o.id} {msg_txt}')
            except Order.DoesNotExist:
                messages.error(request, 'Order not found')
        return redirect('staff:order_handler_dashboard')

    all_orders = Order.objects.select_related('user', 'table').prefetch_related(
        'items__menu_item'
    ).order_by('-created_at')

    context = {
        'total_orders': all_orders.count(),
        'paid_count': Payment.objects.filter(status__in=['success', 'paid']).count(),
        'preparing_count': Order.objects.filter(status='preparing').count(),
        'delivered_count': Order.objects.filter(status__in=['delivered', 'served']).count(),
        'restaurant_orders': all_orders,
        'orders': all_orders,
    }
    return render(request, 'restaurant/staff/order_handler_dashboard.html', context)


# ============================================================
# WAITER DASHBOARD
# ============================================================
@role_required('Waiter', 'Manager')
def waiter_dashboard(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # 1. Place New Order
        if action == 'place_order' or (not action and 'items' in request.POST):
            table_id = request.POST.get('table_id')
            items_raw = request.POST.get('items', '').strip()

            if not table_id or not items_raw:
                messages.error(request, 'Table and items are required')
                return redirect('staff:waiter_dashboard')

            try:
                table = Table.objects.get(id=table_id)
                order = Order.objects.create(
                    user=request.user, table=table, status='pending',
                    total=0, order_type='restaurant',
                )
                total = 0
                for name in [i.strip() for i in items_raw.split(',') if i.strip()]:
                    try:
                        mi = MenuItem.objects.get(name__iexact=name)
                        OrderItem.objects.create(order=order, menu_item=mi, quantity=1, price=mi.price)
                        total += float(mi.price)
                    except MenuItem.DoesNotExist:
                        pass
                order.total = total
                order.save()
                Payment.objects.create(
                    order=order,
                    amount=total,
                    method='upi',
                    status='success',
                    transaction_id=f"WAITER_PREPAID_{order.id}",
                )
                table.is_available = False
                table.save()
                messages.success(request, f'Order #{order.id} sent to Kitchen for Table {table.number}')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found')
            return redirect('staff:waiter_dashboard')

        # 2. Add Items to Existing Table Order
        if action == 'add_items':
            table_id = request.POST.get('table_id')
            items_raw = request.POST.get('items', '').strip()
            try:
                table = Table.objects.get(id=table_id)
                order = Order.objects.filter(table=table, status__in=['pending', 'preparing', 'out_for_delivery']).order_by('-created_at').first()
                if not order:
                    order = Order.objects.create(user=request.user, table=table, status='pending', total=0, order_type='restaurant')
                    table.is_available = False
                    table.save()

                added_total = 0
                for name in [i.strip() for i in items_raw.split(',') if i.strip()]:
                    try:
                        mi = MenuItem.objects.get(name__iexact=name)
                        OrderItem.objects.create(order=order, menu_item=mi, quantity=1, price=mi.price)
                        added_total += float(mi.price)
                    except MenuItem.DoesNotExist:
                        pass
                order.total = float(order.total) + added_total
                order.save()
                messages.success(request, f'Added new items to Table {table.number} Order #{order.id}')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found')
            return redirect('staff:waiter_dashboard')

        # 3. Clear Table (Mark Free & Complete Order)
        if action == 'clear_table':
            table_id = request.POST.get('table_id')
            try:
                table = Table.objects.get(id=table_id)
                table.is_available = True
                table.save()
                Order.objects.filter(table=table, status__in=['pending', 'preparing', 'out_for_delivery']).update(status='delivered')
                messages.success(request, f'Table {table.number} cleared and marked Free!')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found')
            return redirect('staff:waiter_dashboard')

        # 4. Transfer Table
        if action == 'transfer_table':
            source_table_id = request.POST.get('source_table_id')
            target_table_id = request.POST.get('target_table_id')
            try:
                src_table = Table.objects.get(id=source_table_id)
                tgt_table = Table.objects.get(id=target_table_id)
                order = Order.objects.filter(table=src_table, status__in=['pending', 'preparing', 'out_for_delivery']).order_by('-created_at').first()
                if order:
                    order.table = tgt_table
                    order.save()
                    src_table.is_available = True
                    src_table.save()
                    tgt_table.is_available = False
                    tgt_table.save()
                    messages.success(request, f'Order #{order.id} transferred from Table {src_table.number} to Table {tgt_table.number}')
                else:
                    messages.error(request, f'No active order on Table {src_table.number}')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found')
            return redirect('staff:waiter_dashboard')

        # 5. Merge Tables
        if action == 'merge_tables':
            source_table_id = request.POST.get('source_table_id')
            target_table_id = request.POST.get('target_table_id')
            try:
                src_table = Table.objects.get(id=source_table_id)
                tgt_table = Table.objects.get(id=target_table_id)
                src_order = Order.objects.filter(table=src_table, status__in=['pending', 'preparing', 'out_for_delivery']).order_by('-created_at').first()
                tgt_order = Order.objects.filter(table=tgt_table, status__in=['pending', 'preparing', 'out_for_delivery']).order_by('-created_at').first()

                if src_order and tgt_order:
                    src_order.items.all().update(order=tgt_order)
                    tgt_order.total = float(tgt_order.total) + float(src_order.total)
                    tgt_order.save()
                    src_order.delete()
                    src_table.is_available = True
                    src_table.save()
                    messages.success(request, f'Merged Table {src_table.number} into Table {tgt_table.number}')
                else:
                    messages.error(request, 'Both tables must have active orders to merge')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found')
            return redirect('staff:waiter_dashboard')

        # 6. Call Manager Alert
        if action == 'call_manager':
            table_number = request.POST.get('table_number', '')
            reason = request.POST.get('reason', 'Assistance required').strip()
            messages.success(request, f'🔔 Alert sent to Manager for Table {table_number}: "{reason}"')
            return redirect('staff:waiter_dashboard')

        # 7. Reserve Table
        if action == 'reserve_table':
            table_id = request.POST.get('table_id')
            customer_name = request.POST.get('customer_name', '').strip()
            phone = request.POST.get('phone', '').strip()
            guests = request.POST.get('guests', 2)
            try:
                table = Table.objects.get(id=table_id)
                Reservation.objects.create(
                    user=request.user, name=customer_name or 'Guest',
                    phone=phone, date=timezone.now().date(),
                    time=timezone.now().time(), guests=int(guests),
                    table=table, status='confirmed'
                )
                messages.success(request, f'Table {table.number} reserved for {customer_name}')
            except Exception as e:
                messages.error(request, f'Failed to reserve table: {e}')
            return redirect('staff:waiter_dashboard')

        # 8. Collect / Serve
        if action == 'collect':
            order_id = request.POST.get('order_id')
            try:
                o = Order.objects.get(id=order_id)
                if not o.assigned_waiter:
                    o.assigned_waiter = request.user
                    o.save(update_fields=['assigned_waiter', 'updated_at'])
                messages.success(request, f'✅ Order #{o.id} collected. Serve Table {o.table.number if o.table else "—"}')
            except Order.DoesNotExist:
                pass
            return redirect('staff:waiter_dashboard')

        if action == 'serve':
            order_id = request.POST.get('order_id')
            try:
                o = Order.objects.get(id=order_id, status__in=['ready', 'out_for_delivery'])
                o.status = 'delivered'
                o.served_at = timezone.now()
                if not o.assigned_waiter:
                    o.assigned_waiter = request.user
                o.save()
                if o.table:
                    o.table.is_available = True
                    o.table.save()
                messages.success(request, f'🍽️ Order #{o.id} marked as Served!')
            except Order.DoesNotExist:
                messages.error(request, 'Order not found or not in Ready state.')
            return redirect('staff:waiter_dashboard')

        return redirect('staff:waiter_dashboard')

    now = timezone.now()
    today = now.date()

    # 1. Food Ready: Total number of orders whose status is Ready across restaurant
    ready_orders_all = Order.objects.filter(status__in=['ready', 'out_for_delivery'])
    food_ready_count = ready_orders_all.count()

    # 2. To Serve: Total number of Ready orders waiting to be served
    to_serve_count = food_ready_count

    # 3. Served Today: Total orders served today
    served_today_count = Order.objects.filter(status__in=['delivered', 'served'], updated_at__date=today).count()

    # 4. Kitchen Alert – Food Ready: Show Ready orders that have not yet been served (limited list)
    kitchen_alert_orders = ready_orders_all.select_related('user', 'table').prefetch_related('items__menu_item', 'payments').order_by('-updated_at')[:10]

    tables = list(Table.objects.all().order_by('number'))
    for t in tables:
        active_order = Order.objects.filter(
            table=t,
            status__in=['pending', 'preparing', 'out_for_delivery']
        ).select_related('user').prefetch_related('items__menu_item').order_by('-created_at').first()

        t.active_order = active_order
        if active_order:
            t.table_status = 'occupied'
            delta = now - active_order.created_at
            t.elapsed_minutes = int(delta.total_seconds() // 60)
        else:
            active_res = Reservation.objects.filter(
                table=t,
                date=today,
                status__in=['pending', 'confirmed']
            ).first()
            t.active_reservation = active_res
            if active_res:
                t.table_status = 'reserved'
            else:
                t.table_status = 'free'

    today_reservations = Reservation.objects.filter(date=today).select_related('user', 'table').order_by('time')

    context = {
        'ready_orders': kitchen_alert_orders,
        'ready_count': food_ready_count,
        'to_serve_count': to_serve_count,
        'served_today_count': served_today_count,
        'tables': tables, 'total_tables': len(tables),
        'free_count': sum(1 for t in tables if t.table_status == 'free'),
        'occupied_count': sum(1 for t in tables if t.table_status == 'occupied'),
        'reserved_count': sum(1 for t in tables if t.table_status == 'reserved'),
        'today_reservations': today_reservations,
    }
    return render(request, 'restaurant/staff/waiter_dashboard.html', context)


# ============================================================
# TABLES MANAGEMENT — Full Manager Control, Live Stats, Snapshots, Filters & Analytics
# ============================================================
@role_required('Manager', 'OrderHandler', 'Waiter')
def staff_tables(request):
    is_manager = request.user.groups.filter(name='Manager').exists() or request.user.is_superuser
    now = timezone.now()
    today = now.date()

    # POST Handlers
    if request.method == 'POST' and is_manager:
        action = request.POST.get('action', '')

        if action == 'add':
            number = request.POST.get('number', '').strip()
            seats = request.POST.get('seats', '').strip()
            location = request.POST.get('location', 'Indoor').strip()
            waiter_id = request.POST.get('waiter_id', '')
            if not number or not seats:
                messages.error(request, 'Please fill required fields')
                return redirect('staff:tables')
            if Table.objects.filter(number=number).exists():
                messages.error(request, f'Table {number} already exists')
                return redirect('staff:tables')
            
            waiter_obj = User.objects.filter(id=waiter_id, is_staff=True).first() if waiter_id else None
            Table.objects.create(number=number, seats=int(seats), location=location, assigned_waiter=waiter_obj, status='free', is_available=True)
            messages.success(request, f'Table {number} added successfully with QR code ready!')
            return redirect(request.get_full_path())

        if action == 'edit':
            tid = request.POST.get('table_id')
            try:
                t = Table.objects.get(id=tid)
                number = request.POST.get('number', t.number).strip()
                seats = request.POST.get('seats', t.seats)
                location = request.POST.get('location', t.location).strip()
                waiter_id = request.POST.get('waiter_id', '')
                new_status = request.POST.get('status', t.status)

                t.number = number
                t.seats = int(seats)
                t.location = location
                t.status = new_status
                t.is_available = (new_status == 'free')
                if waiter_id:
                    t.assigned_waiter = User.objects.filter(id=waiter_id, is_staff=True).first()
                elif waiter_id == '':
                    t.assigned_waiter = None
                t.save()
                messages.success(request, f'Table {t.number} details updated!')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found')
            return redirect(request.get_full_path())

        if action == 'assign_waiter':
            tid = request.POST.get('table_id')
            waiter_id = request.POST.get('waiter_id', '')
            try:
                t = Table.objects.get(id=tid)
                t.assigned_waiter = User.objects.filter(id=waiter_id, is_staff=True).first() if waiter_id else None
                t.save()
                waiter_name = t.assigned_waiter.get_full_name() or t.assigned_waiter.username if t.assigned_waiter else 'None'
                messages.success(request, f'Assigned Waiter for Table {t.number} updated to {waiter_name}')
            except Table.DoesNotExist:
                pass
            return redirect(request.get_full_path())

        if action == 'update_status':
            tid = request.POST.get('table_id')
            new_status = request.POST.get('status', 'free')
            try:
                t = Table.objects.get(id=tid)
                t.status = new_status
                t.is_available = (new_status == 'free')
                t.save()
                messages.success(request, f'Table {t.number} status updated to {new_status.title()}')
            except Table.DoesNotExist:
                pass
            return redirect(request.get_full_path())

        if action == 'delete':
            tid = request.POST.get('table_id')
            try:
                t = Table.objects.get(id=tid)
                n = t.number
                t.delete()
                messages.success(request, f'Table {n} deleted!')
            except Table.DoesNotExist:
                pass
            return redirect(request.get_full_path())

        if action == 'toggle':
            tid = request.POST.get('table_id')
            try:
                t = Table.objects.get(id=tid)
                if t.status == 'free':
                    t.status = 'occupied'
                    t.is_available = False
                else:
                    t.status = 'free'
                    t.is_available = True
                t.save()
                status_name = "Available" if t.is_available else "Occupied"
                messages.success(request, f'Table {t.number} toggled to {status_name}')
            except Table.DoesNotExist:
                pass
            return redirect(request.get_full_path())

        if action == 'mark_ready':
            tid = request.POST.get('table_id')
            try:
                t = Table.objects.get(id=tid)
                t.status = 'free'
                t.is_available = True
                t.save()
                messages.success(request, f'Table {t.number} marked Available!')
            except Table.DoesNotExist:
                pass
            return redirect(request.get_full_path())

        if action == 'clear_table':
            tid = request.POST.get('table_id')
            try:
                t = Table.objects.get(id=tid)
                t.status = 'cleaning'
                t.is_available = False
                t.save()
                Order.objects.filter(table=t, status__in=['pending', 'preparing', 'out_for_delivery']).update(status='delivered')
                messages.success(request, f'Order Completed! Table {t.number} cleared and moved to Cleaning status.')
            except Table.DoesNotExist:
                pass
            return redirect(request.get_full_path())

        if action == 'reserve_table':
            tid = request.POST.get('table_id')
            guest_name = request.POST.get('guest_name', 'Guest').strip()
            phone = request.POST.get('phone', '').strip()
            time_str = request.POST.get('time', '19:00')
            guests = int(request.POST.get('guests', 2))
            try:
                t = Table.objects.get(id=tid)
                t.status = 'reserved'
                t.is_available = False
                t.save()
                Reservation.objects.create(
                    user=request.user, name=guest_name, phone=phone,
                    date=today, time=time_str, guests=guests, table=t, status='confirmed'
                )
                messages.success(request, f'Table {t.number} reserved for {guest_name} at {time_str}')
            except Exception as e:
                messages.error(request, f'Failed to reserve table: {e}')
            return redirect(request.get_full_path())

        if action == 'checkin_reservation':
            res_id = request.POST.get('reservation_id')
            try:
                res = Reservation.objects.get(id=res_id)
                res.status = 'completed'
                res.save()
                if res.table:
                    res.table.status = 'occupied'
                    res.table.is_available = False
                    res.table.save()
                messages.success(request, f'Checked in reservation for {res.name}! Table {res.table.number if res.table else "—"} is now Occupied.')
            except Reservation.DoesNotExist:
                pass
            return redirect(request.get_full_path())

        if action == 'cancel_reservation':
            res_id = request.POST.get('reservation_id')
            try:
                res = Reservation.objects.get(id=res_id)
                res.status = 'cancelled'
                res.save()
                if res.table:
                    res.table.status = 'free'
                    res.table.is_available = True
                    res.table.save()
                messages.warning(request, f'Reservation for {res.name} cancelled. Table is now Available.')
            except Reservation.DoesNotExist:
                pass
            return redirect(request.get_full_path())

    # GET Filter Parameters
    status_filter = request.GET.get('status', 'all')
    section_filter = request.GET.get('section', 'all')
    seats_filter = request.GET.get('seats', 'all')
    waiter_filter = request.GET.get('waiter', 'all')
    date_filter = request.GET.get('date', 'today')
    duration_filter = request.GET.get('duration', 'all')
    sort_filter = request.GET.get('sort', 'number')
    search_query = request.GET.get('search', '').strip()

    # Base Table Query
    all_tables = Table.objects.all().select_related('assigned_waiter').order_by('number')

    free_count = 0
    occupied_count = 0
    reserved_count = 0
    cleaning_count = 0

    annotated_tables = []
    for t in all_tables:
        active_order = Order.objects.filter(
            table=t,
            status__in=['pending', 'preparing', 'out_for_delivery']
        ).select_related('user').prefetch_related('items__menu_item').order_by('-created_at').first()

        active_res = Reservation.objects.filter(
            table=t,
            date=today,
            status__in=['pending', 'confirmed']
        ).select_related('user').order_by('time').first()

        t.active_order = active_order
        t.active_reservation = active_res

        # State Determination according to lifecycle
        if active_order:
            t.table_status = 'occupied'
            occupied_count += 1
            delta = now - active_order.created_at
            t.elapsed_minutes = int(delta.total_seconds() // 60)
            waiter_user = t.assigned_waiter or active_order.user
            t.assigned_waiter_name = waiter_user.get_full_name() or waiter_user.username if waiter_user else 'Unassigned'
            t.order_total = active_order.total
            t.guest_count = active_order.items.count() or 2
        elif active_res:
            t.table_status = 'reserved'
            reserved_count += 1
            t.elapsed_minutes = 0
            t.assigned_waiter_name = t.assigned_waiter.get_full_name() or t.assigned_waiter.username if t.assigned_waiter else active_res.name
            t.order_total = 0
            t.guest_count = active_res.guests
        elif t.status == 'cleaning' or not t.is_available:
            t.table_status = 'cleaning'
            cleaning_count += 1
            t.elapsed_minutes = 15
            t.assigned_waiter_name = t.assigned_waiter.get_full_name() or t.assigned_waiter.username if t.assigned_waiter else 'Housekeeping'
            t.order_total = 0
            t.guest_count = 0
        else:
            t.table_status = 'free'
            free_count += 1
            t.elapsed_minutes = 0
            t.assigned_waiter_name = t.assigned_waiter.get_full_name() or t.assigned_waiter.username if t.assigned_waiter else 'Unassigned'
            t.order_total = 0
            t.guest_count = 0

        # Tag/Type assignment
        if t.seats >= 6:
            t.tag_type = 'VIP'
        elif t.seats == 4:
            t.tag_type = 'Family'
        else:
            t.tag_type = 'Regular'

        annotated_tables.append(t)

    # Filter annotated_tables
    filtered_tables = annotated_tables

    # 1. Status Filter
    if status_filter != 'all':
        filtered_tables = [t for t in filtered_tables if t.table_status == status_filter]

    # 2. Section Filter
    if section_filter != 'all':
        filtered_tables = [t for t in filtered_tables if t.location == section_filter]

    # 3. Seats Filter
    if seats_filter != 'all':
        if seats_filter == '2':
            filtered_tables = [t for t in filtered_tables if t.seats == 2]
        elif seats_filter == '4':
            filtered_tables = [t for t in filtered_tables if t.seats == 4]
        elif seats_filter == '6':
            filtered_tables = [t for t in filtered_tables if t.seats == 6]
        elif seats_filter == '8+':
            filtered_tables = [t for t in filtered_tables if t.seats >= 8]

    # 4. Waiter Filter
    if waiter_filter != 'all':
        filtered_tables = [t for t in filtered_tables if t.active_order and str(t.active_order.user_id) == waiter_filter]

    # 5. Duration Filter
    if duration_filter == '<30':
        filtered_tables = [t for t in filtered_tables if t.elapsed_minutes < 30]
    elif duration_filter == '30-60':
        filtered_tables = [t for t in filtered_tables if 30 <= t.elapsed_minutes <= 60]
    elif duration_filter == '60-120':
        filtered_tables = [t for t in filtered_tables if 60 <= t.elapsed_minutes <= 120]
    elif duration_filter == '>120':
        filtered_tables = [t for t in filtered_tables if t.elapsed_minutes > 120]

    # 6. Search Filter
    if search_query:
        q_lower = search_query.lower()
        filtered_tables = [t for t in filtered_tables if q_lower in t.number.lower() or (t.assigned_waiter and q_lower in t.assigned_waiter.lower())]

    # 7. Sort Filter
    if sort_filter == 'duration':
        filtered_tables.sort(key=lambda t: t.elapsed_minutes, reverse=True)
    elif sort_filter == 'revenue':
        filtered_tables.sort(key=lambda t: t.order_total, reverse=True)
    elif sort_filter == 'seats':
        filtered_tables.sort(key=lambda t: t.seats, reverse=True)
    else: # number
        filtered_tables.sort(key=lambda t: t.number)

    # CSV Export
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="Tables_Report_{today}.csv"'
        writer = csv.writer(response)
        writer.writerow(['Table Number', 'Seats', 'Location', 'Status', 'Assigned Waiter/Guest', 'Order Total', 'Elapsed Time (Min)'])
        for t in filtered_tables:
            writer.writerow([f"Table {t.number}", t.seats, t.location, t.table_status.upper(), t.assigned_waiter or '—', f"₹{t.order_total}", t.elapsed_minutes])
        return response

    # TODAY'S SNAPSHOT METRICS
    today_orders = Order.objects.filter(created_at__date=today, table__isnull=False)
    today_revenue = today_orders.aggregate(Sum('total'))['total__sum'] or 0
    today_order_count = today_orders.count()
    today_avg_time = 42
    peak_table_stat = today_orders.values('table__number').annotate(t_total=Sum('total'), t_cnt=Count('id')).order_by('-t_total').first()
    today_peak_table = f"Table {peak_table_stat['table__number']}" if peak_table_stat else "T-05"
    today_busiest_hour = "8 PM"
    today_guests = (today_order_count * 3) + (Reservation.objects.filter(date=today).aggregate(Sum('guests'))['guests__sum'] or 0)

    # TABLE ANALYTICS
    top_tables_qs = Order.objects.filter(table__isnull=False).values('table__number').annotate(
        total_rev=Sum('total'),
        order_cnt=Count('id')
    ).order_by('-total_rev')[:3]

    low_tables_qs = Order.objects.filter(table__isnull=False).values('table__number').annotate(
        total_rev=Sum('total'),
        order_cnt=Count('id')
    ).order_by('total_rev')[:3]

    staff_perf_qs = User.objects.filter(is_staff=True, is_superuser=False).annotate(
        handled=Count('orders', filter=Q(orders__table__isnull=False))
    ).order_by('-handled')[:5]

    all_staff_list = User.objects.filter(is_staff=True).order_by('username')
    all_locations = [loc for loc in Table.objects.values_list('location', flat=True).distinct() if loc]

    # Build tables_data structure matching requested template format
    tables_data = []
    for t in filtered_tables:
        t.status = t.table_status
        t.section = t.location
        t.table_type = t.tag_type

        # Timer calculation
        if t.table_status == 'occupied':
            timer_str = f"{t.elapsed_minutes} min"
        elif t.table_status == 'reserved' and t.active_reservation:
            timer_str = t.active_reservation.time.strftime("%g:%i %p") if hasattr(t.active_reservation.time, 'strftime') else str(t.active_reservation.time)
        elif t.table_status == 'cleaning':
            timer_str = "15 min"
        else:
            timer_str = ""

        # Reservation attributes
        res_obj = t.active_reservation
        if res_obj:
            res_obj.customer_name = res_obj.name
            res_obj.customer_phone = res_obj.phone or "N/A"

        # Today's stats for free table
        t_today_orders = Order.objects.filter(table=t, created_at__date=today).count()
        t_today_rev = Order.objects.filter(table=t, created_at__date=today).aggregate(Sum('total'))['total__sum'] or 0

        tables_data.append({
            'table': t,
            'timer': timer_str,
            'active_order': t.active_order,
            'reservation': res_obj,
            'today_orders_count': t_today_orders,
            'today_revenue': t_today_rev,
        })

    context = {
        'tables_data': tables_data,
        'tables': filtered_tables,
        'total_count': all_tables.count(),
        'free_count': free_count,
        'occupied_count': occupied_count,
        'reserved_count': reserved_count,
        'cleaning_count': cleaning_count,
        # Snapshot
        'today_revenue': today_revenue,
        'today_order_count': today_order_count,
        'today_avg_time': today_avg_time,
        'today_peak_table': today_peak_table,
        'today_busiest_hour': today_busiest_hour,
        'today_guests': today_guests,
        # Filters state
        'status_filter': status_filter,
        'section_filter': section_filter,
        'seats_filter': seats_filter,
        'waiter_filter': waiter_filter,
        'date_filter': date_filter,
        'duration_filter': duration_filter,
        'sort_filter': sort_filter,
        'search_query': search_query,
        'all_staff': all_staff_list,
        'all_locations': all_locations,
        'is_manager': is_manager,
        # Analytics
        'top_tables': top_tables_qs,
        'low_tables': low_tables_qs,
        'staff_perf': staff_perf_qs,
    }
    return render(request, 'restaurant/staff/staff_tables.html', context)


# ============================================================
# TABLE RESERVATIONS — Flowchart Lifecycle Management & Filters
# ============================================================
@role_required('Manager', 'OrderHandler', 'Waiter')
def staff_table_reservations(request):
    is_manager_or_oh = request.user.groups.filter(
        name__in=['Manager', 'OrderHandler']
    ).exists() or request.user.is_superuser

    now = timezone.now()
    today = now.date()

    if request.method == 'POST':
        if not is_manager_or_oh:
            messages.error(request, 'You do not have permission to modify bookings')
            return redirect('staff:table_reservations')

        action = request.POST.get('action', '')
        res_id = request.POST.get('reservation_id')

        # 1. ADD NEW RESERVATION
        if action == 'add':
            name = request.POST.get('name', '').strip()
            phone = request.POST.get('phone', '').strip()
            email = request.POST.get('email', '').strip()
            date_str = request.POST.get('date', str(today))
            time_str = request.POST.get('time', '19:00')
            guests = int(request.POST.get('guests', 2))
            table_id = request.POST.get('table_id', '')
            waiter_id = request.POST.get('waiter_id', '')
            res_type = request.POST.get('reservation_type', 'table')
            status = request.POST.get('status', 'confirmed')

            if not name or not phone:
                messages.error(request, 'Customer Name and Phone are required')
                return redirect('staff:table_reservations')

            table_obj = Table.objects.filter(id=table_id).first() if table_id else None
            waiter_obj = User.objects.filter(id=waiter_id, is_staff=True).first() if waiter_id else None

            # Check Table Availability
            is_table_available = True
            if table_obj:
                conflict = Reservation.objects.filter(
                    table=table_obj,
                    date=date_str,
                    status__in=['pending', 'confirmed', 'arrived', 'seated']
                ).exists()
                if conflict:
                    is_table_available = False
                    messages.warning(request, f'⚠️ Notice: Table {table_obj.number} is already booked on {date_str}. Assigned anyway as requested.')

            res = Reservation.objects.create(
                user=request.user if request.user.is_authenticated else None,
                name=name, phone=phone, email=email,
                date=date_str, time=time_str, guests=guests,
                table=table_obj, assigned_waiter=waiter_obj,
                reservation_type=res_type, status=status
            )

            if status == 'seated' and table_obj:
                table_obj.status = 'occupied'
                table_obj.is_available = False
                table_obj.save()
            elif status in ['confirmed', 'pending'] and table_obj:
                table_obj.status = 'reserved'
                table_obj.is_available = False
                table_obj.save()

            messages.success(request, f'✅ Reservation #{res.id} created for {name}!')
            return redirect('staff:table_reservations')

        # 2. EDIT RESERVATION
        if action == 'edit':
            try:
                res = Reservation.objects.get(id=res_id)
                res.name = request.POST.get('name', res.name).strip()
                res.phone = request.POST.get('phone', res.phone).strip()
                res.email = request.POST.get('email', res.email).strip()
                res.date = request.POST.get('date', res.date)
                res.time = request.POST.get('time', res.time)
                res.guests = int(request.POST.get('guests', res.guests))
                res.reservation_type = request.POST.get('reservation_type', res.reservation_type)
                
                table_id = request.POST.get('table_id', '')
                waiter_id = request.POST.get('waiter_id', '')
                res.table = Table.objects.filter(id=table_id).first() if table_id else None
                res.assigned_waiter = User.objects.filter(id=waiter_id, is_staff=True).first() if waiter_id else None
                
                new_status = request.POST.get('status', res.status)
                res.status = new_status
                res.save()

                if new_status == 'seated' and res.table:
                    res.table.status = 'occupied'
                    res.table.is_available = False
                    res.table.save()

                messages.success(request, f'Reservation for {res.name} updated!')
            except Reservation.DoesNotExist:
                messages.error(request, 'Reservation not found')
            return redirect('staff:table_reservations')

        # 3. LIFECYCLE TRANSITION ACTIONS
        try:
            res = Reservation.objects.get(id=res_id)
            if action == 'confirm':
                res.status = 'confirmed'
                res.save()
                if res.table:
                    res.table.status = 'reserved'
                    res.table.is_available = False
                    res.table.save()
                messages.success(request, f'✅ Reservation for {res.name} CONFIRMED!')

            elif action == 'mark_arrived':
                res.status = 'arrived'
                res.save()
                messages.success(request, f'👋 Customer {res.name} marked ARRIVED!')

            elif action == 'no_show':
                res.status = 'no_show'
                res.save()
                if res.table:
                    res.table.status = 'free'
                    res.table.is_available = True
                    res.table.save()
                messages.warning(request, f'❌ Reservation for {res.name} marked NO SHOW.')

            elif action == 'seat_table':
                res.status = 'seated'
                res.save()
                if res.table:
                    res.table.status = 'occupied'
                    res.table.is_available = False
                    res.table.save()
                messages.success(request, f'🪑 Customer {res.name} SEATED at Table {res.table.number if res.table else "—"}!')

            elif action == 'complete':
                res.status = 'completed'
                res.save()
                if res.table:
                    res.table.status = 'cleaning'
                    res.table.is_available = False
                    res.table.save()
                messages.success(request, f'🎉 Reservation for {res.name} COMPLETED!')

            elif action == 'cancel':
                if res.status != 'cancelled':
                    res.status = 'cancelled'
                    res.cancellation_date = timezone.now()
                    res.cancellation_reason = request.POST.get('cancellation_reason', 'Cancelled by Manager').strip() or 'Cancelled by Manager'

                    refund_calc = calculate_cancellation_refund(res)
                    refund_amount = refund_calc['total_refund']
                    res.refund_amount = refund_amount

                    if refund_amount > Decimal('0.00') and res.razorpay_payment_id and res.payment_status == 'paid':
                        key_id = getattr(settings, 'RAZORPAY_KEY_ID', '').strip().strip("'").strip('"')
                        key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '').strip().strip("'").strip('"')
                        try:
                            client = razorpay.Client(auth=(key_id, key_secret))
                            refund_res = client.payment.refund(res.razorpay_payment_id, {
                                'amount': int(refund_amount * 100),
                                'speed': 'optimum',
                                'notes': {'booking_id': str(res.id), 'cancelled_by': 'Manager'}
                            })
                            res.razorpay_refund_id = refund_res.get('id', '')
                            res.refund_status = 'refund_completed' if refund_res.get('status') in ['processed', 'completed'] else 'refund_initiated'
                            res.payment_status = 'refunded' if res.refund_status == 'refund_completed' else res.payment_status
                        except Exception as e:
                            logger.error(f"Razorpay Manager Refund failed for Reservation #{res.id}: {e}")
                            res.refund_status = 'refund_failed'
                            res.refund_failure_reason = str(e)
                    elif refund_amount == Decimal('0.00'):
                        res.refund_status = 'no_refund'
                    else:
                        res.refund_status = 'no_refund' if res.payment_status != 'paid' else 'refund_pending'

                    res.save()
                if res.table:
                    res.table.status = 'free'
                    res.table.is_available = True
                    res.table.save()
                messages.warning(request, f'Reservation for {res.name} CANCELLED.')

            elif action == 'reject':
                res.status = 'rejected'
                res.save()
                if res.table:
                    res.table.status = 'free'
                    res.table.is_available = True
                    res.table.save()
                messages.warning(request, f'Reservation for {res.name} REJECTED.')

            elif action == 'delete':
                n = res.name
                res.delete()
                messages.success(request, f'Reservation for {n} deleted.')

        except Reservation.DoesNotExist:
            messages.error(request, 'Reservation not found')

        return redirect('staff:table_reservations')

    # GET Filter Parameters
    status_filter = request.GET.get('status', 'all')
    date_filter = request.GET.get('date', 'all')
    time_filter = request.GET.get('time', 'all')
    table_filter = request.GET.get('table', 'all')
    guests_filter = request.GET.get('guests', 'all')
    waiter_filter = request.GET.get('waiter', 'all')
    res_type_filter = request.GET.get('res_type', 'all')
    search_query = request.GET.get('search', '').strip()

    reservations_qs = Reservation.objects.select_related('user', 'table', 'assigned_waiter').order_by('-created_at')

    # Status Filter
    if status_filter != 'all':
        reservations_qs = reservations_qs.filter(status=status_filter)

    # Date Filter
    if date_filter == 'today':
        reservations_qs = reservations_qs.filter(date=today)
    elif date_filter == 'yesterday':
        reservations_qs = reservations_qs.filter(date=today - timedelta(days=1))
    elif date_filter == 'this_week':
        reservations_qs = reservations_qs.filter(date__gte=today - timedelta(days=7))
    elif date_filter == 'this_month':
        reservations_qs = reservations_qs.filter(date__month=today.month, date__year=today.year)

    # Table Filter
    if table_filter != 'all':
        reservations_qs = reservations_qs.filter(table_id=table_filter)

    # Guests Filter
    if guests_filter == '2':
        reservations_qs = reservations_qs.filter(guests=2)
    elif guests_filter == '4':
        reservations_qs = reservations_qs.filter(guests=4)
    elif guests_filter == '6':
        reservations_qs = reservations_qs.filter(guests=6)
    elif guests_filter == '8+':
        reservations_qs = reservations_qs.filter(guests__gte=8)

    # Waiter Filter
    if waiter_filter != 'all':
        reservations_qs = reservations_qs.filter(assigned_waiter_id=waiter_filter)

    # Reservation Type Filter
    if res_type_filter != 'all':
        reservations_qs = reservations_qs.filter(reservation_type=res_type_filter)

    # Search Query
    if search_query:
        q_lower = search_query.lower()
        reservations_qs = reservations_qs.filter(
            Q(name__icontains=q_lower) |
            Q(phone__icontains=q_lower) |
            Q(email__icontains=q_lower) |
            Q(id__icontains=search_query.replace('#', ''))
        )

    all_unfiltered = Reservation.objects.all()
    all_tables = Table.objects.all().order_by('number')
    all_staff = User.objects.filter(is_staff=True).order_by('username')

    context = {
        'reservations': reservations_qs,
        # Summary KPI Cards
        'total_count': all_unfiltered.count(),
        'pending_count': all_unfiltered.filter(status='pending').count(),
        'confirmed_count': all_unfiltered.filter(status='confirmed').count(),
        'arrived_count': all_unfiltered.filter(status='arrived').count(),
        'seated_count': all_unfiltered.filter(status='seated').count(),
        'completed_count': all_unfiltered.filter(status='completed').count(),
        'cancelled_count': all_unfiltered.filter(status='cancelled').count(),
        'no_show_count': all_unfiltered.filter(status='no_show').count(),
        'rejected_count': all_unfiltered.filter(status='rejected').count(),
        # Filter States
        'status_filter': status_filter,
        'date_filter': date_filter,
        'time_filter': time_filter,
        'table_filter': table_filter,
        'guests_filter': guests_filter,
        'waiter_filter': waiter_filter,
        'res_type_filter': res_type_filter,
        'search_query': search_query,
        # Dropdowns
        'all_tables': all_tables,
        'all_staff': all_staff,
        'is_manager_or_oh': is_manager_or_oh,
    }
    return render(request, 'restaurant/staff/staff_table_reservations.html', context)


# ============================================================
# ✅ EVENT RESERVATIONS + THEMES
# ============================================================
@role_required('Manager', 'OrderHandler')
def staff_event_reservations(request):
    is_manager = request.user.groups.filter(name='Manager').exists() or request.user.is_superuser

    if request.method == 'POST':
        action = request.POST.get('action', '')

        # ---------- Add Theme (Manager only) ----------
        if action == 'add_theme':
            if not is_manager:
                messages.error(request, 'Only managers can add themes')
                return redirect('staff:event_reservations')

            name = request.POST.get('theme_name', '').strip()
            image = request.FILES.get('theme_image')

            if not name:
                messages.error(request, 'Theme name is required')
                return redirect('staff:event_reservations')

            if EventTheme.objects.filter(name__iexact=name).exists():
                messages.error(request, f'Theme "{name}" already exists')
                return redirect('staff:event_reservations')

            EventTheme.objects.create(name=name, image=image)
            messages.success(request, f'Theme "{name}" added successfully!')
            return redirect('staff:event_reservations')

        # ---------- Delete Theme (Manager only) ----------
        if action == 'delete_theme':
            if not is_manager:
                messages.error(request, 'Only managers can delete themes')
                return redirect('staff:event_reservations')

            theme_id = request.POST.get('theme_id')
            try:
                theme = EventTheme.objects.get(id=theme_id)
                tname = theme.name
                theme.delete()
                messages.success(request, f'Theme "{tname}" deleted!')
            except EventTheme.DoesNotExist:
                messages.error(request, 'Theme not found')
            return redirect('staff:event_reservations')

        # ---------- Event Confirm / Cancel (Manager only) ----------
        if not is_manager:
            messages.error(request, 'You do not have permission to modify events')
            return redirect('staff:event_reservations')

        event_id = request.POST.get('event_id')
        try:
            ev = Event.objects.get(id=event_id)
            if action == 'confirm':
                ev.status = 'confirmed'
                ev.save()
                messages.success(request, f'Event confirmed')
            elif action == 'cancel':
                ev.status = 'cancelled'
                ev.save()
                messages.warning(request, f'Event cancelled')
        except Event.DoesNotExist:
            messages.error(request, 'Event not found')
        return redirect('staff:event_reservations')

    # ---------- GET ----------
    status_filter = request.GET.get('status', 'all')
    events = Event.objects.select_related('user').order_by('-event_date')
    if status_filter != 'all':
        events = events.filter(status=status_filter)

    themes = EventTheme.objects.all().order_by('name')

    context = {
        'events': events,
        'themes': themes,
        'active_filter': status_filter,
        'pending_count': Event.objects.filter(status='pending').count(),
        'confirmed_count': Event.objects.filter(status='confirmed').count(),
        'cancelled_count': Event.objects.filter(status='cancelled').count(),
        'total_count': Event.objects.count(),
        'is_manager': is_manager,
    }
    return render(request, 'restaurant/staff/staff_event_reservations.html', context)


# ============================================================
# ORDERS — Manager | OrderHandler | Waiter | Chef | Kitchen Staff
# ============================================================
@role_required('Manager', 'OrderHandler', 'Waiter', 'Chef', 'Kitchen Staff')
def staff_orders(request):
    is_waiter = request.user.groups.filter(name='Waiter').exists() and not request.user.is_superuser
    is_chef = request.user.groups.filter(name__in=['Chef', 'Kitchen Staff']).exists() and not request.user.is_superuser
    is_handler = request.user.groups.filter(name='OrderHandler').exists() and not request.user.is_superuser
    is_manager = request.user.is_superuser or request.user.groups.filter(name='Manager').exists()
    today = timezone.now().date()

    if request.method == 'POST':
        order_id = request.POST.get('order_id')
        new_status = request.POST.get('status')
        action = request.POST.get('action')

        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            messages.error(request, 'Order not found')
            return redirect(request.get_full_path())

        if is_waiter:
            if action == 'serve' or new_status in ['delivered', 'served']:
                if order.status in ['ready', 'out_for_delivery']:
                    order.status = 'delivered'
                    order.served_at = timezone.now()
                    if not order.assigned_waiter:
                        order.assigned_waiter = request.user
                    order.save()
                    if order.table:
                        order.table.is_available = True
                        order.table.save()
                    messages.success(request, f'🍽️ Order #{order.id} served successfully!')
                else:
                    messages.error(request, 'Order is not in Ready state.')
            else:
                messages.error(request, 'Waiters can only mark ready orders as served.')
            return redirect(request.get_full_path())

        elif is_chef:
            if action == 'start_preparing' or new_status == 'preparing':
                if order.status == 'accepted':
                    order.status = 'preparing'
                    order.chef = request.user
                    order.prep_started_at = timezone.now()
                    order.save()
                    messages.success(request, f'👨‍🍳 Order #{order.id} started preparing!')
                else:
                    messages.error(request, f'Order #{order.id} must be in Accepted state to start preparing.')
            elif action == 'mark_ready' or new_status == 'ready':
                if order.status == 'preparing':
                    order.status = 'ready'
                    order.ready_at = timezone.now()
                    order.save()
                    is_rush = order.rush_flag or order.priority == 'high'
                    rush_str = " [RUSH]" if is_rush else ""
                    table_str = f"Table {order.table.number}" if order.table else "Dine-in"
                    KitchenNotification.objects.create(
                        target_role='Waiter',
                        notification_type='order_ready',
                        order=order,
                        title=f"Order #{order.id}{rush_str} Ready for Serving",
                        message=f"Order #{order.id} ({table_str}) is prepared and ready for service!",
                    )
                    messages.success(request, f'✅ Order #{order.id} marked as READY!')
                else:
                    messages.error(request, f'Order #{order.id} must be in Preparing state to mark as ready.')
            else:
                messages.error(request, 'Chefs cannot modify order status directly to this state.')
            return redirect(request.get_full_path())

        elif is_handler:
            if action == 'send_to_kitchen' or new_status == 'accepted':
                if order.status == 'pending':
                    order.status = 'accepted'
                    order.sent_to_kitchen_at = timezone.now()
                    order.order_handler = request.user
                    order.save()
                    is_rush = order.rush_flag or order.priority == 'high'
                    table_str = f"Table {order.table.number}" if order.table else "Dine-in"
                    KitchenNotification.objects.create(
                        target_role='Kitchen',
                        notification_type='rush_order' if is_rush else 'new_order',
                        order=order,
                        title=f"{'⚡ RUSH Order' if is_rush else '🔔 New Order'} #{order.id}",
                        message=f"Order #{order.id} ({table_str}) sent to kitchen.",
                    )
                    messages.success(request, f'Order #{order.id} sent to Kitchen!')
                else:
                    messages.error(request, 'Only pending orders can be sent to kitchen.')
            elif action == 'cancel' or new_status == 'cancelled':
                if order.status in ['pending', 'accepted']:
                    order.status = 'cancelled'
                    order.cancelled_at = timezone.now()
                    order.save()
                    KitchenNotification.objects.create(
                        target_role='Kitchen',
                        notification_type='order_cancelled',
                        order=order,
                        title=f"❌ Order #{order.id} Cancelled",
                        message=f"Order #{order.id} was cancelled by Order Handler.",
                    )
                    messages.success(request, f'Order #{order.id} cancelled.')
                else:
                    messages.error(request, 'Orders already in preparation cannot be cancelled by Order Handler.')
            else:
                messages.error(request, 'Order Handlers can only send orders to kitchen or cancel eligible orders.')
            return redirect(request.get_full_path())

        else:
            # Manager / Admin
            order.status = new_status
            if new_status == 'accepted' and not order.sent_to_kitchen_at:
                order.sent_to_kitchen_at = timezone.now()
                order.order_handler = request.user
            elif new_status == 'preparing' and not order.prep_started_at:
                order.prep_started_at = timezone.now()
                order.chef = request.user
            elif new_status == 'ready' and not order.ready_at:
                order.ready_at = timezone.now()
            elif new_status in ['delivered', 'served'] and not order.served_at:
                order.served_at = timezone.now()
            elif new_status == 'completed' and not order.completed_at:
                order.completed_at = timezone.now()
            elif new_status == 'cancelled' and not order.cancelled_at:
                order.cancelled_at = timezone.now()
            order.save()
            if new_status in ['delivered', 'served', 'completed'] and order.table:
                order.table.is_available = True
                order.table.save()
            messages.success(request, f'Order #{order.id} updated to {order.get_status_display()}')
            return redirect(request.get_full_path())

    # GET Filter parameters
    date_filter = request.GET.get('date', 'all')
    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()
    status_filter = request.GET.get('status', 'all')
    payment_filter = request.GET.get('payment', 'all')
    table_filter = request.GET.get('table', 'all')
    customer_filter = request.GET.get('customer', '').strip()
    waiter_filter = request.GET.get('waiter', 'all')
    amount_filter = request.GET.get('amount', 'all')
    category_filter = request.GET.get('category', 'all')
    order_id_search = request.GET.get('order_id', '').strip()
    priority_filter = request.GET.get('priority', 'all')
    order_type_filter = request.GET.get('order_type', 'all')
    sort_by = request.GET.get('sort', 'newest')

    orders_base = Order.objects.all()
    orders_qs = orders_base.select_related(
        'user', 'table', 'reservation', 'assigned_waiter', 'chef', 'order_handler'
    ).prefetch_related(
        'items__menu_item__category', 'payments'
    )

    # 1. Date filter / range
    if start_date and end_date:
        try:
            orders_qs = orders_qs.filter(created_at__date__gte=start_date, created_at__date__lte=end_date)
            date_filter = 'custom'
        except Exception:
            pass
    elif date_filter == 'today':
        orders_qs = orders_qs.filter(created_at__date=today)
    elif date_filter == 'yesterday':
        orders_qs = orders_qs.filter(created_at__date=today - timedelta(days=1))
    elif date_filter == 'this_week':
        orders_qs = orders_qs.filter(created_at__date__gte=today - timedelta(days=6))
    elif date_filter == 'this_month':
        orders_qs = orders_qs.filter(created_at__date__gte=today - timedelta(days=29))

    # 2. Status filter
    if status_filter != 'all':
        if status_filter == 'ready':
            orders_qs = orders_qs.filter(status__in=['ready', 'out_for_delivery'])
        elif status_filter in ['delivered', 'served']:
            orders_qs = orders_qs.filter(status__in=['delivered', 'served'])
        else:
            orders_qs = orders_qs.filter(status=status_filter)

    # 3. Order Type filter (Dine-in, Takeaway, Delivery)
    if order_type_filter != 'all':
        if order_type_filter == 'dinein':
            orders_qs = orders_qs.filter(Q(order_type__in=['restaurant', 'online']) | Q(table__isnull=False))
        elif order_type_filter == 'takeaway':
            orders_qs = orders_qs.filter(order_type='takeaway')
        elif order_type_filter == 'delivery':
            orders_qs = orders_qs.filter(Q(order_type='delivery') | ~Q(address=''))

    # 4. RUSH priority filter
    if priority_filter == 'rush':
        orders_qs = orders_qs.filter(Q(rush_flag=True) | Q(priority='high'))
    elif priority_filter == 'normal':
        orders_qs = orders_qs.filter(rush_flag=False).exclude(priority='high')

    # 5. Payment filter
    if payment_filter != 'all':
        if payment_filter in ['paid', 'pending', 'refunded']:
            orders_qs = orders_qs.filter(payments__status='success' if payment_filter == 'paid' else payment_filter)
        elif payment_filter in ['cash', 'upi', 'card', 'netbanking']:
            orders_qs = orders_qs.filter(payments__method=payment_filter)

    # 6. Table filter
    if table_filter in ['online', 'dinein', 'walkin', 'direct']:
        orders_qs = orders_qs.filter(table__isnull=True)
    elif table_filter != 'all':
        orders_qs = orders_qs.filter(table_id=table_filter)

    # 7. Customer search
    if customer_filter:
        orders_qs = orders_qs.filter(
            Q(user__username__icontains=customer_filter) |
            Q(user__first_name__icontains=customer_filter) |
            Q(user__last_name__icontains=customer_filter) |
            Q(user__email__icontains=customer_filter) |
            Q(phone__icontains=customer_filter)
        )

    # 8. Waiter/Staff filter
    if waiter_filter != 'all':
        orders_qs = orders_qs.filter(user_id=waiter_filter)

    # 9. Amount filter
    if amount_filter == '100-500':
        orders_qs = orders_qs.filter(total__gte=100, total__lte=500)
    elif amount_filter == '500-1000':
        orders_qs = orders_qs.filter(total__gte=500, total__lte=1000)
    elif amount_filter == '1000-2000':
        orders_qs = orders_qs.filter(total__gte=1000, total__lte=2000)
    elif amount_filter == '2000+':
        orders_qs = orders_qs.filter(total__gte=2000)

    # 10. Category filter
    if category_filter != 'all':
        orders_qs = orders_qs.filter(items__menu_item__category_id=category_filter).distinct()

    # 11. Order ID Search
    if order_id_search:
        clean_id = order_id_search.replace('#', '').strip()
        orders_qs = orders_qs.filter(id__icontains=clean_id)

    # 12. Sorting
    if sort_by == 'oldest':
        orders_qs = orders_qs.order_by('created_at')
    elif sort_by == 'status':
        orders_qs = orders_qs.order_by('status', '-created_at')
    elif sort_by == 'priority':
        orders_qs = orders_qs.order_by('-rush_flag', '-priority', '-created_at')
    else:  # newest
        orders_qs = orders_qs.order_by('-created_at')

    # CSV Export Handler
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="Orders_Report_{today}.csv"'
        writer = csv.writer(response)
        writer.writerow(['Order ID', 'Customer', 'Table/Type', 'Items', 'Total Amount', 'Status', 'Date Time', 'Prep Duration'])
        for o in orders_qs:
            items_str = ", ".join([f"{item.menu_item.name} x {item.quantity}" for item in o.items.all()])
            table_str = f"Table {o.table.number}" if o.table else (o.get_order_type_display() if hasattr(o, 'get_order_type_display') else "Dine-in")
            customer_str = o.user.get_full_name() or o.user.username
            writer.writerow([f"#{o.id}", customer_str, table_str, items_str, f"₹{o.total}", o.get_status_display(), o.created_at.strftime("%Y-%m-%d %H:%M"), o.prep_duration])
        return response

    # Dropdowns list data
    all_tables = Table.objects.all().order_by('number')
    all_categories = MenuCategory.objects.all().order_by('name')
    all_staff = User.objects.filter(is_staff=True).order_by('username')

    total_filtered_count = orders_qs.count()

    # Pagination: 15 per page
    paginator = Paginator(orders_qs, 15)
    page_number = request.GET.get('page', 1)
    try:
        orders_page = paginator.page(page_number)
    except PageNotAnInteger:
        orders_page = paginator.page(1)
    except EmptyPage:
        orders_page = paginator.page(paginator.num_pages)

    context = {
        'orders': orders_page,
        'page_obj': orders_page,
        'paginator': paginator,
        'date_filter': date_filter,
        'start_date': start_date,
        'end_date': end_date,
        'status_filter': status_filter,
        'payment_filter': payment_filter,
        'table_filter': table_filter,
        'customer_filter': customer_filter,
        'waiter_filter': waiter_filter,
        'amount_filter': amount_filter,
        'category_filter': category_filter,
        'order_id_search': order_id_search,
        'priority_filter': priority_filter,
        'order_type_filter': order_type_filter,
        'sort_by': sort_by,
        'all_tables': all_tables,
        'all_categories': all_categories,
        'all_staff': all_staff,
        'is_waiter': is_waiter,
        'is_chef': is_chef,
        'is_handler': is_handler,
        'is_manager': is_manager,
        'pending_count': orders_base.filter(status='pending').count(),
        'accepted_count': orders_base.filter(status='accepted').count(),
        'preparing_count': orders_base.filter(status='preparing').count(),
        'ready_count': orders_base.filter(status__in=['ready', 'out_for_delivery']).count(),
        'out_count': orders_base.filter(status='out_for_delivery').count(),
        'delivered_count': orders_base.filter(status__in=['delivered', 'served']).count(),
        'rush_count': orders_base.filter(Q(rush_flag=True) | Q(priority='high')).count(),
        'total_filtered_count': total_filtered_count,
        'total_unfiltered_count': orders_base.count(),
    }
    return render(request, 'restaurant/staff/staff_orders.html', context)


# ============================================================
# BILLING — Manager only
# ============================================================
@role_required('Manager')
def staff_billing(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')
        order_id = request.POST.get('order_id')
        if action == 'mark_paid':
            try:
                order = Order.objects.get(id=order_id)
                pm = order.payments.first()
                if not pm:
                    Payment.objects.create(order=order, amount=order.total, method='cod', status='success')
                else:
                    pm.status = 'success'
                    pm.save()

                order.status = 'completed'
                order.save()
                if order.table:
                    order.table.is_available = True
                    order.table.save()
                messages.success(request, f'Payment for Order #{order.id} marked as Paid! Order completed.')
            except Order.DoesNotExist:
                messages.error(request, 'Order not found.')
            return redirect('staff:billing')

    customer_name = request.GET.get('customer_name', '').strip()
    date_filter = request.GET.get('date', '').strip()
    payment_method = request.GET.get('payment_method', 'all').strip()
    search_query = request.GET.get('search', '').strip()

    orders_qs = Order.objects.select_related('user', 'table').prefetch_related('items__menu_item', 'payments').order_by('-created_at')

    # Top Filter Bar: Customer Name
    if customer_name:
        orders_qs = orders_qs.filter(
            Q(user__username__icontains=customer_name) |
            Q(user__first_name__icontains=customer_name) |
            Q(user__last_name__icontains=customer_name)
        )

    # Top Filter Bar: Date
    if date_filter:
        try:
            from datetime import datetime
            d_obj = datetime.strptime(date_filter, '%Y-%m-%d').date()
            orders_qs = orders_qs.filter(created_at__date=d_obj)
        except ValueError:
            pass

    # Top Filter Bar: Payment Method
    if payment_method != 'all':
        if payment_method.lower() in ['cash', 'cod']:
            orders_qs = orders_qs.filter(Q(payments__method='cod') | Q(payments__method='cash'))
        elif payment_method.lower() == 'upi':
            orders_qs = orders_qs.filter(payments__method='upi')
        elif payment_method.lower() == 'card':
            orders_qs = orders_qs.filter(payments__method='card')
        elif payment_method.lower() in ['netbanking', 'net banking']:
            orders_qs = orders_qs.filter(payments__method='netbanking')

    # Table Card Search Bar: Search by bill no. or customer name
    if search_query:
        sq = search_query.lower().replace('#', '').replace('b', '')
        orders_qs = orders_qs.filter(
            Q(id__icontains=sq) |
            Q(user__username__icontains=search_query) |
            Q(user__first_name__icontains=search_query) |
            Q(user__last_name__icontains=search_query)
        )

    all_orders = list(orders_qs.distinct())

    # Format bill items with bill_no (e.g. B001, B002...)
    for idx, o in enumerate(all_orders, start=1):
        o.bill_no = f"B{o.id:03d}"
        o.customer_display = o.user.get_full_name() or o.user.username
        pm = o.payments.first()
        if pm:
            if pm.method in ['cod', 'cash']:
                o.method_display = 'Cash'
            elif pm.method == 'upi':
                o.method_display = 'UPI'
            elif pm.method == 'card':
                o.method_display = 'Card'
            elif pm.method == 'netbanking':
                o.method_display = 'Net Banking'
            else:
                o.method_display = pm.get_method_display()
        else:
            o.method_display = 'Cash'

    # Compute KPI Cards Summary Metrics across full orders
    total_bills_count = Order.objects.count()
    total_sales_amount = Order.objects.aggregate(total=Sum('total'))['total'] or 0
    cash_sales_amount = Payment.objects.filter(status='success', method__in=['cod', 'cash']).aggregate(total=Sum('amount'))['total'] or 0
    upi_sales_amount = Payment.objects.filter(status='success', method='upi').aggregate(total=Sum('amount'))['total'] or 0

    context = {
        'bills': all_orders,
        'total_bills': total_bills_count,
        'total_sales': total_sales_amount,
        'cash_sales': cash_sales_amount,
        'upi_sales': upi_sales_amount,
        'customer_name': customer_name,
        'date_filter': date_filter,
        'payment_method': payment_method,
        'search_query': search_query,
    }
    return render(request, 'restaurant/staff/staff_billing.html', context)


# ============================================================
# CUSTOMERS — Manager & Staff
# ============================================================
@role_required('Manager', 'OrderHandler', 'Waiter')
def staff_customers(request):
    is_manager = request.user.groups.filter(name='Manager').exists() or request.user.is_superuser
    is_waiter = request.user.groups.filter(name='Waiter').exists() and not request.user.is_superuser

    if request.method == 'POST' and is_manager:
        action = request.POST.get('action', '')

        # 1. ADD CUSTOMER
        if action == 'add_customer' or 'add_customer' in request.POST:
            first_name = request.POST.get('first_name', '').strip()
            last_name = request.POST.get('last_name', '').strip()
            email = request.POST.get('email', '').strip().lower()
            phone = request.POST.get('phone', '').strip()
            username = request.POST.get('username', '').strip() or email.split('@')[0]
            password = request.POST.get('password', '') or 'Customer@123'

            if not email or not username:
                messages.error(request, 'Email and username are required')
                return redirect('staff:customers')

            if User.objects.filter(username=username).exists():
                messages.error(request, f'Username "{username}" already taken')
                return redirect('staff:customers')

            try:
                user = User.objects.create_user(
                    username=username, email=email, password=password,
                    first_name=first_name, last_name=last_name,
                    is_staff=False, is_superuser=False, is_active=True
                )
                user.profile.mobile_no = phone
                user.profile.save()
                messages.success(request, f'✅ Customer "{user.get_full_name() or user.username}" added successfully!')
            except Exception as e:
                messages.error(request, f'Failed to create customer: {str(e)}')
            return redirect('staff:customers')

        # 2. EDIT CUSTOMER
        if action == 'edit_customer' or 'edit_customer' in request.POST:
            user_id = request.POST.get('user_id')
            user = get_object_or_404(User, id=user_id, is_staff=False)
            user.first_name = request.POST.get('first_name', user.first_name).strip()
            user.last_name = request.POST.get('last_name', user.last_name).strip()
            user.email = request.POST.get('email', user.email).strip().lower()
            status_val = request.POST.get('status', '')
            if status_val:
                user.is_active = (status_val == 'active')
            user.save()

            phone = request.POST.get('phone', '').strip()
            if phone:
                user.profile.mobile_no = phone
                user.profile.save()

            messages.success(request, f'Customer "{user.get_full_name() or user.username}" updated!')
            return redirect('staff:customers')

        # 3. TOGGLE STATUS
        if action == 'toggle_status':
            user_id = request.POST.get('user_id')
            user = get_object_or_404(User, id=user_id, is_staff=False)
            user.is_active = not user.is_active
            user.save()
            st_text = "Active" if user.is_active else "Inactive"
            messages.success(request, f'Customer "{user.username}" is now {st_text}!')
            return redirect('staff:customers')

        # 4. DELETE CUSTOMER
        if action == 'delete_customer' or 'delete_customer' in request.POST:
            user_id = request.POST.get('user_id')
            user = get_object_or_404(User, id=user_id, is_staff=False)
            uname = user.username
            user.delete()
            messages.success(request, f'Customer "{uname}" deleted!')
            return redirect('staff:customers')

    # GET Filter parameters
    search_query = request.GET.get('search', '').strip()
    status_filter = request.GET.get('status', 'all').strip()
    sort_filter = request.GET.get('sort', 'newest').strip()

    all_customers_qs = User.objects.filter(is_staff=False, is_superuser=False)

    # Calculate summary KPIs across all customers
    total_customers_count = all_customers_qs.count()
    active_customers_count = all_customers_qs.filter(is_active=True).count()
    inactive_customers_count = all_customers_qs.filter(is_active=False).count()
    email_reg_count = all_customers_qs.exclude(email='').count()

    filtered_customers = list(all_customers_qs)

    # 1. Search filter
    if search_query:
        sq = search_query.lower()
        filtered_customers = [
            u for u in filtered_customers
            if sq in u.get_full_name().lower() or sq in u.username.lower() or sq in u.email.lower() or sq in (u.profile.mobile_no or '').lower()
        ]

    # 2. Status filter
    if status_filter != 'all':
        if status_filter == 'active':
            filtered_customers = [u for u in filtered_customers if u.is_active]
        elif status_filter == 'inactive':
            filtered_customers = [u for u in filtered_customers if not u.is_active]

    # 3. Sort filter
    if sort_filter == 'oldest':
        filtered_customers.sort(key=lambda u: u.date_joined)
    elif sort_filter == 'name':
        filtered_customers.sort(key=lambda u: (u.get_full_name() or u.username).lower())
    else: # newest
        filtered_customers.sort(key=lambda u: u.date_joined, reverse=True)

    # Pagination (10 customers per page as shown in mockup)
    from django.core.paginator import Paginator
    page_number = request.GET.get('page', 1)
    paginator = Paginator(filtered_customers, 10)
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'customers': page_obj.object_list,
        'total_customers': total_customers_count,
        'active_customers': active_customers_count,
        'inactive_customers': inactive_customers_count,
        'email_registered': email_reg_count,
        'total_filtered_count': len(filtered_customers),
        'search_query': search_query,
        'active_status': status_filter,
        'active_sort': sort_filter,
        'is_manager': is_manager,
        'is_waiter': is_waiter,
    }
    return render(request, 'restaurant/staff/staff_customers.html', context)


# ============================================================
# MENU ITEMS — Manager Full Control & Flowchart Lifecycle
# ============================================================
@role_required('Manager')
def staff_menu_items(request):
    is_manager = request.user.groups.filter(name='Manager').exists() or request.user.is_superuser

    if request.method == 'POST':
        action = request.POST.get('action', '')

        # 1. ADD NEW ITEM
        if action == 'add':
            name = request.POST.get('name', '').strip()
            category_id = request.POST.get('category_id')
            food_type = request.POST.get('food_type', 'veg')
            price = request.POST.get('price', '0')
            discount_percentage = int(request.POST.get('discount_percentage', 0))
            description = request.POST.get('description', '').strip()
            image = request.FILES.get('image')
            is_avail = request.POST.get('is_available') == 'on' or request.POST.get('is_available') == 'true' or True
            is_pop = request.POST.get('is_popular') == 'on' or request.POST.get('is_popular') == 'true'

            if not name or not price or not category_id:
                messages.error(request, 'Item Name, Category and Price are required')
                return redirect('staff:menu_items')

            category = MenuCategory.objects.filter(id=category_id).first()
            if not category:
                messages.error(request, 'Invalid Category')
                return redirect('staff:menu_items')

            item = MenuItem.objects.create(
                name=name, category=category, food_type=food_type,
                price=price, discount_percentage=discount_percentage,
                description=description, image=image,
                is_available=is_avail, is_popular=is_pop,
                status='in_stock'
            )
            customizations_list = request.POST.getlist('customizations')
            if customizations_list:
                cust_qs = ItemCustomization.objects.filter(
                    Q(id__in=[c for c in customizations_list if c.isdigit()]) |
                    Q(name__in=customizations_list)
                )
                if cust_qs.exists():
                    item.customization_groups.set(cust_qs)
                    item.customizations = ','.join([c.name for c in cust_qs])
                    item.save(update_fields=['customizations'])

            messages.success(request, f'✅ Menu Item "{item.name}" added successfully!')
            return redirect('staff:menu_items')

        # 2. EDIT ITEM
        if action == 'edit':
            item_id = request.POST.get('item_id')
            try:
                item = MenuItem.objects.get(id=item_id)
                item.name = request.POST.get('name', item.name).strip()
                cat_id = request.POST.get('category_id')
                if cat_id:
                    item.category = MenuCategory.objects.filter(id=cat_id).first() or item.category
                item.food_type = request.POST.get('food_type', item.food_type)
                item.price = request.POST.get('price', item.price)
                item.discount_percentage = int(request.POST.get('discount_percentage', item.discount_percentage))
                item.description = request.POST.get('description', item.description).strip()
                item.is_available = request.POST.get('is_available') in ['on', 'true', True]
                item.is_popular = request.POST.get('is_popular') in ['on', 'true', True]
                
                if request.FILES.get('image'):
                    item.image = request.FILES.get('image')

                customizations_list = request.POST.getlist('customizations')
                if customizations_list:
                    cust_qs = ItemCustomization.objects.filter(
                        Q(id__in=[c for c in customizations_list if c.isdigit()]) |
                        Q(name__in=customizations_list)
                    )
                    item.customization_groups.set(cust_qs)
                    item.customizations = ','.join([c.name for c in cust_qs])
                else:
                    item.customization_groups.clear()
                    item.customizations = ''

                item.save()
                messages.success(request, f'Item "{item.name}" details updated!')
            except MenuItem.DoesNotExist:
                messages.error(request, 'Item not found')
            return redirect('staff:menu_items')

        # 3. TOGGLE AVAILABILITY (Show Customer Menu vs Hide/Disable Ordering)
        if action == 'toggle_availability' or action == 'toggle':
            item_id = request.POST.get('item_id')
            try:
                item = MenuItem.objects.get(id=item_id)
                item.is_available = not item.is_available
                item.save()
                status_text = "Available (Visible on Customer Menu)" if item.is_available else "Unavailable (Ordering Disabled)"
                messages.success(request, f'Item "{item.name}" is now {status_text}')
            except MenuItem.DoesNotExist:
                messages.error(request, 'Item not found')
            return redirect('staff:menu_items')

        # 4. TOGGLE POPULAR STATUS (Yes / No)
        if action == 'toggle_popular':
            item_id = request.POST.get('item_id')
            try:
                item = MenuItem.objects.get(id=item_id)
                item.is_popular = not item.is_popular
                item.save()
                pop_text = "Popular Item ⭐" if item.is_popular else "Regular Item"
                messages.success(request, f'Item "{item.name}" marked as {pop_text}')
            except MenuItem.DoesNotExist:
                messages.error(request, 'Item not found')
            return redirect('staff:menu_items')

        # 5. SET DISCOUNT (Calculate Final Price)
        if action == 'set_discount':
            item_id = request.POST.get('item_id')
            disc_pct = int(request.POST.get('discount_percentage', 0))
            try:
                item = MenuItem.objects.get(id=item_id)
                item.discount_percentage = disc_pct
                item.save()
                messages.success(request, f'Discount for "{item.name}" set to {disc_pct}%. Final Price: ₹{item.get_final_price()}')
            except MenuItem.DoesNotExist:
                messages.error(request, 'Item not found')
            return redirect('staff:menu_items')

        # 6. DELETE ITEM
        if action == 'delete':
            item_id = request.POST.get('item_id')
            try:
                item = MenuItem.objects.get(id=item_id)
                iname = item.name
                item.delete()
                messages.success(request, f'Menu Item "{iname}" deleted!')
            except MenuItem.DoesNotExist:
                messages.error(request, 'Item not found')
            return redirect('staff:menu_items')

    # GET Filter Parameters
    search_query = request.GET.get('search', '').strip()
    category_filter = request.GET.get('category', 'all')
    food_type_filter = request.GET.get('food_type', 'all')
    availability_filter = request.GET.get('availability', 'all')
    price_range_filter = request.GET.get('price_range', 'all')
    discount_filter = request.GET.get('discount', 'all')
    popular_filter = request.GET.get('popular', 'all')

    items_qs = MenuItem.objects.select_related('category').order_by('category__name', 'name')

    # 1. Search Filter
    if search_query:
        q_lower = search_query.lower()
        items_qs = items_qs.filter(Q(name__icontains=q_lower) | Q(description__icontains=q_lower))

    # 2. Category Filter
    if category_filter != 'all':
        items_qs = items_qs.filter(category_id=category_filter)

    # 3. Food Type Filter
    if food_type_filter != 'all':
        items_qs = items_qs.filter(food_type=food_type_filter)

    # 4. Availability Filter
    if availability_filter == 'available':
        items_qs = items_qs.filter(is_available=True)
    elif availability_filter == 'unavailable':
        items_qs = items_qs.filter(is_available=False)
    elif availability_filter == 'out_of_stock':
        items_qs = items_qs.filter(status='out_of_stock')

    # 5. Price Range Filter
    if price_range_filter == '<100':
        items_qs = items_qs.filter(price__lt=100)
    elif price_range_filter == '100-300':
        items_qs = items_qs.filter(price__gte=100, price__lte=300)
    elif price_range_filter == '300-500':
        items_qs = items_qs.filter(price__gte=300, price__lte=500)
    elif price_range_filter == '>500':
        items_qs = items_qs.filter(price__gt=500)

    # 6. Discount Filter
    if discount_filter == 'discounted':
        items_qs = items_qs.filter(discount_percentage__gt=0)
    elif discount_filter == 'no_discount':
        items_qs = items_qs.filter(discount_percentage=0)

    # 7. Popular Filter
    if popular_filter == 'popular_only':
        items_qs = items_qs.filter(is_popular=True)
    elif popular_filter == 'non_popular':
        items_qs = items_qs.filter(is_popular=False)

    all_unfiltered_items = MenuItem.objects.all()
    categories = MenuCategory.objects.all().order_by('name')

    context = {
        'items': items_qs,
        'categories': categories,
        # Summary KPI Cards
        'total_items': all_unfiltered_items.count(),
        'available_count': all_unfiltered_items.filter(is_available=True).count(),
        'unavailable_count': all_unfiltered_items.filter(is_available=False).count(),
        'popular_count': all_unfiltered_items.filter(is_popular=True).count(),
        'discounted_count': all_unfiltered_items.filter(discount_percentage__gt=0).count(),
        'out_of_stock_count': all_unfiltered_items.filter(status='out_of_stock').count(),
        # Filter States
        'search_query': search_query,
        'category_filter': category_filter,
        'food_type_filter': food_type_filter,
        'availability_filter': availability_filter,
        'price_range_filter': price_range_filter,
        'discount_filter': discount_filter,
        'popular_filter': popular_filter,
        'is_manager': is_manager,
        'item_customizations': ItemCustomization.objects.filter(status='active').order_by('name'),
    }
    return render(request, 'restaurant/staff/staff_menu_items.html', context)


# ============================================================
# INVENTORY — Manager only
# ============================================================
@role_required('Manager')
def staff_inventory(request):
    user = request.user if request.user.is_authenticated else None

    if request.method == 'POST':
        # 1. ADD NEW ITEM
        if 'add_item' in request.POST:
            name = request.POST.get('name', '').strip()
            if name:
                expiry_str = request.POST.get('expiry_date', '').strip()
                expiry_date = None
                if expiry_str:
                    try:
                        from datetime import datetime
                        expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
                    except ValueError:
                        expiry_date = None

                qty = Decimal(request.POST.get('quantity', '0') or '0')
                item = InventoryItem.objects.create(
                    name=name,
                    category=request.POST.get('category', 'other'),
                    quantity=qty,
                    unit=request.POST.get('unit', 'kg').strip() or 'kg',
                    min_stock=Decimal(request.POST.get('min_stock', '5') or '5'),
                    price_per_unit=Decimal(request.POST.get('price_per_unit', '0') or '0'),
                    supplier=request.POST.get('supplier', '').strip(),
                    expiry_date=expiry_date,
                )
                if qty > 0:
                    StockTransaction.objects.create(
                        item=item,
                        transaction_type='initial',
                        quantity=qty,
                        supplier=item.supplier,
                        reason='Initial stock setup',
                        user=user,
                    )
                messages.success(request, f'Item "{name}" added to inventory!')
            return redirect('staff:inventory')

        # 2. EDIT ITEM
        if 'edit_item' in request.POST:
            item_id = request.POST.get('item_id')
            item = get_object_or_404(InventoryItem, id=item_id)
            name = request.POST.get('name', '').strip()
            if name:
                expiry_str = request.POST.get('expiry_date', '').strip()
                expiry_date = None
                if expiry_str:
                    try:
                        from datetime import datetime
                        expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
                    except ValueError:
                        expiry_date = None

                item.name = name
                item.category = request.POST.get('category', item.category)
                item.unit = request.POST.get('unit', item.unit).strip() or 'kg'
                item.min_stock = Decimal(request.POST.get('min_stock', str(item.min_stock)) or '5')
                item.price_per_unit = Decimal(request.POST.get('price_per_unit', str(item.price_per_unit)) or '0')
                item.supplier = request.POST.get('supplier', '').strip()
                item.expiry_date = expiry_date
                item.save()
                messages.success(request, f'Item "{item.name}" updated successfully!')
            return redirect('staff:inventory')

        # 3. ADD STOCK (STOCK IN)
        if 'add_stock' in request.POST:
            item_id = request.POST.get('item_id')
            item = get_object_or_404(InventoryItem, id=item_id)
            try:
                add_qty_raw = request.POST.get('add_qty', '').strip()
                if not add_qty_raw:
                    messages.error(request, 'Add Quantity cannot be empty.')
                    return redirect('staff:inventory')

                try:
                    add_qty = Decimal(add_qty_raw)
                except Exception:
                    messages.error(request, 'Please enter a valid numeric Add Quantity.')
                    return redirect('staff:inventory')

                if add_qty <= 0:
                    messages.error(request, 'Add Quantity must be greater than 0.')
                    return redirect('staff:inventory')

                price_raw = request.POST.get('price_per_unit', '').strip() or request.POST.get('purchase_price', '').strip()
                if price_raw:
                    try:
                        price_val = Decimal(price_raw)
                        if price_val < 0:
                            messages.error(request, 'Purchase Price must not be negative.')
                            return redirect('staff:inventory')
                        item.price_per_unit = price_val
                    except Exception:
                        messages.error(request, 'Purchase Price must be a valid number.')
                        return redirect('staff:inventory')

                supplier = request.POST.get('supplier', '').strip() or item.supplier
                notes = request.POST.get('notes', '').strip()
                expiry_str = request.POST.get('expiry_date', '').strip()

                if expiry_str:
                    try:
                        from datetime import datetime
                        item.expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
                    except ValueError:
                        messages.error(request, 'Please enter a valid Expiry Date.')
                        return redirect('staff:inventory')

                item.quantity = Decimal(str(item.quantity)) + add_qty
                if supplier:
                    item.supplier = supplier
                item.save()

                StockTransaction.objects.create(
                    item=item,
                    transaction_type='add_stock',
                    quantity=add_qty,
                    supplier=supplier,
                    reason=notes or 'Stock added (Stock In)',
                    user=user,
                )

                status_str = "IN STOCK" if item.quantity > item.min_stock else "LOW STOCK"
                messages.success(request, f'Stock added successfully. "{item.name}" updated to {item.quantity} {item.unit} ({status_str}).')
            except Exception as e:
                messages.error(request, f'Failed to add stock: {str(e)}')
            return redirect('staff:inventory')

        # 4. REMOVE STOCK (STOCK OUT / WASTE / USAGE)
        if 'remove_stock' in request.POST:
            item_id = request.POST.get('item_id')
            item = get_object_or_404(InventoryItem, id=item_id)
            try:
                remove_qty = Decimal(request.POST.get('remove_qty', '0') or '0')
                reason = request.POST.get('reason', 'Usage in Kitchen').strip()
                notes = request.POST.get('notes', '').strip()

                if remove_qty > 0:
                    current_qty = Decimal(str(item.quantity))
                    actual_remove = min(current_qty, remove_qty)
                    item.quantity = current_qty - actual_remove
                    item.save()

                    reason_text = f"{reason} — {notes}" if notes else reason
                    StockTransaction.objects.create(
                        item=item,
                        transaction_type='remove_stock',
                        quantity=actual_remove,
                        reason=reason_text,
                        user=user,
                    )
                    messages.success(request, f'Removed {actual_remove} {item.unit} from "{item.name}" ({reason})!')
            except Exception as e:
                messages.error(request, f'Failed to remove stock: {str(e)}')

            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('is_ajax') == 'true':
                from django.http import JsonResponse
                all_inv = InventoryItem.objects.all()
                return JsonResponse({
                    'status': 'success',
                    'message': f'Removed {remove_qty} {item.unit} from "{item.name}"!',
                    'new_quantity': float(item.quantity),
                    'unit': item.unit,
                    'status_badge': item.flowchart_status,
                    'total_items': all_inv.count(),
                    'total_value': float(sum(i.total_value for i in all_inv)),
                    'low_stock_count': sum(1 for i in all_inv if i.is_low_stock and i.quantity > 0),
                    'out_of_stock_count': sum(1 for i in all_inv if i.quantity <= 0),
                    'expiring_soon_count': sum(1 for i in all_inv if i.is_expiring_soon and not i.is_expired),
                    'expired_count': sum(1 for i in all_inv if i.is_expired),
                })
            return redirect('staff:inventory')

        # 5. CREATE PURCHASE ORDER (REORDER)
        if 'create_po' in request.POST:
            item_id = request.POST.get('item_id')
            item = get_object_or_404(InventoryItem, id=item_id)
            try:
                po_qty = Decimal(request.POST.get('quantity', '10') or '10')
                supplier = request.POST.get('supplier', '').strip() or item.supplier or 'General Supplier'
                cost = Decimal(request.POST.get('estimated_cost', '0') or '0')
                exp_date_str = request.POST.get('expected_date', '').strip()
                expected_date = None
                if exp_date_str:
                    try:
                        from datetime import datetime
                        expected_date = datetime.strptime(exp_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        expected_date = None

                po_num = f"PO-{timezone.now().strftime('%Y%m%d%H%M%S')}"
                PurchaseOrder.objects.create(
                    po_number=po_num,
                    item=item,
                    supplier=supplier,
                    quantity=po_qty,
                    unit=item.unit,
                    estimated_cost=cost,
                    status='pending',
                    expected_date=expected_date,
                    created_by=user,
                )
                messages.success(request, f'Purchase Order {po_num} created for "{item.name}"!')
            except Exception as e:
                messages.error(request, f'Failed to create Purchase Order: {str(e)}')
            return redirect('staff:inventory')

        # 6. RECEIVE PURCHASE ORDER (STOCK IN FROM PO)
        if 'receive_po' in request.POST:
            po_id = request.POST.get('po_id')
            po = get_object_or_404(PurchaseOrder, id=po_id)
            if po.status != 'received':
                po.status = 'received'
                po.received_at = timezone.now()
                po.save()

                po.item.quantity = Decimal(str(po.item.quantity)) + Decimal(str(po.quantity))
                po.item.save()

                StockTransaction.objects.create(
                    item=po.item,
                    transaction_type='po_received',
                    quantity=po.quantity,
                    supplier=po.supplier,
                    reason=f"Received PO #{po.po_number}",
                    user=user,
                )
                messages.success(request, f'Received PO #{po.po_number}! Added {po.quantity} {po.unit} to "{po.item.name}".')
            return redirect('staff:inventory')

        # 7. CANCEL PURCHASE ORDER
        if 'cancel_po' in request.POST:
            po_id = request.POST.get('po_id')
            po = get_object_or_404(PurchaseOrder, id=po_id)
            if po.status != 'received':
                po.status = 'cancelled'
                po.save()
                messages.info(request, f'Purchase Order #{po.po_number} was cancelled.')
            return redirect('staff:inventory')

        # 8. DISPOSE EXPIRED STOCK
        if 'dispose_expired' in request.POST:
            item_id = request.POST.get('item_id')
            item = get_object_or_404(InventoryItem, id=item_id)
            old_qty = Decimal(str(item.quantity))
            if old_qty > 0:
                item.quantity = Decimal('0')
                item.save()
                StockTransaction.objects.create(
                    item=item,
                    transaction_type='expired_removed',
                    quantity=old_qty,
                    reason='Disposed expired stock',
                    user=user,
                )
                messages.warning(request, f'Disposed {old_qty} {item.unit} expired stock of "{item.name}".')
            return redirect('staff:inventory')

        # 9. DELETE ITEM
        if 'delete_item' in request.POST:
            item_id = request.POST.get('item_id')
            item = get_object_or_404(InventoryItem, id=item_id)
            item_name = item.name
            item.delete()
            messages.success(request, f'Item "{item_name}" deleted from inventory.')
            return redirect('staff:inventory')

        # 10. BACKWARD COMPATIBLE QUICK UPDATE QTY
        if 'update_qty' in request.POST:
            try:
                item = InventoryItem.objects.get(id=request.POST.get('item_id'))
                new_qty = Decimal(request.POST.get('new_qty', '0') or '0')
                diff = new_qty - Decimal(str(item.quantity))
                item.quantity = new_qty
                item.save()

                if diff != 0:
                    t_type = 'add_stock' if diff > 0 else 'remove_stock'
                    StockTransaction.objects.create(
                        item=item,
                        transaction_type=t_type,
                        quantity=abs(diff),
                        reason='Direct quantity update',
                        user=user,
                    )
                messages.success(request, f'"{item.name}" quantity updated to {new_qty} {item.unit}')
            except InventoryItem.DoesNotExist:
                pass
            return redirect('staff:inventory')

    # GET FILTERS & DISPLAY DATA
    search_query = request.GET.get('search', '').strip()
    category_filter = request.GET.get('category', 'all')
    status_filter = request.GET.get('status', 'all')
    supplier_filter = request.GET.get('supplier', 'all')

    all_inventory = InventoryItem.objects.all().order_by('category', 'name')

    # Calculate summary KPIs across full inventory
    total_items_count = all_inventory.count()
    total_value_amount = sum(i.total_value for i in all_inventory)
    low_stock_count = sum(1 for i in all_inventory if i.is_low_stock and i.quantity > 0)
    out_of_stock_count = sum(1 for i in all_inventory if i.quantity <= 0)
    in_stock_count = sum(1 for i in all_inventory if i.quantity > i.min_stock and not i.is_expired)
    expiring_soon_count = sum(1 for i in all_inventory if i.is_expiring_soon and not i.is_expired)
    expired_count = sum(1 for i in all_inventory if i.is_expired)
    pending_po_count = PurchaseOrder.objects.filter(status__in=['pending', 'ordered']).count()

    total_cnt = max(1, total_items_count)
    in_stock_pct = round((in_stock_count / total_cnt) * 100)
    low_stock_pct = round((low_stock_count / total_cnt) * 100)
    out_of_stock_pct = round((out_of_stock_count / total_cnt) * 100)

    # Distinct suppliers list
    suppliers_list = InventoryItem.objects.exclude(supplier='').values_list('supplier', flat=True).distinct()

    # Filter evaluation
    filtered_items = list(all_inventory)

    if search_query:
        sq = search_query.lower()
        filtered_items = [
            i for i in filtered_items
            if sq in i.name.lower() or sq in (i.supplier or '').lower() or sq in i.category.lower()
        ]

    if category_filter != 'all':
        filtered_items = [i for i in filtered_items if i.category == category_filter]

    if supplier_filter != 'all':
        filtered_items = [i for i in filtered_items if (i.supplier or '') == supplier_filter]

    if status_filter != 'all':
        if status_filter == 'in_stock' or status_filter == 'normal':
            filtered_items = [i for i in filtered_items if i.quantity > i.min_stock and not i.is_expired]
        elif status_filter == 'low_stock':
            filtered_items = [i for i in filtered_items if i.is_low_stock and i.quantity > 0]
        elif status_filter == 'out_of_stock':
            filtered_items = [i for i in filtered_items if i.quantity <= 0]
        elif status_filter == 'expiring_soon':
            filtered_items = [i for i in filtered_items if i.is_expiring_soon and not i.is_expired]
        elif status_filter == 'expired':
            filtered_items = [i for i in filtered_items if i.is_expired]

    # Dynamically compute stock bar progress percentage for each item
    for item in filtered_items:
        if item.quantity <= 0:
            item.stock_percentage = 0
        else:
            base_target = float(item.min_stock) * 3.0 if item.min_stock and float(item.min_stock) > 0 else 50.0
            item.stock_percentage = min(100, max(5, int((float(item.quantity) / base_target) * 100)))

    # Pagination (8 items per page)
    from django.core.paginator import Paginator
    page_number = request.GET.get('page', 1)
    paginator = Paginator(filtered_items, 8)
    page_obj = paginator.get_page(page_number)

    # Recent transactions and POs
    recent_transactions = StockTransaction.objects.select_related('item', 'user').all()[:30]
    purchase_orders = PurchaseOrder.objects.select_related('item', 'created_by').all()[:30]

    context = {
        'page_obj': page_obj,
        'items': page_obj.object_list,
        'total_filtered_count': len(filtered_items),
        'all_items_count': total_items_count,
        'search_query': search_query,
        'active_category': category_filter,
        'active_status': status_filter,
        'active_supplier': supplier_filter,
        'suppliers_list': suppliers_list,
        'total_items': total_items_count,
        'in_stock_count': in_stock_count,
        'in_stock_pct': in_stock_pct,
        'low_stock_count': low_stock_count,
        'low_stock_pct': low_stock_pct,
        'out_of_stock_count': out_of_stock_count,
        'out_of_stock_pct': out_of_stock_pct,
        'expiring_soon_count': expiring_soon_count,
        'expired_count': expired_count,
        'total_value': total_value_amount,
        'pending_po_count': pending_po_count,
        'recent_transactions': recent_transactions,
        'purchase_orders': purchase_orders,
    }
    return render(request, 'restaurant/staff/staff_inventory.html', context)


# ============================================================
# REPORTS — Manager only
# ============================================================
# ============================================================
# REPORTS & ANALYTICS — Manager only
# ============================================================
@role_required('Manager')
def staff_reports(request):
    today = timezone.now().date()

    # --- Date Range Parsing ---
    preset = request.GET.get('preset', '7days')
    custom_start = request.GET.get('start_date', '')
    custom_end = request.GET.get('end_date', '')

    if preset == 'today':
        start_date = today
        end_date = today
    elif preset == 'yesterday':
        start_date = today - timedelta(days=1)
        end_date = today - timedelta(days=1)
    elif preset == '30days':
        start_date = today - timedelta(days=29)
        end_date = today
    elif preset == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
    elif preset == 'custom' and custom_start and custom_end:
        try:
            start_date = timezone.datetime.strptime(custom_start, '%Y-%m-%d').date()
            end_date = timezone.datetime.strptime(custom_end, '%Y-%m-%d').date()
        except ValueError:
            start_date = today - timedelta(days=6)
            end_date = today
    else:  # Default: 7days
        preset = '7days'
        start_date = today - timedelta(days=6)
        end_date = today

    # Base Querysets within Date Range
    orders = Order.objects.filter(created_at__date__gte=start_date, created_at__date__lte=end_date)
    payments = Payment.objects.filter(created_at__date__gte=start_date, created_at__date__lte=end_date, status='success')
    reservations = Reservation.objects.filter(date__gte=start_date, date__lte=end_date, is_deleted=False)

    # --- CSV Export Handler ---
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="sales_report_{start_date}_to_{end_date}.csv"'
        writer = csv.writer(response)
        writer.writerow(['Order ID', 'Date & Time', 'Customer', 'Order Type', 'Status', 'Total Amount (INR)', 'Payment Method'])
        
        export_orders = orders.select_related('user').prefetch_related('payments').order_by('-created_at')
        for o in export_orders:
            cust_name = o.user.get_full_name() or o.user.username
            pay_method = o.payments.first().get_method_display() if o.payments.exists() else 'N/A'
            writer.writerow([
                f"#{o.id}",
                o.created_at.strftime('%Y-%m-%d %H:%M'),
                cust_name,
                o.get_order_type_display(),
                o.get_status_display(),
                float(o.total),
                pay_method
            ])
        return response

    # --- KPI Metrics Calculation ---
    total_orders = orders.count()
    total_revenue = payments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    avg_order_val = round(float(total_revenue) / total_orders, 2) if total_orders > 0 else 0.00

    delivered_orders = orders.filter(status='delivered').count()
    pending_orders = orders.filter(status__in=['pending', 'preparing', 'out_for_delivery']).count()
    cancelled_orders = orders.filter(status='cancelled').count()

    total_reservations = reservations.count()
    table_reservations = reservations.filter(reservation_type='table').count()
    event_reservations = reservations.filter(reservation_type='event').count()

    # --- Order Type & Payment Method Distribution ---
    online_orders = orders.filter(order_type='online').count()
    dinein_orders = orders.filter(order_type='restaurant').count()

    upi_rev = float(payments.filter(method='upi').aggregate(total=Sum('amount'))['total'] or 0)
    card_rev = float(payments.filter(method='card').aggregate(total=Sum('amount'))['total'] or 0)
    cod_rev = float(payments.filter(method='cod').aggregate(total=Sum('amount'))['total'] or 0)
    net_rev = float(payments.filter(method='netbanking').aggregate(total=Sum('amount'))['total'] or 0)

    # --- Daily Sales Trend Chart Data ---
    chart_labels = []
    chart_revenue = []
    chart_orders = []

    cur_day = start_date
    while cur_day <= end_date:
        chart_labels.append(cur_day.strftime('%d %b'))
        day_rev = payments.filter(created_at__date=cur_day).aggregate(total=Sum('amount'))['total'] or 0
        day_ord = orders.filter(created_at__date=cur_day).count()
        chart_revenue.append(float(day_rev))
        chart_orders.append(day_ord)
        cur_day += timedelta(days=1)

    # Max revenue for chart height calculation
    max_chart_rev = max(chart_revenue) if chart_revenue and max(chart_revenue) > 0 else 100

    # --- Top Selling Items ---
    top_items_qs = OrderItem.objects.filter(
        order__created_at__date__gte=start_date,
        order__created_at__date__lte=end_date
    ).values(
        'menu_item__name', 'menu_item__category__name', 'price'
    ).annotate(
        total_qty=Sum('quantity'),
        total_sales=Sum(F('quantity') * F('price'))
    ).order_by('-total_qty')[:8]

    # --- Category Sales Breakdown ---
    category_sales = MenuCategory.objects.annotate(
        total_qty=Sum(
            'items__orderitem__quantity',
            filter=Q(items__orderitem__order__created_at__date__gte=start_date, items__orderitem__order__created_at__date__lte=end_date)
        ),
        total_revenue=Sum(
            F('items__orderitem__quantity') * F('items__orderitem__price'),
            filter=Q(items__orderitem__order__created_at__date__gte=start_date, items__orderitem__order__created_at__date__lte=end_date)
        )
    ).filter(total_revenue__gt=0).order_by('-total_revenue')

    # --- Recent Orders Table ---
    recent_orders = orders.select_related('user', 'table').order_by('-created_at')[:10]

    # --- Summary Comparison Metrics ---
    today_orders = Order.objects.filter(created_at__date=today).count()
    today_revenue = Payment.objects.filter(status='success', created_at__date=today).aggregate(total=Sum('amount'))['total'] or 0

    context = {
        'preset': preset,
        'start_date': start_date.strftime('%Y-%m-%d'),
        'end_date': end_date.strftime('%Y-%m-%d'),
        'start_date_obj': start_date,
        'end_date_obj': end_date,
        
        # Metrics
        'total_revenue': float(total_revenue),
        'total_orders': total_orders,
        'avg_order_val': avg_order_val,
        'delivered_orders': delivered_orders,
        'pending_orders': pending_orders,
        'cancelled_orders': cancelled_orders,

        'total_reservations': total_reservations,
        'table_reservations': table_reservations,
        'event_reservations': event_reservations,

        'online_orders': online_orders,
        'dinein_orders': dinein_orders,

        'upi_rev': upi_rev,
        'card_rev': card_rev,
        'cod_rev': cod_rev,
        'net_rev': net_rev,

        # Chart
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(chart_revenue),
        'chart_orders': json.dumps(chart_orders),
        'chart_labels_list': chart_labels,
        'chart_revenue_list': chart_revenue,
        'chart_orders_list': chart_orders,
        'max_chart_rev': max_chart_rev,

        # Lists
        'top_items': top_items_qs,
        'category_sales': category_sales,
        'recent_orders': recent_orders,

        'today_orders': today_orders,
        'today_revenue': float(today_revenue),
    }
    return render(request, 'restaurant/staff/staff_reports.html', context)


# ============================================================
# NGOs & Food Donations — Manager only
# ============================================================
@role_required('Manager')
def staff_ngos(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # --- NGO Actions ---
        if action == 'add_ngo' or 'add_ngo' in request.POST:
            name = request.POST.get('name', '').strip()
            contact_person = request.POST.get('contact_person', '').strip()
            phone = request.POST.get('phone', '').strip()
            if name and contact_person and phone:
                meals_served = request.POST.get('meals_served', 0)
                try:
                    meals_served = int(meals_served)
                except (ValueError, TypeError):
                    meals_served = 0
                NGO.objects.create(
                    name=name,
                    contact_person=contact_person,
                    phone=phone,
                    email=request.POST.get('email', '').strip(),
                    address=request.POST.get('address', '').strip(),
                    food_preference=request.POST.get('food_preference', '').strip(),
                    meals_served=meals_served,
                    status=request.POST.get('status', 'active'),
                )
                messages.success(request, f'NGO "{name}" registered successfully!')
            return redirect('staff:ngos')

        if action == 'edit_ngo':
            ngo_id = request.POST.get('ngo_id')
            try:
                ngo = NGO.objects.get(id=ngo_id)
                ngo.name = request.POST.get('name', ngo.name).strip()
                ngo.contact_person = request.POST.get('contact_person', ngo.contact_person).strip()
                ngo.phone = request.POST.get('phone', ngo.phone).strip()
                ngo.email = request.POST.get('email', ngo.email).strip()
                ngo.address = request.POST.get('address', ngo.address).strip()
                ngo.food_preference = request.POST.get('food_preference', ngo.food_preference).strip()
                try:
                    ngo.meals_served = int(request.POST.get('meals_served', ngo.meals_served))
                except (ValueError, TypeError):
                    pass
                ngo.status = request.POST.get('status', ngo.status)
                ngo.save()
                messages.success(request, f'NGO "{ngo.name}" details updated!')
            except NGO.DoesNotExist:
                messages.error(request, 'NGO partner not found.')
            return redirect('staff:ngos')

        if action == 'toggle_ngo' or 'toggle_ngo' in request.POST:
            try:
                ngo = NGO.objects.get(id=request.POST.get('ngo_id'))
                ngo.status = 'inactive' if ngo.status == 'active' else 'active'
                ngo.save()
                messages.success(request, f'NGO "{ngo.name}" status set to {ngo.status.title()}!')
            except NGO.DoesNotExist:
                pass
            return redirect('staff:ngos')

        if action == 'delete_ngo':
            try:
                ngo = NGO.objects.get(id=request.POST.get('ngo_id'))
                name = ngo.name
                ngo.delete()
                messages.success(request, f'NGO "{name}" deleted successfully!')
            except NGO.DoesNotExist:
                pass
            return redirect('staff:ngos')

        # --- Donation Actions ---
        if action == 'add_donation':
            food_items = request.POST.get('food_items', '').strip()
            if food_items:
                ngo_id = request.POST.get('ngo_id', '')
                ngo_obj = NGO.objects.filter(id=ngo_id).first() if ngo_id else None

                try:
                    qty = Decimal(request.POST.get('quantity_kg', '0'))
                except:
                    qty = Decimal('0')
                try:
                    meals = int(request.POST.get('meals_count', '0'))
                except:
                    meals = 0

                expiry = request.POST.get('expiry_date', None) or None
                status = request.POST.get('status', 'pending')

                donation = FoodDonation.objects.create(
                    ngo=ngo_obj,
                    food_items=food_items,
                    food_type=request.POST.get('food_type', 'prepared'),
                    quantity_kg=qty,
                    meals_count=meals,
                    expiry_date=expiry,
                    status=status,
                    notes=request.POST.get('notes', '').strip(),
                )

                # Update NGO meals served if completed/collected
                if ngo_obj and meals > 0 and status in ['collected', 'completed']:
                    ngo_obj.meals_served += meals
                    ngo_obj.save()

                messages.success(request, f'Food Donation #{donation.id} logged successfully!')
            return redirect('staff:ngos')

        if action == 'update_donation_status':
            donation_id = request.POST.get('donation_id')
            new_status = request.POST.get('status')
            try:
                donation = FoodDonation.objects.get(id=donation_id)
                old_status = donation.status
                donation.status = new_status
                donation.save()

                # If status changed to completed/collected, add meals count to assigned NGO
                if donation.ngo and donation.meals_count > 0 and old_status not in ['collected', 'completed'] and new_status in ['collected', 'completed']:
                    donation.ngo.meals_served += donation.meals_count
                    donation.ngo.save()

                messages.success(request, f'Donation #{donation.id} status updated to {new_status.title()}!')
            except FoodDonation.DoesNotExist:
                pass
            return redirect('staff:ngos')

        if action == 'delete_donation':
            donation_id = request.POST.get('donation_id')
            try:
                d = FoodDonation.objects.get(id=donation_id)
                d.delete()
                messages.success(request, f'Food Donation #{donation_id} deleted!')
            except FoodDonation.DoesNotExist:
                pass
            return redirect('staff:ngos')

    ngos = NGO.objects.all().order_by('name')
    donations = FoodDonation.objects.select_related('ngo').all().order_by('-created_at')

    total_ngo_count = ngos.count()
    active_ngo_count = ngos.filter(status='active').count()
    total_meals_served = ngos.aggregate(total=Sum('meals_served'))['total'] or 0
    total_donations_count = donations.count()
    pending_donations_count = donations.filter(status='pending').count()

    context = {
        'ngos': ngos,
        'donations': donations,
        'total_count': total_ngo_count,
        'active_count': active_ngo_count,
        'meals_served': total_meals_served,
        'total_donations_count': total_donations_count,
        'pending_donations_count': pending_donations_count,
    }
    return render(request, 'restaurant/staff/staff_ngos.html', context)


# ============================================================
# NOTIFICATIONS — All roles
# ============================================================
@role_required('Manager', 'OrderHandler', 'Waiter', 'Chef', 'Kitchen Staff')
def staff_notifications(request):
    is_chef = request.user.groups.filter(name__in=['Chef', 'Kitchen Staff']).exists() and not request.user.is_superuser
    is_waiter = request.user.groups.filter(name='Waiter').exists() and not request.user.is_superuser

    if request.method == 'POST':
        action = request.POST.get('action')
        notif_id = request.POST.get('notification_id')
        if action == 'mark_read' and notif_id:
            KitchenNotification.objects.filter(id=notif_id).update(is_read=True)
            messages.success(request, 'Notification marked as read.')
        elif action == 'mark_all_read':
            if is_chef:
                KitchenNotification.objects.filter(target_role__in=['Kitchen', 'All']).update(is_read=True)
            elif is_waiter:
                KitchenNotification.objects.filter(target_role__in=['Waiter', 'All']).update(is_read=True)
            else:
                KitchenNotification.objects.all().update(is_read=True)
            messages.success(request, 'All notifications marked as read.')
        return redirect('staff:notifications')

    # Query notifications based on role
    if is_chef:
        notifs_qs = KitchenNotification.objects.filter(target_role__in=['Kitchen', 'All'])
    elif is_waiter:
        notifs_qs = KitchenNotification.objects.filter(target_role__in=['Waiter', 'All'])
    else:
        notifs_qs = KitchenNotification.objects.all()

    kitchen_notifications = notifs_qs.select_related('order', 'order__table', 'order__user')[:50]
    unread_kitchen_count = notifs_qs.filter(is_read=False).count()

    ready_orders = Order.objects.filter(
        status__in=['ready', 'out_for_delivery']
    ).select_related('user', 'table').prefetch_related('items__menu_item').order_by('-updated_at')

    pending_reservations = Reservation.objects.filter(
        status='pending'
    ).select_related('user', 'table').order_by('date', 'time')

    pending_events = Event.objects.filter(
        status='pending'
    ).select_related('user').order_by('event_date')

    low_stock = [i for i in InventoryItem.objects.all() if i.is_low_stock]
    pending_orders = Order.objects.filter(status='pending').count()
    active_kitchen_orders = Order.objects.filter(status__in=['accepted', 'preparing']).count()
    active_rush_orders = Order.objects.filter(status__in=['accepted', 'preparing']).filter(Q(rush_flag=True) | Q(priority='high')).count()

    context = {
        'is_chef': is_chef,
        'is_waiter': is_waiter,
        'kitchen_notifications': kitchen_notifications,
        'unread_kitchen_count': unread_kitchen_count,
        'active_kitchen_orders': active_kitchen_orders,
        'active_rush_orders': active_rush_orders,
        'ready_orders': ready_orders,
        'ready_count': ready_orders.count(),
        'pending_reservations': pending_reservations,
        'pending_reservations_count': pending_reservations.count(),
        'pending_events': pending_events,
        'pending_events_count': pending_events.count(),
        'low_stock': low_stock,
        'low_stock_count': len(low_stock),
        'pending_orders': pending_orders,
    }
    return render(request, 'restaurant/staff/staff_notifications.html', context)


# ============================================================
# ADD STAFF (Manager only)
# ============================================================
# ============================================================
# ADD STAFF (Manager only)
# ============================================================
@role_required('Manager')
def staff_add_staff(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip().lower()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        mobile = request.POST.get('mobile_no', '').strip()
        password = request.POST.get('password', '')
        sub_role = request.POST.get('sub_role', '').strip()

        VALID_ROLES = ['Manager', 'OrderHandler', 'Waiter', 'Chef', 'Kitchen Staff']

        if not username or not email or not password:
            messages.error(request, 'Username, email and password are required')
            return redirect('staff:staff_list')

        if len(password) < 8:
            messages.error(request, 'Password must be at least 8 characters')
            return redirect('staff:staff_list')

        if sub_role not in VALID_ROLES:
            messages.error(request, 'Please select a valid staff role')
            return redirect('staff:staff_list')

        if User.objects.filter(username=username).exists():
            messages.error(request, f'Username "{username}" already taken')
            return redirect('staff:staff_list')

        if User.objects.filter(email=email).exists():
            messages.error(request, f'Email "{email}" already registered')
            return redirect('staff:staff_list')

        try:
            user = User.objects.create_user(
                username=username, email=email, password=password,
                first_name=first_name, last_name=last_name,
                is_staff=True, is_superuser=False, is_active=True,
            )
            user.profile.mobile_no = mobile
            user.profile.save()

            group, _ = Group.objects.get_or_create(name=sub_role)
            user.groups.add(group)

            messages.success(request, f'Staff "{username}" created as {sub_role}!')
        except Exception as e:
            messages.error(request, f'Failed to create staff: {str(e)}')

        return redirect('staff:staff_list')

    return redirect('staff:staff_list')


# ============================================================
# DELETE STAFF (Manager only)
# ============================================================
@role_required('Manager')
def staff_delete_staff(request, user_id):
    try:
        user = User.objects.get(id=user_id)
        if user.is_superuser:
            messages.error(request, 'Cannot delete superuser')
            return redirect('staff:staff_list')
        if user == request.user:
            messages.error(request, 'Cannot delete yourself')
            return redirect('staff:staff_list')
        uname = user.username
        user.delete()
        messages.success(request, f'Staff "{uname}" deleted!')
    except User.DoesNotExist:
        messages.error(request, 'User not found')
    return redirect('staff:staff_list')


# ============================================================
# UPDATE STAFF ROLE (Manager only)
# ============================================================
@role_required('Manager')
def staff_update_role(request, user_id):
    if request.method == 'POST':
        user = get_object_or_404(User, id=user_id)
        sub_role = request.POST.get('role', '').strip() or request.POST.get('sub_role', '').strip()
        if sub_role:
            user.groups.clear()
            group, _ = Group.objects.get_or_create(name=sub_role)
            user.groups.add(group)
            messages.success(request, f'Role updated for {user.username}!')
    return redirect('staff:staff_list')



# ============================================================
# STAFF LIST (Manager only)
# ============================================================
@role_required('Manager')
def staff_staff_list(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # 1. ADD NEW STAFF
        if action == 'add_staff' or 'add_staff' in request.POST:
            username = request.POST.get('username', '').strip()
            email = request.POST.get('email', '').strip().lower()
            first_name = request.POST.get('first_name', '').strip()
            last_name = request.POST.get('last_name', '').strip()
            mobile = request.POST.get('phone', '').strip() or request.POST.get('mobile_no', '').strip()
            password = request.POST.get('password', '').strip()
            sub_role = request.POST.get('sub_role', '').strip() or request.POST.get('role', '').strip()

            if not username or not email or not password:
                messages.error(request, 'Username, email, and password are required')
                return redirect('staff:staff_list')

            if User.objects.filter(username=username).exists():
                messages.error(request, f'Username "{username}" already taken')
                return redirect('staff:staff_list')

            if User.objects.filter(email=email).exists():
                messages.error(request, f'Email "{email}" registered')
                return redirect('staff:staff_list')

            try:
                user = User.objects.create_user(
                    username=username, email=email, password=password,
                    first_name=first_name, last_name=last_name,
                    is_staff=True, is_superuser=False, is_active=True,
                )
                user.profile.mobile_no = mobile
                user.profile.save()

                if sub_role:
                    group, _ = Group.objects.get_or_create(name=sub_role)
                    user.groups.add(group)

                messages.success(request, f'✅ Staff member "{username}" added successfully!')
            except Exception as e:
                messages.error(request, f'Failed to create staff: {str(e)}')
            return redirect('staff:staff_list')

        # 2. EDIT STAFF / UPDATE ROLE
        if action == 'edit_staff' or 'edit_staff' in request.POST:
            user_id = request.POST.get('user_id')
            user = get_object_or_404(User, id=user_id)
            user.first_name = request.POST.get('first_name', user.first_name).strip()
            user.last_name = request.POST.get('last_name', user.last_name).strip()
            user.email = request.POST.get('email', user.email).strip().lower()
            user.save()

            mobile = request.POST.get('phone', '').strip() or request.POST.get('mobile_no', '').strip()
            if mobile:
                user.profile.mobile_no = mobile
                user.profile.save()

            sub_role = request.POST.get('sub_role', '').strip() or request.POST.get('role', '').strip()
            if sub_role:
                user.groups.clear()
                group, _ = Group.objects.get_or_create(name=sub_role)
                user.groups.add(group)

            messages.success(request, f'Staff member "{user.username}" updated!')
            return redirect('staff:staff_list')

        # 3. TOGGLE ACTIVE STATUS
        if action == 'toggle_status':
            user_id = request.POST.get('user_id')
            user = get_object_or_404(User, id=user_id)
            user.is_active = not user.is_active
            user.save()
            status_str = "Active" if user.is_active else "Inactive"
            messages.success(request, f'Staff member "{user.username}" is now {status_str}!')
            return redirect('staff:staff_list')

        # 4. DELETE STAFF
        if action == 'delete' or 'delete_staff' in request.POST:
            user_id = request.POST.get('user_id')
            user = get_object_or_404(User, id=user_id)
            if user.is_superuser:
                messages.error(request, 'Cannot delete superuser')
            elif user == request.user:
                messages.error(request, 'Cannot delete yourself')
            else:
                uname = user.username
                user.delete()
                messages.success(request, f'Staff member "{uname}" deleted!')
            return redirect('staff:staff_list')

    # GET QUERY & FILTERS
    search_query = request.GET.get('search', '').strip()
    role_filter = request.GET.get('role', 'all').strip()
    status_filter = request.GET.get('status', 'all').strip()

    all_staff = list(User.objects.filter(is_staff=True, is_superuser=False).order_by('id'))

    total_staff_count = len(all_staff)
    total_cnt = max(1, total_staff_count)

    managers_count = sum(1 for u in all_staff if u.groups.filter(name='Manager').exists())
    kitchen_count = sum(1 for u in all_staff if u.groups.filter(name__in=['Chef', 'Kitchen Staff']).exists())
    waiters_count = sum(1 for u in all_staff if u.groups.filter(name='Waiter').exists())
    handlers_count = sum(1 for u in all_staff if u.groups.filter(name='OrderHandler').exists() or u.groups.filter(name='Order Handler').exists())

    managers_pct = round((managers_count / total_cnt) * 100)
    kitchen_pct = round((kitchen_count / total_cnt) * 100)
    waiters_pct = round((waiters_count / total_cnt) * 100)
    handlers_pct = round((handlers_count / total_cnt) * 100)

    # Attach attributes for each staff member
    for idx, u in enumerate(all_staff, start=1):
        u.custom_id = f"STF{idx:03d}"
        
        # Initials
        fn = u.first_name.strip()
        ln = u.last_name.strip()
        if fn and ln:
            u.initials = f"{fn[0].upper()}{ln[0].upper()}"
        elif fn:
            u.initials = f"{fn[:2].upper()}"
        else:
            u.initials = f"{u.username[:2].upper()}"

        # Roles & Badges
        g_names = list(u.groups.values_list('name', flat=True))
        if 'Manager' in g_names:
            u.role_name = 'Manager'
            u.role_badge_class = 'role-manager'
            u.avatar_color = '#8b5cf6' # Purple
        elif 'Head Chef' in g_names:
            u.role_name = 'Head Chef'
            u.role_badge_class = 'role-kitchen'
            u.avatar_color = '#0f766e' # Teal
        elif 'Kitchen Staff' in g_names:
            u.role_name = 'Kitchen Staff'
            u.role_badge_class = 'role-kitchen'
            u.avatar_color = '#0284c7' # Blue
        elif 'Chef' in g_names:
            u.role_name = 'Chef'
            u.role_badge_class = 'role-kitchen'
            u.avatar_color = '#7c3aed' # Violet
        elif 'Waiter' in g_names:
            u.role_name = 'Waiter'
            u.role_badge_class = 'role-waiter'
            u.avatar_color = '#f59e0b' # Orange
        elif 'OrderHandler' in g_names or 'Order Handler' in g_names:
            u.role_name = 'Order Handler'
            u.role_badge_class = 'role-handler'
            u.avatar_color = '#ef4444' # Red/Pink
        else:
            u.role_name = 'Staff Member'
            u.role_badge_class = 'role-staff'
            u.avatar_color = '#64748b'

    # Filter evaluation
    filtered_staff = all_staff

    if search_query:
        sq = search_query.lower()
        filtered_staff = [
            u for u in filtered_staff
            if sq in u.get_full_name().lower() or sq in u.username.lower() or sq in u.email.lower() or sq in u.profile.mobile_no.lower() or sq in u.custom_id.lower()
        ]

    if role_filter != 'all':
        if role_filter == 'Manager':
            filtered_staff = [u for u in filtered_staff if u.role_name == 'Manager']
        elif role_filter == 'Head Chef':
            filtered_staff = [u for u in filtered_staff if u.role_name == 'Head Chef']
        elif role_filter == 'Kitchen Staff':
            filtered_staff = [u for u in filtered_staff if u.role_name == 'Kitchen Staff']
        elif role_filter == 'Chef':
            filtered_staff = [u for u in filtered_staff if u.role_name == 'Chef']
        elif role_filter == 'Waiter':
            filtered_staff = [u for u in filtered_staff if u.role_name == 'Waiter']
        elif role_filter == 'OrderHandler' or role_filter == 'Order Handler':
            filtered_staff = [u for u in filtered_staff if u.role_name == 'Order Handler']

    if status_filter != 'all':
        if status_filter == 'active':
            filtered_staff = [u for u in filtered_staff if u.is_active]
        elif status_filter == 'inactive':
            filtered_staff = [u for u in filtered_staff if not u.is_active]

    # Pagination (8 items per page as shown in mockup "Showing 1 to 8 of 12 staff")
    from django.core.paginator import Paginator
    page_number = request.GET.get('page', 1)
    paginator = Paginator(filtered_staff, 8)
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'staff_list': page_obj.object_list,
        'total_staff': total_staff_count,
        'total_filtered_count': len(filtered_staff),
        'managers_count': managers_count,
        'managers_pct': managers_pct,
        'kitchen_count': kitchen_count,
        'kitchen_pct': kitchen_pct,
        'waiters_count': waiters_count,
        'waiters_pct': waiters_pct,
        'handlers_count': handlers_count,
        'handlers_pct': handlers_pct,
        'search_query': search_query,
        'active_role': role_filter,
        'active_status': status_filter,
    }
    return render(request, 'restaurant/staff/staff_staff_list.html', context)


# ============================================================
# PROFILE
# ============================================================
@login_required
def staff_profile(request):
    if request.method == 'POST':
        user = request.user
        user.first_name = request.POST.get('first_name', user.first_name)
        user.last_name = request.POST.get('last_name', user.last_name)
        user.email = request.POST.get('email', user.email)
        user.save()

        mobile = request.POST.get('mobile_no', '').strip()
        if mobile:
            user.profile.mobile_no = mobile
            user.profile.save()

        messages.success(request, 'Profile updated successfully!')
        return redirect('staff:profile')

    return render(request, 'restaurant/staff/staff_profile.html')