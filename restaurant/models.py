from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
import random



# ============================================================
class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    mobile_no = models.CharField(max_length=15, blank=True)
    address = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.user.username} Profile"


@receiver(post_save, sender=User)
def create_or_update_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)
    else:
        instance.profile.save()


# ============================================================
# UNIFIED OTP MODEL
# ============================================================
class OTPVerification(models.Model):
    PURPOSE_CHOICES = [
        ('signup', 'Signup'),
        ('login', 'Login'),
        ('reset', 'Password Reset'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='otps')
    otp = models.CharField(max_length=6)
    purpose = models.CharField(max_length=10, choices=PURPOSE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    is_verified = models.BooleanField(default=False)

    def is_expired(self):
        return (timezone.now() - self.created_at).total_seconds() > 600

    @staticmethod
    def generate_otp():
        return str(random.randint(100000, 999999))

    def __str__(self):
        return f"{self.user.username} - {self.purpose} - {self.otp}"


# ============================================================
# TABLE
# ============================================================
class Table(models.Model):
    LOCATION_CHOICES = [
        ('Indoor', 'Indoor'),
        ('Outdoor', 'Outdoor'),
        ('Window', 'Window'),
        ('Rooftop', 'Rooftop'),
    ]
    STATUS_CHOICES = [
        ('free', 'Available'),
        ('occupied', 'Occupied'),
        ('reserved', 'Reserved'),
        ('cleaning', 'Cleaning'),
    ]
    number = models.CharField(max_length=10, unique=True)
    seats = models.IntegerField(default=4)
    location = models.CharField(max_length=50, choices=LOCATION_CHOICES, default='Indoor')
    is_available = models.BooleanField(default=True)
    assigned_waiter = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tables')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='free')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Table {self.number} ({self.seats} seats)"

    class Meta:
        ordering = ['number']


# ============================================================
# MENU
# ============================================================
class MenuCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default='')
    franchises = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Menu Categories"
        ordering = ['name']


# ✅ SUBCATEGORY MODEL
class MenuSubcategory(models.Model):
    name = models.CharField(max_length=100)
    category = models.ForeignKey(
        MenuCategory,
        on_delete=models.CASCADE,
        related_name='subcategories'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.category.name})"

    class Meta:
        ordering = ['name']
        verbose_name = "Menu Subcategory"
        verbose_name_plural = "Menu Subcategories"
        unique_together = ['name', 'category']


# ✅ MENU ITEM
class MenuItem(models.Model):
    STATUS_CHOICES = [
        ('in_stock', 'In Stock'),
        ('out_of_stock', 'Out of Stock'),
        ('coming_soon', 'Coming Soon'),
    ]
    FOOD_TYPE_CHOICES = [
        ('veg', 'Veg 🟢'),
        ('non_veg', 'Non-Veg 🔴'),
        ('vegan', 'Vegan 🌱'),
        ('egg', 'Egg 🟡'),
    ]

    name = models.CharField(max_length=200)
    category = models.ForeignKey(MenuCategory, on_delete=models.CASCADE, related_name='items')
    subcategory = models.ForeignKey(
        MenuSubcategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='items'
    )
    price = models.DecimalField(max_digits=8, decimal_places=2)
    discount_percentage = models.IntegerField(default=0)
    discount_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    food_type = models.CharField(max_length=20, choices=FOOD_TYPE_CHOICES, default='veg')
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='menu/', blank=True, null=True)
    customizations = models.TextField(blank=True, default='')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='in_stock',
    )
    is_available = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False)
    rating = models.IntegerField(default=4)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - ₹{self.price}"

    def get_final_price(self):
        if self.discount_percentage > 0:
            return round(float(self.price) * (1 - self.discount_percentage / 100.0), 2)
        elif self.discount_price > 0 and self.discount_price < self.price:
            return float(self.discount_price)
        return float(self.price)

    def customization_list(self):
        return [c.strip() for c in self.customizations.split(',') if c.strip()]

    class Meta:
        ordering = ['-created_at']


