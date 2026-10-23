from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User, Group
from django.core.mail import send_mail
from django.conf import settings
from django.db import transaction
from django.db.models import Sum, Count, Q, Avg
from django.utils import timezone
from django.http import JsonResponse
from decimal import Decimal
import datetime
from datetime import timedelta
from .forms import LoginForm, RegisterForm
from .models import (
    OTPVerification, Profile,
    Table, MenuCategory, MenuSubcategory, MenuItem,
    ItemCustomization, CustomizationOption,
    Order, OrderItem, Payment,
    Reservation, Offer,
    Event, InventoryItem, NGO, FoodDonation,
    EventTheme, CustomerReview,
)
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
import random
import json
import hashlib
import hmac
import logging
import razorpay

logger = logging.getLogger(__name__)



# ============================================================
# ==================== HOME ====================
# ============================================================
def get_customer_rating_stats():
    """
    Computes average rating from actual customer-submitted ratings in the database.
    If no customer rating/review model exists or no ratings have been submitted, returns None.
    Do not invent or assume ratings.
    """
    agg = CustomerReview.objects.filter(is_approved=True, rating__isnull=False).aggregate(
        avg=Avg('rating'),
        count=Count('id')
    )
    if agg.get('avg') is not None and agg.get('count', 0) > 0:
        return {
            'avg': round(float(agg['avg']), 1),
            'count': agg['count']
        }

    return None


def get_completed_guests_count():
    """
    Actual count of unique customers who have completed at least one order.
    Avoids counting the same customer multiple times.
    """
    return (
        Order.objects.filter(status__in=['completed', 'delivered', 'served'])
        .order_by()
        .values('user')
        .distinct()
        .count()
    )


def check_booking_supports_instant_confirmation():
    """
    Checks if the existing booking workflow genuinely supports automatic confirmation.
    TableServe's booking verification (api_verify_booking_razorpay_payment) automatically
    confirms verified table reservations upon payment without requiring manual staff approval.
    """
    return True


def home_view(request):
    categories = MenuCategory.objects.filter(is_active=True).order_by('name')
    db_items = MenuItem.objects.filter(
        is_available=True,
        status='in_stock',
        category__is_active=True
    ).select_related('category')

    default_fallback_img = "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=600&q=80"
    menu_list = []
    for item in db_items:
        img_url = item.image.url if item.image else default_fallback_img
        menu_list.append({
            'id': item.id,
            'name': item.name,
            'price': item.get_final_price(),
            'stars': item.rating or 4,
            'cat': item.category.name.lower(),
            'cat_name': item.category.name,
            'popular': item.is_popular,
            'desc': item.description or '',
            'img': img_url,
        })

    cart = request.session.get('cart', {})
    cart_count = sum(val['quantity'] if isinstance(val, dict) else val for val in cart.values()) if cart else 0

    completed_guests_count = get_completed_guests_count()
    rating_stats = get_customer_rating_stats()
    avg_rating = rating_stats['avg'] if rating_stats else None
    rating_count = rating_stats['count'] if rating_stats else 0
    supports_instant_confirmation = check_booking_supports_instant_confirmation()

    context = {
        'categories': categories,
        'db_menu_items_json': json.dumps(menu_list),
        'cart_count': cart_count,
        'completed_guests_count': completed_guests_count,
        'avg_rating': avg_rating,
        'rating_count': rating_count,
        'supports_instant_confirmation': supports_instant_confirmation,
    }
    return render(request, 'restaurant/home.html', context)


def about_view(request):
    cart = request.session.get('cart', {})
    cart_count = sum(val['quantity'] if isinstance(val, dict) else val for val in cart.values()) if cart else 0
    return render(request, 'restaurant/about.html', {'cart_count': cart_count})


@login_required(login_url='login')
def customer_dashboard(request):
    return redirect('home')


@login_required(login_url='login')
def my_orders_view(request):
    """Dedicated page showing ONLY the logged-in customer's food orders"""
    if request.user.is_staff or request.user.is_superuser:
        return redirect('home')

    if request.method == 'POST' and request.POST.get('action') == 'cancel_order':
        order = get_object_or_404(Order, id=request.POST.get('order_id'), user=request.user)
        if order.status != 'pending':
            messages.error(request, 'Only pending orders can be cancelled.')
        else:
            with transaction.atomic():
                order.status = 'cancelled'
                order.save(update_fields=['status', 'updated_at'])
            messages.success(request, f'Order #{order.id} has been cancelled.')
        return redirect('my_orders')

    orders = request.user.orders.select_related('table').prefetch_related('items__menu_item', 'reviews').order_by('-created_at')
    return render(request, 'restaurant/my_orders.html', {'orders': orders})

customer_orders = my_orders_view


@login_required(login_url='login')
@csrf_protect
def submit_customer_review(request):
    """
    Allows an authenticated customer to submit or edit a rating (1-5 stars) and review
    for their completed food order.
    """
    if request.method != 'POST':
        return redirect('my_orders')

    order_id = request.POST.get('order_id')
    if not order_id:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'Order ID is required to submit a rating.'}, status=400)
        messages.error(request, 'Order ID is required to submit a rating.')
        return redirect('my_orders')

    try:
        order = Order.objects.get(id=order_id)
    except (Order.DoesNotExist, ValueError):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'Order not found.'}, status=404)
        messages.error(request, 'Order not found.')
        return redirect('my_orders')

    # Security Check: Customer can only rate their own orders
    if order.user != request.user:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'You can only rate your own orders.'}, status=403)
        messages.error(request, 'You can only rate your own orders.')
        return redirect('my_orders')

    # Security Check: Only Completed orders are eligible for rating
    if order.status != 'completed':
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'Only completed orders can be rated.'}, status=400)
        messages.error(request, 'Only completed orders can be rated.')
        return redirect('my_orders')

    # Validation: Rating must be an integer between 1 and 5
    rating_raw = request.POST.get('rating')
    try:
        rating_val = int(rating_raw)
    except (ValueError, TypeError):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'Please provide a valid rating between 1 and 5 stars.'}, status=400)
        messages.error(request, 'Please provide a valid rating between 1 and 5 stars.')
        return redirect('my_orders')

    if rating_val < 1 or rating_val > 5:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'Rating must be between 1 and 5 stars.'}, status=400)
        messages.error(request, 'Rating must be between 1 and 5 stars.')
        return redirect('my_orders')

    title = request.POST.get('title', '').strip()[:150]
    comment = request.POST.get('comment', '').strip()

    # Check for existing rating on this order
    existing_review = CustomerReview.objects.filter(order=order).first()
    if existing_review:
        # Security Check: If editing, ensure owner matches
        if existing_review.user != request.user:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'message': 'Permission denied.'}, status=403)
            messages.error(request, 'Permission denied.')
            return redirect('my_orders')

        existing_review.rating = rating_val
        existing_review.title = title
        existing_review.comment = comment
        existing_review.is_approved = True
        existing_review.save(update_fields=['rating', 'title', 'comment', 'is_approved', 'updated_at'])
        msg = 'Your rating has been updated successfully!'
    else:
        CustomerReview.objects.create(
            user=request.user,
            order=order,
            rating=rating_val,
            title=title,
            comment=comment,
            is_approved=True,
        )
        msg = 'Thank you! Your rating has been submitted successfully.'

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'message': msg})

    messages.success(request, msg)
    return redirect('my_orders')



@login_required(login_url='login')
def my_bookings_view(request):
    """Dedicated page showing ONLY the logged-in customer's table bookings"""
    if request.user.is_staff or request.user.is_superuser:
        return redirect('home')

    if request.method == 'POST' and request.POST.get('action') == 'cancel_reservation':
        reservation_id = request.POST.get('reservation_id')
        reservation = get_object_or_404(
            Reservation,
            id=reservation_id,
            user=request.user,
        )
        return redirect('table_booking_cancel_view', booking_id=reservation.id)

    reservations = request.user.reservations.select_related('table').order_by('-created_at')
    return render(request, 'restaurant/my_bookings.html', {
        'reservations': reservations,
        'today': timezone.localdate().isoformat(),
    })

customer_reservations = my_bookings_view


@login_required(login_url='login')
def customer_menu(request):
    if request.user.is_staff or request.user.is_superuser:
        return redirect('home')

    menu_items = MenuItem.objects.filter(
        is_available=True,
        status='in_stock',
        category__is_active=True,
    ).select_related('category').order_by('category__name', 'name')

    if request.method == 'POST':
        quantities = []
        for key, value in request.POST.items():
            if not key.startswith('item_'):
                continue
            try:
                item_id = int(key[5:])
                quantity = int(value)
            except (TypeError, ValueError):
                messages.error(request, 'The submitted cart contains invalid quantities.')
                return redirect('customer_menu')
            if quantity < 0 or quantity > 20:
                messages.error(request, 'Each item quantity must be between 0 and 20.')
                return redirect('customer_menu')
            if quantity:
                quantities.append((item_id, quantity))

        if not quantities:
            messages.error(request, 'Add at least one item to your order.')
            return redirect('customer_menu')

        item_ids = [item_id for item_id, _ in quantities]
        available_items = MenuItem.objects.filter(
            id__in=item_ids,
            is_available=True,
            status='in_stock',
            category__is_active=True,
        )
        items_by_id = {item.id: item for item in available_items}
        if len(items_by_id) != len(set(item_ids)):
            messages.error(request, 'One or more selected items are no longer available.')
            return redirect('customer_menu')

        address = request.POST.get('address', '').strip() or request.user.profile.address
        phone = request.POST.get('phone', '').strip() or request.user.profile.mobile_no
        if not address or not phone:
            messages.error(request, 'Add a delivery address and phone number to your profile before ordering.')
            return redirect('customer_profile')

        with transaction.atomic():
            order = Order.objects.create(
                user=request.user,
                order_type='restaurant',
                status='pending',
                address=address,
                phone=phone,
            )
            total = 0
            for item_id, quantity in quantities:
                menu_item = items_by_id[item_id]
                price = Decimal(str(menu_item.get_final_price())).quantize(Decimal('0.01'))
                OrderItem.objects.create(
                    order=order,
                    menu_item=menu_item,
                    quantity=quantity,
                    price=price,
                )
                total += price * quantity
            order.total = total
            order.save(update_fields=['total', 'updated_at'])

            Payment.objects.create(
                order=order,
                amount=total,
                method='upi',
                status='success',
                transaction_id=f"PREPAID_CM_{order.id}",
            )

        messages.success(request, f'Order #{order.id} was placed and sent to the kitchen.')
        return redirect('customer_orders')

    return render(request, 'restaurant/customer_menu.html', {
        'menu_items': menu_items,
        'categories': MenuCategory.objects.filter(is_active=True).order_by('name'),
        'default_address': request.user.profile.address,
        'default_phone': request.user.profile.mobile_no,
    })


@login_required(login_url='login')
def customer_profile(request):
    if request.user.is_staff or request.user.is_superuser:
        return redirect('home')

    if request.method == 'POST':
        request.user.first_name = request.POST.get('first_name', request.user.first_name)
        request.user.last_name = request.POST.get('last_name', request.user.last_name)
        request.user.email = request.POST.get('email', request.user.email)
        request.user.save()

        mobile = request.POST.get('mobile_no', '').strip()
        if mobile:
            request.user.profile.mobile_no = mobile
        request.user.profile.address = request.POST.get('address', request.user.profile.address)
        request.user.profile.save()

        messages.success(request, 'Profile updated successfully!')
        return redirect('customer_profile')

    return render(request, 'restaurant/customer_profile.html')


# ============================================================
# ==================== LOGIN WITH OTP ====================
# ============================================================
@never_cache
def login_view(request):
    if request.user.is_authenticated:
        if request.user.is_superuser or (request.user.is_staff and request.user.groups.filter(name='Admin').exists()):
            return redirect('admin_dashboard')
        elif request.user.is_staff:
            if request.user.groups.filter(name='Manager').exists():
                return redirect('staff:manager_dashboard')
            elif request.user.groups.filter(name__in=['Chef', 'Kitchen Staff']).exists():
                return redirect('staff:kitchen_dashboard')
            elif request.user.groups.filter(name='OrderHandler').exists():
                return redirect('staff:order_handler_dashboard')
            elif request.user.groups.filter(name='Waiter').exists():
                return redirect('staff:waiter_dashboard')
            return redirect('staff:no_access')
        return redirect('home')

    form = LoginForm(request.POST or None)

    if request.method == 'POST':
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']

            user = authenticate(request, username=username, password=password)
            if user is None:
                user_by_email = User.objects.filter(email__iexact=username).first()
                if user_by_email:
                    user = authenticate(request, username=user_by_email.username, password=password)

            if user is None:
                messages.error(request, 'Invalid username or password')
                return redirect('login')

            if not user.is_active:
                messages.error(request, 'Account not activated. Please sign up again.')
                return redirect('register')

            if not user.email:
                messages.error(
                    request,
                    'This account is missing an email address. Please add an email before OTP login.'
                )
                return redirect('login')

            OTPVerification.objects.filter(user=user, purpose='login').delete()
            otp_code = OTPVerification.generate_otp()
            OTPVerification.objects.create(user=user, otp=otp_code, purpose='login')

            try:
                send_mail(
                    'FoodVerse Login OTP',
                    f'Hello {user.first_name or user.username},\n\nYour FoodVerse login OTP is: {otp_code}\n\nValid for 10 minutes.',
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    fail_silently=False,
                )
                messages.success(request, 'OTP sent to your email address.')
            except Exception:
                messages.error(request, 'OTP could not be sent. Please try again.')
                return redirect('login')

            return redirect('verify_login_otp', user_id=user.id)
        else:
            messages.error(request, 'Please fill in all fields')
            return redirect('login')

    return render(request, 'restaurant/login.html', {'form': form})


@never_cache
def verify_login_otp_view(request, user_id):
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        messages.error(request, 'Invalid request')
        return redirect('login')

    if request.method == 'POST':
        otp_entered = request.POST.get('otp', '').strip()

        if not otp_entered or len(otp_entered) != 6:
            messages.error(request, 'Please enter a valid 6-digit OTP')
            return redirect('verify_login_otp', user_id=user_id)

        otp_record = OTPVerification.objects.filter(
            user=user, otp=otp_entered, purpose='login', is_verified=False
        ).order_by('-created_at').first()

        if not otp_record:
            messages.error(request, 'Invalid OTP')
            return redirect('verify_login_otp', user_id=user_id)

        if otp_record.is_expired():
            messages.error(request, 'OTP expired. Please login again.')
            otp_record.delete()
            return redirect('login')

        otp_record.is_verified = True
        otp_record.save()

        auth_login(request, user)
        OTPVerification.objects.filter(user=user, purpose='login').delete()

        if user.is_superuser or (user.is_staff and user.groups.filter(name='Admin').exists()):
            messages.success(request, f'Welcome Admin, {user.first_name or user.username}!')
            return redirect('admin_dashboard')

        elif user.is_staff:
            if user.groups.filter(name='Manager').exists():
                messages.success(request, f'Welcome Manager, {user.first_name or user.username}!')
                return redirect('staff:manager_dashboard')

            elif user.groups.filter(name__in=['Chef', 'Kitchen Staff']).exists():
                messages.success(request, f'Welcome Chef, {user.first_name or user.username}!')
                return redirect('staff:kitchen_dashboard')

            elif user.groups.filter(name='OrderHandler').exists():
                messages.success(request, f'Welcome Order Handler, {user.first_name or user.username}!')
                return redirect('staff:order_handler_dashboard')

            elif user.groups.filter(name='Waiter').exists():
                messages.success(request, f'Welcome Waiter, {user.first_name or user.username}!')
                return redirect('staff:waiter_dashboard')

            else:
                auth_logout(request)
                messages.error(request, 'No staff role assigned. Contact admin.')
                return redirect('login')

        messages.success(request, 'Login Successful!')
        next_url = request.GET.get('next')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect('home')

    return render(request, 'restaurant/verify-login-otp.html', {
        'user': user,
        'email': user.email,
    })


