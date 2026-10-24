# TableServe — Restaurant Management System

A full-featured Restaurant Management System built with Django, featuring Customer Ordering, Table & Event Reservations, Staff & Role Management, Kitchen Display System (KDS), Inventory & Waste Management, and Comprehensive Analytics.

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- **Python 3.10+** (Tested with Python 3.13)
- Windows PowerShell or Command Prompt

### 2. Activate Virtual Environment & Run
You can start the server immediately with the provided script:
```powershell
.\run_server.bat
```
Or manually:
```powershell
# 1. Activate venv
.\venv\Scripts\Activate.ps1

# 2. Run migrations (already up to date)
python manage.py migrate

# 3. Start development server
python manage.py runserver
```

---

## 🔑 Login Portals & Pre-configured Credentials

> **OTP Note in Development:**
> During local development, the 6-digit OTP is automatically printed in your terminal console AND displayed in the top green notification toast on screen.

### 1. Admin Portal
- **Local URL**: `http://127.0.0.1:8000/admin-login/`
-Login Credentials: For demo access, please contact the project author.
-Access: Restaurant administrative dashboard, financial reports, user management, NGO food donation tracking, and other     administrative features.

### 2. Staff Portals (Manager / Waiter / Kitchen / Order Handler)
- **URL**: `http://127.0.0.1:8000/login/`

| Role | Username | Password | Direct Portal |
| :--- | :--- | :--- | :--- |
| **Manager** | `amit_sharma` | `Staff@123` | `/staff/manager/` |
| **Kitchen Manager** | `kitchen_manager` | `Manager@123` | `/staff/manager/` |
| **Chef / Kitchen Staff** | `chef_mario` | `Chef@123` | `/kitchen/` |
| **Waiter** | `jayesh_parmar` | `Staff@123` | `/staff/waiter/` |
| **Order Handler** | `sanjay_prajapati` | `Staff@123` | `/staff/order-handler/` |

### 3. Customer Portal
- **URL**: `http://127.0.0.1:8000/login/`
- **Username**: `ramesh` (or any seeded customer: `neha`, `vikram`, `pooja`)
- **Password**: `Customer@123`
- *Access*: View menu, reserve tables, book themes/events, place orders, track status.

### 4. Django Built-in Admin
- **URL**: `http://127.0.0.1:8000/django-admin/`
- **Username**: `admin`
- **Password**: `admin@1817`

---

## 🛠️ Management & Seeding Scripts

If you ever wish to re-seed or populate mockup data, use the seed scripts:
```powershell
python seed_staff.py         # Seeds staff accounts & role groups
python seed_customers.py     # Seeds sample customer records
python seed_kitchen_data.py  # Seeds kitchen stations, chef availability & tickets
python seed_ngos.py          # Seeds NGO partners and food donation logs
python seed_bills.py         # Seeds sample orders & billing transactions
```

---

## 🧪 Running Tests
```powershell
python test_staff_view.py
python test_reports_view.py
python test_ngo_view.py
```

---

## 📁 Project Directory Structure
```
Restaurant Project/
├── db.sqlite3                 # SQLite Database (fully migrated)
├── manage.py                  # Django Management Script
├── media/                     # Uploaded files (event themes, menu items)
├── restaurant/                # Main Django Application
│   ├── admin.py               # Django Admin configuration
│   ├── forms.py               # Forms (Login, Registration, etc.)
│   ├── kitchen_models.py      # Kitchen Display System models
│   ├── kitchen_views.py       # Kitchen live order boards & ticket management
│   ├── models.py              # Core models (Orders, Tables, Reservations, Menu, Staff)
│   ├── staff_views.py         # Staff portal views (Manager, Waiter, Order Handler)
│   ├── static/                # Static assets (CSS, images, icons)
│   ├── templates/             # HTML templates (Customer, Admin, Staff, Kitchen)
│   ├── urls.py                # App routing
│   └── views.py               # Main customer views, auth & OTP flows
├── restaurant_project/        # Django Project configuration
│   ├── settings.py            # Global settings
│   ├── urls.py                # Main URL routing
│   └── wsgi.py                # WSGI entry point
├── requirements.txt           # Project dependencies
├── run_server.bat             # One-click Windows launch script
└── venv/                      # Local Python 3.13 virtual environment
```