# ============================================================
# ITEM CUSTOMIZATION (Groups + Options)
# ============================================================
class ItemCustomization(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    linked_items = models.ManyToManyField(
        MenuItem,
        blank=True,
        related_name='customization_groups'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Item Customization"
        verbose_name_plural = "Item Customizations"


class CustomizationOption(models.Model):
    customization = models.ForeignKey(
        ItemCustomization,
        on_delete=models.CASCADE,
        related_name='options'
    )
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} (+₹{self.price})"

    class Meta:
        ordering = ['id']


# ============================================================
# ORDERS
# ============================================================
class Order(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('preparing', 'Preparing'),
        ('ready', 'Ready'),
        ('served', 'Served'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    ORDER_TYPE_CHOICES = [
        ('restaurant', 'Dine-in'),
        ('takeaway', 'Takeaway'),
        ('online', 'Dine-in'),  # Backward compatibility
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending')
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=15, blank=True)
    priority = models.CharField(max_length=20, default='normal')
    kitchen_notes = models.TextField(blank=True, default='')
    rush_flag = models.BooleanField(default=False)

    table = models.ForeignKey(
        Table,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='orders',
    )
    reservation = models.ForeignKey(
        'Reservation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pre_orders',
    )
    is_pre_order = models.BooleanField(default=False)
    order_type = models.CharField(
        max_length=20,
        choices=ORDER_TYPE_CHOICES,
        default='restaurant',
    )
    assigned_waiter = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_orders',
    )
    order_handler = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='handled_orders',
    )
    chef = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prepared_orders',
    )

    sent_to_kitchen_at = models.DateTimeField(null=True, blank=True)
    prep_started_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    served_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)

        now = timezone.now()
        if self.status == 'accepted' and not self.sent_to_kitchen_at:
            self.sent_to_kitchen_at = now
            if update_fields is not None:
                update_fields.add('sent_to_kitchen_at')
        elif self.status == 'preparing' and not self.prep_started_at:
            self.prep_started_at = now
            if update_fields is not None:
                update_fields.add('prep_started_at')
        elif self.status == 'ready' and not self.ready_at:
            self.ready_at = now
            if update_fields is not None:
                update_fields.add('ready_at')
        elif self.status in ['served', 'delivered'] and not self.served_at:
            self.served_at = now
            if update_fields is not None:
                update_fields.add('served_at')
        elif self.status == 'completed' and not self.completed_at:
            self.completed_at = now
            if update_fields is not None:
                update_fields.add('completed_at')
        elif self.status == 'cancelled' and not self.cancelled_at:
            self.cancelled_at = now
            if update_fields is not None:
                update_fields.add('cancelled_at')

        if update_fields is not None:
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.id} - {self.user.username}"

    def item_count(self):
        return sum(item.quantity for item in self.items.all())

    @property
    def prep_duration(self):
        if self.prep_started_at and self.ready_at:
            delta = self.ready_at - self.prep_started_at
            total_seconds = int(delta.total_seconds())
            if total_seconds < 0:
                return "—"
            mins = total_seconds // 60
            secs = total_seconds % 60
            if mins == 0:
                return f"{secs}s"
            elif secs == 0:
                return f"{mins} min{'s' if mins > 1 else ''}"
            else:
                return f"{mins}m {secs}s"
        elif self.prep_started_at and self.status == 'preparing':
            delta = timezone.now() - self.prep_started_at
            mins = max(0, int(delta.total_seconds()) // 60)
            return f"{mins} mins (cooking)"
        return "—"

    @property
    def timeline_steps(self):
        """
        Lifecycle steps:
        Customer Order -> Payment Successful -> Sent to Kitchen -> Preparing -> Ready -> Served -> Completed
        """
        pm = self.payments.first()
        payment_time = pm.created_at if pm else self.created_at
        is_paid = self.payment_status in ['paid', 'success']

        status_rank = {
            'pending': 1,
            'accepted': 3,
            'preparing': 4,
            'ready': 5,
            'served': 6,
            'delivered': 6,
            'completed': 7,
            'cancelled': -1,
        }
        current_rank = status_rank.get(self.status, 1)

        steps = [
            {
                'key': 'ordered',
                'label': 'Customer Order',
                'time': self.created_at,
                'status_state': 'completed',
                'info': f"Order placed by {self.user.get_full_name() or self.user.username}",
            },
            {
                'key': 'paid',
                'label': 'Payment Successful',
                'time': payment_time if is_paid else None,
                'status_state': 'completed' if is_paid else 'pending',
                'info': f"Prepaid ({self.payment_method})" if is_paid else "Payment pending",
            },
            {
                'key': 'sent_to_kitchen',
                'label': 'Sent to Kitchen',
                'time': self.sent_to_kitchen_at,
                'status_state': 'completed' if (self.sent_to_kitchen_at or current_rank >= 3) else ('active' if self.status == 'pending' else 'pending'),
                'info': f"Handled by {self.order_handler.get_full_name() or self.order_handler.username}" if self.order_handler else "Order Handler verified",
            },
            {
                'key': 'preparing',
                'label': 'Preparing',
                'time': self.prep_started_at,
                'status_state': 'completed' if (self.ready_at or current_rank >= 5) else ('active' if self.status == 'preparing' else 'pending'),
                'info': f"Chef {self.chef.get_full_name() or self.chef.username}" if self.chef else "Kitchen preparation",
            },
            {
                'key': 'ready',
                'label': 'Ready',
                'time': self.ready_at,
                'status_state': 'completed' if (self.served_at or current_rank >= 6) else ('active' if self.status == 'ready' else 'pending'),
                'info': f"Prepared in {self.prep_duration}" if (self.ready_at and self.prep_started_at) else "Food ready for service",
            },
            {
                'key': 'served',
                'label': 'Served',
                'time': self.served_at,
                'status_state': 'completed' if (self.served_at or current_rank >= 6) else 'pending',
                'info': f"Served by {self.assigned_waiter.get_full_name() or self.assigned_waiter.username}" if self.assigned_waiter else "Delivered to table/customer",
            },
            {
                'key': 'completed',
                'label': 'Completed',
                'time': self.completed_at,
                'status_state': 'completed' if (self.completed_at or self.status == 'completed') else 'pending',
                'info': "Order lifecycle finalized",
            },
        ]
        return steps

    def item_count(self):
        return sum(item.quantity for item in self.items.all())

    @property
    def payment_obj(self):
        return self.payments.first()

    @property
    def payment_status(self):
        pm = self.payments.first()
        if pm:
            return 'paid' if pm.status in ['success', 'paid'] else pm.status
        if self.reservation and self.reservation.payment_status:
            return self.reservation.payment_status
        # In TableServe prepaid workflow, orders in system are prepaid
        if self.status in ['accepted', 'preparing', 'ready', 'served', 'delivered', 'completed']:
            return 'paid'
        return 'paid'

    @property
    def payment_method(self):
        pm = self.payments.first()
        if pm:
            if pm.method in ['cod', 'cash']:
                return 'Cash'
            elif pm.method == 'upi':
                return 'UPI'
            elif pm.method == 'card':
                return 'Card'
            return pm.get_method_display()
        return 'Cash'

    class Meta:
        ordering = ['-created_at']


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=1)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    customization_details = models.TextField(blank=True, default='')

    def line_total(self):
        return self.quantity * self.price

    def __str__(self):
        return f"{self.quantity}x {self.menu_item.name}"



