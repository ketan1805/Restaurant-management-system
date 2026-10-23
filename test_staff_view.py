import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'restaurant_project.settings')
django.setup()

from django.test import RequestFactory
from django.contrib.auth.models import User
from restaurant.staff_views import staff_customers

manager = User.objects.filter(groups__name='Manager').first()

rf = RequestFactory()
request = rf.get('/staff/customers/')
request.user = manager

response = staff_customers(request)
print("HTTP Status Code:", response.status_code)
print("Staff customers view executed successfully without errors!")
