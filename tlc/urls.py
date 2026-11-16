from django.urls import path

from . import views

# Define URL patterns for the application
urlpatterns = [
    # URL for the image upload page
    path("", views.upload_image, name="upload_image"),
    # Health-check endpoint for orchestration probes (no trailing slash so a
    # probe hitting /healthz gets 200, not an APPEND_SLASH 301).
    path("healthz", views.healthz, name="healthz"),
    # URL for calculating RF values based on image data and user annotations
    path("calculate_rf/", views.calculate_rf, name="calculate_rf"),
    # The CSV, annotated image and annotation file of one result, as one ZIP
    path("download-results/", views.download_results, name="download_results"),
    # Candidate detection with TLC-SpotDet (spots, inside the Min/Max band) and
    # TLC-LineDet (baseline and solvent front)
    path("detect-spots/", views.detect_spots, name="detect_spots"),
    path("detect-lines/", views.detect_lines, name="detect_lines"),
    # Store a local image once; later requests refer to it by name
    path("upload-image/", views.upload_image_file, name="upload_image_file"),
    # URL for fetching and saving an image from a remote URL
    path("fetch-image/", views.fetch_and_save_image, name="fetch_image"),
    # URL for serving an image file to the client
    path("serve-image/", views.serve_image, name="serve_image"),
]
