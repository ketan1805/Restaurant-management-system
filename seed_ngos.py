import os
import django
from decimal import Decimal
from datetime import date, timedelta

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'restaurant_project.settings')
django.setup()

from restaurant.models import NGO, FoodDonation

def seed():
    ngos_data = [
        {
            "name": "Robin Hood Army",
            "contact_person": "Anand Sharma",
            "phone": "+91 98765 12345",
            "email": "contact@robinhoodarmy.org",
            "address": "Navrangpura, Ahmedabad",
            "food_preference": "Cooked Meals & Prepared Food",
            "meals_served": 450,
            "status": "active"
        },
        {
            "name": "Annakshetra Foundation",
            "contact_person": "Priya Patel",
            "phone": "+91 98234 56789",
            "email": "info@annakshetra.org",
            "address": "CG Road, Ahmedabad",
            "food_preference": "Raw Ingredients & Vegetables",
            "meals_served": 320,
            "status": "active"
        },
        {
            "name": "Akshaya Patra Alliance",
            "contact_person": "Ramesh Shah",
            "phone": "+91 97123 45678",
            "email": "ahmedabad@akshayapatra.org",
            "address": "SG Highway, Ahmedabad",
            "food_preference": "Any Food & Packaged Items",
            "meals_served": 680,
            "status": "active"
        },
        {
            "name": "Food For Needy Trust",
            "contact_person": "Sanjay Mehta",
            "phone": "+91 99090 88776",
            "email": "support@foodforneedy.org",
            "address": "Maninagar, Ahmedabad",
            "food_preference": "Pure Veg Meals",
            "meals_served": 190,
            "status": "active"
        }
    ]

    ngo_objs = []
    for data in ngos_data:
        ngo, created = NGO.objects.get_or_create(
            name=data["name"],
            defaults=data
        )
        if not created:
            for k, v in data.items():
                setattr(ngo, k, v)
            ngo.save()
        ngo_objs.append(ngo)

    donations_data = [
        {
            "ngo": ngo_objs[0],
            "food_items": "45 Packets Veg Paneer Biryani & Raita",
            "food_type": "prepared",
            "quantity_kg": Decimal("18.5"),
            "meals_count": 45,
            "expiry_date": date.today() + timedelta(days=1),
            "status": "completed",
            "notes": "Picked up from back kitchen door by Anand Sharma."
        },
        {
            "ngo": ngo_objs[1],
            "food_items": "25 KG Raw Tomatoes & Fresh Vegetables",
            "food_type": "raw",
            "quantity_kg": Decimal("25.0"),
            "meals_count": 50,
            "expiry_date": date.today() + timedelta(days=3),
            "status": "collected",
            "notes": "Delivered to Annakshetra community kitchen."
        },
        {
            "ngo": ngo_objs[2],
            "food_items": "30 Liters Surplus Soup & Bread Baskets",
            "food_type": "prepared",
            "quantity_kg": Decimal("15.0"),
            "meals_count": 35,
            "expiry_date": date.today(),
            "status": "approved",
            "notes": "Ready for pickup tonight before 9:30 PM."
        },
        {
            "ngo": ngo_objs[3],
            "food_items": "50 Packets Assorted Bakery Items & Muffins",
            "food_type": "packaged",
            "quantity_kg": Decimal("12.0"),
            "meals_count": 50,
            "expiry_date": date.today() + timedelta(days=2),
            "status": "pending",
            "notes": "Event surplus from afternoon corporate booking."
        }
    ]

    for d_data in donations_data:
        FoodDonation.objects.get_or_create(
            food_items=d_data["food_items"],
            defaults=d_data
        )

    print(f"Seeded {len(ngo_objs)} NGOs and {len(donations_data)} Food Donations successfully.")

if __name__ == "__main__":
    seed()