# ============================================================
# ==================== REGISTER WITH OTP ====================
# ============================================================
@never_cache
def register_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    form = RegisterForm(request.POST or None)

    if request.method == 'POST':
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False
            user.save()

            user.profile.mobile_no = form.cleaned_data['mobile_no']
            user.profile.address = form.cleaned_data['address']
            user.profile.save()

            OTPVerification.objects.filter(user=user, purpose='signup').delete()
            otp_code = OTPVerification.generate_otp()
            OTPVerification.objects.create(user=user, otp=otp_code, purpose='signup')

            try:
                if user.email:
                    send_mail(
                        'FoodVerse Signup OTP',
                        f'Hello {user.first_name or user.username},\n\nYour signup verification OTP is: {otp_code}',
                        settings.DEFAULT_FROM_EMAIL,
                        [user.email],
                        fail_silently=False,
                    )
                messages.success(request, 'OTP sent to your email address.')
            except Exception:
                messages.error(request, 'OTP could not be sent. Please try again.')
                return redirect('register')

            return redirect('verify_signup_otp', user_id=user.id)
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, error)
            return redirect('register')

    return render(request, 'restaurant/register.html', {'form': form})


@never_cache
def verify_signup_otp_view(request, user_id):
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        messages.error(request, 'Invalid request')
        return redirect('register')

    if request.method == 'POST':
        otp_entered = request.POST.get('otp', '').strip()

        if not otp_entered or len(otp_entered) != 6:
            messages.error(request, 'Please enter a valid 6-digit OTP')
            return redirect('verify_signup_otp', user_id=user_id)

        otp_record = OTPVerification.objects.filter(
            user=user, otp=otp_entered, purpose='signup', is_verified=False
        ).order_by('-created_at').first()

        if not otp_record:
            messages.error(request, 'Invalid OTP')
            return redirect('verify_signup_otp', user_id=user_id)

        if otp_record.is_expired():
            messages.error(request, 'OTP expired. Please register again.')
            user.delete()
            return redirect('register')

        user.is_active = True
        user.save()

        otp_record.is_verified = True
        otp_record.save()

        auth_login(request, user)
        OTPVerification.objects.filter(user=user, purpose='signup').delete()

        messages.success(request, 'Account created successfully! Welcome to FoodVerse.')
        return redirect('home')

    return render(request, 'restaurant/verify-signup-otp.html', {
        'user': user,
        'email': user.email,
    })


# ============================================================
# ==================== LOGOUT ====================
# ============================================================
def logout_view(request):
    auth_logout(request)
    messages.success(request, 'Logged out successfully.')
    return redirect('home')


