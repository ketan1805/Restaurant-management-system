import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'restaurant_project.settings')
django.setup()

from django.contrib.auth.models import User, Group
from restaurant.models import Profile

groups = ['Manager', 'Kitchen Staff', 'Waiter', 'OrderHandler']
for g in groups:
    Group.objects.get_or_create(name=g)

mockup_staff = [
    {'username': 'amit_sharma', 'email': 'amit.sharma@restaurant.com', 'first_name': 'Amit', 'last_name': 'Sharma', 'role': 'Manager', 'phone': '+91 98765 43210', 'active': True},
    {'username': 'ravi_kumar', 'email': 'ravi.kumar@restaurant.com', 'first_name': 'Ravi', 'last_name': 'Kumar', 'role': 'Manager', 'phone': '+91 98765 43211', 'active': True},
    {'username': 'suresh_patel', 'email': 'suresh.patel@restaurant.com', 'first_name': 'Suresh', 'last_name': 'Patel', 'role': 'Kitchen Staff', 'phone': '+91 98765 43212', 'active': True},
    {'username': 'vikram_thakor', 'email': 'vikram.thakor@restaurant.com', 'first_name': 'Vikram', 'last_name': 'Thakor', 'role': 'Kitchen Staff', 'phone': '+91 98765 43213', 'active': True},
    {'username': 'jayesh_parmar', 'email': 'jayesh.parmar@restaurant.com', 'first_name': 'Jayesh', 'last_name': 'Parmar', 'role': 'Waiter', 'phone': '+91 98765 43214', 'active': True},
    {'username': 'neha_desai', 'email': 'neha.desai@restaurant.com', 'first_name': 'Neha', 'last_name': 'Desai', 'role': 'Waiter', 'phone': '+91 98765 43215', 'active': True},
    {'username': 'sanjay_prajapati', 'email': 'sanjay.prajapati@restaurant.com', 'first_name': 'Sanjay', 'last_name': 'Prajapati', 'role': 'OrderHandler', 'phone': '+91 98765 43216', 'active': True},
    {'username': 'pooja_dholakiya', 'email': 'pooja.dholakiya@restaurant.com', 'first_name': 'Pooja', 'last_name': 'Dholakiya', 'role': 'OrderHandler', 'phone': '+91 98765 43217', 'active': False},
]

for item in mockup_staff:
    user, created = User.objects.get_or_create(
        username=item['username'],
        defaults={
            'email': item['email'],
            'first_name': item['first_name'],
            'last_name': item['last_name'],
            'is_staff': True,
            'is_superuser': False,
            'is_active': item['active'],
        }
    )
    if not created:
        user.email = item['email']
        user.first_name = item['first_name']
        user.last_name = item['last_name']
        user.is_staff = True
    user.set_password('Staff@123')
    user.save()
    
    # Ensure profile
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.mobile_no = item['phone']
    profile.save()

    # Ensure role group
    user.groups.clear()
    g_obj = Group.objects.get(name=item['role'])
    user.groups.add(g_obj)

print("Successfully seeded staff data!")
