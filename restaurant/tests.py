import datetime
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import MenuCategory, MenuItem, Order, OrderItem, Payment, Reservation, Table, KitchenNotification, CustomerReview


class CustomerPanelAccessTests(TestCase):
    def test_regular_customer_is_redirected_to_home(self):
        user = User.objects.create_user(
            username='customer_dashboard_user',
            email='customer.dashboard@example.com',
            password='Customer@123',
            is_staff=False,
        )

        self.client.force_login(user)
        response = self.client.get('/login/', follow=True)

        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_customer_panel_redirects_to_home(self):
        user = User.objects.create_user(
            username='customer_panel_user',
            email='customer.panel@example.com',
            password='Customer@123',
            is_staff=False,
        )

        self.client.force_login(user)
        response = self.client.get('/customer-panel/')

        self.assertRedirects(response, '/')

    def test_customer_checkout_creates_order_items(self):
        user = User.objects.create_user(
            username='customer_checkout_user',
            email='customer.checkout@example.com',
            password='Customer@123',
        )
        user.profile.address = '12 Main Street'
        user.profile.mobile_no = '9876543210'
        user.profile.save()
        category = MenuCategory.objects.create(name='Customer Test Menu')
        menu_item = MenuItem.objects.create(
            name='Test Paneer Bowl',
            category=category,
            price='120.00',
            is_available=True,
            status='in_stock',
        )

        self.client.force_login(user)
        menu_response = self.client.get('/customer-panel/menu/')
        self.assertEqual(menu_response.status_code, 200)
        self.assertContains(menu_response, 'Test Paneer Bowl')

        response = self.client.post('/customer-panel/menu/', {
            f'item_{menu_item.id}': '2',
            'address': '12 Main Street',
            'phone': '9876543210',
        })

        self.assertRedirects(response, '/customer-panel/orders/')
        order = Order.objects.get(user=user)
        self.assertEqual(order.total, 240)
        self.assertEqual(order.items.get().quantity, 2)

    def test_customer_can_book_and_cancel_own_reservation(self):
        user = User.objects.create_user(
            username='customer_booking_user',
            email='customer.booking@example.com',
            password='Customer@123',
        )
        table = Table.objects.create(number='C-01', seats=4, location='Indoor')
        booking_date = timezone.localdate() + timedelta(days=1)

        self.client.force_login(user)
        reservation = Reservation.objects.create(
            user=user,
            name='Customer Booking',
            phone='9876543210',
            date=booking_date,
            time='18:30',
            guests=3,
            table=table,
            status='pending',
        )