# ============================================================
# PAYMENTS
# ============================================================
class Payment(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]
    METHOD_CHOICES = [
        ('upi', 'UPI'),
        ('card', 'Card'),
        ('cod', 'Cash on Delivery'),
        ('netbanking', 'Net Banking'),
    ]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='payments')
    transaction_id = models.CharField(max_length=50, unique=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='upi')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.transaction_id:
            self.transaction_id = f"TXN-{random.randint(100000, 999999)}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.transaction_id} - ₹{self.amount}"

    class Meta:
        ordering = ['-created_at']


# ============================================================
# RESERVATIONS
# ============================================================
class Reservation(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('arrived', 'Arrived'),
        ('seated', 'Seated'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No Show'),
        ('rejected', 'Rejected'),
    ]
    RESERVATION_TYPE_CHOICES = [
        ('table', 'Table Reservation'),
        ('event', 'Event Reservation'),
        ('vip', 'VIP Reservation'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('refunded', 'Refunded'),
    ]
    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('upi', 'UPI'),
        ('card', 'Card'),
        ('netbanking', 'Net Banking'),
    ]
    REFUND_STATUS_CHOICES = [
        ('not_applicable', 'Not Applicable'),
        ('refund_pending', 'Refund Pending'),
        ('refund_initiated', 'Refund Initiated'),
        ('refund_completed', 'Refund Completed'),
        ('refund_failed', 'Refund Failed'),
        ('no_refund', 'No Refund'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reservations', null=True, blank=True)
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=15, blank=True)
    email = models.CharField(max_length=100, blank=True, default='')
    franchise = models.CharField(max_length=100, default='Main Branch', blank=True)
    date = models.DateField()
    time = models.TimeField()
    guests = models.IntegerField(default=2)
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name='reservations')
    assigned_waiter = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_reservations')
    reservation_type = models.CharField(max_length=20, choices=RESERVATION_TYPE_CHOICES, default='table')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    special_request = models.TextField(blank=True, default='')
    payment_method = models.CharField(max_length=50, choices=PAYMENT_METHOD_CHOICES, default='cash', blank=True)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='pending')
    payment_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    booking_charge = models.DecimalField(max_digits=10, decimal_places=2, default=200.00)
    pre_order_selected = models.BooleanField(default=False)
    pre_order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=200.00)
    razorpay_order_id = models.CharField(max_length=100, blank=True, default='')
    razorpay_payment_id = models.CharField(max_length=100, blank=True, default='')
    cancellation_date = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True, default='')
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    refund_status = models.CharField(max_length=30, choices=REFUND_STATUS_CHOICES, default='not_applicable')
    razorpay_refund_id = models.CharField(max_length=100, blank=True, default='')
    refund_failure_reason = models.TextField(blank=True, default='')
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.date} {self.time}"

    class Meta:
        ordering = ['-created_at']