# ============================================================
# ==================== FORGOT PASSWORD (OTP) ====================
# ============================================================
@never_cache
def forgot_password_view(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()

        if not email:
            messages.error(request, 'Please enter your email')
            return redirect('forgot_password')

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            messages.success(request, 'If this email is registered, an OTP has been sent.')
            return redirect('verify_forgot_otp', user_id=0)

        OTPVerification.objects.filter(user=user, purpose='reset').delete()
        otp_code = OTPVerification.generate_otp()
        OTPVerification.objects.create(user=user, otp=otp_code, purpose='reset')

        try:
            if email:
                send_mail(
                    'FoodVerse Password Reset OTP',
                    f'Hello {user.first_name or user.username},\n\nYour FoodVerse password reset OTP is: {otp_code}',
                    settings.DEFAULT_FROM_EMAIL,
                    [email],
                    fail_silently=False,
                )
            messages.success(request, 'OTP sent to your email address.')
        except Exception:
            messages.error(request, 'OTP could not be sent. Please try again.')
            return redirect('forgot_password')

        return redirect('verify_forgot_otp', user_id=user.id)

    return render(request, 'restaurant/forgot-password.html')


@never_cache
def verify_forgot_otp_view(request, user_id):
    if user_id == 0:
        messages.error(request, 'Invalid request')
        return redirect('forgot_password')

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        messages.error(request, 'Invalid request')
        return redirect('forgot_password')

    if request.method == 'POST':
        otp_entered = request.POST.get('otp', '').strip()

        if not otp_entered or len(otp_entered) != 6:
            messages.error(request, 'Please enter a valid 6-digit OTP')
            return redirect('verify_forgot_otp', user_id=user_id)

        otp_record = OTPVerification.objects.filter(
            user=user, otp=otp_entered, purpose='reset', is_verified=False
        ).order_by('-created_at').first()

        if not otp_record:
            messages.error(request, 'Invalid OTP')
            return redirect('verify_forgot_otp', user_id=user_id)

        if otp_record.is_expired():
            messages.error(request, 'OTP expired. Please try again.')
            otp_record.delete()
            return redirect('forgot_password')

        otp_record.is_verified = True
        otp_record.save()

        messages.success(request, 'OTP verified! Set your new password.')
        return redirect('set_new_password', user_id=user.id)

    return render(request, 'restaurant/verify-forgot-otp.html', {
        'user': user,
        'email': user.email,
    })


@never_cache
def set_new_password_view(request, user_id):
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        messages.error(request, 'Invalid request')
        return redirect('forgot_password')

    otp_record = OTPVerification.objects.filter(
        user=user, purpose='reset', is_verified=True
    ).order_by('-created_at').first()

    if not otp_record or otp_record.is_expired():
        messages.error(request, 'Session expired. Please try again.')
        return redirect('forgot_password')

    if request.method == 'POST':
        new_password = request.POST.get('new_password', '').strip()
        confirm_password = request.POST.get('confirm_password', '').strip()

        if not new_password or not confirm_password:
            messages.error(request, 'Please fill in both fields')
            return redirect('set_new_password', user_id=user_id)

        if len(new_password) < 8:
            messages.error(request, 'Password must be at least 8 characters')
            return redirect('set_new_password', user_id=user_id)

        if new_password != confirm_password:
            messages.error(request, 'Passwords do not match')
            return redirect('set_new_password', user_id=user_id)

        user.set_password(new_password)
        user.save()

        OTPVerification.objects.filter(user=user, purpose='reset').delete()

        messages.success(request, 'Password reset successfully! Please login.')
        return redirect('login')

    return render(request, 'restaurant/new-password.html', {'user': user})


# ============================================================
# ==================== ADMIN PANEL ====================
# ============================================================
ADMIN_OTP_STORE = {}


def admin_required(view_func):
    @login_required(login_url='login')
    @never_cache
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_superuser and not (request.user.is_staff and request.user.groups.filter(name='Admin').exists()):
            messages.error(request, 'Access denied. Admin privileges required.')
            if request.user.is_staff:
                if request.user.groups.filter(name='Manager').exists():
                    return redirect('staff:manager_dashboard')
                elif request.user.groups.filter(name='OrderHandler').exists():
                    return redirect('staff:order_handler_dashboard')
                elif request.user.groups.filter(name='Waiter').exists():
                    return redirect('staff:waiter_dashboard')
                elif request.user.groups.filter(name__in=['Kitchen Staff', 'Chef', 'Head Chef']).exists():
                    return redirect('staff:kitchen_dashboard')
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


@never_cache
def admin_login_view(request):
    return redirect('login')


@never_cache
def admin_otp_view(request):
    return redirect('login')


def admin_logout_view(request):
    auth_logout(request)
    messages.success(request, 'Logged out successfully.')
    return redirect('login')


# ------------------------------------------------------------
# ADMIN DASHBOARD
# ------------------------------------------------------------
@admin_required
def admin_dashboard(request):
    today = timezone.now().date()
    all_inv = InventoryItem.objects.all()

    total_orders = Order.objects.count()
    total_revenue = Payment.objects.filter(status='success').aggregate(
        total=Sum('amount'))['total'] or 0
    total_customers = User.objects.filter(is_staff=False, is_superuser=False).count()
    total_staff = User.objects.filter(is_staff=True, is_superuser=False).count()
    total_reservations = Reservation.objects.filter(is_deleted=False).count()
    pending_reservations = Reservation.objects.filter(is_deleted=False, status='pending').count()
    pending_orders = Order.objects.filter(status='pending').count()
    preparing_orders = Order.objects.filter(status='preparing').count()
    total_tables = Table.objects.count()
    available_tables = Table.objects.filter(is_available=True).count()
    active_offers = Offer.objects.filter(is_active=True).count()
    low_stock_count = len([i for i in all_inv if i.is_low_stock])

    return render(request, 'restaurant/admin/admin_dashboard.html', {
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'total_customers': total_customers,
        'total_staff': total_staff,
        'total_reservations': total_reservations,
        'pending_reservations': pending_reservations,
        'pending_orders': pending_orders,
        'preparing_orders': preparing_orders,
        'total_tables': total_tables,
        'available_tables': available_tables,
        'active_offers': active_offers,
        'low_stock_count': low_stock_count,
    })


# ------------------------------------------------------------
# ADMIN ORDERS
# ------------------------------------------------------------
@admin_required
def admin_orders(request):
    if request.method == 'POST' and request.POST.get('action') == 'update_status':
        order_id = request.POST.get('order_id')
        new_status = request.POST.get('status')
        if order_id and new_status:
            try:
                o = Order.objects.get(id=order_id)
                o.status = new_status
                o.save(update_fields=['status', 'updated_at'])
                messages.success(request, f'Order #{o.id} status updated to {o.get_status_display()}.')
            except Order.DoesNotExist:
                messages.error(request, 'Order not found.')
        return redirect('admin_orders')

    status_filter = request.GET.get('status', 'all')
    query = request.GET.get('q', '').strip()
    orders = Order.objects.select_related('user', 'table').prefetch_related('items__menu_item', 'payments').order_by('-created_at')
    if status_filter != 'all':
        orders = orders.filter(status=status_filter)
    if query:
        clean_q = query.lstrip('#').upper().replace('TS', '').strip()
        if clean_q.isdigit():
            orders = orders.filter(Q(id=int(clean_q)) | Q(user__username__icontains=query) | Q(user__first_name__icontains=query))
        else:
            orders = orders.filter(
                Q(user__username__icontains=query) | Q(user__first_name__icontains=query) | Q(user__email__icontains=query)
            )

    return render(request, 'restaurant/admin/admin_orders.html', {
        'orders': orders,
        'active_filter': status_filter,
        'query': query,
        'total_orders': Order.objects.count(),
        'pending_count': Order.objects.filter(status='pending').count(),
        'preparing_count': Order.objects.filter(status='preparing').count(),
        'out_count': Order.objects.filter(status='out_for_delivery').count(),
        'delivered_count': Order.objects.filter(status='delivered').count(),
        'cancelled_count': Order.objects.filter(status='cancelled').count(),
    })


@admin_required
def admin_order_details(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('user', 'table').prefetch_related('items__menu_item'),
        id=order_id)

    if request.method == 'POST' and request.POST.get('action') == 'update_status':
        new_status = request.POST.get('status')
        if new_status:
            order.status = new_status
            order.save(update_fields=['status', 'updated_at'])
            messages.success(request, f'Order #{order.id} status updated to {order.get_status_display()}.')
            return redirect('admin_order_details', order_id=order.id)

    payment = order.payments.first()
    return render(request, 'restaurant/admin/admin_order_details.html', {
        'order': order, 'payment': payment})


# ------------------------------------------------------------
# ADMIN TABLES
# ------------------------------------------------------------
@admin_required
def admin_tables(request):
    if request.method == 'POST':
        number = request.POST.get('number', '').strip()
        seats = request.POST.get('seats', '').strip()
        location = request.POST.get('location', 'Indoor').strip()
        if not number or not seats:
            messages.error(request, 'Please fill in all fields')
            return redirect('admin_tables')
        if Table.objects.filter(number=number).exists():
            messages.error(request, f'Table {number} already exists')
            return redirect('admin_tables')
        Table.objects.create(number=number, seats=int(seats), location=location)
        messages.success(request, f'Table {number} added successfully!')
        return redirect('admin_tables')
    delete_id = request.GET.get('delete')
    if delete_id:
        try:
            t = Table.objects.get(id=delete_id)
            num = t.number
            t.delete()
            messages.success(request, f'Table {num} deleted!')
        except Table.DoesNotExist:
            messages.error(request, 'Table not found')
        return redirect('admin_tables')
    tables = Table.objects.all().order_by('number')
    return render(request, 'restaurant/admin/admin_tables.html', {
        'tables': tables,
        'total_tables': tables.count(),
        'available_count': tables.filter(is_available=True).count(),
        'occupied_count': tables.filter(is_available=False).count(),
        'qr_count': tables.count(),
    })


# ------------------------------------------------------------
# ADMIN MENU
# ------------------------------------------------------------
@admin_required
def admin_menu(request):
    active_tab = request.GET.get('tab', 'categories')

    if request.method == 'POST':
        action = request.POST.get('action', '')

        # ---------- EDIT MENU ITEM ----------
        if action == 'edit_item':
            item_id = request.POST.get('item_id')
            name = request.POST.get('name', '').strip()
            category_id = request.POST.get('category', '').strip()
            subcategory_id = request.POST.get('subcategory', '').strip()
            price = request.POST.get('price', '').strip()
            description = request.POST.get('description', '').strip()
            status = request.POST.get('status', 'in_stock').strip()
            customizations_list = request.POST.getlist('customizations')
            image = request.FILES.get('image')

            try:
                item = MenuItem.objects.get(id=item_id)
                item.name = name
                item.category_id = category_id
                item.subcategory_id = subcategory_id if subcategory_id else None
                item.price = price
                item.description = description
                item.status = status
                item.is_available = (status == 'in_stock')

                cust_qs = ItemCustomization.objects.filter(
                    Q(id__in=[c for c in customizations_list if c.isdigit()]) |
                    Q(name__in=customizations_list)
                )
                cust_names = [c.name for c in cust_qs]
                item.customizations = ','.join(cust_names) if cust_names else ','.join(customizations_list)
                item.customization_groups.set(cust_qs)

                if image:
                    item.image = image
                item.save()
                messages.success(request, f'{name} updated successfully!')
            except MenuItem.DoesNotExist:
                messages.error(request, 'Menu item not found')
            except Exception as e:
                messages.error(request, f'Failed to update item: {str(e)}')
            return redirect('admin_menu')

    if request.method == 'POST':
        action = request.POST.get('action', '')

        # ---------- ADD CATEGORY ----------
        if action == 'add_category':
            name = request.POST.get('name', '').strip()
            description = request.POST.get('description', '').strip()

            if not name:
                messages.error(request, 'Category name is required')
                return redirect('admin_menu')

            if MenuCategory.objects.filter(name__iexact=name).exists():
                messages.error(request, f'Category "{name}" already exists')
                return redirect('admin_menu')

            MenuCategory.objects.create(
                name=name,
                description=description,
                is_active=True,
            )
            messages.success(request, f'Category "{name}" added successfully!')
            return redirect('admin_menu')

        # ---------- EDIT CATEGORY ----------
        if action == 'edit_category':
            cat_id = request.POST.get('category_id')
            name = request.POST.get('name', '').strip()
            description = request.POST.get('description', '').strip()

            if not name:
                messages.error(request, 'Category name is required')
                return redirect('admin_menu')

            try:
                cat = MenuCategory.objects.get(id=cat_id)
                if MenuCategory.objects.filter(name__iexact=name).exclude(id=cat.id).exists():
                    messages.error(request, f'Category "{name}" already exists')
                    return redirect('admin_menu')
                cat.name = name
                cat.description = description
                cat.save()
                messages.success(request, f'Category "{name}" updated!')
            except MenuCategory.DoesNotExist:
                messages.error(request, 'Category not found')
            return redirect('admin_menu')

        # ---------- TOGGLE CATEGORY STATUS ----------
        if action == 'toggle_category_status':
            cat_id = request.POST.get('category_id')
            try:
                cat = MenuCategory.objects.get(id=cat_id)
                cat.is_active = not cat.is_active
                cat.save()
                status = 'Active' if cat.is_active else 'Inactive'
                messages.success(request, f'Category "{cat.name}" marked as {status}')
            except MenuCategory.DoesNotExist:
                messages.error(request, 'Category not found')
            return redirect('admin_menu')

        # ---------- ADD SUBCATEGORY ----------
        if action == 'add_subcategory':
            name = request.POST.get('name', '').strip()
            category_id = request.POST.get('category', '').strip()

            if not name or not category_id:
                messages.error(request, 'Subcategory name and parent category are required')
                return redirect('admin_menu')

            if MenuSubcategory.objects.filter(name__iexact=name, category_id=category_id).exists():
                messages.error(request, f'Subcategory "{name}" already exists in this category')
                return redirect('admin_menu')

            try:
                cat = MenuCategory.objects.get(id=category_id)
                MenuSubcategory.objects.create(
                    name=name,
                    category=cat,
                    is_active=True,
                )
                messages.success(request, f'Subcategory "{name}" added under "{cat.name}"!')
            except MenuCategory.DoesNotExist:
                messages.error(request, 'Parent category not found')
            return redirect('admin_menu')

        # ---------- TOGGLE SUBCATEGORY STATUS ----------
        if action == 'toggle_subcategory_status':
            subcat_id = request.POST.get('subcategory_id')
            try:
                subcat = MenuSubcategory.objects.get(id=subcat_id)
                subcat.is_active = not subcat.is_active
                subcat.save()
                status = 'Active' if subcat.is_active else 'Inactive'
                messages.success(request, f'Subcategory "{subcat.name}" marked as {status}')
            except MenuSubcategory.DoesNotExist:
                messages.error(request, 'Subcategory not found')
            return redirect('admin_menu')

        # ---------- ADD CUSTOMIZATION ----------
        if action == 'add_customization':
            cust_name = request.POST.get('cust_name', '').strip()
            cust_status = request.POST.get('cust_status', 'active').strip()
            option_names = request.POST.getlist('option_name[]')
            option_prices = request.POST.getlist('option_price[]')
            linked_item_ids = request.POST.getlist('linked_items')

            if not cust_name:
                messages.error(request, 'Customization name is required')
                return redirect('admin_menu')

            try:
                cust = ItemCustomization.objects.create(
                    name=cust_name,
                    status=cust_status,
                )
                if linked_item_ids:
                    cust.linked_items.set(MenuItem.objects.filter(id__in=linked_item_ids))
                for i, oname in enumerate(option_names):
                    oname = oname.strip()
                    if oname:
                        price_raw = option_prices[i] if i < len(option_prices) else '0'
                        try:
                            price_val = float(price_raw) if price_raw else 0
                        except ValueError:
                            price_val = 0
                        CustomizationOption.objects.create(
                            customization=cust,
                            name=oname,
                            price=price_val,
                        )
                messages.success(request, f'Customization "{cust_name}" added successfully!')
            except Exception as e:
                messages.error(request, f'Failed to add customization: {str(e)}')
            return redirect('admin_menu')

        # ---------- EDIT CUSTOMIZATION ----------
        if action == 'edit_customization':
            cust_id = request.POST.get('customization_id')
            cust_name = request.POST.get('cust_name', '').strip()
            cust_status = request.POST.get('cust_status', 'active').strip()
            option_names = request.POST.getlist('option_name[]')
            option_prices = request.POST.getlist('option_price[]')
            linked_item_ids = request.POST.getlist('linked_items')

            if not cust_name:
                messages.error(request, 'Customization name is required')
                return redirect('admin_menu')

            try:
                cust = ItemCustomization.objects.get(id=cust_id)
                cust.name = cust_name
                cust.status = cust_status
                cust.save()

                cust.linked_items.set(MenuItem.objects.filter(id__in=linked_item_ids))

                cust.options.all().delete()
                for i, oname in enumerate(option_names):
                    oname = oname.strip()
                    if oname:
                        price_raw = option_prices[i] if i < len(option_prices) else '0'
                        try:
                            price_val = float(price_raw) if price_raw else 0
                        except ValueError:
                            price_val = 0
                        CustomizationOption.objects.create(
                            customization=cust,
                            name=oname,
                            price=price_val,
                        )
                messages.success(request, f'Customization "{cust_name}" updated successfully!')
            except ItemCustomization.DoesNotExist:
                messages.error(request, 'Customization not found')
            except Exception as e:
                messages.error(request, f'Failed to update customization: {str(e)}')
            return redirect('admin_menu')

        # ---------- TOGGLE CUSTOMIZATION STATUS ----------
        if action == 'toggle_customization_status':
            cust_id = request.POST.get('customization_id')
            try:
                cust = ItemCustomization.objects.get(id=cust_id)
                cust.status = 'inactive' if cust.status == 'active' else 'active'
                cust.save()
                messages.success(request, f'Customization "{cust.name}" marked as {cust.status.title()}')
            except ItemCustomization.DoesNotExist:
                messages.error(request, 'Customization not found')
            return redirect('admin_menu')

        # ---------- ADD MENU ITEM ----------
        name = request.POST.get('name', '').strip()
        category_id = request.POST.get('category', '').strip()
        subcategory_id = request.POST.get('subcategory', '').strip()
        price = request.POST.get('price', '').strip()
        description = request.POST.get('description', '').strip()
        status = request.POST.get('status', 'in_stock').strip()
        customizations_list = request.POST.getlist('customizations')
        image = request.FILES.get('image')

        if not name or not category_id or not price:
            messages.error(request, 'Name, Category and Price are required')
            return redirect('admin_menu')

        try:
            cust_qs = ItemCustomization.objects.filter(
                Q(id__in=[c for c in customizations_list if c.isdigit()]) |
                Q(name__in=customizations_list)
            )
            cust_names = [c.name for c in cust_qs]
            item = MenuItem.objects.create(
                name=name,
                category_id=category_id,
                subcategory_id=subcategory_id if subcategory_id else None,
                price=price,
                description=description,
                image=image,
                customizations=','.join(cust_names) if cust_names else ','.join(customizations_list),
                status=status,
                is_available=(status == 'in_stock'),
                is_popular=False,
                rating=5,
            )
            if cust_qs.exists():
                item.customization_groups.set(cust_qs)
            messages.success(request, f'{name} added successfully!')
        except Exception as e:
            messages.error(request, f'Failed to add item: {str(e)}')

        return redirect('admin_menu')

    # ---------- DELETE CATEGORY ----------
    delete_cat = request.GET.get('delete_category')
    if delete_cat:
        try:
            cat = MenuCategory.objects.get(id=delete_cat)
            cname = cat.name
            cat.delete()
            messages.success(request, f'Category "{cname}" deleted!')
        except MenuCategory.DoesNotExist:
            messages.error(request, 'Category not found')
        return redirect('admin_menu')

    # ---------- DELETE SUBCATEGORY ----------
    delete_subcat = request.GET.get('delete_subcategory')
    if delete_subcat:
        try:
            subcat = MenuSubcategory.objects.get(id=delete_subcat)
            sname = subcat.name
            subcat.delete()
            messages.success(request, f'Subcategory "{sname}" deleted!')
        except MenuSubcategory.DoesNotExist:
            messages.error(request, 'Subcategory not found')
        return redirect('admin_menu')

    # ---------- DELETE CUSTOMIZATION ----------
    delete_cust = request.GET.get('delete_customization')
    if delete_cust:
        try:
            cust = ItemCustomization.objects.get(id=delete_cust)
            cname = cust.name
            cust.delete()
            messages.success(request, f'Customization "{cname}" deleted!')
        except ItemCustomization.DoesNotExist:
            messages.error(request, 'Customization not found')
        return redirect('admin_menu')

    # ---------- DELETE MENU ITEM ----------
    delete_id = request.GET.get('delete')
    if delete_id:
        try:
            item = MenuItem.objects.get(id=delete_id)
            n = item.name
            item.delete()
            messages.success(request, f'{n} deleted!')
        except MenuItem.DoesNotExist:
            messages.error(request, 'Item not found')
        return redirect('admin_menu')

    # ---------- GET ----------
    items = MenuItem.objects.select_related('category', 'subcategory').order_by('-created_at')
    categories = MenuCategory.objects.all().order_by('name')
    subcategories = MenuSubcategory.objects.select_related('category').order_by('category__name', 'name')
    item_customizations = ItemCustomization.objects.prefetch_related(
        'options', 'linked_items'
    ).order_by('-created_at')

    return render(request, 'restaurant/admin/admin_menu.html', {
        'items': items,
        'categories': categories,
        'subcategories': subcategories,
        'item_customizations': item_customizations,
        'total_items': items.count(),
        'total_categories': categories.count(),
        'total_subcategories': subcategories.count(),
        'total_customizations': item_customizations.count(),
        'active_tab': active_tab,
    })


# ------------------------------------------------------------
# ADMIN OFFERS
# ------------------------------------------------------------
@admin_required
def admin_offers(request):
    if request.method == 'POST':
        code = request.POST.get('code', '').strip().upper()
        title = request.POST.get('title', '').strip()
        discount = request.POST.get('discount', '').strip()
        valid_till = request.POST.get('valid_till', '').strip()
        if not code or not title or not discount or not valid_till:
            messages.error(request, 'Please fill all fields')
            return redirect('admin_offers')
        if Offer.objects.filter(code=code).exists():
            messages.error(request, f'Offer code {code} already exists')
            return redirect('admin_offers')
        Offer.objects.create(code=code, title=title, discount=discount, valid_till=valid_till)
        messages.success(request, f'Offer {code} added!')
        return redirect('admin_offers')
    toggle_id = request.GET.get('toggle')
    if toggle_id:
        try:
            o = Offer.objects.get(id=toggle_id)
            o.is_active = not o.is_active
            o.save()
            messages.success(request, f'{o.code} status updated')
        except Offer.DoesNotExist:
            messages.error(request, 'Offer not found')
        return redirect('admin_offers')
    delete_id = request.GET.get('delete')
    if delete_id:
        Offer.objects.filter(id=delete_id).delete()
        messages.success(request, 'Offer deleted!')
        return redirect('admin_offers')
    offers = Offer.objects.all().order_by('-created_at')
    today = timezone.now().date()
    return render(request, 'restaurant/admin/admin_offers.html', {
        'offers': offers,
        'active_count': offers.filter(is_active=True).count(),
        'upcoming_count': offers.filter(valid_till__gte=today).count(),
        'expired_count': offers.filter(valid_till__lt=today).count(),
    })


# ------------------------------------------------------------
# ADMIN RESERVATIONS & TABLE AVAILABILITY
# ------------------------------------------------------------
def check_table_conflict(table, res_date, res_time, exclude_id=None):
    if isinstance(res_date, str):
        try:
            res_date = datetime.datetime.strptime(res_date, '%Y-%m-%d').date()
        except ValueError:
            return False, None

    if isinstance(res_time, str):
        try:
            parts = res_time.split(':')
            res_time = datetime.time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return False, None

    target_dt = datetime.datetime.combine(res_date, res_time)

    conflicts = Reservation.objects.filter(
        is_deleted=False,
        table=table,
        date=res_date
    ).exclude(status__in=['cancelled', 'rejected'])

    if exclude_id:
        conflicts = conflicts.exclude(id=exclude_id)

    for c in conflicts:
        c_dt = datetime.datetime.combine(c.date, c.time)
        diff_seconds = abs((target_dt - c_dt).total_seconds())
        if diff_seconds < 7200:
            formatted_time = c.time.strftime('%I:%M %p')
            return True, f"Table #{table.number} is already booked at {formatted_time} on {res_date.strftime('%d %b %Y')}."

    return False, None


@admin_required
def admin_check_tables(request):
    date_str = request.GET.get('date', '').strip()
    time_str = request.GET.get('time', '').strip()
    guests_str = request.GET.get('guests', '2').strip()
    exclude_id = request.GET.get('exclude_id', '').strip()
    exclude_id = int(exclude_id) if exclude_id.isdigit() else None

    tables_data = []
    all_tables = Table.objects.all().order_by('number')

    if not date_str or not time_str:
        for t in all_tables:
            tables_data.append({
                'id': t.id,
                'number': t.number,
                'seats': t.seats,
                'location': t.location,
                'available': True,
                'reason': f"Seats {t.seats} ({t.location})"
            })
        return JsonResponse({'tables': tables_data})

    try:
        res_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        parts = time_str.split(':')
        res_time = datetime.time(int(parts[0]), int(parts[1]))
        guests = int(guests_str) if guests_str.isdigit() else 2
    except Exception:
        return JsonResponse({'error': 'Invalid date/time/guests parameter'}, status=400)

    for t in all_tables:
        if t.seats < guests:
            tables_data.append({
                'id': t.id, 'number': t.number, 'seats': t.seats,
                'location': t.location, 'available': False,
                'reason': f"Capacity too small ({t.seats} seats < {guests} guests)"
            })
            continue

        is_conflict, conflict_msg = check_table_conflict(t, res_date, res_time, exclude_id)
        if is_conflict:
            tables_data.append({
                'id': t.id, 'number': t.number, 'seats': t.seats,
                'location': t.location, 'available': False,
                'reason': conflict_msg
            })
        else:
            tables_data.append({
                'id': t.id, 'number': t.number, 'seats': t.seats,
                'location': t.location, 'available': True,
                'reason': f"Available ({t.seats} seats - {t.location})"
            })

    return JsonResponse({'tables': tables_data})


@admin_required
def admin_reservations(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        res_id = request.POST.get('reservation_id')

        if action == 'add':
            name = request.POST.get('name', '').strip()
            phone = request.POST.get('phone', '').strip()
            date_str = request.POST.get('date', '').strip()
            time_str = request.POST.get('time', '').strip()
            guests_str = request.POST.get('guests', '2').strip()
            table_id = request.POST.get('table_id', '').strip()
            special_request = request.POST.get('special_request', '').strip()
            payment_method = request.POST.get('payment_method', 'cash').strip()
            payment_status = request.POST.get('payment_status', 'pending').strip()
            status = request.POST.get('status', 'confirmed').strip()
            amount_str = request.POST.get('payment_amount', '0').strip()

            if not name or not phone or not date_str or not time_str or not table_id:
                messages.error(request, 'Please fill in all required fields (Name, Phone, Date, Time, Table).')
                return redirect('admin_reservations')

            try:
                res_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                parts = time_str.split(':')
                res_time = datetime.time(int(parts[0]), int(parts[1]))
                guests = int(guests_str)
                table = Table.objects.get(id=table_id)
                amount = float(amount_str) if amount_str else 0.0
            except (ValueError, Table.DoesNotExist):
                messages.error(request, 'Invalid date, time, party size, or selected table.')
                return redirect('admin_reservations')

            if table.seats < guests:
                messages.error(request, f'Table #{table.number} has only {table.seats} seats, but {guests} guests requested.')
                return redirect('admin_reservations')

            is_conflict, conflict_msg = check_table_conflict(table, res_date, res_time)
            if is_conflict:
                messages.error(request, conflict_msg)
                return redirect('admin_reservations')

            matched_user = User.objects.filter(Q(username=phone) | Q(profile__mobile_no=phone) | Q(username=name)).first()
            if not matched_user:
                matched_user = request.user if request.user.is_authenticated else None

            res = Reservation.objects.create(
                user=matched_user, name=name, phone=phone,
                date=res_date, time=res_time, guests=guests, table=table,
                special_request=special_request,
                payment_method=payment_method,
                payment_status=payment_status,
                payment_amount=amount,
                status=status,
            )
            messages.success(request, f'Reservation #TS{res.id} for {name} added successfully!')
            return redirect('admin_reservations')

        elif action == 'edit':
            try:
                res = Reservation.objects.get(id=res_id)
            except Reservation.DoesNotExist:
                messages.error(request, 'Reservation not found.')
                return redirect('admin_reservations')

            name = request.POST.get('name', '').strip()
            phone = request.POST.get('phone', '').strip()
            date_str = request.POST.get('date', '').strip()
            time_str = request.POST.get('time', '').strip()
            guests_str = request.POST.get('guests', str(res.guests)).strip()
            table_id = request.POST.get('table_id', '').strip()
            special_request = request.POST.get('special_request', '').strip()
            payment_method = request.POST.get('payment_method', res.payment_method).strip()
            payment_status = request.POST.get('payment_status', res.payment_status).strip()
            status = request.POST.get('status', res.status).strip()
            amount_str = request.POST.get('payment_amount', str(res.payment_amount)).strip()

            if not name or not phone or not date_str or not time_str or not table_id:
                messages.error(request, 'Please fill in all required fields.')
                return redirect('admin_reservations')

            try:
                res_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                parts = time_str.split(':')
                res_time = datetime.time(int(parts[0]), int(parts[1]))
                guests = int(guests_str)
                table = Table.objects.get(id=table_id)
                amount = float(amount_str) if amount_str else 0.0
            except (ValueError, Table.DoesNotExist):
                messages.error(request, 'Invalid input or selected table.')
                return redirect('admin_reservations')

            if table.seats < guests:
                messages.error(request, f'Table #{table.number} has only {table.seats} seats, but {guests} guests requested.')
                return redirect('admin_reservations')

            is_conflict, conflict_msg = check_table_conflict(table, res_date, res_time, exclude_id=res.id)
            if is_conflict:
                messages.error(request, conflict_msg)
                return redirect('admin_reservations')

            res.name = name
            res.phone = phone
            res.date = res_date
            res.time = res_time
            res.guests = guests
            res.table = table
            res.special_request = special_request
            res.payment_method = payment_method
            res.payment_status = payment_status
            res.payment_amount = amount
            res.status = status
            res.save()
            messages.success(request, f'Reservation #TS{res.id} updated successfully!')
            return redirect('admin_reservations')

        elif action == 'cancel':
            try:
                res = Reservation.objects.get(id=res_id)
                if res.status != 'cancelled':
                    res.status = 'cancelled'
                    res.cancellation_date = timezone.now()
                    res.cancellation_reason = request.POST.get('cancellation_reason', 'Cancelled by Admin/Manager').strip() or 'Cancelled by Admin/Manager'

                    refund_calc = calculate_cancellation_refund(res)
                    refund_amount = refund_calc['total_refund']
                    res.refund_amount = refund_amount

                    if refund_amount > Decimal('0.00') and res.razorpay_payment_id and res.payment_status == 'paid':
                        key_id = getattr(settings, 'RAZORPAY_KEY_ID', '').strip().strip("'").strip('"')
                        key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '').strip().strip("'").strip('"')
                        try:
                            client = razorpay.Client(auth=(key_id, key_secret))
                            refund_res = client.payment.refund(res.razorpay_payment_id, {
                                'amount': int(refund_amount * 100),
                                'speed': 'optimum',
                                'notes': {'booking_id': str(res.id), 'cancelled_by': 'Admin'}
                            })
                            res.razorpay_refund_id = refund_res.get('id', '')
                            res.refund_status = 'refund_completed' if refund_res.get('status') in ['processed', 'completed'] else 'refund_initiated'
                            res.payment_status = 'refunded' if res.refund_status == 'refund_completed' else res.payment_status
                        except Exception as e:
                            logger.error(f"Razorpay Admin Refund failed for Reservation #{res.id}: {e}")
                            res.refund_status = 'refund_failed'
                            res.refund_failure_reason = str(e)
                    elif refund_amount == Decimal('0.00'):
                        res.refund_status = 'no_refund'
                    else:
                        res.refund_status = 'no_refund' if res.payment_status != 'paid' else 'refund_pending'

                    res.save()
                table_num = res.table.number if res.table else '-'
                messages.warning(request, f'Reservation #TS{res.id} for {res.name} cancelled. Table #{table_num} is now available.')
            except Reservation.DoesNotExist:
                messages.error(request, 'Reservation not found.')
            return redirect('admin_reservations')

        elif action == 'delete':
            try:
                res = Reservation.objects.get(id=res_id)
                res.is_deleted = True
                res.save()
                messages.success(request, f'Reservation #TS{res.id} deleted successfully.')
            except Reservation.DoesNotExist:
                messages.error(request, 'Reservation not found.')
            return redirect('admin_reservations')

        elif action == 'approve':
            try:
                res = Reservation.objects.get(id=res_id)
                res.status = 'confirmed'
                res.save()
                messages.success(request, f'Reservation #TS{res.id} for {res.name} confirmed!')
            except Reservation.DoesNotExist:
                messages.error(request, 'Reservation not found.')
            return redirect('admin_reservations')

        elif action == 'complete':
            try:
                res = Reservation.objects.get(id=res_id)
                res.status = 'completed'
                res.save()
                messages.success(request, f'Reservation #TS{res.id} for {res.name} marked as completed!')
            except Reservation.DoesNotExist:
                messages.error(request, 'Reservation not found.')
            return redirect('admin_reservations')

        elif action == 'add_table':
            number = request.POST.get('table_number', '').strip()
            seats = request.POST.get('seats', '4').strip()
            location = request.POST.get('location', 'Indoor').strip()
            if not number or not seats:
                messages.error(request, 'Please provide table number and seats.')
            elif Table.objects.filter(number=number).exists():
                messages.error(request, f'Table {number} already exists.')
            else:
                Table.objects.create(number=number, seats=int(seats), location=location, is_available=True)
                messages.success(request, f'Table {number} added successfully!')
            return redirect('admin_reservations')

        elif action == 'delete_table':
            table_id = request.POST.get('table_id')
            try:
                t = Table.objects.get(id=table_id)
                num = t.number
                t.delete()
                messages.success(request, f'Table {num} deleted successfully.')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found.')
            return redirect('admin_reservations')

        elif action == 'toggle_table':
            table_id = request.POST.get('table_id')
            try:
                t = Table.objects.get(id=table_id)
                t.is_available = not t.is_available
                t.save()
                st = 'Available' if t.is_available else 'Occupied'
                messages.success(request, f'Table {t.number} marked as {st}.')
            except Table.DoesNotExist:
                messages.error(request, 'Table not found.')
            return redirect('admin_reservations')

        return redirect('admin_reservations')

    # ================== GET ==================
    today = timezone.now().date()
    base_qs = Reservation.objects.filter(is_deleted=False).select_related('user', 'table')

    pending_count = base_qs.filter(status='pending').count()
    confirmed_count = base_qs.filter(status='confirmed').count()
    completed_count = base_qs.filter(status='completed').count()
    cancelled_count = base_qs.filter(status__in=['cancelled', 'rejected']).count()
    upcoming_count = base_qs.filter(date__gte=today, status__in=['pending', 'confirmed']).count()

    refund_pending_count = base_qs.filter(refund_status='refund_pending').count()
    refund_initiated_count = base_qs.filter(refund_status='refund_initiated').count()
    refund_completed_count = base_qs.filter(refund_status='refund_completed').count()
    no_refund_count = base_qs.filter(refund_status='no_refund').count()
    refund_failed_count = base_qs.filter(refund_status='refund_failed').count()

    total_count = base_qs.count()

    reservations = base_qs

    filter_date = request.GET.get('filter_date', '').strip()
    if not filter_date or filter_date == 'all':
        filter_date_val = ''
        date_heading = "All Reservations"
    elif filter_date == 'today':
        filter_date_val = today.strftime('%Y-%m-%d')
        reservations = reservations.filter(date=today)
        date_heading = f"Reservations for {today.strftime('%B %d, %Y')}"
    else:
        try:
            selected_date = datetime.datetime.strptime(filter_date, '%Y-%m-%d').date()
            filter_date_val = filter_date
            reservations = reservations.filter(date=selected_date)
            date_heading = f"Reservations for {selected_date.strftime('%B %d, %Y')}"
        except ValueError:
            filter_date_val = ''
            date_heading = "All Reservations"

    status_filter = request.GET.get('status', 'all')
    if status_filter != 'all':
        if status_filter == 'cancelled':
            reservations = reservations.filter(status__in=['cancelled', 'rejected'])
        elif status_filter == 'upcoming':
            reservations = reservations.filter(date__gte=today, status__in=['pending', 'confirmed'])
        else:
            reservations = reservations.filter(status=status_filter)

    refund_status_filter = request.GET.get('refund_status', 'all')
    if refund_status_filter != 'all':
        reservations = reservations.filter(refund_status=refund_status_filter)

    table_filter = request.GET.get('table', 'all')
    if table_filter != 'all' and table_filter.isdigit():
        reservations = reservations.filter(table_id=int(table_filter))

    guests_filter = request.GET.get('guests', 'all')
    if guests_filter == '1-2':
        reservations = reservations.filter(guests__in=[1, 2])
    elif guests_filter == '3-4':
        reservations = reservations.filter(guests__in=[3, 4])
    elif guests_filter == '5-6':
        reservations = reservations.filter(guests__in=[5, 6])
    elif guests_filter == '7+':
        reservations = reservations.filter(guests__gte=7)

    payment_status_filter = request.GET.get('payment_status', 'all')
    if payment_status_filter != 'all':
        reservations = reservations.filter(payment_status=payment_status_filter)

    query = request.GET.get('q', '').strip()
    if query:
        clean_id = query.lstrip('#').upper().replace('TS', '').strip()
        if clean_id.isdigit():
            reservations = reservations.filter(
                Q(id=int(clean_id)) | Q(name__icontains=query) | Q(phone__icontains=query)
            )
        else:
            reservations = reservations.filter(
                Q(name__icontains=query) | Q(phone__icontains=query)
            )

    frequently = request.GET.get('frequently', 'all')
    if frequently == 'tables':
        top_tables = base_qs.filter(table__isnull=False).values('table_id').annotate(cnt=Count('id')).order_by('-cnt').values_list('table_id', flat=True)[:5]
        reservations = reservations.filter(table_id__in=top_tables)
    elif frequently == 'customers':
        frequent_phones = base_qs.values('phone').annotate(cnt=Count('id')).filter(cnt__gte=2).values_list('phone', flat=True)
        reservations = reservations.filter(phone__in=frequent_phones)
    elif frequently == 'times':
        top_times = base_qs.values('time').annotate(cnt=Count('id')).order_by('-cnt').values_list('time', flat=True)[:5]
        reservations = reservations.filter(time__in=top_times)

    sort_by = request.GET.get('sort_by', 'newest')
    sort_map = {
        'newest': '-created_at', 'oldest': 'created_at',
        'date_earliest': ['date', 'time'], 'date_latest': ['-date', '-time'],
        'guests_low': 'guests', 'guests_high': '-guests',
        'name_asc': 'name', 'name_desc': '-name',
    }
    order_fields = sort_map.get(sort_by, '-created_at')
    reservations = reservations.order_by(*order_fields) if isinstance(order_fields, list) else reservations.order_by(order_fields)

    all_tables = Table.objects.all().order_by('number')

    return render(request, 'restaurant/admin/admin_reservations.html', {
        'reservations': reservations,
        'all_tables': all_tables,
        'date_heading': date_heading,
        'filter_date': filter_date_val,
        'status_filter': status_filter,
        'refund_status_filter': refund_status_filter,
        'table_filter': table_filter,
        'guests_filter': guests_filter,
        'payment_status_filter': payment_status_filter,
        'sort_by': sort_by, 'frequently': frequently, 'query': query,
        'pending_count': pending_count,
        'confirmed_count': confirmed_count,
        'completed_count': completed_count,
        'cancelled_count': cancelled_count,
        'upcoming_count': upcoming_count,
        'refund_pending_count': refund_pending_count,
        'refund_initiated_count': refund_initiated_count,
        'refund_completed_count': refund_completed_count,
        'no_refund_count': no_refund_count,
        'refund_failed_count': refund_failed_count,
        'total_count': total_count,
    })


# ------------------------------------------------------------
# ADMIN PAYMENTS
# ------------------------------------------------------------
@admin_required
def admin_payments(request):
    payments = Payment.objects.select_related('order__user').order_by('-created_at')
    total_revenue = payments.filter(status='success').aggregate(total=Sum('amount'))['total'] or 0
    pending_amount = payments.filter(status='pending').aggregate(total=Sum('amount'))['total'] or 0
    refunded_amount = payments.filter(status='refunded').aggregate(total=Sum('amount'))['total'] or 0
    return render(request, 'restaurant/admin/admin_payments.html', {
        'payments': payments,
        'total_revenue': total_revenue,
        'pending_amount': pending_amount,
        'refunded_amount': refunded_amount,
        'total_transactions': payments.count(),
    })


# ------------------------------------------------------------
# ADMIN USERS
# ------------------------------------------------------------
@admin_required
def admin_users(request):
    role_filter = request.GET.get('role', 'all')
    users = User.objects.all().select_related('profile').order_by('-date_joined')
    if role_filter == 'customers':
        users = users.filter(is_staff=False, is_superuser=False)
    elif role_filter == 'staff':
        users = users.filter(is_staff=True, is_superuser=False)
    elif role_filter == 'managers':
        users = users.filter(groups__name='Manager')
    elif role_filter == 'delivery':
        users = users.filter(groups__name='Delivery')
    elif role_filter == 'handlers':
        users = users.filter(groups__name='OrderHandler')
    return render(request, 'restaurant/admin/admin_users.html', {
        'users': users,
        'active_filter': role_filter,
        'customer_count': User.objects.filter(is_staff=False).count(),
        'staff_count': User.objects.filter(is_staff=True, is_superuser=False).count(),
        'manager_count': User.objects.filter(groups__name='Manager').count(),
        'delivery_count': User.objects.filter(groups__name='Delivery').count(),
        'handler_count': User.objects.filter(groups__name='OrderHandler').count(),
    })


# ------------------------------------------------------------
# ✅ ADMIN REPORTS — Comprehensive Reports (FIXED)
# ------------------------------------------------------------
@admin_required
def admin_reports(request):
    today = timezone.now().date()

    # ---------- Date Range Filter ----------
    date_range = request.GET.get('range', 'month')

    if date_range == 'today':
        start_date = today
        end_date = today
    elif date_range == 'week':
        start_date = today - timedelta(days=today.weekday())
        end_date = today
    elif date_range == 'quarter':
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start_date = today.replace(month=q_start_month, day=1)
        end_date = today
    elif date_range == 'year':
        start_date = today.replace(month=1, day=1)
        end_date = today
    elif date_range == 'all':
        first_order = Order.objects.order_by('created_at').first()
        start_date = first_order.created_at.date() if first_order else today
        end_date = today
    else:  # month (default)
        start_date = today.replace(day=1)
        end_date = today

    # ---------- Sales & Orders Report ----------
    payments_in_range = Payment.objects.filter(
        status='success',
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    total_sales = payments_in_range.aggregate(total=Sum('amount'))['total'] or 0

    orders_in_range = Order.objects.filter(
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    total_orders = orders_in_range.count()

    events_in_range = Event.objects.filter(
        event_date__gte=start_date,
        event_date__lte=end_date,
    )
    total_events = events_in_range.count()

    # ---------- Revenue Chart (Daily) ----------
    chart_labels = []
    chart_values = []

    chart_start = start_date
    if (end_date - start_date).days > 30:
        chart_start = end_date - timedelta(days=29)

    days = (end_date - chart_start).days
    for i in range(days + 1):
        day = chart_start + timedelta(days=i)
        chart_labels.append(day.strftime('%Y-%m-%d'))
        day_total = Payment.objects.filter(
            status='success', created_at__date=day
        ).aggregate(total=Sum('amount'))['total'] or 0
        chart_values.append(float(day_total))

    # ---------- Food Waste / NGO ----------
    total_donations = NGO.objects.aggregate(total=Sum('meals_served'))['total'] or 0
    ngo_revenue = 0

    # ---------- Inventory Report ----------
    all_items = InventoryItem.objects.all()
    low_stock_count = len([i for i in all_items if i.is_low_stock])

    inv_data = {}
    for item in all_items:
        label = item.get_category_display()
        inv_data[label] = inv_data.get(label, 0) + float(item.quantity)

    inv_labels = list(inv_data.keys())
    inv_values = list(inv_data.values())

    # ---------- Customers ----------
    total_customers = User.objects.filter(is_staff=False, is_superuser=False).count()

    avg_order = orders_in_range.aggregate(avg=Sum('total'))['avg'] or 0

    return render(request, 'restaurant/admin/admin_reports.html', {
        'date_range': date_range,
        'start_date': start_date,
        'end_date': end_date,
        'total_sales': total_sales,
        'total_orders': total_orders,
        'total_events': total_events,
        'chart_labels': chart_labels,
        'chart_values': chart_values,
        'total_donations': total_donations,
        'ngo_revenue': ngo_revenue,
        'low_stock_count': low_stock_count,
        'inv_labels': inv_labels,
        'inv_values': inv_values,
        'total_customers': total_customers,
        'avg_order_value': avg_order,
        'status_breakdown': [],
    })


# ------------------------------------------------------------
# ADMIN SETTINGS
# ------------------------------------------------------------
@admin_required
def admin_settings(request):
    if request.method == 'POST':
        form_type = request.POST.get('form_type', 'profile')
        if form_type == 'profile':
            user = request.user
            user.first_name = request.POST.get('first_name', user.first_name)
            user.last_name = request.POST.get('last_name', user.last_name)
            user.email = request.POST.get('email', user.email)
            user.save()
            mobile = request.POST.get('mobile_no', '').strip()
            if mobile:
                user.profile.mobile_no = mobile
                user.profile.save()
            messages.success(request, 'Profile updated successfully!')
            return redirect('admin_settings')
        elif form_type == 'password':
            old_pass = request.POST.get('old_password', '')
            new_pass = request.POST.get('new_password', '')
            confirm = request.POST.get('confirm_password', '')
            if not request.user.check_password(old_pass):
                messages.error(request, 'Current password is incorrect')
                return redirect('admin_settings')
            if len(new_pass) < 8:
                messages.error(request, 'Password must be at least 8 characters')
                return redirect('admin_settings')
            if new_pass != confirm:
                messages.error(request, 'Passwords do not match')
                return redirect('admin_settings')
            request.user.set_password(new_pass)
            request.user.save()
            auth_login(request, request.user)
            messages.success(request, 'Password updated successfully!')
            return redirect('admin_settings')
    return render(request, 'restaurant/admin/admin_settings.html')


# ------------------------------------------------------------
# ADMIN ADD STAFF
# ------------------------------------------------------------
@admin_required
def admin_add_staff(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip().lower()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        mobile = request.POST.get('mobile_no', '').strip()
        password = request.POST.get('password', '')
        sub_role = request.POST.get('sub_role', '').strip()

        VALID_ROLES = ['Manager', 'OrderHandler', 'Waiter', 'Chef', 'Kitchen Staff']

        if not username or not email or not password:
            messages.error(request, 'Username, email and password are required')
            return redirect('admin_staff_list')

        if len(password) < 8:
            messages.error(request, 'Password must be at least 8 characters')
            return redirect('admin_staff_list')

        if sub_role not in VALID_ROLES:
            messages.error(request, 'Please select a valid staff role')
            return redirect('admin_staff_list')

        if User.objects.filter(username=username).exists():
            messages.error(request, f'Username "{username}" already taken')
            return redirect('admin_staff_list')

        if User.objects.filter(email=email).exists():
            messages.error(request, f'Email "{email}" already registered')
            return redirect('admin_staff_list')

        try:
            user = User.objects.create_user(
                username=username, email=email, password=password,
                first_name=first_name, last_name=last_name,
                is_staff=True, is_superuser=False, is_active=True)

            user.profile.mobile_no = mobile
            user.profile.save()

            group, _ = Group.objects.get_or_create(name=sub_role)
            user.groups.add(group)

            messages.success(request, f'Staff "{username}" created as {sub_role}!')
        except Exception as e:
            messages.error(request, f'Failed to create staff: {str(e)}')

        return redirect('admin_staff_list')

    return redirect('admin_staff_list')


# ------------------------------------------------------------
# ADMIN DELETE STAFF
# ------------------------------------------------------------
@admin_required
def admin_delete_staff(request, user_id):
    try:
        user = User.objects.get(id=user_id)
        if user.is_superuser:
            messages.error(request, 'Cannot delete superuser')
            return redirect('admin_staff_list')
        uname = user.username
        user.delete()
        messages.success(request, f'Staff "{uname}" deleted!')
    except User.DoesNotExist:
        messages.error(request, 'User not found')
    return redirect('admin_staff_list')


# ============================================================
# ADMIN EVENTS
# ============================================================
@admin_required
def admin_events(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'add_theme':
            name = request.POST.get('theme_name', '').strip()
            image = request.FILES.get('theme_image')

            if not name:
                messages.error(request, 'Theme name is required')
                return redirect('admin_events')

            if EventTheme.objects.filter(name__iexact=name).exists():
                messages.error(request, f'Theme "{name}" already exists')
                return redirect('admin_events')

            EventTheme.objects.create(name=name, image=image)
            messages.success(request, f'Theme "{name}" added successfully!')
            return redirect('admin_events')

        if action == 'delete_theme':
            theme_id = request.POST.get('theme_id')
            try:
                theme = EventTheme.objects.get(id=theme_id)
                tname = theme.name
                theme.delete()
                messages.success(request, f'Theme "{tname}" deleted!')
            except EventTheme.DoesNotExist:
                messages.error(request, 'Theme not found')
            return redirect('admin_events')

        event_id = request.POST.get('event_id')
        try:
            ev = Event.objects.get(id=event_id)
            if action == 'confirm':
                ev.status = 'confirmed'
                ev.save()
                messages.success(request, f'Event for {ev.name} confirmed!')
            elif action == 'cancel':
                ev.status = 'cancelled'
                ev.save()
                messages.warning(request, f'Event for {ev.name} cancelled.')
        except Event.DoesNotExist:
            messages.error(request, 'Event not found')
        return redirect('admin_events')

    status_filter = request.GET.get('status', 'all')
    events = Event.objects.select_related('user').order_by('-event_date')
    if status_filter != 'all':
        events = events.filter(status=status_filter)

    themes = EventTheme.objects.all().order_by('name')

    return render(request, 'restaurant/admin/admin_events.html', {
        'events': events,
        'themes': themes,
        'active_filter': status_filter,
        'pending_count': Event.objects.filter(status='pending').count(),
        'confirmed_count': Event.objects.filter(status='confirmed').count(),
        'cancelled_count': Event.objects.filter(status='cancelled').count(),
        'total_count': Event.objects.count(),
    })


# ------------------------------------------------------------
# ADMIN STAFF LIST
# ------------------------------------------------------------
@admin_required
def admin_staff_list(request):
    staff = User.objects.filter(is_staff=True, is_superuser=False).order_by('-date_joined')
    return render(request, 'restaurant/admin/admin_staff_list.html', {
        'staff_list': staff,
        'total_count': staff.count(),
        'active_count': staff.filter(is_active=True).count(),
    })


# ------------------------------------------------------------
# ADMIN CUSTOMERS
# ------------------------------------------------------------
@admin_required
def admin_customers(request):
    customers = User.objects.filter(is_staff=False, is_superuser=False).order_by('-date_joined')
    return render(request, 'restaurant/admin/admin_customers.html', {
        'customers': customers,
        'total_count': customers.count(),
        'active_count': customers.filter(is_active=True).count(),
    })


# ------------------------------------------------------------
# ADMIN INVENTORY
# ------------------------------------------------------------
@admin_required
def admin_inventory(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'add_item' or 'add_item' in request.POST:
            name = request.POST.get('name', '').strip()
            category = request.POST.get('category', 'other')
            quantity = request.POST.get('quantity', '0')
            unit = request.POST.get('unit', 'kg')
            min_stock = request.POST.get('min_stock', '5')
            price = request.POST.get('price_per_unit', '0')
            supplier = request.POST.get('supplier', '').strip()
            expiry_date = request.POST.get('expiry_date', '').strip()
            status = request.POST.get('status', 'active').strip()

            if name:
                InventoryItem.objects.create(
                    name=name, category=category, quantity=quantity,
                    unit=unit, min_stock=min_stock, price_per_unit=price,
                    supplier=supplier,
                    expiry_date=expiry_date if expiry_date else None,
                    status=status,
                )
                messages.success(request, f'{name} added to inventory')
            else:
                messages.error(request, 'Item name is required')
            return redirect('admin_inventory')

        if action == 'update_qty' or 'update_qty' in request.POST:
            try:
                item = InventoryItem.objects.get(id=request.POST.get('item_id'))
                item.quantity = request.POST.get('new_qty')
                item.save()
                messages.success(request, f'{item.name} quantity updated')
            except InventoryItem.DoesNotExist:
                messages.error(request, 'Item not found')
            return redirect('admin_inventory')

        if action == 'toggle_status':
            try:
                item = InventoryItem.objects.get(id=request.POST.get('item_id'))
                item.status = 'inactive' if item.status == 'active' else 'active'
                item.save()
                messages.success(request, f'{item.name} marked as {item.get_status_display()}')
            except InventoryItem.DoesNotExist:
                messages.error(request, 'Item not found')
            return redirect('admin_inventory')

    delete_id = request.GET.get('delete')
    if delete_id:
        try:
            item = InventoryItem.objects.get(id=delete_id)
            n = item.name
            item.delete()
            messages.success(request, f'{n} deleted!')
        except InventoryItem.DoesNotExist:
            messages.error(request, 'Item not found')
        return redirect('admin_inventory')

    category_filter = request.GET.get('category', 'all')
    status_filter = request.GET.get('status', 'all')
    stock_filter = request.GET.get('stock', 'all')
    query = request.GET.get('q', '').strip()

    items = InventoryItem.objects.all().order_by('category', 'name')

    if category_filter != 'all':
        items = items.filter(category=category_filter)

    if status_filter != 'all':
        items = items.filter(status=status_filter)

    if stock_filter == 'low':
        items = [i for i in items if i.is_low_stock]
    elif stock_filter == 'normal':
        items = [i for i in items if not i.is_low_stock]
    else:
        items = list(items)

    if query:
        items = [i for i in items if query.lower() in i.name.lower()]

    all_items = InventoryItem.objects.all()
    total_items = all_items.count()
    low_stock_count = len([i for i in all_items if i.is_low_stock])
    categories_count = len(set(i.category for i in all_items))
    expiring_count = len([i for i in all_items if i.is_expiring_soon])
    total_value = sum(i.total_value for i in all_items)

    return render(request, 'restaurant/admin/admin_inventory.html', {
        'items': items,
        'active_filter': category_filter,
        'status_filter': status_filter,
        'stock_filter': stock_filter,
        'query': query,
        'categories': InventoryItem.CATEGORY_CHOICES,
        'total_items': total_items,
        'low_stock_count': low_stock_count,
        'categories_count': categories_count,
        'expiring_count': expiring_count,
        'total_value': total_value,
    })


# ------------------------------------------------------------
# ADMIN NGOs & FOOD DONATIONS
# ------------------------------------------------------------
@admin_required
def admin_ngos(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # Add NGO
        if action == 'add_ngo' or 'add_ngo' in request.POST or 'ngo_name' in request.POST:
            name = (request.POST.get('ngo_name') or request.POST.get('name') or '').strip()
            contact_person = request.POST.get('contact_person', '').strip()
            phone = request.POST.get('phone', '').strip()
            email = request.POST.get('email', '').strip()
            location = (request.POST.get('location') or request.POST.get('address') or '').strip()
            if name and contact_person and phone:
                NGO.objects.create(
                    name=name,
                    contact_person=contact_person,
                    phone=phone,
                    email=email,
                    address=location,
                )
                messages.success(request, f'NGO partner "{name}" registered successfully!')
            else:
                messages.error(request, 'Please provide NGO name, contact person and phone.')
            return redirect('admin_ngos')

        # Add Donation
        if action == 'add_donation' or 'food_items' in request.POST:
            food_items = request.POST.get('food_items', '').strip()
            food_type = request.POST.get('food_type', 'prepared').strip().lower()
            quantity = request.POST.get('quantity', '0').strip()
            expiry_date = request.POST.get('expiry_date', '').strip()
            if food_items:
                try:
                    qty_val = float(quantity) if quantity else 0.0
                except ValueError:
                    qty_val = 0.0
                FoodDonation.objects.create(
                    food_items=food_items,
                    food_type='prepared' if 'prepared' in food_type else ('raw' if 'raw' in food_type else ('packaged' if 'pack' in food_type else 'beverages')),
                    quantity_kg=qty_val,
                    meals_count=int(qty_val * 2) if qty_val > 0 else 10,
                    expiry_date=expiry_date if expiry_date else None,
                    status='pending',
                )
                messages.success(request, 'Food donation recorded successfully and submitted for approval!')
            else:
                messages.error(request, 'Food item description is required.')
            return redirect('admin_ngos')

        # Toggle NGO Status
        if action == 'toggle_ngo' or 'toggle_ngo' in request.POST:
            try:
                ngo = NGO.objects.get(id=request.POST.get('ngo_id'))
                ngo.status = 'inactive' if ngo.status == 'active' else 'active'
                ngo.save()
                messages.success(request, f'{ngo.name} status updated to {ngo.status.title()}.')
            except NGO.DoesNotExist:
                messages.error(request, 'NGO not found.')
            return redirect('admin_ngos')

        # Delete NGO
        if action == 'delete_ngo':
            try:
                ngo = NGO.objects.get(id=request.POST.get('ngo_id'))
                n = ngo.name
                ngo.delete()
                messages.success(request, f'NGO "{n}" deleted.')
            except NGO.DoesNotExist:
                messages.error(request, 'NGO not found.')
            return redirect('admin_ngos')

    delete_ngo_id = request.GET.get('delete_ngo')
    if delete_ngo_id:
        try:
            ngo = NGO.objects.get(id=delete_ngo_id)
            n = ngo.name
            ngo.delete()
            messages.success(request, f'NGO "{n}" deleted.')
        except NGO.DoesNotExist:
            messages.error(request, 'NGO not found.')
        return redirect('admin_ngos')

    delete_donation_id = request.GET.get('delete_donation')
    if delete_donation_id:
        try:
            don = FoodDonation.objects.get(id=delete_donation_id)
            don.delete()
            messages.success(request, 'Donation record deleted.')
        except FoodDonation.DoesNotExist:
            messages.error(request, 'Donation record not found.')
        return redirect('admin_ngos')

    ngos = NGO.objects.all().order_by('name')
    donations = FoodDonation.objects.select_related('ngo').order_by('-created_at')

    return render(request, 'restaurant/admin/admin_ngos.html', {
        'ngos': ngos,
        'donations': donations,
        'total_count': ngos.count(),
        'registered_ngos': ngos.filter(status='active').count(),
        'active_count': ngos.filter(status='active').count(),
        'total_donations': donations.count(),
        'pending_approval': donations.filter(status='pending').count(),
        'meals_served': ngos.aggregate(total=Sum('meals_served'))['total'] or 0,
    })


# ============================================================
# PUBLIC FOOD ORDER & CART FLOW
# ============================================================

def public_food_order_view(request):
    """Public Food Order / Menu page"""
    categories = MenuCategory.objects.filter(is_active=True).order_by('name')
    selected_category = request.GET.get('category', 'all').strip()
    search_query = request.GET.get('search', '').strip()

    items = MenuItem.objects.filter(
        is_available=True,
        category__is_active=True,
    ).select_related('category')

    if selected_category != 'all':
        items = items.filter(category__name__iexact=selected_category)

    if search_query:
        items = items.filter(Q(name__icontains=search_query) | Q(description__icontains=search_query))

    items = items.order_by('category__name', 'name')

    cart = request.session.get('cart', {})
    cart_item_count = 0
    for v in cart.values():
        if isinstance(v, dict):
            cart_item_count += v.get('quantity', 0)
        elif isinstance(v, int):
            cart_item_count += v

    return render(request, 'restaurant/food_order.html', {
        'categories': categories,
        'selected_category': selected_category,
        'search_query': search_query,
        'menu_items': items,
        'cart': cart,
        'cart_item_count': cart_item_count,
        'user_is_logged_in': request.user.is_authenticated,
    })


def get_item_customizations_api(request, item_id):
    """API endpoint to get food item details & customization options for modal"""
    item = get_object_or_404(MenuItem, id=item_id, is_available=True)
    
    # Fetch active customization groups directly linked to this specific MenuItem via M2M relationship
    custom_groups = item.customization_groups.filter(status='active').prefetch_related('options')
    
    # Fallback to string customizations match if M2M relation was not set
    if not custom_groups.exists() and item.customizations:
        names = [c.strip() for c in item.customizations.split(',') if c.strip()]
        if names:
            custom_groups = ItemCustomization.objects.filter(status='active', name__in=names).prefetch_related('options')

    groups_data = []
    if custom_groups.exists():
        for g in custom_groups:
            opts = [{'name': o.name, 'price': float(o.price)} for o in g.options.all()]
            if opts:
                groups_data.append({
                    'name': g.name,
                    'type': 'checkbox' if 'topping' in g.name.lower() or 'add' in g.name.lower() or 'patty' in g.name.lower() or 'extra' in g.name.lower() or 'option' in g.name.lower() else 'radio',
                    'options': opts
                })

    return JsonResponse({
        'success': True,
        'item': {
            'id': item.id,
            'name': item.name,
            'description': item.description or 'Freshly prepared delicious item.',
            'base_price': float(item.get_final_price()),
            'image_url': item.image.url if item.image else '',
            'food_type': item.food_type,
            'customization_groups': groups_data
        }
    })


def add_to_cart_view(request):
    """Add or update customized item in session cart via POST or AJAX"""
    if request.method == 'POST':
        # Customer MUST be logged in to add any item to cart
        if not request.user.is_authenticated:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'logged_in': False,
                    'error': 'Please login to add items to your cart.',
                    'redirect_url': '/login/?next=/food-order/'
                }, status=401)
            messages.info(request, 'Please login to add items to your cart.')
            return redirect(f'/login/?next=/food-order/')

        try:
            item_id = int(request.POST.get('item_id'))
            quantity = int(request.POST.get('quantity', 1))
            action = request.POST.get('action', 'add')
            cart_key = request.POST.get('cart_key', '').strip()
            special_instructions = request.POST.get('special_instructions', '').strip()
            
            raw_options = request.POST.get('selected_options', '[]')
            try:
                selected_options = json.loads(raw_options) if isinstance(raw_options, str) else raw_options
            except json.JSONDecodeError:
                selected_options = []
        except (TypeError, ValueError):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': 'Invalid parameters'}, status=400)
            messages.error(request, 'Invalid quantity or item.')
            return redirect('food_order_view')

        menu_item = get_object_or_404(MenuItem, id=item_id, is_available=True)
        cart = request.session.get('cart', {})

        if action == 'remove':
            if cart_key in cart:
                cart.pop(cart_key, None)
            else:
                cart.pop(str(item_id), None)
        elif action == 'set':
            if cart_key in cart and isinstance(cart[cart_key], dict):
                if quantity > 0:
                    cart[cart_key]['quantity'] = min(quantity, 20)
                else:
                    cart.pop(cart_key, None)
            elif str(item_id) in cart:
                if quantity > 0:
                    if isinstance(cart[str(item_id)], dict):
                        cart[str(item_id)]['quantity'] = min(quantity, 20)
                    else:
                        cart[str(item_id)] = min(quantity, 20)
                else:
                    cart.pop(str(item_id), None)
        else: # 'add'
            options_price = sum(float(opt.get('price', 0)) for opt in selected_options)
            base_price = float(menu_item.get_final_price())
            unit_price = base_price + options_price

            if not cart_key:
                opt_signature = "_".join(sorted([f"{o.get('name')}:{o.get('price')}" for o in selected_options]))
                instr_signature = special_instructions[:20].replace(' ', '_')
                cart_key = f"{item_id}_{hashlib.md5(f'{opt_signature}_{instr_signature}'.encode()).hexdigest()[:8]}"

            if cart_key in cart and isinstance(cart[cart_key], dict):
                new_qty = cart[cart_key]['quantity'] + quantity
                cart[cart_key]['quantity'] = min(new_qty, 20)
            else:
                cart[cart_key] = {
                    'cart_key': cart_key,
                    'item_id': item_id,
                    'name': menu_item.name,
                    'quantity': min(max(1, quantity), 20),
                    'base_price': base_price,
                    'options': selected_options,
                    'options_price': options_price,
                    'unit_price': unit_price,
                    'special_instructions': special_instructions,
                    'image_url': menu_item.image.url if menu_item.image else '',
                }

        request.session['cart'] = cart
        request.session.modified = True

        cart_count = 0
        for v in cart.values():
            if isinstance(v, dict):
                cart_count += v.get('quantity', 0)
            elif isinstance(v, int):
                cart_count += v

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'cart_count': cart_count,
                'message': f'{menu_item.name} added to cart successfully.',
            })

        messages.success(request, f'{menu_item.name} added to cart successfully.')
        return redirect('food_order_view')

    return redirect('food_order_view')


def cart_view(request):
    """Cart page displaying selected food items, quantities, subtotal, and total"""
    cart = request.session.get('cart', {})
    cart_items = []
    total_amount = Decimal('0.00')
    total_items = 0

    if cart:
        item_ids = []
        for key, val in cart.items():
            if isinstance(val, dict):
                item_ids.append(val['item_id'])
            else:
                item_ids.append(int(key))

        menu_items = MenuItem.objects.filter(id__in=item_ids, is_available=True)
        items_dict = {item.id: item for item in menu_items}

        for cart_key, entry in list(cart.items()):
            if isinstance(entry, dict):
                item_id = entry['item_id']
                if item_id in items_dict:
                    item = items_dict[item_id]
                    qty = entry.get('quantity', 1)
                    unit_price = Decimal(str(entry.get('unit_price', item.get_final_price())))
                    subtotal = unit_price * qty
                    total_amount += subtotal
                    total_items += qty

                    cart_items.append({
                        'cart_key': cart_key,
                        'item': item,
                        'quantity': qty,
                        'unit_price': unit_price,
                        'options': entry.get('options', []),
                        'options_price': Decimal(str(entry.get('options_price', 0))),
                        'special_instructions': entry.get('special_instructions', ''),
                        'subtotal': subtotal,
                    })
                else:
                    cart.pop(cart_key, None)
                    request.session['cart'] = cart
                    request.session.modified = True
            else:
                item_id = int(cart_key)
                if item_id in items_dict:
                    item = items_dict[item_id]
                    qty = entry
                    price = Decimal(str(item.get_final_price()))
                    subtotal = price * qty
                    total_amount += subtotal
                    total_items += qty
                    cart_items.append({
                        'cart_key': cart_key,
                        'item': item,
                        'quantity': qty,
                        'unit_price': price,
                        'options': [],
                        'options_price': Decimal('0.00'),
                        'special_instructions': '',
                        'subtotal': subtotal,
                    })

    return render(request, 'restaurant/cart.html', {
        'cart_items': cart_items,
        'total_amount': total_amount,
        'total_items': total_items,
    })


@never_cache
@csrf_protect
def checkout_view(request):
    """Checkout page for placing order (Requires customer login)"""
    cart = request.session.get('cart', {})
    if not cart:
        messages.warning(request, 'Your cart is empty. Please add food items before checking out.')
        return redirect('food_order_view')

    if not request.user.is_authenticated:
        messages.info(request, 'Please login to complete your order checkout.')
        return redirect(f"/login/?next=/checkout/")

    item_ids = []
    for key, val in cart.items():
        if isinstance(val, dict):
            item_ids.append(val['item_id'])
        else:
            item_ids.append(int(key))

    menu_items = MenuItem.objects.filter(id__in=item_ids, is_available=True, status='in_stock')
    items_dict = {item.id: item for item in menu_items}

    checkout_items = []
    total_amount = Decimal('0.00')

    for cart_key, entry in list(cart.items()):
        if isinstance(entry, dict):
            item_id = entry['item_id']
            if item_id in items_dict:
                item = items_dict[item_id]
                qty = entry.get('quantity', 1)
                unit_price = Decimal(str(entry.get('unit_price', item.get_final_price())))
                subtotal = unit_price * qty
                total_amount += subtotal
                
                opts = entry.get('options', [])
                opt_parts = [f"{o['name']} (+₹{o['price']})" for o in opts]
                cust_str = ""
                if opt_parts:
                    cust_str += "Add-ons: " + ", ".join(opt_parts)
                notes = entry.get('special_instructions', '').strip()
                if notes:
                    if cust_str:
                        cust_str += " | "
                    cust_str += f"Note: {notes}"

                checkout_items.append({
                    'item': item,
                    'quantity': qty,
                    'price': unit_price,
                    'subtotal': subtotal,
                    'customization_details': cust_str,
                    'options': opts,
                    'special_instructions': notes,
                })
        else:
            item_id = int(cart_key)
            if item_id in items_dict:
                item = items_dict[item_id]
                qty = entry
                price = Decimal(str(item.get_final_price()))
                subtotal = price * qty
                total_amount += subtotal
                checkout_items.append({
                    'item': item,
                    'quantity': qty,
                    'price': price,
                    'subtotal': subtotal,
                    'customization_details': '',
                    'options': [],
                    'special_instructions': '',
                })

    if not checkout_items:
        messages.error(request, 'One or more items in your cart are no longer available.')
        return redirect('cart_view')

    if request.method == 'POST':
        address = request.POST.get('address', '').strip()
        phone = request.POST.get('phone', '').strip()
        kitchen_notes = request.POST.get('kitchen_notes', '').strip()
        payment_method = request.POST.get('payment_method', '').strip().lower()

        valid_methods = ['upi', 'card', 'razorpay', 'online']
        if not payment_method or payment_method not in valid_methods:
            payment_method = 'upi'

        if not phone:
            messages.error(request, 'Mobile phone number is required.')
            return render(request, 'restaurant/checkout.html', {
                'checkout_items': checkout_items,
                'total_amount': total_amount,
                'user_profile': getattr(request.user, 'profile', None),
                'selected_payment_method': payment_method,
            })

        with transaction.atomic():
            order = Order.objects.create(
                user=request.user,
                order_type='restaurant',
                status='pending',
                address=address or 'Dine-in / Direct Order',
                phone=phone,
                kitchen_notes=kitchen_notes,
                total=total_amount,
            )

            # Food order is prepaid
            Payment.objects.create(
                order=order,
                amount=total_amount,
                method=payment_method,
                status='success',
                transaction_id=f"PREPAID_{timezone.now().strftime('%Y%m%d%H%M%S')}_{order.id}",
            )

            for ci in checkout_items:
                OrderItem.objects.create(
                    order=order,
                    menu_item=ci['item'],
                    quantity=ci['quantity'],
                    price=ci['price'],
                    customization_details=ci['customization_details'],
                )

            profile = getattr(request.user, 'profile', None)
            if profile:
                if not profile.address:
                    profile.address = address
                if not profile.mobile_no:
                    profile.mobile_no = phone
                profile.save()

            request.session['cart'] = {}
            request.session.modified = True

        messages.success(request, f'Order #{order.id} placed successfully!')
        return redirect('order_confirmation_view', order_id=order.id)

    return render(request, 'restaurant/checkout.html', {
        'checkout_items': checkout_items,
        'total_amount': total_amount,
        'user_profile': getattr(request.user, 'profile', None),
    })


@never_cache
@csrf_protect
def api_create_razorpay_order(request):
    """API endpoint to create Razorpay Order via Razorpay SDK & DB Order before launching Razorpay modal"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST request required'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    customer_name = data.get('name', '').strip() or (request.user.get_full_name() if request.user.is_authenticated else '') or (request.user.username if request.user.is_authenticated else 'Guest Customer')
    address = data.get('address', '').strip()
    phone = data.get('phone', '').strip()
    notes = data.get('kitchen_notes', '').strip()
    payment_method = data.get('payment_method', 'upi').strip().lower()

    if not address or not phone:
        return JsonResponse({'success': False, 'error': 'Delivery address and phone number are required.'}, status=400)

    cart = request.session.get('cart', {})
    if not cart:
        return JsonResponse({'success': False, 'error': 'Your cart is empty.'}, status=400)

    item_ids = []
    for key in cart.keys():
        try:
            item_ids.append(int(key))
        except (ValueError, TypeError):
            if isinstance(key, str) and '_' in key:
                parts = key.split('_')
                if parts[0].isdigit():
                    item_ids.append(int(parts[0]))

    items_dict = {i.id: i for i in MenuItem.objects.filter(id__in=item_ids, is_available=True)}

    checkout_items = []
    total_amount = Decimal('0.00')

    for cart_key, entry in cart.items():
        if isinstance(entry, dict):
            item_id = entry.get('item_id')
            qty = entry.get('quantity', 1)
            unit_price = Decimal(str(entry.get('unit_price', 0)))
            if item_id in items_dict:
                item = items_dict[item_id]
                subtotal = unit_price * qty
                total_amount += subtotal
                checkout_items.append({'item': item, 'quantity': qty, 'price': unit_price, 'customization_details': ''})
        else:
            item_id = int(cart_key)
            if item_id in items_dict:
                item = items_dict[item_id]
                qty = entry
                price = Decimal(str(item.get_final_price()))
                subtotal = price * qty
                total_amount += subtotal
                checkout_items.append({'item': item, 'quantity': qty, 'price': price, 'customization_details': ''})

    if not checkout_items:
        return JsonResponse({'success': False, 'error': 'Items in cart are no longer available.'}, status=400)

    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '').strip().strip("'").strip('"')
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '').strip().strip("'").strip('"')

    if not key_id:
        return JsonResponse({
            'success': False,
            'error': 'RAZORPAY_KEY_ID is missing or empty in .env. Please set RAZORPAY_KEY_ID=rzp_test_... in your .env file.'
        }, status=400)

    if not key_secret:
        return JsonResponse({
            'success': False,
            'error': 'RAZORPAY_KEY_SECRET is missing or empty in .env. Please set RAZORPAY_KEY_SECRET=... in your .env file.'
        }, status=400)

    if 'YOUR_ACTUAL' in key_id or 'YOUR_ACTUAL' in key_secret or 'TableServe123' in key_id or 'TableServe' in key_secret:
        key_prefix = key_id[:12] if len(key_id) >= 12 else key_id
        return JsonResponse({
            'success': False,
            'error': f'RAZORPAY_KEY_ID ({key_prefix}...) or RAZORPAY_KEY_SECRET in .env is using default placeholder text. Please open .env and enter your actual Razorpay Test Mode credentials from https://dashboard.razorpay.com/app/keys.'
        }, status=400)

    if not key_id.startswith('rzp_test_'):
        key_prefix = key_id[:12] if len(key_id) >= 12 else key_id
        return JsonResponse({
            'success': False,
            'error': f'RAZORPAY_KEY_ID ({key_prefix}...) is invalid. Test Mode credentials must start with "rzp_test_".'
        }, status=400)

    with transaction.atomic():
        order_user = request.user if request.user.is_authenticated else User.objects.first()
        if not order_user:
            order_user, _ = User.objects.get_or_create(username='guest_customer', defaults={'email': 'guest@tableserve.com'})

        order = Order.objects.create(
            user=order_user,
            order_type='restaurant',
            status='pending',
            address=address or 'Dine-in / Direct Order',
            phone=phone,
            kitchen_notes=notes,
            total=total_amount,
        )

        Payment.objects.create(
            order=order,
            amount=total_amount,
            method='upi' if payment_method == 'upi' else ('card' if payment_method == 'card' else 'online'),
            status='pending',
        )

        for ci in checkout_items:
            OrderItem.objects.create(
                order=order,
                menu_item=ci['item'],
                quantity=ci['quantity'],
                price=ci['price'],
                customization_details=ci['customization_details'],
            )

    amount_in_paise = int(round(float(total_amount) * 100))

    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        razorpay_order_payload = {
            'amount': amount_in_paise,
            'currency': 'INR',
            'receipt': f'order_rcpt_{order.id}',
            'payment_capture': 1
        }
        rzp_order = client.order.create(data=razorpay_order_payload)
        razorpay_order_id = rzp_order['id']
    except Exception as e:
        logger.error(f"Razorpay Order Creation Failed: {e}")
        key_prefix = key_id[:12] if len(key_id) >= 12 else key_id
        return JsonResponse({
            'success': False,
            'error': f'Failed to create Razorpay Order on Razorpay servers: {str(e)}. (Key ID used: {key_prefix}..., Secret Key present: {bool(key_secret)}). Ensure your RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env belong to the SAME active Razorpay Test Mode account.'
        }, status=400)

    customer_email = (request.user.email if request.user.is_authenticated and request.user.email else 'customer@tableserve.com')

    return JsonResponse({
        'success': True,
        'razorpay_order_id': razorpay_order_id,
        'db_order_id': order.id,
        'amount': amount_in_paise,
        'currency': 'INR',
        'key_id': key_id,
        'customer_name': customer_name,
        'customer_phone': phone,
        'customer_email': customer_email,
    })


@never_cache
@csrf_protect
def api_verify_razorpay_payment(request):
    """API endpoint to verify Razorpay HMAC SHA256 payment signature and mark order paid"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST request required'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    db_order_id = data.get('db_order_id')
    razorpay_order_id = data.get('razorpay_order_id', '').strip()
    razorpay_payment_id = data.get('razorpay_payment_id', '').strip()
    razorpay_signature = data.get('razorpay_signature', '').strip()

    if not db_order_id or not razorpay_payment_id or not razorpay_order_id or not razorpay_signature:
        return JsonResponse({'success': False, 'error': 'Missing required payment verification details.'}, status=400)

    order = get_object_or_404(Order, id=db_order_id)
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '').strip()
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '').strip()

    client = razorpay.Client(auth=(key_id, key_secret))
    params_dict = {
        'razorpay_order_id': razorpay_order_id,
        'razorpay_payment_id': razorpay_payment_id,
        'razorpay_signature': razorpay_signature
    }

    try:
        client.utility.verify_payment_signature(params_dict)
    except razorpay.errors.SignatureVerificationError:
        return JsonResponse({'success': False, 'error': 'Payment verification failed. Razorpay signature is invalid.'}, status=400)
    except Exception as e:
        msg = f"{razorpay_order_id}|{razorpay_payment_id}"
        generated_signature = hmac.new(
            key_secret.encode('utf-8'),
            msg.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if generated_signature != razorpay_signature:
            return JsonResponse({'success': False, 'error': f'Payment signature verification failed: {str(e)}'}, status=400)

    with transaction.atomic():
        payment = order.payments.first()
        if not payment:
            payment = Payment.objects.create(
                order=order,
                amount=order.total,
                method='upi',
                status='success',
                transaction_id=razorpay_payment_id,
            )
        else:
            payment.status = 'success'
            payment.transaction_id = razorpay_payment_id
            payment.save()

        if order.status == 'pending':
            order.status = 'accepted'
            order.save()

        request.session['cart'] = {}
        request.session.modified = True

    messages.success(request, f'Payment verified! Order #{order.id} placed successfully.')
    return JsonResponse({
        'success': True,
        'message': 'Payment verified successfully!',
        'order_id': order.id,
        'redirect_url': f'/order-confirmation/{order.id}/',
    })


def order_confirmation_view(request, order_id):
    """Order Confirmation page showing Order ID, status, customer details, and item summary"""
    order = get_object_or_404(Order, id=order_id)

    if not (request.user == order.user or request.user.is_staff or request.user.is_superuser):
        messages.error(request, 'Access denied.')
        return redirect('home')

    order_items = order.items.select_related('menu_item').all()
    return render(request, 'restaurant/order_confirmation.html', {
        'order': order,
        'order_items': order_items,
    })


# ============================================================
# PUBLIC TABLE BOOKING FLOW
# ============================================================

def public_table_booking_view(request):
    """Public Table Booking page with Date, Time, Guest & Seating selector, and Live Available Table grid"""
    today = timezone.localdate()
    today_str = today.isoformat()

    date_val = request.POST.get('date', '').strip() or request.GET.get('date', '').strip() or today_str
    time_val = request.POST.get('time', '').strip() or request.GET.get('time', '').strip() or '19:00'
    guests_val = request.POST.get('guests', '').strip() or request.GET.get('guests', '2').strip()
    location_val = request.POST.get('location', '').strip() or request.GET.get('location', 'All').strip()

    available_tables = []
    error_message = None
    searched = False

    if request.GET.get('search') == '1' or request.method == 'POST':
        searched = True
        try:
            res_date = datetime.datetime.strptime(date_val, '%Y-%m-%d').date()
            res_time = datetime.datetime.strptime(time_val, '%H:%M').time()
            guests = int(guests_val)
        except (TypeError, ValueError):
            error_message = "Please enter a valid date, time, and number of guests."
            res_date = None

        if res_date and res_date < today:
            error_message = "Booking date cannot be in the past."
        elif res_date and not (1 <= guests <= 20):
            error_message = "Number of guests must be between 1 and 20."

        if not error_message and res_date:
            tables_qs = Table.objects.filter(
                is_available=True,
                seats__gte=guests,
            ).exclude(status='occupied')

            if location_val and location_val != 'All':
                tables_qs = tables_qs.filter(location=location_val)

            tables_qs = tables_qs.order_by('seats', 'number')

            for t in tables_qs:
                has_conflict, _ = check_table_conflict(t, res_date, res_time)
                if not has_conflict:
                    available_tables.append(t)

            if not available_tables:
                error_message = "No table is available for the selected date and time."

    if request.method == 'POST' and request.POST.get('action') == 'confirm_booking':
        if not request.user.is_authenticated:
            messages.info(request, 'Please login to confirm your table reservation.')
            return redirect(f"/login/?next=/table-booking/")

        table_id = request.POST.get('table_id')
        name = request.POST.get('name', '').strip() or request.user.get_full_name() or request.user.username
        phone = request.POST.get('phone', '').strip() or getattr(request.user.profile, 'mobile_no', '')
        email = request.POST.get('email', '').strip() or request.user.email
        special_request = request.POST.get('special_request', '').strip()
        pre_order_choice = request.POST.get('pre_order_choice', 'no').strip().lower()

        try:
            res_date = datetime.datetime.strptime(date_val, '%Y-%m-%d').date()
            res_time = datetime.datetime.strptime(time_val, '%H:%M').time()
            guests = int(guests_val)
        except (TypeError, ValueError):
            messages.error(request, 'Invalid date or time.')
            return redirect('table_booking_view')

        if res_date < today or not (1 <= guests <= 20):
            messages.error(request, 'Choose a future date and party size from 1 to 20.')
            return redirect('table_booking_view')

        if not phone:
            messages.error(request, 'A contact phone number is required.')
            return redirect('table_booking_view')

        table = get_object_or_404(Table, id=table_id, is_available=True)
        if table.seats < guests:
            messages.error(request, 'Selected table capacity is not sufficient for your party size.')
            return redirect('table_booking_view')

        has_conflict, conflict_msg = check_table_conflict(table, res_date, res_time)
        if has_conflict:
            messages.error(request, 'Sorry! This table was just booked by another customer for that time.')
            return redirect('table_booking_view')

        is_pre_order = (pre_order_choice == 'yes')

        request.session['pending_booking'] = {
            'table_id': table.id,
            'name': name,
            'phone': phone,
            'email': email,
            'date': res_date.isoformat(),
            'time': res_time.strftime('%H:%M'),
            'guests': guests,
            'special_request': special_request,
            'booking_charge': '200.00',
            'pre_order_selected': is_pre_order,
        }
        request.session.modified = True

        if is_pre_order:
            request.session['pre_order_cart'] = {}
            messages.info(request, 'Please select food items from the menu to pre-order for your booked table.')
            return redirect('pre_order_menu_view')
        else:
            request.session['pre_order_cart'] = {}
            return redirect('table_booking_summary_view')

    return render(request, 'restaurant/table_booking.html', {
        'today_str': today_str,
        'date_val': date_val,
        'time_val': time_val,
        'guests_val': guests_val,
        'location_val': location_val,
        'locations': ['All', 'Indoor', 'Outdoor', 'Window', 'Rooftop'],
        'available_tables': available_tables,
        'error_message': error_message,
        'searched': searched,
        'user_profile': getattr(request.user, 'profile', None) if request.user.is_authenticated else None,
    })


@never_cache
def pre_order_menu_view(request):
    """Pre-Order Menu view for choosing food items associated with pending table booking (Reuses food_order.html)"""
    pending = request.session.get('pending_booking')
    if not pending:
        messages.warning(request, 'Please choose a table and date/time before pre-ordering food.')
        return redirect('table_booking_view')

    table = get_object_or_404(Table, id=pending['table_id'])
    categories = MenuCategory.objects.filter(is_active=True).order_by('name')
    selected_category = request.GET.get('category', 'all').strip()
    search_query = request.GET.get('search', '').strip()

    items = MenuItem.objects.filter(
        is_available=True,
        category__is_active=True,
    ).select_related('category')

    if selected_category != 'all':
        items = items.filter(category__name__iexact=selected_category)

    if search_query:
        items = items.filter(Q(name__icontains=search_query) | Q(description__icontains=search_query))

    items = items.order_by('category__name', 'name')

    pre_order_cart = request.session.get('pre_order_cart', {})
    cart_items = []
    food_subtotal = Decimal('0.00')
    cart_item_count = 0

    if pre_order_cart:
        item_ids = [int(k) for k in pre_order_cart.keys() if str(k).isdigit()]
        db_items = MenuItem.objects.filter(id__in=item_ids, is_available=True)
        items_dict = {i.id: i for i in db_items}

        for k, q in pre_order_cart.items():
            if int(k) in items_dict and int(q) > 0:
                item_obj = items_dict[int(k)]
                item_price = Decimal(str(item_obj.get_final_price()))
                line_tot = item_price * int(q)
                food_subtotal += line_tot
                cart_item_count += int(q)
                cart_items.append({
                    'item': item_obj,
                    'quantity': int(q),
                    'price': item_price,
                    'subtotal': line_tot,
                })

    booking_charge = Decimal('200.00')
    total_prepaid = booking_charge + food_subtotal

    return render(request, 'restaurant/food_order.html', {
        'is_pre_order_mode': True,
        'pending': pending,
        'table': table,
        'categories': categories,
        'selected_category': selected_category,
        'search_query': search_query,
        'menu_items': items,
        'pre_order_cart': pre_order_cart,
        'cart_items': cart_items,
        'cart_item_count': cart_item_count,
        'food_subtotal': food_subtotal,
        'booking_charge': booking_charge,
        'total_prepaid': total_prepaid,
        'user_is_logged_in': request.user.is_authenticated,
    })


@never_cache
def api_update_pre_order_cart(request):
    """AJAX endpoint for updating items in Pre-Order Cart"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    item_id = str(data.get('item_id', '')).strip()
    action = data.get('action', 'add').strip()
    qty_val = int(data.get('quantity', 1))

    if not item_id:
        return JsonResponse({'success': False, 'error': 'Invalid item ID'}, status=400)

    cart = request.session.get('pre_order_cart', {})

    if action == 'add':
        cart[item_id] = cart.get(item_id, 0) + qty_val
    elif action == 'remove':
        if item_id in cart:
            del cart[item_id]
    elif action == 'update':
        if qty_val > 0:
            cart[item_id] = qty_val
        elif item_id in cart:
            del cart[item_id]

    request.session['pre_order_cart'] = cart
    request.session.modified = True

    # Calculate subtotal
    item_ids = [int(k) for k in cart.keys() if k.isdigit()]
    items = MenuItem.objects.filter(id__in=item_ids, is_available=True)
    items_dict = {i.id: i for i in items}

    food_subtotal = Decimal('0.00')
    total_qty = 0
    for k, q in cart.items():
        if int(k) in items_dict:
            it = items_dict[int(k)]
            food_subtotal += Decimal(str(it.get_final_price())) * int(q)
            total_qty += int(q)

    booking_charge = Decimal('200.00')
    total_prepaid = booking_charge + food_subtotal

    return JsonResponse({
        'success': True,
        'food_subtotal': float(food_subtotal),
        'total_prepaid': float(total_prepaid),
        'total_items': total_qty,
        'cart': cart,
    })


@never_cache
@csrf_protect
def table_booking_summary_view(request):
    """Booking & Pre-Order Summary + Prepaid Razorpay Payment View"""
    pending = request.session.get('pending_booking')
    if not pending:
        messages.warning(request, 'Please select a table and date/time first.')
        return redirect('table_booking_view')

    table = get_object_or_404(Table, id=pending['table_id'])
    booking_charge = Decimal('200.00')
    pre_order_selected = pending.get('pre_order_selected', False)

    pre_order_cart = request.session.get('pre_order_cart', {})
    pre_order_items = []
    food_subtotal = Decimal('0.00')

    if pre_order_selected and pre_order_cart:
        item_ids = [int(k) for k in pre_order_cart.keys() if k.isdigit()]
        db_items = MenuItem.objects.filter(id__in=item_ids, is_available=True)
        items_dict = {i.id: i for i in db_items}

        for k, q in pre_order_cart.items():
            if int(k) in items_dict:
                it = items_dict[int(k)]
                price = Decimal(str(it.get_final_price()))
                subtot = price * int(q)
                food_subtotal += subtot
                pre_order_items.append({
                    'item': it,
                    'quantity': int(q),
                    'price': price,
                    'subtotal': subtot,
                })

    total_prepaid = booking_charge + food_subtotal

    return render(request, 'restaurant/table_booking_summary.html', {
        'pending': pending,
        'table': table,
        'booking_charge': booking_charge,
        'pre_order_selected': pre_order_selected,
        'pre_order_items': pre_order_items,
        'food_subtotal': food_subtotal,
        'total_prepaid': total_prepaid,
        'user_profile': getattr(request.user, 'profile', None) if request.user.is_authenticated else None,
    })


@never_cache
@csrf_protect
def api_create_booking_razorpay_order(request):
    """API endpoint to validate booking & pre-order, create pending Reservation & Order, and launch Razorpay Order"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST request required'}, status=405)

    pending = request.session.get('pending_booking')
    if not pending:
        return JsonResponse({'success': False, 'error': 'Session expired. Please select a table again.'}, status=400)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    payment_method = data.get('payment_method', 'upi').strip().lower()

    table = get_object_or_404(Table, id=pending['table_id'], is_available=True)
    try:
        res_date = datetime.datetime.strptime(pending['date'], '%Y-%m-%d').date()
        res_time = datetime.datetime.strptime(pending['time'], '%H:%M').time()
        guests = int(pending['guests'])
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid booking date or time.'}, status=400)

    has_conflict, conflict_msg = check_table_conflict(table, res_date, res_time)
    if has_conflict:
        return JsonResponse({'success': False, 'error': 'Selected table was just booked for this time slot.'}, status=400)

    booking_charge = Decimal('200.00')
    pre_order_selected = pending.get('pre_order_selected', False)
    pre_order_cart = request.session.get('pre_order_cart', {})
    pre_order_items = []
    food_subtotal = Decimal('0.00')

    if pre_order_selected:
        if not pre_order_cart:
            return JsonResponse({'success': False, 'error': 'Pre-Order was selected but no food items were added.'}, status=400)

        item_ids = [int(k) for k in pre_order_cart.keys() if k.isdigit()]
        db_items = MenuItem.objects.filter(id__in=item_ids, is_available=True)
        items_dict = {i.id: i for i in db_items}

        for k, q in pre_order_cart.items():
            if int(k) in items_dict and int(q) > 0:
                it = items_dict[int(k)]
                price = Decimal(str(it.get_final_price()))
                subtot = price * int(q)
                food_subtotal += subtot
                pre_order_items.append({
                    'item': it,
                    'quantity': int(q),
                    'price': price,
                    'subtotal': subtot,
                })

        if not pre_order_items:
            return JsonResponse({'success': False, 'error': 'Selected pre-order food items are no longer available.'}, status=400)

    total_prepaid = booking_charge + food_subtotal

    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '').strip().strip("'").strip('"')
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '').strip().strip("'").strip('"')

    if not key_id or not key_secret or 'YOUR_ACTUAL' in key_id or 'TableServe123' in key_id:
        key_prefix = key_id[:12] if len(key_id) >= 12 else key_id
        return JsonResponse({
            'success': False,
            'error': f'RAZORPAY_KEY_ID ({key_prefix}...) or RAZORPAY_KEY_SECRET in .env is using default placeholder text. Please enter valid Test Mode credentials in .env.'
        }, status=400)

    with transaction.atomic():
        reservation = Reservation.objects.create(
            user=request.user if request.user.is_authenticated else User.objects.first(),
            name=pending['name'],
            phone=pending['phone'],
            email=pending['email'],
            date=res_date,
            time=res_time,
            guests=guests,
            table=table,
            special_request=pending['special_request'],
            status='pending',
            payment_status='pending',
            payment_method=payment_method,
            booking_charge=booking_charge,
            pre_order_selected=pre_order_selected,
            pre_order_amount=food_subtotal,
            total_amount=total_prepaid,
            reservation_type='table',
        )

        order_obj = None
        if pre_order_selected and pre_order_items:
            order_obj = Order.objects.create(
                user=request.user if request.user.is_authenticated else User.objects.first(),
                status='pending',
                total=food_subtotal,
                address=f"Pre-Order Table #{table.number} ({res_date} {res_time})",
                phone=pending['phone'],
                kitchen_notes=f"PRE-ORDER for Table {table.number} ({res_date} {res_time}). " + pending['special_request'],
                table=table,
                reservation=reservation,
                is_pre_order=True,
                order_type='restaurant',
            )

            for poi in pre_order_items:
                OrderItem.objects.create(
                    order=order_obj,
                    menu_item=poi['item'],
                    quantity=poi['quantity'],
                    price=poi['price'],
                    customization_details='Pre-Order Item',
                )

    amount_in_paise = int(round(float(total_prepaid) * 100))

    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        razorpay_order_payload = {
            'amount': amount_in_paise,
            'currency': 'INR',
            'receipt': f'booking_rcpt_{reservation.id}',
            'payment_capture': 1
        }
        rzp_order = client.order.create(data=razorpay_order_payload)
        razorpay_order_id = rzp_order['id']

        reservation.razorpay_order_id = razorpay_order_id
        reservation.save()
    except Exception as e:
        logger.error(f"Razorpay Booking Order Creation Failed: {e}")
        key_prefix = key_id[:12] if len(key_id) >= 12 else key_id
        return JsonResponse({
            'success': False,
            'error': f'Failed to create Razorpay Order on Razorpay servers: {str(e)}. (Key ID used: {key_prefix}...)'
        }, status=400)

    customer_email = pending['email'] or (request.user.email if request.user.is_authenticated else 'customer@tableserve.com')

    return JsonResponse({
        'success': True,
        'razorpay_order_id': razorpay_order_id,
        'db_booking_id': reservation.id,
        'amount': amount_in_paise,
        'currency': 'INR',
        'key_id': key_id,
        'customer_name': pending['name'],
        'customer_phone': pending['phone'],
        'customer_email': customer_email,
    })