class StaffRolePermissionAndWorkflowTests(TestCase):
    def setUp(self):
        self.manager_group, _ = Group.objects.get_or_create(name='Manager')
        self.handler_group, _ = Group.objects.get_or_create(name='OrderHandler')
        self.waiter_group, _ = Group.objects.get_or_create(name='Waiter')

        self.manager = User.objects.create_user(username='mgr_user', email='mgr@example.com', password='Password@123', is_staff=True)
        self.manager.groups.add(self.manager_group)

        self.handler = User.objects.create_user(username='handler_user', email='handler@example.com', password='Password@123', is_staff=True)
        self.handler.groups.add(self.handler_group)

        self.waiter = User.objects.create_user(username='waiter_user', email='waiter@example.com', password='Password@123', is_staff=True)
        self.waiter.groups.add(self.waiter_group)

        self.customer = User.objects.create_user(username='cust_user', email='cust@example.com', password='Password@123', is_staff=False)

    def test_staff_role_login_redirects(self):
        self.client.force_login(self.manager)
        response = self.client.get('/login/')
        self.assertRedirects(response, '/staff/manager/')

        self.client.force_login(self.handler)
        response = self.client.get('/login/')
        self.assertRedirects(response, '/staff/order-handler/')

        self.client.force_login(self.waiter)
        response = self.client.get('/login/')
        self.assertRedirects(response, '/staff/waiter/')

        self.client.force_login(self.customer)
        response = self.client.get('/login/')
        self.assertRedirects(response, '/')

    def test_waiter_cannot_access_manager_or_order_handler_pages(self):
        self.client.force_login(self.waiter)
        response = self.client.get('/staff/manager/', follow=True)
        self.assertContains(response, 'Access denied')

        response = self.client.get('/staff/inventory/', follow=True)
        self.assertContains(response, 'Access denied')

        response = self.client.get('/staff/reports/', follow=True)
        self.assertContains(response, 'Access denied')

    def test_order_handler_cannot_access_manager_only_pages(self):
        self.client.force_login(self.handler)
        response = self.client.get('/staff/manager/', follow=True)
        self.assertContains(response, 'Access denied')

        response = self.client.get('/staff/inventory/', follow=True)
        self.assertContains(response, 'Access denied')

    def test_cross_panel_order_workflow(self):
        table = Table.objects.create(number='T-10', seats=4, location='Indoor')
        category = MenuCategory.objects.create(name='Main Course')
        item = MenuItem.objects.create(name='Veg Biryani', category=category, price=200, is_available=True)

        order = Order.objects.create(user=self.customer, table=table, status='pending', total=200, order_type='restaurant')

        # 1. Order Handler accepts order
        self.client.force_login(self.handler)
        self.client.post('/staff/order-handler/', {'order_id': order.id, 'status': 'accepted'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')

        # 2. Order Handler updates status to preparing and ready
        self.client.post('/staff/order-handler/', {'order_id': order.id, 'status': 'preparing'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')

        self.client.post('/staff/order-handler/', {'order_id': order.id, 'status': 'ready'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

        # 2. Waiter serves order
        self.client.force_login(self.waiter)
        self.client.post('/staff/orders/', {'order_id': order.id, 'action': 'serve'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')


class RazorpayApiAuthenticationTests(TestCase):
    def setUp(self):
        self.category = MenuCategory.objects.create(name='Test Category')
        self.item = MenuItem.objects.create(
            name='Test Pizza',
            category=self.category,
            price='250.00',
            is_available=True
        )

    def test_create_order_without_cart_returns_error(self):
        response = self.client.post('/api/create-razorpay-order/', {
            'phone': '9876543210',
            'address': 'Test Address 123',
            'payment_method': 'upi'
        }, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('empty', data['error'].lower())

    @override_settings(RAZORPAY_KEY_ID='rzp_test_YOUR_ACTUAL_TEST_KEY_HERE', RAZORPAY_KEY_SECRET='YOUR_ACTUAL_TEST_SECRET_HERE')
    def test_create_order_with_cart_validates_credentials(self):
        session = self.client.session
        session['cart'] = {str(self.item.id): 1}
        session.save()

        response = self.client.post('/api/create-razorpay-order/', {
            'name': 'John Customer',
            'phone': '9876543210',
            'address': '123 Main Street',
            'payment_method': 'upi'
        }, content_type='application/json')

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('placeholder text', data['error'].lower())
        # Ensure secret key is NEVER printed in error responses
        self.assertNotIn('YOUR_ACTUAL_TEST_SECRET_HERE', response.content.decode('utf-8'))


class TableBookingPreOrderFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='booking_test_user',
            email='booking.test@example.com',
            password='Password@123'
        )
        self.table = Table.objects.create(number='T-99', seats=4, location='Indoor', is_available=True)
        self.category = MenuCategory.objects.create(name='Test Category')
        self.item = MenuItem.objects.create(
            name='Test Pizza',
            category=self.category,
            price='250.00',
            is_available=True,
            status='in_stock'
        )
        self.tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()

    def test_scenario1_table_booking_no_preorder(self):
        """TEST 1: Customer books table -> Selects No Pre-Order -> Pays only ₹200 booking charge -> Booking confirmed"""
        self.client.force_login(self.user)
        response = self.client.post('/table-booking/', {
            'action': 'confirm_booking',
            'date': self.tomorrow,
            'time': '19:00',
            'guests': '2',
            'table_id': str(self.table.id),
            'name': 'Test Customer',
            'phone': '9876543210',
            'email': 'cust@example.com',
            'pre_order_choice': 'no'
        })
        self.assertRedirects(response, '/table-booking/summary/')

        session = self.client.session
        pending = session.get('pending_booking')
        self.assertIsNotNone(pending)
        self.assertFalse(pending['pre_order_selected'])
        self.assertEqual(pending['booking_charge'], '200.00')

    def test_scenario2_table_booking_yes_preorder(self):
        """TEST 2: Customer books table -> Selects Yes Pre-Order -> Adds food -> Pays ₹200 + Food total -> Booking & Pre-Order confirmed"""
        self.client.force_login(self.user)
        self.client.post('/table-booking/', {
            'action': 'confirm_booking',
            'date': self.tomorrow,
            'time': '19:00',
            'guests': '2',
            'table_id': str(self.table.id),
            'name': 'Test Customer',
            'phone': '9876543210',
            'email': 'cust@example.com',
            'pre_order_choice': 'yes'
        })

        session = self.client.session
        pending = session.get('pending_booking')
        self.assertTrue(pending['pre_order_selected'])

        # Add food item to pre-order cart via AJAX
        cart_res = self.client.post('/api/pre-order-cart/update/', {
            'item_id': str(self.item.id),
            'action': 'add',
            'quantity': 2
        }, content_type='application/json')
        self.assertEqual(cart_res.status_code, 200)
        cart_data = cart_res.json()
        self.assertEqual(cart_data['food_subtotal'], 500.00)
        self.assertEqual(cart_data['total_prepaid'], 700.00)  # ₹200 + ₹500

    def test_scenario3_table_booking_then_later_food_order(self):
        """TEST 3: Customer books table without pre-order -> Booking confirmed -> Later orders food from Menu"""
        reservation = Reservation.objects.create(
            user=self.user,
            name='Test Customer',
            phone='9876543210',
            date=timezone.localdate() + timedelta(days=1),
            time='19:00',
            guests=2,
            table=self.table,
            status='confirmed',
            payment_status='paid',
            booking_charge=200.00,
            pre_order_selected=False,
            total_amount=200.00
        )
        self.assertEqual(reservation.status, 'confirmed')

        # Customer later visits food order page
        self.client.force_login(self.user)
        res = self.client.get('/food-order/')
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Test Pizza')


class TableBookingCancellationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='cancel_test_user',
            email='cancel.test@example.com',
            password='Customer@123'
        )
        self.table = Table.objects.create(number='T-10', seats=4, location='Indoor')
        self.category = MenuCategory.objects.create(name='Test Category')
        self.menu_item = MenuItem.objects.create(
            name='Test Food Item',
            category=self.category,
            price=300.00,
            is_available=True
        )

    def test_cancellation_more_than_24_hours_refund_100_percent(self):
        """Test > 24 hours cancellation gives 100% refund on table charge and pre-order"""
        future_date = timezone.localdate() + timedelta(days=2)
        res = Reservation.objects.create(
            user=self.user,
            name='100 Percent Refund Customer',
            date=future_date,
            time=datetime.time(19, 0),
            guests=2,
            table=self.table,
            status='confirmed',
            payment_status='paid',
            booking_charge=200.00,
            pre_order_selected=True,
            pre_order_amount=300.00,
            total_amount=500.00,
            razorpay_payment_id='pay_test_100pct'
        )
        order = Order.objects.create(
            user=self.user,
            reservation=res,
            is_pre_order=True,
            status='accepted',
            total=300.00
        )

        self.client.force_login(self.user)
        cancel_page = self.client.get(f'/table-booking/{res.id}/cancel/')
        self.assertEqual(cancel_page.status_code, 200)
        self.assertContains(cancel_page, '100% refund')

        confirm_res = self.client.post(f'/table-booking/{res.id}/cancel/confirm/', {
            'cancellation_reason': 'Change of plans'
        }, follow=True)

        self.assertEqual(confirm_res.status_code, 200)
        res.refresh_from_db()
        self.assertEqual(res.status, 'cancelled')
        self.assertEqual(res.refund_amount, Decimal('500.00'))  # 100% of ₹500

    def test_cancellation_food_prep_started_zero_food_refund(self):
        """Test pre-order food prep started makes pre-order non-refundable"""
        future_date = timezone.localdate() + timedelta(days=2)
        res = Reservation.objects.create(
            user=self.user,
            name='Food Prep Started Customer',
            date=future_date,
            time=datetime.time(19, 0),
            guests=2,
            table=self.table,
            status='confirmed',
            payment_status='paid',
            booking_charge=200.00,
            pre_order_selected=True,
            pre_order_amount=300.00,
            total_amount=500.00,
            razorpay_payment_id='pay_test_food_prep'
        )
        order = Order.objects.create(
            user=self.user,
            reservation=res,
            is_pre_order=True,
            status='preparing',  # Kitchen started preparing
            total=300.00
        )

        self.client.force_login(self.user)
        cancel_page = self.client.get(f'/table-booking/{res.id}/cancel/')
        self.assertEqual(cancel_page.status_code, 200)
        self.assertContains(cancel_page, 'Food preparation has already started')

        confirm_res = self.client.post(f'/table-booking/{res.id}/cancel/confirm/', {
            'cancellation_reason': 'Running late'
        }, follow=True)

        res.refresh_from_db()
        self.assertEqual(res.status, 'cancelled')
        self.assertEqual(res.refund_amount, Decimal('200.00'))  # Table charge ₹200 (100%), food ₹0


class TableBookingStatusConnectivityTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username='connect_customer', password='Pass@123', email='cust@test.com')
        self.admin = User.objects.create_superuser(username='connect_admin', password='Pass@123', email='admin@test.com')
        
        manager_group, _ = Group.objects.get_or_create(name='Manager')
        self.manager = User.objects.create_user(username='connect_manager', password='Pass@123', email='mgr@test.com', is_staff=True)
        self.manager.groups.add(manager_group)

        self.table = Table.objects.create(number='T-100', seats=4, location='Window', is_available=True)

    def test_customer_booking_appears_in_admin_manager_and_my_bookings(self):
        future_date = timezone.localdate() + timedelta(days=1)
        booking = Reservation.objects.create(
            user=self.customer,
            name='John Connectivity',
            phone='9998887776',
            email='john@test.com',
            date=future_date,
            time=datetime.time(19, 30),
            guests=4,
            table=self.table,
            status='confirmed',
            payment_status='paid',
            booking_charge=Decimal('200.00'),
            pre_order_selected=True,
            pre_order_amount=Decimal('350.00'),
            total_amount=Decimal('550.00'),
            razorpay_payment_id='pay_connect_123'
        )

        # 1. Verify Customer My Bookings
        self.client.force_login(self.customer)
        cust_resp = self.client.get('/my-bookings/')
        self.assertEqual(cust_resp.status_code, 200)
        self.assertContains(cust_resp, f'Booking #TB{booking.id}')
        self.assertContains(cust_resp, 'T-100')
        self.assertContains(cust_resp, 'Confirmed')

        # 2. Verify Admin Panel
        self.client.force_login(self.admin)
        admin_resp = self.client.get('/admin-panel/reservations/')
        self.assertEqual(admin_resp.status_code, 200)
        self.assertContains(admin_resp, 'John Connectivity')
        self.assertContains(admin_resp, 'Table T-100')
        self.assertContains(admin_resp, 'Confirmed')

        # 3. Verify Manager Panel
        self.client.force_login(self.manager)
        mgr_resp = self.client.get('/staff/table-reservations/')
        self.assertEqual(mgr_resp.status_code, 200)
        self.assertContains(mgr_resp, 'John Connectivity')
        self.assertContains(mgr_resp, 'Table T-100')
        self.assertContains(mgr_resp, 'Confirmed')

    def test_manager_status_update_reflects_in_admin_and_customer(self):
        future_date = timezone.localdate() + timedelta(days=1)
        booking = Reservation.objects.create(
            user=self.customer,
            name='Sarah StatusTest',
            phone='8887776665',
            date=future_date,
            time=datetime.time(20, 0),
            guests=2,
            table=self.table,
            status='confirmed',
            payment_status='paid',
        )

        # Manager seats the guest
        self.client.force_login(self.manager)
        update_resp = self.client.post('/staff/table-reservations/', {
            'action': 'seat_table',
            'reservation_id': booking.id
        }, follow=True)
        self.assertEqual(update_resp.status_code, 200)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'seated')

        # Verify status in Admin Panel
        self.client.force_login(self.admin)
        admin_resp = self.client.get('/admin-panel/reservations/')
        self.assertContains(admin_resp, 'Seated')

        # Verify status in Customer My Bookings
        self.client.force_login(self.customer)
        cust_resp = self.client.get('/my-bookings/')
        self.assertContains(cust_resp, 'Seated')

    def test_cancellation_reflects_across_all_panels(self):
        future_date = timezone.localdate() + timedelta(days=2)
        booking = Reservation.objects.create(
            user=self.customer,
            name='Cancel Alignment Test',
            phone='7776665554',
            date=future_date,
            time=datetime.time(18, 0),
            guests=2,
            table=self.table,
            status='confirmed',
            payment_status='paid',
            booking_charge=Decimal('200.00'),
            total_amount=Decimal('200.00'),
            razorpay_payment_id='pay_cancel_test'
        )

        # Customer cancels booking
        self.client.force_login(self.customer)
        cancel_resp = self.client.post(f'/table-booking/{booking.id}/cancel/confirm/', {
            'cancellation_reason': 'Emergency plans'
        }, follow=True)
        self.assertEqual(cancel_resp.status_code, 200)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'cancelled')

        # Verify Admin Panel shows Cancelled
        self.client.force_login(self.admin)
        admin_resp = self.client.get('/admin-panel/reservations/')
        self.assertContains(admin_resp, 'Cancelled')

        # Verify Manager Panel shows Cancelled
        self.client.force_login(self.manager)
        mgr_resp = self.client.get('/staff/table-reservations/')
        self.assertContains(mgr_resp, 'Cancelled')


class MenuItemCustomizationMappingTests(TestCase):
    def setUp(self):
        from .models import ItemCustomization, CustomizationOption, MenuCategory

        self.cat = MenuCategory.objects.create(name='Test Category')

        self.pizza_item = MenuItem.objects.create(
            name='Test Pizza',
            category=self.cat,
            price=Decimal('250.00'),
            is_available=True,
            status='in_stock'
        )
        self.pasta_item = MenuItem.objects.create(
            name='Test Pasta',
            category=self.cat,
            price=Decimal('200.00'),
            is_available=True,
            status='in_stock'
        )
        self.bread_item = MenuItem.objects.create(
            name='Test Garlic Bread',
            category=self.cat,
            price=Decimal('100.00'),
            is_available=True,
            status='in_stock'
        )

        # Pizza customization group
        self.pizza_size = ItemCustomization.objects.create(name='Pizza Size', status='active')
        self.pizza_size.linked_items.add(self.pizza_item)
        CustomizationOption.objects.create(customization=self.pizza_size, name='Small', price=Decimal('0.00'))
        CustomizationOption.objects.create(customization=self.pizza_size, name='Medium', price=Decimal('50.00'))

        # Pasta customization group
        self.pasta_type = ItemCustomization.objects.create(name='Pasta Type', status='active')
        self.pasta_type.linked_items.add(self.pasta_item)
        CustomizationOption.objects.create(customization=self.pasta_type, name='Penne', price=Decimal('0.00'))
        CustomizationOption.objects.create(customization=self.pasta_type, name='Spaghetti', price=Decimal('20.00'))

    def test_pizza_customization_returns_only_pizza_options(self):
        response = self.client.get(f'/food-order/item-details/{self.pizza_item.id}/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        groups = data['item']['customization_groups']
        group_names = [g['name'] for g in groups]
        self.assertIn('Pizza Size', group_names)
        self.assertNotIn('Pasta Type', group_names)

    def test_pasta_customization_returns_only_pasta_options(self):
        response = self.client.get(f'/food-order/item-details/{self.pasta_item.id}/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        groups = data['item']['customization_groups']
        group_names = [g['name'] for g in groups]
        self.assertIn('Pasta Type', group_names)
        self.assertNotIn('Pizza Size', group_names)

    def test_no_customization_returns_empty_groups(self):
        response = self.client.get(f'/food-order/item-details/{self.bread_item.id}/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        groups = data['item']['customization_groups']
        self.assertEqual(len(groups), 0)

    def test_admin_linking_customization_to_menu_item(self):
        admin = User.objects.create_superuser('admin_cust', 'admin.cust@example.com', 'Admin@123')
        self.client.force_login(admin)

        response = self.client.post('/admin-panel/menu/', {
            'action': 'add_customization',
            'cust_name': 'Garlic Bread Dip',
            'cust_status': 'active',
            'option_name[]': ['Cheese Dip', 'Garlic Mayo'],
            'option_price[]': ['30', '20'],
            'linked_items': [self.bread_item.id]
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.bread_item.refresh_from_db()
        groups = list(self.bread_item.customization_groups.values_list('name', flat=True))
        self.assertIn('Garlic Bread Dip', groups)


class KitchenPanelChefWorkflowTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group, User
        from .models import MenuCategory, MenuItem, Order, OrderItem, Payment, Table

        self.chef_group, _ = Group.objects.get_or_create(name='Chef')
        self.waiter_group, _ = Group.objects.get_or_create(name='Waiter')
        self.handler_group, _ = Group.objects.get_or_create(name='OrderHandler')
        self.manager_group, _ = Group.objects.get_or_create(name='Manager')

        self.admin = User.objects.create_superuser('admin_wf', 'admin.wf@example.com', 'Admin@123')
        self.chef = User.objects.create_user('chef_sanjay', 'sanjay.chef@example.com', 'Chef@12345', is_staff=True)
        self.chef.groups.add(self.chef_group)

        self.waiter_1 = User.objects.create_user('waiter_raj', 'raj.waiter@example.com', 'Waiter@123', is_staff=True)
        self.waiter_1.groups.add(self.waiter_group)

        self.waiter_2 = User.objects.create_user('waiter_amit', 'amit.waiter@example.com', 'Waiter@123', is_staff=True)
        self.waiter_2.groups.add(self.waiter_group)

        self.handler = User.objects.create_user('handler_pooja', 'pooja.handler@example.com', 'Handler@123', is_staff=True)
        self.handler.groups.add(self.handler_group)

        self.customer = User.objects.create_user('customer_kunal', 'kunal.cust@example.com', 'Cust@12345')

        self.category = MenuCategory.objects.create(name='Dinner')
        self.item = MenuItem.objects.create(
            name='Paneer Tikka', category=self.category,
            price=Decimal('250.00'), is_available=True, status='in_stock'
        )
        self.table = Table.objects.create(number='T-05', seats=4, location='Main Hall')

    def test_admin_can_create_chef_staff_account(self):
        self.client.force_login(self.admin)
        response = self.client.post('/admin-panel/staff/add/', {
            'username': 'new_head_chef',
            'email': 'headchef@restaurant.com',
            'first_name': 'Vikas',
            'last_name': 'Khanna',
            'mobile_no': '9876543210',
            'password': 'Password@123',
            'sub_role': 'Chef'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        new_chef = User.objects.get(username='new_head_chef')
        self.assertTrue(new_chef.is_staff)
        self.assertTrue(new_chef.groups.filter(name='Chef').exists())

    def test_chef_login_redirects_directly_to_kitchen_dashboard(self):
        self.client.force_login(self.chef)
        dash_response = self.client.get('/staff/', follow=True)
        self.assertRedirects(dash_response, '/staff/kitchen/', fetch_redirect_response=False)

        kitchen_response = self.client.get('/staff/kitchen/')
        self.assertEqual(kitchen_response.status_code, 200)
        self.assertContains(kitchen_response, 'Kitchen Display System')

    def test_unauthorized_users_denied_access_to_kitchen_dashboard(self):
        # Customer
        self.client.force_login(self.customer)
        cust_resp = self.client.get('/staff/kitchen/', follow=True)
        self.assertContains(cust_resp, 'Staff access required')

        # Waiter
        self.client.force_login(self.waiter_1)
        waiter_resp = self.client.get('/staff/kitchen/', follow=True)
        self.assertRedirects(waiter_resp, '/staff/no-access/', fetch_redirect_response=False)

    def test_end_to_end_prepaid_order_handler_chef_waiter_customer_flow(self):
        # 1. Customer places order with prepaid payment
        order = Order.objects.create(
            user=self.customer,
            table=self.table,
            status='pending',
            total=Decimal('250.00'),
            order_type='restaurant'
        )
        OrderItem.objects.create(order=order, menu_item=self.item, quantity=1, price=self.item.price)
        Payment.objects.create(order=order, amount=Decimal('250.00'), method='upi', status='success')

        # Customer sees Order Placed
        self.client.force_login(self.customer)
        cust_orders = self.client.get('/my-orders/')
        self.assertEqual(cust_orders.status_code, 200)
        self.assertContains(cust_orders, 'Order Placed')

        # 2. Order Handler accepts and sends to Kitchen
        self.client.force_login(self.handler)
        oh_resp = self.client.post('/staff/order-handler/', {
            'order_id': order.id,
            'status': 'accepted'
        }, follow=True)
        self.assertEqual(oh_resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')

        # Customer sees Order Accepted
        self.client.force_login(self.customer)
        cust_orders = self.client.get('/my-orders/')
        self.assertContains(cust_orders, 'Order Accepted')

        # 3. Chef starts preparing
        self.client.force_login(self.chef)
        kitchen_resp = self.client.get('/staff/kitchen/')
        self.assertContains(kitchen_resp, f'Order #{order.id}')

        prep_resp = self.client.post('/staff/kitchen/', {
            'action': 'start_preparing',
            'order_id': order.id
        }, follow=True)
        self.assertEqual(prep_resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')

        # Customer sees Preparing Your Order
        self.client.force_login(self.customer)
        cust_orders = self.client.get('/my-orders/')
        self.assertContains(cust_orders, 'Preparing Your Order')

        # 4. Chef marks ready (Order is ready for any waiter to serve)
        self.client.force_login(self.chef)
        ready_resp = self.client.post('/staff/kitchen/', {
            'action': 'mark_ready',
            'order_id': order.id
        }, follow=True)
        self.assertEqual(ready_resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')
        self.assertIsNone(order.assigned_waiter)

        # Customer sees Food Ready
        self.client.force_login(self.customer)
        cust_orders = self.client.get('/my-orders/')
        self.assertContains(cust_orders, 'Food Ready')

        # 5. Any Waiter can serve the ready order
        self.client.force_login(self.waiter_1)
        waiter_dash = self.client.get('/staff/waiter/')
        self.assertContains(waiter_dash, f'Order #{order.id}')

        serve_resp = self.client.post('/staff/waiter/', {
            'action': 'serve',
            'order_id': order.id
        }, follow=True)
        self.assertEqual(serve_resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')

        # Customer sees Served
        self.client.force_login(self.customer)
        cust_orders = self.client.get('/my-orders/')
        self.assertContains(cust_orders, 'Served')

        # Billing integrity: Exactly 1 payment record exists; no secondary charge generated
        self.assertEqual(order.payments.count(), 1)
        self.assertEqual(order.payments.first().status, 'success')

    def test_chef_marks_order_ready_without_forced_waiter_assignment(self):
        order_2 = Order.objects.create(
            user=self.customer, table=self.table, status='preparing',
            total=Decimal('300.00')
        )

        self.client.force_login(self.chef)
        self.client.post('/staff/kitchen/', {
            'action': 'mark_ready',
            'order_id': order_2.id
        }, follow=True)

        order_2.refresh_from_db()
        self.assertEqual(order_2.status, 'ready')


class AdminAuthAndLogoutTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin_test_user',
            email='admin.test@example.com',
            password='AdminPassword@123'
        )

    def test_admin_logout_redirects_to_main_login(self):
        self.client.force_login(self.admin)
        response = self.client.get('/admin-logout/')
        self.assertRedirects(response, '/login/')

    def test_admin_panel_requires_login_redirects_to_main_login(self):
        response = self.client.get('/admin-panel/')
        self.assertRedirects(response, '/login/?next=/admin-panel/')

    def test_admin_login_url_redirects_to_main_login(self):
        response = self.client.get('/admin-login/')
        self.assertRedirects(response, '/login/')


class WaiterDashboardAndAssignedOrdersTests(TestCase):
    def setUp(self):
        self.waiter_group, _ = Group.objects.get_or_create(name='Waiter')
        self.waiter1 = User.objects.create_user(
            username='waiter_one', email='w1@test.com', password='Password@123', is_staff=True
        )
        self.waiter1.groups.add(self.waiter_group)

        self.waiter2 = User.objects.create_user(
            username='waiter_two', email='w2@test.com', password='Password@123', is_staff=True
        )
        self.waiter2.groups.add(self.waiter_group)

        self.customer = User.objects.create_user(
            username='cust_test', email='c@test.com', password='Password@123'
        )

        self.table1 = Table.objects.create(number='T-1', seats=4, location='Indoor')
        self.table2 = Table.objects.create(number='T-2', seats=2, location='Indoor')

        self.cat = MenuCategory.objects.create(name='Drinks')
        self.item = MenuItem.objects.create(name='Cold Coffee', category=self.cat, price=Decimal('120.00'))

    def test_waiter_dashboard_summary_metrics(self):
        o1 = Order.objects.create(user=self.customer, table=self.table1, status='ready', total=Decimal('120.00'))
        OrderItem.objects.create(order=o1, menu_item=self.item, quantity=1, price=Decimal('120.00'))

        o2 = Order.objects.create(user=self.customer, table=self.table2, status='ready', total=Decimal('120.00'))
        OrderItem.objects.create(order=o2, menu_item=self.item, quantity=1, price=Decimal('120.00'))

        o3 = Order.objects.create(user=self.customer, table=self.table1, status='delivered', total=Decimal('120.00'))

        self.client.force_login(self.waiter1)
        response = self.client.get('/staff/waiter/')
        self.assertEqual(response.status_code, 200)

        # ready_count: total ready across restaurant = 2
        self.assertEqual(response.context['ready_count'], 2)
        # to_serve_count: total ready orders waiting to be served = 2
        self.assertEqual(response.context['to_serve_count'], 2)
        # served_today_count: delivered today = 1
        self.assertEqual(response.context['served_today_count'], 1)
        # Kitchen alert contains both o1 and o2
        self.assertIn(o1, response.context['ready_orders'])
        self.assertIn(o2, response.context['ready_orders'])

    def test_waiter_can_serve_ready_order_from_dashboard(self):
        o1 = Order.objects.create(user=self.customer, table=self.table1, status='ready', total=Decimal('120.00'))
        self.client.force_login(self.waiter1)
        response = self.client.post('/staff/waiter/', {'action': 'serve', 'order_id': o1.id}, follow=True)
        self.assertEqual(response.status_code, 200)
        o1.refresh_from_db()
        self.assertEqual(o1.status, 'delivered')

    def test_any_waiter_can_serve_any_ready_order(self):
        o2 = Order.objects.create(user=self.customer, table=self.table2, status='ready', total=Decimal('120.00'))
        self.client.force_login(self.waiter1)
        response = self.client.post('/staff/orders/', {'action': 'serve', 'order_id': o2.id}, follow=True)
        self.assertEqual(response.status_code, 200)
        o2.refresh_from_db()
        self.assertEqual(o2.status, 'delivered')

    def test_waiter_cannot_serve_preparing_or_pending_order(self):
        o_prep = Order.objects.create(user=self.customer, table=self.table1, status='preparing', total=Decimal('120.00'))
        self.client.force_login(self.waiter1)
        response = self.client.post('/staff/orders/', {'action': 'serve', 'order_id': o_prep.id}, follow=True)
        o_prep.refresh_from_db()
        self.assertEqual(o_prep.status, 'preparing')

    def test_orders_page_shows_all_orders_for_waiter(self):
        o1 = Order.objects.create(user=self.customer, table=self.table1, status='ready', total=Decimal('120.00'))
        o2 = Order.objects.create(user=self.customer, table=self.table2, status='ready', total=Decimal('120.00'))
        self.client.force_login(self.waiter1)
        response = self.client.get('/staff/orders/')
        self.assertEqual(response.status_code, 200)
        orders = list(response.context['orders'])
        self.assertIn(o1, orders)
        self.assertIn(o2, orders)


class KitchenOrderTrackingWorkflowTests(TestCase):
    def setUp(self):
        self.chef_group = Group.objects.create(name='Chef')
        self.waiter_group = Group.objects.create(name='Waiter')
        self.handler_group = Group.objects.create(name='OrderHandler')

        self.chef_user = User.objects.create_user(
            username='chef_test', email='chef@test.com', password='Password@123', is_staff=True
        )
        self.chef_user.groups.add(self.chef_group)

        self.waiter_user = User.objects.create_user(
            username='waiter_k_test', email='waiter_k@test.com', password='Password@123', is_staff=True
        )
        self.waiter_user.groups.add(self.waiter_group)

        self.handler_user = User.objects.create_user(
            username='handler_k_test', email='handler_k@test.com', password='Password@123', is_staff=True
        )
        self.handler_user.groups.add(self.handler_group)

        self.customer = User.objects.create_user(
            username='cust_k_test', email='cust_k@test.com', password='Password@123'
        )

        self.table = Table.objects.create(number='KT-01', seats=4, location='Indoor', is_available=False)
        self.cat = MenuCategory.objects.create(name='Kitchen Category')
        self.item = MenuItem.objects.create(name='Special Biryani', category=self.cat, price=Decimal('250.00'))

    def test_kitchen_dashboard_sections_and_summary_cards(self):
        o1 = Order.objects.create(user=self.customer, table=self.table, status='accepted', total=Decimal('250.00'), rush_flag=True)
        OrderItem.objects.create(order=o1, menu_item=self.item, quantity=1, price=Decimal('250.00'))

        o2 = Order.objects.create(user=self.customer, table=self.table, status='preparing', total=Decimal('250.00'))
        OrderItem.objects.create(order=o2, menu_item=self.item, quantity=1, price=Decimal('250.00'))

        o3 = Order.objects.create(user=self.customer, table=self.table, status='ready', total=Decimal('250.00'))
        OrderItem.objects.create(order=o3, menu_item=self.item, quantity=1, price=Decimal('250.00'))

        self.client.force_login(self.chef_user)
        response = self.client.get('/staff/kitchen/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['new_orders_count'], 1)
        self.assertEqual(response.context['preparing_count'], 1)
        self.assertEqual(response.context['ready_count'], 1)
        self.assertEqual(response.context['rush_count'], 1)

    def test_chef_workflow_start_prep_and_mark_ready(self):
        order = Order.objects.create(user=self.customer, table=self.table, status='accepted', total=Decimal('250.00'))
        self.client.force_login(self.chef_user)

        # 1. Start Preparing
        res = self.client.post('/staff/kitchen/', {'action': 'start_preparing', 'order_id': order.id}, follow=True)
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')
        self.assertIsNotNone(order.prep_started_at)
        self.assertEqual(order.chef, self.chef_user)

        # 2. Mark Ready
        res2 = self.client.post('/staff/kitchen/', {'action': 'mark_ready', 'order_id': order.id}, follow=True)
        self.assertEqual(res2.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')
        self.assertIsNotNone(order.ready_at)
        self.assertNotEqual(order.prep_duration, '—')

        # 3. Notification created for Waiter
        notif = KitchenNotification.objects.filter(order=order, target_role='Waiter').first()
        self.assertIsNotNone(notif)
        self.assertEqual(notif.notification_type, 'order_ready')

    def test_chef_cannot_mark_served(self):
        order = Order.objects.create(user=self.customer, table=self.table, status='ready', total=Decimal('250.00'))
        self.client.force_login(self.chef_user)
        res = self.client.post('/staff/orders/', {'action': 'serve', 'order_id': order.id}, follow=True)
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

    def test_order_handler_sends_to_kitchen_and_creates_notification(self):
        order = Order.objects.create(user=self.customer, table=self.table, status='pending', total=Decimal('250.00'), rush_flag=True)
        self.client.force_login(self.handler_user)
        res = self.client.post('/staff/order-handler/', {'status': 'accepted', 'order_id': order.id}, follow=True)
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')
        self.assertIsNotNone(order.sent_to_kitchen_at)
        self.assertEqual(order.order_handler, self.handler_user)

        # Notification for Kitchen
        notif = KitchenNotification.objects.filter(order=order, target_role='Kitchen').first()
        self.assertIsNotNone(notif)
        self.assertEqual(notif.notification_type, 'rush_order')

    def test_waiter_marks_served_and_frees_table(self):
        order = Order.objects.create(user=self.customer, table=self.table, status='ready', total=Decimal('250.00'))
        self.client.force_login(self.waiter_user)
        res = self.client.post('/staff/orders/', {'action': 'serve', 'order_id': order.id}, follow=True)
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertIsNotNone(order.served_at)
        self.assertEqual(order.assigned_waiter, self.waiter_user)
        self.table.refresh_from_db()
        self.assertTrue(self.table.is_available)

    def test_chef_can_access_all_orders_and_notifications(self):
        self.client.force_login(self.chef_user)
        res_orders = self.client.get('/staff/orders/')
        self.assertEqual(res_orders.status_code, 200)

        res_notif = self.client.get('/staff/notifications/')
        self.assertEqual(res_notif.status_code, 200)

    def test_kitchen_notification_mark_read_and_mark_all_read(self):
        n1 = KitchenNotification.objects.create(target_role='Kitchen', title='Alert 1', message='msg 1', is_read=False)
        n2 = KitchenNotification.objects.create(target_role='Kitchen', title='Alert 2', message='msg 2', is_read=False)
        self.client.force_login(self.chef_user)

        # Mark single
        res1 = self.client.post('/staff/notifications/', {'action': 'mark_read', 'notification_id': n1.id}, follow=True)
        self.assertEqual(res1.status_code, 200)
        n1.refresh_from_db()
        self.assertTrue(n1.is_read)

        # Mark all
        res2 = self.client.post('/staff/notifications/', {'action': 'mark_all_read'}, follow=True)
        self.assertEqual(res2.status_code, 200)
        n2.refresh_from_db()
        self.assertTrue(n2.is_read)


class HomepageStatisticsTests(TestCase):
    def test_homepage_empty_database_shows_fallback_stats(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

        # Context assertions
        self.assertEqual(response.context['completed_guests_count'], 0)
        self.assertIsNone(response.context['avg_rating'])
        self.assertTrue(response.context['supports_instant_confirmation'])

        # HTML content assertions
        content = response.content.decode('utf-8')
        self.assertIn('Be our first guest', content)
        self.assertIn('No ratings yet', content)
        self.assertIn('Instant', content)
        self.assertIn('Confirmation', content)
        self.assertNotIn('12k+', content)
        self.assertNotIn('4.9★', content)

    def test_homepage_single_user_multiple_completed_orders_counted_once(self):
        user = User.objects.create_user(
            username='repeat_guest',
            email='repeat@example.com',
            password='Password@123'
        )
        Order.objects.create(user=user, status='completed', total=Decimal('150.00'))
        Order.objects.create(user=user, status='delivered', total=Decimal('200.00'))

        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['completed_guests_count'], 1)

        content = response.content.decode('utf-8')
        self.assertIn('>1</h4><span>Happy guest</span>', content)
        self.assertIn('Loved by 1 guest', content)
        self.assertNotIn('Be our first guest', content)

    def test_homepage_counts_only_completed_served_delivered_customers(self):
        u1 = User.objects.create_user(username='u1', email='u1@example.com', password='Password@123')
        u2 = User.objects.create_user(username='u2', email='u2@example.com', password='Password@123')
        u3 = User.objects.create_user(username='u3', email='u3@example.com', password='Password@123')
        u4 = User.objects.create_user(username='u4', email='u4@example.com', password='Password@123')
        u5 = User.objects.create_user(username='u5', email='u5@example.com', password='Password@123')

        Order.objects.create(user=u1, status='completed', total=Decimal('100.00'))
        Order.objects.create(user=u2, status='delivered', total=Decimal('100.00'))
        Order.objects.create(user=u3, status='served', total=Decimal('100.00'))
        Order.objects.create(user=u4, status='pending', total=Decimal('100.00'))
        Order.objects.create(user=u5, status='cancelled', total=Decimal('100.00'))

        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['completed_guests_count'], 3)

        content = response.content.decode('utf-8')
        self.assertIn('>3</h4><span>Happy guests</span>', content)
        self.assertIn('Loved by 3 guests', content)

    def test_homepage_with_customer_reviews_shows_avg_rating(self):
        user = User.objects.create_user(username='reviewer_user', email='reviewer@example.com', password='Password@123')
        CustomerReview.objects.create(user=user, rating=4, is_approved=True)
        CustomerReview.objects.create(user=user, rating=5, is_approved=True)
        # Unapproved review should not count in average
        CustomerReview.objects.create(user=user, rating=1, is_approved=False)

        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['avg_rating'], 4.5)

        content = response.content.decode('utf-8')
        self.assertIn('4.5', content)
        self.assertIn('★', content)
        self.assertIn('4.5★ average rating', content)
        self.assertNotIn('No ratings yet', content)


class CustomerReviewModelAndSubmissionTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='review_test_user',
            email='review_test@example.com',
            password='Password@123'
        )

    def test_create_customer_review_model(self):
        order = Order.objects.create(user=self.customer, status='completed', total=Decimal('350.00'))
        review = CustomerReview.objects.create(
            user=self.customer,
            order=order,
            rating=5,
            title='Outstanding Experience!',
            comment='The food was freshly made and flavorful.',
            is_approved=True,
        )
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.title, 'Outstanding Experience!')
        self.assertTrue(review.is_approved)
        self.assertIn('review_test_user - 5★', str(review))

    def test_submit_customer_review_view(self):
        order = Order.objects.create(user=self.customer, status='completed', total=Decimal('220.00'))
        self.client.force_login(self.customer)

        response = self.client.post('/reviews/submit/', {
            'order_id': order.id,
            'rating': '5',
            'title': 'Great Service',
            'comment': 'Prompt serving and delicious meals!',
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        review = CustomerReview.objects.get(order=order)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.user, self.customer)
        self.assertEqual(review.title, 'Great Service')
        self.assertEqual(review.comment, 'Prompt serving and delicious meals!')

    def test_submit_customer_review_invalid_rating(self):
        self.client.force_login(self.customer)
        response = self.client.post('/reviews/submit/', {
            'rating': '10',  # invalid rating
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomerReview.objects.count(), 0)

    def test_submit_customer_review_unauthenticated(self):
        response = self.client.post('/reviews/submit/', {
            'rating': '5',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_customer_completed_order_can_submit_rating(self):
        order = Order.objects.create(user=self.customer, status='completed', total=Decimal('250.00'))
        self.client.force_login(self.customer)

        # GET my-orders should have "Rate Now"
        res_get = self.client.get('/my-orders/')
        self.assertEqual(res_get.status_code, 200)
        self.assertIn('Rate Now', res_get.content.decode('utf-8'))

        # Submit rating
        res_post = self.client.post('/reviews/submit/', {
            'order_id': order.id,
            'rating': '5',
            'title': 'Superb dining',
            'comment': 'Authentic taste and warm hospitality.',
        }, follow=True)
        self.assertEqual(res_post.status_code, 200)

        # Verify saved in DB
        self.assertTrue(CustomerReview.objects.filter(order=order, rating=5).exists())

        # Verify displayed in my-orders
        content = res_post.content.decode('utf-8')
        self.assertIn('Your Rating: 5 / 5★', content)
        self.assertIn('Superb dining', content)
        self.assertIn('Authentic taste and warm hospitality.', content)
        self.assertIn('Edit Rating', content)

    def test_customer_pending_or_preparing_order_cannot_submit_rating(self):
        order_pending = Order.objects.create(user=self.customer, status='pending', total=Decimal('100.00'))
        order_prep = Order.objects.create(user=self.customer, status='preparing', total=Decimal('150.00'))
        self.client.force_login(self.customer)

        # Check UI does not render review modal for pending or preparing
        res = self.client.get('/my-orders/')
        self.assertEqual(res.status_code, 200)
        content = res.content.decode('utf-8')
        self.assertNotIn(f'#reviewModal{order_pending.id}', content)
        self.assertNotIn(f'#reviewModal{order_prep.id}', content)

        # Attempt to POST rating for pending order
        self.client.post('/reviews/submit/', {'order_id': order_pending.id, 'rating': '5'}, follow=True)
        self.assertFalse(CustomerReview.objects.filter(order=order_pending).exists())

        # Attempt to POST rating for preparing order
        self.client.post('/reviews/submit/', {'order_id': order_prep.id, 'rating': '4'}, follow=True)
        self.assertFalse(CustomerReview.objects.filter(order=order_prep).exists())

    def test_customer_cannot_rate_another_customers_order(self):
        other_customer = User.objects.create_user(username='other_cust', email='other@example.com', password='Password@123')
        order = Order.objects.create(user=other_customer, status='completed', total=Decimal('300.00'))

        # Logged in as self.customer, try to rate other_customer's order
        self.client.force_login(self.customer)
        self.client.post('/reviews/submit/', {
            'order_id': order.id,
            'rating': '5',
            'comment': 'Unauthorized review',
        }, follow=True)

        self.assertFalse(CustomerReview.objects.filter(order=order).exists())

    def test_rating_boundary_validation(self):
        order = Order.objects.create(user=self.customer, status='completed', total=Decimal('100.00'))
        self.client.force_login(self.customer)

        # Rating 0 rejected
        self.client.post('/reviews/submit/', {'order_id': order.id, 'rating': '0'})
        self.assertFalse(CustomerReview.objects.filter(order=order).exists())

        # Rating 6 rejected
        self.client.post('/reviews/submit/', {'order_id': order.id, 'rating': '6'})
        self.assertFalse(CustomerReview.objects.filter(order=order).exists())

        # Non-numeric rejected
        self.client.post('/reviews/submit/', {'order_id': order.id, 'rating': 'abc'})
        self.assertFalse(CustomerReview.objects.filter(order=order).exists())

    def test_duplicate_rating_updates_existing_and_prevents_duplicate_rows(self):
        order = Order.objects.create(user=self.customer, status='completed', total=Decimal('120.00'))
        self.client.force_login(self.customer)

        # First rating
        self.client.post('/reviews/submit/', {
            'order_id': order.id,
            'rating': '4',
            'comment': 'Good food',
        })
        self.assertEqual(CustomerReview.objects.filter(order=order).count(), 1)
        self.assertEqual(CustomerReview.objects.get(order=order).rating, 4)

        # Second rating (update)
        self.client.post('/reviews/submit/', {
            'order_id': order.id,
            'rating': '5',
            'comment': 'Updated: Excellent food!',
        })
        # Exactly 1 review exists for this order
        self.assertEqual(CustomerReview.objects.filter(order=order).count(), 1)
        updated_rev = CustomerReview.objects.get(order=order)
        self.assertEqual(updated_rev.rating, 5)
        self.assertEqual(updated_rev.comment, 'Updated: Excellent food!')

    def test_submitted_rating_persists_across_page_refreshes(self):
        order = Order.objects.create(user=self.customer, status='completed', total=Decimal('180.00'))
        self.client.force_login(self.customer)

        self.client.post('/reviews/submit/', {
            'order_id': order.id,
            'rating': '5',
            'comment': 'Persistent comment',
        })

        # Load multiple times
        res1 = self.client.get('/my-orders/')
        self.assertIn('Persistent comment', res1.content.decode('utf-8'))

        res2 = self.client.get('/my-orders/')
        self.assertIn('Persistent comment', res2.content.decode('utf-8'))

    def test_homepage_average_updates_dynamically_from_submitted_ratings(self):
        # 1. Initially empty
        res_empty = self.client.get('/')
        self.assertIn('No ratings yet', res_empty.content.decode('utf-8'))

        # 2. Add first rating
        o1 = Order.objects.create(user=self.customer, status='completed', total=Decimal('100.00'))
        self.client.force_login(self.customer)
        self.client.post('/reviews/submit/', {'order_id': o1.id, 'rating': '4'})

        res1 = self.client.get('/')
        self.assertEqual(res1.context['avg_rating'], 4.0)
        self.assertEqual(res1.context['rating_count'], 1)
        self.assertIn('4.0', res1.content.decode('utf-8'))

        # 3. Add second rating by another user
        c2 = User.objects.create_user(username='c2', email='c2@example.com', password='Password@123')
        o2 = Order.objects.create(user=c2, status='completed', total=Decimal('100.00'))
        self.client.force_login(c2)
        self.client.post('/reviews/submit/', {'order_id': o2.id, 'rating': '5'})

        res2 = self.client.get('/')
        # Average of 4 and 5 is 4.5
        self.assertEqual(res2.context['avg_rating'], 4.5)
        self.assertEqual(res2.context['rating_count'], 2)
        self.assertIn('4.5', res2.content.decode('utf-8'))





