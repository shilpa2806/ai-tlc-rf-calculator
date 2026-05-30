from django.urls import path
from . import views
#from circle_detector.views import calculate_rf, detect_spots_api

# Define URL patterns for the application
urlpatterns = [
    # URL for the image upload page
    path('', views.upload_image, name='upload_image'),

    # URL for calculating RF values based on image data and user annotations
    path('calculate_rf/', views.calculate_rf, name='calculate_rf'),

    # URL for uploading local images for local processing
    path('upload-local-image/', views.upload_local_image, name='upload_local_image'),

    # URL for fetching and saving an image from a remote URL
    path('fetch-image/', views.fetch_and_save_image, name='fetch_image'),

    # URL for serving an image file to the client
    path('serve-image/', views.serve_image, name='serve_image'),
    
    path("auto_detect_spots/", views.auto_detect_spots, name="auto_detect_spots"),
    
    path("auto_detect_lines/", views.auto_detect_lines, name="auto_detect_lines"),
    
    
]

