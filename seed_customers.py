import os
import django
from datetime import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'restaurant_project.settings')
django.setup()

from django.contrib.auth.models import User
from restaurant.models import Profile

mockup_customers = [
    {'name': 'Ramesh Patel', 'phone': '+91 98765 43210', 'email': 'ramesh@gmail.com', 'date': '2026-09-12', 'active': True},
    {'name': 'Neha Shah', 'phone': '+91 87654 32109', 'email': 'neha@gmail.com', 'date': '2026-09-11', 'active': True},
    {'name': 'Vikram Joshi', 'phone': '+91 90123 45678', 'email': 'vikram@gmail.com', 'date': '2026-09-10', 'active': True},
    {'name': 'Pooja Desai', 'phone': '+91 95555 12345', 'email': 'pooja@gmail.com', 'date': '2026-09-09', 'active': True},
    {'name': 'Amit Thakor', 'phone': '+91 98211 22334', 'email': 'amit@gmail.com', 'date': '2026-09-08', 'active': True},
    {'name': 'Kavita Shah', 'phone': '+91 94000 66778', 'email': 'kavita@gmail.com', 'date': '2026-09-07', 'active': True},
    {'name': 'Sanjay Parmar', 'phone': '+91 98711 55667', 'email': 'sanjay@gmail.com', 'date': '2026-09-06', 'active': True},
    {'name': 'Dhara Mehta', 'phone': '+91 95123 44556', 'email': 'dhara@gmail.com', 'date': '2026-09-05', 'active': True},
    {'name': 'Hitesh Solanki', 'phone': '+91 99788 77665', 'email': 'hitesh@gmail.com', 'date': '2026-09-03', 'active': False},
    {'name': 'Trupti Patel', 'phone': '+91 91234 56789', 'email': 'trupti@gmail.com', 'date': '2026-09-01', 'active': False},
    {'name': 'Chirag Vora', 'phone': '+91 98111 22334', 'email': 'chirag@gmail.com', 'date': '2026-08-28', 'active': True},
    {'name': 'Bhavna Randeria', 'phone': '+91 97222 33445', 'email': 'bhavna@gmail.com', 'date': '2026-08-25', 'active': True},
]

for c in mockup_customers:
    fn, *ln = c['name'].split(' ')
    ln_str = ln[0] if ln else ''
    uname = c['email'].split('@')[0]
    
    user, created = User.objects.get_or_create(
        username=uname,
        defaults={
            'first_name': fn,
            'last_name': ln_str,
            'email': c['email'],
            'is_staff': False,
            'is_superuser': False,
            'is_active': c['active'],
        }
    )
    if not created:
        user.first_name = fn
        user.last_name = ln_str
        user.email = c['email']
        user.is_staff = False
        user.is_active = c['active']
        user.save()
    user.set_password('Customer@123')
    user.save()

    dt = datetime.strptime(c['date'], '%Y-%m-%d')
    User.objects.filter(id=user.id).update(date_joined=dt)

    profile, _ = Profile.objects.get_or_create(user=user)
    profile.mobile_no = c['phone']
    profile.save()

print("Successfully seeded customer records!")