# ============================================================
# OFFERS
# ============================================================
class Offer(models.Model):
    code = models.CharField(max_length=20, unique=True)
    title = models.CharField(max_length=200)
    discount = models.CharField(max_length=50)
    valid_till = models.DateField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} - {self.title}"

    class Meta:
        ordering = ['-created_at']


# ============================================================
# EVENT RESERVATIONS
# ============================================================
class Event(models.Model):
    EVENT_TYPES = [
        ('birthday', 'Birthday Party'),
        ('wedding', 'Wedding'),
        ('corporate', 'Corporate Event'),
        ('anniversary', 'Anniversary'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='events')
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=15)
    email = models.EmailField(blank=True)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES, default='other')
    event_date = models.DateField()
    event_time = models.TimeField()
    guests = models.IntegerField(default=10)
    venue = models.CharField(max_length=200, blank=True)
    special_requests = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.get_event_type_display()}"

    class Meta:
        ordering = ['-created_at']


# ============================================================
# ✅ INVENTORY — with StockIn/Out, Purchase Orders, Expiry & History
# ============================================================
class InventoryItem(models.Model):
    CATEGORY_CHOICES = [
        ('vegetables', 'Vegetables'),
        ('fruits', 'Fruits'),
        ('dairy', 'Dairy'),
        ('grains', 'Grains'),
        ('spices', 'Spices'),
        ('beverages', 'Beverages'),
        ('food', 'Food'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]

    name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit = models.CharField(max_length=20, default='kg')
    min_stock = models.DecimalField(max_digits=10, decimal_places=2, default=5)
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    supplier = models.CharField(max_length=200, blank=True)

    expiry_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active',
    )

    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.quantity} {self.unit})"

    @property
    def is_low_stock(self):
        return self.quantity <= self.min_stock

    @property
    def total_value(self):
        return self.quantity * self.price_per_unit

    @property
    def is_expired(self):
        if not self.expiry_date:
            return False
        from datetime import date
        return self.expiry_date < date.today()

    @property
    def is_expiring_soon(self):
        """Item expires within 7 days"""
        if not self.expiry_date:
            return False
        from datetime import date, timedelta
        return date.today() <= self.expiry_date <= date.today() + timedelta(days=7)

    @property
    def is_empty(self):
        return self.quantity <= 0

    @property
    def flowchart_status(self):
        if self.quantity <= 0:
            return "EMPTY"
        if self.is_expired or self.is_expiring_soon:
            return "ALERT"
        if self.is_low_stock:
            return "LOW"
        return "NORMAL"

    class Meta:
        ordering = ['name']
        verbose_name_plural = "Inventory Items"


class StockTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('add_stock', 'Add Stock (In)'),
        ('remove_stock', 'Remove Stock (Out / Waste)'),
        ('po_received', 'Purchase Order Received'),
        ('expired_removed', 'Expired Stock Disposal'),
        ('initial', 'Initial Setup'),
    ]
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPES)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    supplier = models.CharField(max_length=200, blank=True, default='')
    reason = models.CharField(max_length=255, blank=True, default='')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.item.name} - {self.get_transaction_type_display()} ({self.quantity})"

    class Meta:
        ordering = ['-created_at']


class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Order'),
        ('ordered', 'Placed with Supplier'),
        ('received', 'Received / Fulfilled'),
        ('cancelled', 'Cancelled'),
    ]
    po_number = models.CharField(max_length=50, unique=True)
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='purchase_orders')
    supplier = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit = models.CharField(max_length=20, default='kg')
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    expected_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    received_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.po_number} - {self.item.name} ({self.status})"

    class Meta:
        ordering = ['-created_at']


# ============================================================
# NGO PARTNERS
# ============================================================
class NGO(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ]
    name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=100)
    phone = models.CharField(max_length=15)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    food_preference = models.CharField(max_length=200, blank=True)
    meals_served = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    joined_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        verbose_name = "NGO"
        verbose_name_plural = "NGOs"


# ============================================================
# FOOD DONATIONS
# ============================================================
class FoodDonation(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('collected', 'Collected'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    FOOD_TYPE_CHOICES = [
        ('prepared', 'Prepared Food'),
        ('raw', 'Raw Material'),
        ('packaged', 'Packaged Food'),
        ('beverages', 'Beverages'),
    ]

    ngo = models.ForeignKey(NGO, on_delete=models.SET_NULL, null=True, blank=True, related_name='donations')
    food_items = models.TextField(help_text="Food item details & description")
    food_type = models.CharField(max_length=30, choices=FOOD_TYPE_CHOICES, default='prepared')
    quantity_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    meals_count = models.IntegerField(default=0, help_text="Number of meals provided")
    expiry_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        ngo_name = self.ngo.name if self.ngo else 'Unassigned'
        return f"Donation #{self.id} - {ngo_name} ({self.meals_count} meals)"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Food Donation"
        verbose_name_plural = "Food Donations"



# ============================================================
# EVENT THEMES
# ============================================================
class EventTheme(models.Model):
    name = models.CharField(max_length=100, unique=True)
    image = models.ImageField(upload_to='event_themes/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        verbose_name = "Event Theme"
        verbose_name_plural = "Event Themes"


# ============================================================
# KITCHEN & WAITER NOTIFICATIONS
# ============================================================
class KitchenNotification(models.Model):
    NOTIFICATION_TYPES = [
        ('new_order', 'New Order'),
        ('rush_order', 'RUSH Order'),
        ('order_cancelled', 'Order Cancelled'),
        ('order_ready', 'Order Ready'),
        ('order_updated', 'Order Updated'),
    ]
    ROLE_CHOICES = [
        ('Kitchen', 'Kitchen'),
        ('Waiter', 'Waiter'),
        ('All', 'All Staff'),
    ]

    target_role = models.CharField(max_length=30, choices=ROLE_CHOICES, default='Kitchen')
    notification_type = models.CharField(max_length=30, choices=NOTIFICATION_TYPES, default='new_order')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='kitchen_notifications', null=True, blank=True)
    title = models.CharField(max_length=200, default='')
    message = models.CharField(max_length=255)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.target_role}] {self.title or self.notification_type}: {self.message}"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Kitchen Notification"
        verbose_name_plural = "Kitchen Notifications"


# ============================================================
# CUSTOMER REVIEW & RATING
# ============================================================
class CustomerReview(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='customer_reviews')
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviews')
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Rating between 1 and 5 stars"
    )
    title = models.CharField(max_length=150, blank=True, default='')
    comment = models.TextField(blank=True, default='')
    is_approved = models.BooleanField(default=True, help_text="Approved reviews are counted in public statistics")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.rating}★ ({self.created_at.strftime('%d %b %Y')})"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Customer Review"
        verbose_name_plural = "Customer Reviews"
        constraints = [
            models.UniqueConstraint(
                fields=['order'],
                condition=models.Q(order__isnull=False),
                name='unique_order_review'
            )
        ]