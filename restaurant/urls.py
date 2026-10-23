from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static
from . import views
from . import staff_urls

urlpatterns = [
    # ============================================================
    # HOME
    # ============================================================
    path('', views.home_view, name='home'),
    path('about/', views.about_view, name='about'),

    # ============================================================
    # AUTH
    # ============================================================
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),

    # ---------- SIGNUP OTP ----------
    path('verify-signup-otp/<int:user_id>/',
         views.verify_signup_otp_view,
         name='verify_signup_otp'),

    # ---------- LOGIN OTP ----------
    path('verify-login-otp/<int:user_id>/',
         views.verify_login_otp_view,
         name='verify_login_otp'),

    # ---------- FORGOT PASSWORD OTP ----------
    path('forgot-password/',
         views.forgot_password_view,
         name='forgot_password'),
    path('verify-forgot-otp/<int:user_id>/',
         views.verify_forgot_otp_view,
         name='verify_forgot_otp'),
    path('set-new-password/<int:user_id>/',
         views.set_new_password_view,
         name='set_new_password'),

    # ---------- CHANGE PASSWORD (logged-in) ----------
    path('change-password/',
        auth_views.PasswordChangeView.as_view(
            template_name='restaurant/change_password.html',
            success_url='/password-change/done/'
        ),
        name='password_change'
    ),
    path('password-change/done/',
        auth_views.PasswordChangeDoneView.as_view(
            template_name='restaurant/password_change_done.html'
        ),
        name='password_change_done'
    ),

    # ============================================================
    # ADMIN PANEL
    # ============================================================

    # ---------- ADMIN AUTH ----------
    path('admin-login/',
         views.admin_login_view,
         name='admin_login'),
    path('admin-otp/',
         views.admin_otp_view,
         name='admin_otp_verify'),
    path('admin-logout/',
         views.admin_logout_view,
         name='admin_logout'),

    # ---------- ADMIN DASHBOARD ----------
    path('admin-panel/',
         views.admin_dashboard,
         name='admin_dashboard'),

    # ---------- ADMIN TABLE RESERVATIONS ----------
    path('admin-panel/reservations/',
         views.admin_reservations,
         name='admin_reservations'),
    path('admin-panel/reservations/check-tables/',
         views.admin_check_tables,
         name='admin_check_tables'),

    # ---------- ADMIN EVENT RESERVATIONS ----------
    path('admin-panel/events/',
         views.admin_events,
         name='admin_events'),

    # ---------- ADMIN ORDERS ----------
    path('admin-panel/orders/',
         views.admin_orders,
         name='admin_orders'),
    path('admin-panel/orders/<int:order_id>/',
         views.admin_order_details,
         name='admin_order_details'),

    # ---------- ADMIN MENU ITEMS ----------
    path('admin-panel/menu/',
         views.admin_menu,
         name='admin_menu'),

    # ---------- ADMIN STAFF LIST ----------
    path('admin-panel/staff-list/',
         views.admin_staff_list,
         name='admin_staff_list'),

    # ---------- ADMIN CUSTOMERS ----------
    path('admin-panel/customers/',
         views.admin_customers,
         name='admin_customers'),

    # ---------- ADMIN INVENTORY ----------
    path('admin-panel/inventory/',
         views.admin_inventory,
         name='admin_inventory'),

    # ---------- ADMIN REPORTS ----------
    path('admin-panel/reports/',
         views.admin_reports,
         name='admin_reports'),

    # ---------- ADMIN NGOs ----------
    path('admin-panel/ngos/',
         views.admin_ngos,
         name='admin_ngos'),

    # ---------- ADMIN ADD STAFF ----------
    path('admin-panel/staff/add/',
         views.admin_add_staff,
         name='admin_add_staff'),

    # ---------- ADMIN DELETE STAFF ----------
    path('admin-panel/staff/<int:user_id>/delete/',
         views.admin_delete_staff,
         name='admin_delete_staff'),

    # ============================================================
    # EXISTING ADMIN URLs (keep for backward compatibility)
    # ============================================================
    path('admin-panel/tables/',
         views.admin_tables,
         name='admin_tables'),
    path('admin-panel/offers/',
         views.admin_offers,
         name='admin_offers'),
    path('admin-panel/payments/',
         views.admin_payments,
         name='admin_payments'),
    path('admin-panel/users/',
         views.admin_users,
         name='admin_users'),
    path('admin-panel/settings/',
         views.admin_settings,
         name='admin_settings'),

    # ============================================================
    # STAFF PANEL — separate app (uses staff_urls.py)
    # ============================================================
    path('staff/', include('restaurant.staff_urls')),

      # ============================================================
      # SEPARATE CUSTOMER PAGES: MY BOOKINGS & MY ORDERS
      # ============================================================
      path('my-bookings/', views.my_bookings_view, name='my_bookings'),
      path('my-orders/', views.my_orders_view, name='my_orders'),
      path('customer-panel/', views.customer_dashboard, name='customer_dashboard'),
      path('customer-panel/menu/', views.customer_menu, name='customer_menu'),
      path('customer-panel/orders/', views.my_orders_view, name='customer_orders'),
      path('customer-panel/reservations/', views.my_bookings_view, name='customer_reservations'),
      path('customer-panel/profile/', views.customer_profile, name='customer_profile'),
      path('reviews/submit/', views.submit_customer_review, name='submit_customer_review'),

     # ============================================================
     # PUBLIC FOOD ORDER & TABLE BOOKING FLOW
     # ============================================================
     path('food-order/', views.public_food_order_view, name='food_order_view'),
     path('food-order/item-details/<int:item_id>/', views.get_item_customizations_api, name='get_item_customizations_api'),
     path('cart/', views.cart_view, name='cart_view'),
     path('cart/add/', views.add_to_cart_view, name='add_to_cart_view'),
     path('checkout/', views.checkout_view, name='checkout_view'),
     path('api/create-razorpay-order/', views.api_create_razorpay_order, name='api_create_razorpay_order'),
     path('api/verify-razorpay-payment/', views.api_verify_razorpay_payment, name='api_verify_razorpay_payment'),
     path('order-confirmation/<int:order_id>/', views.order_confirmation_view, name='order_confirmation_view'),
     path('table-booking/', views.public_table_booking_view, name='table_booking_view'),
     path('table-booking/pre-order-menu/', views.pre_order_menu_view, name='pre_order_menu_view'),
     path('table-booking/summary/', views.table_booking_summary_view, name='table_booking_summary_view'),
     path('table-booking/check-tables/', views.api_check_tables, name='api_check_tables'),
     path('api/create-booking-razorpay-order/', views.api_create_booking_razorpay_order, name='api_create_booking_razorpay_order'),
     path('api/verify-booking-razorpay-payment/', views.api_verify_booking_razorpay_payment, name='api_verify_booking_razorpay_payment'),
     path('api/pre-order-cart/update/', views.api_update_pre_order_cart, name='api_update_pre_order_cart'),
     path('booking-confirmation/<int:booking_id>/', views.booking_confirmation_view, name='booking_confirmation_view'),
     path('table-booking/<int:booking_id>/cancel/', views.table_booking_cancel_view, name='table_booking_cancel_view'),
     path('table-booking/<int:booking_id>/cancel/confirm/', views.api_confirm_table_booking_cancellation, name='api_confirm_table_booking_cancellation'),
     path('table-booking/<int:booking_id>/cancellation-confirmation/', views.table_booking_cancellation_confirmation_view, name='table_booking_cancellation_confirmation_view'),
]


# ============================================================
# ✅ MEDIA FILES (Development) — EventTheme images, MenuItem images, etc.
# ============================================================
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)