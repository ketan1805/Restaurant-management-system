import os
import django
from decimal import Decimal
from datetime import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'restaurant_project.settings')
django.setup()

from django.contrib.auth.models import User
from restaurant.models import Order, OrderItem, MenuItem, MenuCategory, Payment

category, _ = MenuCategory.objects.get_or_create(name='Main Course')
item1, _ = MenuItem.objects.get_or_create(name='Paneer Butter Masala', defaults={'category': category, 'price': Decimal('180.00')})
item2, _ = MenuItem.objects.get_or_create(name='Butter Naan', defaults={'category': category, 'price': Decimal('35.00')})
item3, _ = MenuItem.objects.get_or_create(name='Jeera Rice', defaults={'category': category, 'price': Decimal('120.00')})

mockup_bills = [
    {'code': 'B001', 'name': 'Ramesh Patel', 'date': '2026-09-27 14:32:00', 'amount': Decimal('250.00'), 'method': 'cod', 'method_name': 'Cash'},
    {'code': 'B002', 'name': 'Neha Shah', 'date': '2026-09-27 12:15:00', 'amount': Decimal('180.00'), 'method': 'upi', 'method_name': 'UPI'},
    {'code': 'B003', 'name': 'Vikram Joshi', 'date': '2026-09-27 10:05:00', 'amount': Decimal('320.00'), 'method': 'card', 'method_name': 'Card'},
    {'code': 'B004', 'name': 'Pooja Desai', 'date': '2026-09-26 18:45:00', 'amount': Decimal('150.00'), 'method': 'cod', 'method_name': 'Cash'},
    {'code': 'B005', 'name': 'Amit Thakor', 'date': '2026-09-26 17:20:00', 'amount': Decimal('420.00'), 'method': 'upi', 'method_name': 'UPI'},
    {'code': 'B006', 'name': 'Kavita Shah', 'date': '2026-09-26 16:10:00', 'amount': Decimal('290.00'), 'method': 'cod', 'method_name': 'Cash'},
]

for b in mockup_bills:
    fn, *ln = b['name'].split(' ')
    ln_str = ln[0] if ln else ''
    uname = b['name'].lower().replace(' ', '_')
    user, _ = User.objects.get_or_create(username=uname, defaults={'first_name': fn, 'last_name': ln_str, 'email': f"{uname}@example.com"})
    
    dt = datetime.strptime(b['date'], '%Y-%m-%d %H:%M:%S')
    order = Order.objects.create(
        user=user,
        status='delivered',
        total=b['amount'],
        order_type='restaurant',
    )
    # override created_at
    Order.objects.filter(id=order.id).update(created_at=dt)
    
    # Add order items
    OrderItem.objects.create(order=order, menu_item=item1, quantity=1, price=item1.price)
    
    # Create Payment
    Payment.objects.create(
        order=order,
        amount=b['amount'],
        method=b['method'],
        status='success',
        transaction_id=f"TXN-{b['code']}"
    )

print("Successfully seeded billing data!")
