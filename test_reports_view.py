import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'restaurant_project.settings')
django.setup()

from django.test import RequestFactory
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from restaurant.staff_views import staff_reports

def test():
    factory = RequestFactory()
    request = factory.get('/staff/reports/')
    
    manager = User.objects.filter(is_superuser=True).first()
    if not manager:
        manager = User.objects.filter(is_staff=True).first()
    
    request.user = manager
    setattr(request, 'session', {})
    messages = FallbackStorage(request)
    setattr(request, '_messages', messages)

    response = staff_reports(request)
    print("Status code:", response.status_code)
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    print("Reports View rendered successfully with status code 200!")

if __name__ == "__main__":
    test()
