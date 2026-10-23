from django.contrib import admin
from .models import (
    Profile, OTPVerification,
    Table, MenuCategory, MenuItem,
    Order, OrderItem, Payment,
    Reservation, Offer,
)


# ============================================================
# ADMIN SITE CUSTOMIZATION
# ============================================================
admin.site.site_header = "TableServe Administration"
admin.site.site_title = "TableServe Admin"
admin.site.index_title = "Manage your restaurant"


# ============================================================
# PROFILE
# ============================================================
@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'mobile_no', 'address')
    search_fields = ('user__username', 'user__email', 'mobile_no')


# ============================================================
# OTP
# ============================================================
@admin.register(OTPVerification)
class OTPVerificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'otp', 'purpose', 'is_verified', 'created_at')
    list_filter = ('purpose', 'is_verified', 'created_at')
    search_fields = ('user__username', 'user__email', 'otp')
    readonly_fields = ('created_at',)


# ============================================================
# TABLE
# ============================================================
@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ('number', 'seats', 'location', 'is_available', 'created_at')
    list_filter = ('location', 'is_available')
    search_fields = ('number',)
    list_editable = ('is_available',)


# ============================================================
# MENU
# ============================================================
@admin.register(MenuCategory)
class MenuCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at')
    search_fields = ('name',)


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'price', 'is_available', 'is_popular', 'rating', 'created_at')
    list_filter = ('category', 'is_available', 'is_popular', 'rating')
    search_fields = ('name', 'description')
    list_editable = ('is_available', 'is_popular', 'price')
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'category', 'price', 'description')
        }),
        ('Media & Status', {
            'fields': ('image', 'is_available', 'is_popular', 'rating')
        }),
    )


# ============================================================
# ORDER
# ============================================================
class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ('line_total',)

    def line_total(self, obj):
        return f"₹{obj.line_total()}" if obj.pk else "—"
    line_total.short_description = "Line Total"


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'status', 'total', 'item_count', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username', 'user__email', 'id')
    list_editable = ('status',)
    readonly_fields = ('created_at', 'updated_at')
    inlines = [OrderItemInline]
    date_hierarchy = 'created_at'

    def item_count(self, obj):
        return obj.item_count()
    item_count.short_description = "Items"


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ('order', 'menu_item', 'quantity', 'price', 'line_total_display')
    list_filter = ('menu_item__category',)
    search_fields = ('order__id', 'menu_item__name')

    def line_total_display(self, obj):
        return f"₹{obj.line_total()}"
    line_total_display.short_description = "Total"


# ============================================================
# PAYMENT
# ============================================================
@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'order', 'amount', 'method', 'status', 'created_at')
    list_filter = ('status', 'method', 'created_at')
    search_fields = ('transaction_id', 'order__id')
    list_editable = ('status',)
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


# ============================================================
# RESERVATION
# ============================================================
@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'date', 'time', 'guests', 'table', 'status', 'created_at')
    list_filter = ('status', 'date', 'guests')
    search_fields = ('name', 'user__username', 'phone')
    list_editable = ('status', 'table')
    date_hierarchy = 'date'
    readonly_fields = ('created_at',)


# ============================================================
# OFFER
# ============================================================
@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ('code', 'title', 'discount', 'valid_till', 'is_active', 'created_at')
    list_filter = ('is_active', 'valid_till')
    search_fields = ('code', 'title')
    list_editable = ('is_active',)
    readonly_fields = ('created_at',)

    # At bottom of admin.py
from .models import Event, InventoryItem, NGO


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('name', 'event_type', 'event_date', 'event_time', 'guests', 'status')
    list_filter = ('status', 'event_type', 'event_date')
    search_fields = ('name', 'phone', 'email')
    list_editable = ('status',)
    date_hierarchy = 'event_date'


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'quantity', 'unit', 'min_stock', 'price_per_unit', 'supplier')
    list_filter = ('category',)
    search_fields = ('name', 'supplier')
    list_editable = ('quantity',)


@admin.register(NGO)
class NGOAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_person', 'phone', 'meals_served', 'status')
    list_filter = ('status',)
    search_fields = ('name', 'contact_person')
    list_editable = ('status', 'meals_served')


# ============================================================
# CUSTOMER REVIEW
# ============================================================
from .models import CustomerReview


@admin.register(CustomerReview)
class CustomerReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'rating', 'title', 'order', 'is_approved', 'created_at')
    list_filter = ('rating', 'is_approved', 'created_at')
    search_fields = ('user__username', 'user__email', 'title', 'comment')
    list_editable = ('is_approved',)
    readonly_fields = ('user', 'order', 'rating', 'title', 'comment', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False