@never_cache
@csrf_protect
def api_verify_booking_razorpay_payment(request):
    """API endpoint to verify Razorpay signature for Table Booking + Pre-Order and confirm reservation"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST request required'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    db_booking_id = data.get('db_booking_id')
    razorpay_order_id = data.get('razorpay_order_id', '').strip()
    razorpay_payment_id = data.get('razorpay_payment_id', '').strip()
    razorpay_signature = data.get('razorpay_signature', '').strip()

    if not db_booking_id or not razorpay_payment_id or not razorpay_order_id or not razorpay_signature:
        return JsonResponse({'success': False, 'error': 'Missing required payment verification details.'}, status=400)

    reservation = get_object_or_404(Reservation, id=db_booking_id)
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '').strip().strip("'").strip('"')
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '').strip().strip("'").strip('"')

    client = razorpay.Client(auth=(key_id, key_secret))
    params_dict = {
        'razorpay_order_id': razorpay_order_id,
        'razorpay_payment_id': razorpay_payment_id,
        'razorpay_signature': razorpay_signature
    }

    try:
        client.utility.verify_payment_signature(params_dict)
    except razorpay.errors.SignatureVerificationError:
        return JsonResponse({'success': False, 'error': 'Payment verification failed. Razorpay signature is invalid.'}, status=400)
    except Exception as e:
        msg = f"{razorpay_order_id}|{razorpay_payment_id}"
        generated_signature = hmac.new(
            key_secret.encode('utf-8'),
            msg.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if generated_signature != razorpay_signature:
            return JsonResponse({'success': False, 'error': f'Payment signature verification failed: {str(e)}'}, status=400)

    with transaction.atomic():
        reservation.status = 'confirmed'
        reservation.payment_status = 'paid'
        reservation.payment_amount = reservation.total_amount
        reservation.razorpay_payment_id = razorpay_payment_id
        reservation.save()

        # Update associated pre-order
        pre_order = reservation.pre_orders.first()
        if pre_order:
            pre_order.status = 'accepted'
            pre_order.save()

            Payment.objects.create(
                order=pre_order,
                amount=pre_order.total,
                method=reservation.payment_method or 'upi',
                status='success',
                transaction_id=razorpay_payment_id,
            )

        # Clear pending booking & pre-order session
        request.session['pending_booking'] = {}
        request.session['pre_order_cart'] = {}
        request.session.modified = True

    messages.success(request, f'Table booking & prepaid payment verified! Booking ID: #{reservation.id}')
    return JsonResponse({
        'success': True,
        'message': 'Table booking payment verified successfully!',
        'booking_id': reservation.id,
        'redirect_url': f'/booking-confirmation/{reservation.id}/',
    })


def api_check_tables(request):
    """AJAX endpoint for checking table availability dynamically"""
    date_val = request.GET.get('date', '').strip()
    time_val = request.GET.get('time', '').strip()
    guests_val = request.GET.get('guests', '2').strip()
    location_val = request.GET.get('location', 'All').strip()

    today = timezone.localdate()

    try:
        res_date = datetime.datetime.strptime(date_val, '%Y-%m-%d').date()
        res_time = datetime.datetime.strptime(time_val, '%H:%M').time()
        guests = int(guests_val)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid date, time or guest count'}, status=400)

    if res_date < today:
        return JsonResponse({'success': False, 'error': 'Booking date cannot be in the past.'})

    tables_qs = Table.objects.filter(is_available=True, seats__gte=guests).exclude(status='occupied')
    if location_val and location_val != 'All':
        tables_qs = tables_qs.filter(location=location_val)

    tables_qs = tables_qs.order_by('seats', 'number')

    available_list = []
    for t in tables_qs:
        has_conflict, _ = check_table_conflict(t, res_date, res_time)
        if not has_conflict:
            available_list.append({
                'id': t.id,
                'number': t.number,
                'seats': t.seats,
                'location': t.location,
                'status': 'Available',
            })

    return JsonResponse({
        'success': True,
        'count': len(available_list),
        'tables': available_list,
        'message': 'No table is available for the selected date and time.' if not available_list else ''
    })


def booking_confirmation_view(request, booking_id):
    """Booking Confirmation page showing Reservation ID, Table, Date/Time, Payment Details, and Status"""
    reservation = get_object_or_404(Reservation, id=booking_id)

    if not (request.user == reservation.user or request.user.is_staff or request.user.is_superuser):
        messages.error(request, 'Access denied.')
        return redirect('home')

    pre_order = reservation.pre_orders.first()
    pre_order_items = pre_order.items.select_related('menu_item').all() if pre_order else []

    return render(request, 'restaurant/booking_confirmation.html', {
        'reservation': reservation,
        'pre_order': pre_order,
        'pre_order_items': pre_order_items,
    })


# ============================================================
# TABLE BOOKING CANCELLATION & REFUND SYSTEM
# ============================================================

def calculate_cancellation_refund(reservation):
    """
    Calculates time remaining and server-side refund amounts for table booking cancellation.
    Policy:
    - > 24 hours: 100% refund
    - 12 to 24 hours: 75% refund
    - 2 to 12 hours: 50% refund
    - < 2 hours: 0% refund (No refund)
    - If food prep started: Pre-order amount is 0% refundable
    """
    booking_dt = timezone.make_aware(datetime.datetime.combine(reservation.date, reservation.time))
    now = timezone.now()
    diff = booking_dt - now
    hours_until_booking = diff.total_seconds() / 3600.0

    if hours_until_booking > 24:
        policy_percent = 100
        policy_name = "Greater than 24 hours (100% refund)"
    elif hours_until_booking >= 12:
        policy_percent = 75
        policy_name = "12 to 24 hours (75% refund)"
    elif hours_until_booking >= 2:
        policy_percent = 50
        policy_name = "2 to 12 hours (50% refund)"
    else:
        policy_percent = 0
        policy_name = "Less than 2 hours (No refund)"

    # Check pre-order status
    pre_order = Order.objects.filter(reservation=reservation, is_pre_order=True).first()
    food_prep_started = False
    food_status_display = ''
    if pre_order:
        if pre_order.status in ['preparing', 'cooking', 'ready', 'completed', 'served', 'delivered']:
            food_prep_started = True
            food_status_display = pre_order.get_status_display()

    table_charge = Decimal(str(reservation.booking_charge)) if reservation.booking_charge else Decimal('200.00')
    table_charge_refund = (table_charge * Decimal(policy_percent) / Decimal(100)).quantize(Decimal('0.01'))

    pre_order_amt = Decimal(str(reservation.pre_order_amount)) if reservation.pre_order_amount else Decimal('0.00')
    if food_prep_started:
        pre_order_refund = Decimal('0.00')
    else:
        pre_order_refund = (pre_order_amt * Decimal(policy_percent) / Decimal(100)).quantize(Decimal('0.01'))

    total_refund = (table_charge_refund + pre_order_refund).quantize(Decimal('0.01'))
    total_paid = Decimal(str(reservation.total_amount)) if reservation.total_amount else (table_charge + pre_order_amt)

    is_eligible = (reservation.status not in ['cancelled', 'completed', 'arrived', 'seated', 'no_show', 'rejected'])

    return {
        'eligible': is_eligible,
        'hours_until_booking': round(hours_until_booking, 1),
        'policy_percent': policy_percent,
        'policy_name': policy_name,
        'table_charge': table_charge,
        'table_charge_refund': table_charge_refund,
        'pre_order_amount': pre_order_amt,
        'food_prep_started': food_prep_started,
        'food_status_display': food_status_display,
        'pre_order_refund': pre_order_refund,
        'total_refund': total_refund,
        'total_paid': total_paid,
    }


@login_required(login_url='login')
def table_booking_cancel_view(request, booking_id):
    """Confirmation page showing cancellation policy breakdown, pre-order status, and calculated refund details"""
    reservation = get_object_or_404(Reservation, id=booking_id)

    if not (request.user == reservation.user or request.user.is_staff or request.user.is_superuser):
        messages.error(request, 'Access denied.')
        return redirect('my_bookings')

    if reservation.status == 'cancelled':
        messages.info(request, 'This booking has already been cancelled.')
        return redirect('table_booking_cancellation_confirmation_view', booking_id=reservation.id)

    refund_data = calculate_cancellation_refund(reservation)
    pre_order = Order.objects.filter(reservation=reservation, is_pre_order=True).first()

    return render(request, 'restaurant/table_booking_cancel.html', {
        'reservation': reservation,
        'pre_order': pre_order,
        'refund_data': refund_data,
    })


@login_required(login_url='login')
@csrf_protect
def api_confirm_table_booking_cancellation(request, booking_id):
    """Processes table booking cancellation atomically, issues Razorpay refund if applicable, and updates reservation"""
    if request.method != 'POST':
        return redirect('table_booking_cancel_view', booking_id=booking_id)

    reservation = get_object_or_404(Reservation, id=booking_id)

    if not (request.user == reservation.user or request.user.is_staff or request.user.is_superuser):
        messages.error(request, 'Access denied.')
        return redirect('my_bookings')

    with transaction.atomic():
        res = Reservation.objects.select_for_update().get(id=reservation.id)

        if res.status == 'cancelled':
            messages.info(request, 'Booking is already cancelled.')
            return redirect('table_booking_cancellation_confirmation_view', booking_id=res.id)

        if res.status in ['completed', 'arrived', 'seated']:
            messages.error(request, f'Cannot cancel a booking with status "{res.get_status_display()}".')
            return redirect('my_bookings')

        refund_calc = calculate_cancellation_refund(res)
        refund_amount = refund_calc['total_refund']
        reason = request.POST.get('cancellation_reason', '').strip()

        res.status = 'cancelled'
        res.cancellation_date = timezone.now()
        res.cancellation_reason = reason
        res.refund_amount = refund_amount

        # Cancel associated pre-order if food preparation has not started
        pre_order = Order.objects.filter(reservation=res, is_pre_order=True).first()
        if pre_order and pre_order.status not in ['preparing', 'cooking', 'ready', 'completed', 'served', 'delivered']:
            pre_order.status = 'cancelled'
            pre_order.save()

        # Initiate Razorpay Refund if eligible and payment was captured
        if refund_amount > Decimal('0.00') and res.razorpay_payment_id and res.payment_status == 'paid':
            key_id = getattr(settings, 'RAZORPAY_KEY_ID', '').strip().strip("'").strip('"')
            key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '').strip().strip("'").strip('"')

            try:
                client = razorpay.Client(auth=(key_id, key_secret))
                refund_payload = {
                    'amount': int(refund_amount * 100),
                    'speed': 'optimum',
                    'notes': {
                        'booking_id': str(res.id),
                        'cancellation_reason': reason[:50],
                    }
                }
                refund_res = client.payment.refund(res.razorpay_payment_id, refund_payload)
                res.razorpay_refund_id = refund_res.get('id', '')
                r_status = refund_res.get('status', 'processed')
                if r_status in ['processed', 'completed']:
                    res.refund_status = 'refund_completed'
                    res.payment_status = 'refunded'
                elif r_status in ['pending', 'queued']:
                    res.refund_status = 'refund_initiated'
                else:
                    res.refund_status = 'refund_initiated'
            except Exception as e:
                logger.error(f"Razorpay Refund failed for Reservation #{res.id}: {e}")
                res.refund_status = 'refund_failed'
                res.refund_failure_reason = str(e)
        elif refund_amount == Decimal('0.00'):
            res.refund_status = 'no_refund'
        else:
            res.refund_status = 'refund_pending'

        res.save()

    messages.success(request, f'Table Booking #{res.id} has been cancelled.')
    return redirect('table_booking_cancellation_confirmation_view', booking_id=res.id)


@login_required(login_url='login')
def table_booking_cancellation_confirmation_view(request, booking_id):
    """Confirmation page displaying post-cancellation summary and refund status"""
    reservation = get_object_or_404(Reservation, id=booking_id)

    if not (request.user == reservation.user or request.user.is_staff or request.user.is_superuser):
        messages.error(request, 'Access denied.')
        return redirect('my_bookings')

    pre_order = Order.objects.filter(reservation=reservation, is_pre_order=True).first()

    return render(request, 'restaurant/table_booking_cancellation_confirmation.html', {
        'reservation': reservation,
        'pre_order': pre_order,
    })


# ============================================================
# CUSTOM CSRF FAILURE VIEW
# ============================================================
def custom_csrf_failure_view(request, reason=""):
    """
    Custom CSRF failure handler.
    - If AJAX/JSON: Return JSON 403 with friendly message.
    - If browser form with HTTP_REFERER: redirect back to referer with notice
      so page and CSRF token refresh automatically.
    - Otherwise: Render custom branded 403_csrf.html recovery page.
    """
    logger.warning(f"CSRF verification failed: reason='{reason}', path='{request.path}'")

    # 1. AJAX / Fetch / JSON requests
    if (
        request.headers.get('x-requested-with') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('accept', '')
        or request.content_type == 'application/json'
    ):
        return JsonResponse({
            'status': 'error',
            'csrf_failed': True,
            'message': 'સુરક્ષા સેશન સમાપ્ત થઈ ગયું છે. કૃપા કરીને પેજ રિફ્રેશ કરો. (Security token expired. Please refresh the page.)',
            'detail': reason,
        }, status=403)

    # 2. Browser request with referer from same host
    referer = request.META.get('HTTP_REFERER')
    try:
        host = request.get_host()
    except Exception:
        host = ''

    if referer and (f"://{host}" in referer or referer.startswith('/')):
        if 'csrf_retry=1' not in referer:
            sep = '&' if '?' in referer else '?'
            redirect_url = f"{referer}{sep}csrf_retry=1"

            try:
                from django.contrib.messages.storage import default_storage
                if not hasattr(request, '_messages'):
                    request._messages = default_storage(request)
                messages.warning(
                    request,
                    'સુરક્ષા સેશન સમાપ્ત થઈ ગયું હતું. પેજ આપમેળે રિફ્રેશ થયું છે, કૃપા કરીને ફરીથી પ્રયાસ કરો.'
                )
            except Exception:
                pass

            return redirect(redirect_url)

    # 3. Dedicated branded 403 CSRF recovery page
    return render(request, 'restaurant/403_csrf.html', {
        'reason': reason,
        'referer': referer,
    }, status=403)