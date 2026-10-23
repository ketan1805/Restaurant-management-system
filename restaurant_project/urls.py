from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static


# ============================================================
# ADMIN SITE CUSTOMIZATION
# ============================================================
admin.site.site_header = "TableServe Administration"
admin.site.site_title = "TableServe Admin"
admin.site.index_title = "Welcome to TableServe Admin Panel"


# ============================================================
# URL PATTERNS
# ============================================================
urlpatterns = [
    # Django built-in admin (site owner)
    path('django-admin/', admin.site.urls),

    # All app URLs (includes restaurant.urls → which includes staff.urls)
    path('', include('restaurant.urls')),
]


# ============================================================
# STATIC & MEDIA FILES (Development only)
# ============================================================
if settings.DEBUG:
    # Media files (user uploads — event theme images, menu images, etc.)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # Static files (CSS, JS, images from restaurant/static/)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0])