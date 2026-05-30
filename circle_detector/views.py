import os
import cv2
import numpy as np
import matplotlib.pyplot as plt
from sklearn.cluster import DBSCAN
from django.shortcuts import render
from django.core.files.storage import FileSystemStorage
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
import json
import csv
from django.core.files.storage import default_storage
import requests
import logging
import tempfile
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
import base64
import io
from PIL import Image as PILImage
import time
import uuid

from .spot_onnx import SpotONNX
from .line_onnx import LineONNX



SPOT_MODEL = SpotONNX(str(settings.SPOT_MODEL_PATH), imgsz=getattr(settings, "SPOT_IMGSZ_DEFAULT", 1280))
LINE_MODEL = LineONNX(str(settings.LINE_MODEL_PATH), imgsz=getattr(settings, "LINE_IMGSZ_DEFAULT", 1024))


def _safe_local_image_path(local_image_path):
    """Resolve a user-provided media-relative path safely."""
    if not local_image_path:
        return None

    normalized = os.path.normpath(local_image_path).lstrip(os.sep)
    full_path = os.path.normpath(os.path.join(settings.MEDIA_ROOT, normalized))
    media_root = os.path.normpath(str(settings.MEDIA_ROOT))

    if os.path.commonpath([full_path, media_root]) != media_root:
        return None
    if not os.path.exists(full_path):
        return None
    return full_path


def _load_image_from_payload(is_local_image, image_src=None, image_data=None, local_image_path=None):
    """Load an image as OpenCV BGR from request payload fields."""
    if is_local_image:
        if image_data:
            header, encoded = image_data.split(",", 1) if image_data.startswith("data:image") else ("", image_data)
            img_bytes = base64.b64decode(encoded)
            pil_image = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
            return cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)

        resolved_local_path = _safe_local_image_path(local_image_path)
        if resolved_local_path:
            image = cv2.imread(resolved_local_path)
            if image is not None:
                return image

        raise ValueError("Missing imageData/localImagePath for local image")

    if not image_src:
        raise ValueError("Missing imageSrc")

    image_path = os.path.join(settings.MEDIA_ROOT, image_src)
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Image not found: {image_src}")
    return image

# View for handling image upload
def upload_image(request):
    """
    Handle image upload and rendering.

    This view handles both GET and POST requests. When an image is uploaded via
    a POST request, it saves the image to the server's file system and redirects
    the user to the annotation page. For GET requests, it renders the image upload page.

    Args:
        request (HttpRequest): The HTTP request object containing image file data (if POST).

    Returns:
        HttpResponse: A rendered HTML template for either image upload or annotation,
                      depending on the request method.
    """
    ## Check if the request method is POST and an image file is provided
    #if request.method == 'POST' and request.FILES.get('image'):
    #    image = request.FILES['image']  # Get the uploaded image file
    #    fs = FileSystemStorage()  # Initialize file system storage handler
    #    filename = fs.save(image.name, image)  # Save the uploaded image to the server
    #    uploaded_file_url = fs.url(filename)  # Get the URL of the saved image
    #
    #    # Render the annotation page with the URL of the uploaded image
    #    return render(request, 'circle_detector/annotate.html', {
    #        'uploaded_file_url': uploaded_file_url,
    #    })

    # If the request method is GET, render the upload page template
    return render(request, 'circle_detector/annotate.html')


@csrf_exempt
def upload_local_image(request):
    """Accept a local file upload and store it under MEDIA_ROOT/local_uploads."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)

    image_file = request.FILES.get('image')
    if not image_file:
        return JsonResponse({'error': 'No image file provided'}, status=400)

    if not image_file.content_type or not image_file.content_type.startswith('image/'):
        return JsonResponse({'error': 'Uploaded file is not an image'}, status=400)

    upload_dir = os.path.join(settings.MEDIA_ROOT, 'local_uploads')
    os.makedirs(upload_dir, exist_ok=True)

    _, ext = os.path.splitext(image_file.name)
    ext = ext if ext else '.png'
    filename = f"{uuid.uuid4().hex}{ext}"
    relative_path = os.path.join('local_uploads', filename)

    saved_path = default_storage.save(relative_path, image_file)
    return JsonResponse({
        'imagePath': saved_path,
        'imageUrl': f"/serve-image/?file_path={os.path.join(settings.MEDIA_ROOT, saved_path)}",
        'originalName': image_file.name,
    })



# Configure logging for the module
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Set up logging
logger = logging.getLogger(__name__)

@csrf_exempt
def calculate_rf(request):
    """
    Handles the calculation of RF values from the spots and lines in the request body.
    Now supports both ELN images (file paths) and local uploaded images (base64 data).
    """
    if request.method == 'POST':
        try:
            # Parse the incoming JSON data
            data = json.loads(request.body)

            # Extract data from the request
            lines = data.get('lines')
            spots = data.get('spots')
            image_src = data.get('imageSrc')
            is_local_image = data.get('isLocalImage', False)
            image_data = data.get('imageData')  # Base64 data for local images
            local_image_path = data.get('localImagePath')
            canvas_width = data.get('canvasWidth', 0)
            canvas_height = data.get('canvasHeight', 0)
            scale_factor = data.get('scaleFactor', 1)

            logger.info(f"Processing request - Image: {image_src}, Local: {is_local_image}")
            logger.info(f"Canvas dimensions: {canvas_width}x{canvas_height}")

            # Check for missing data
            if not lines:
                logger.error('Lines data is missing')
                return JsonResponse({'error': 'Missing "lines" data in request'}, status=400)
            if not spots:
                logger.error('Spots data is missing')
                return JsonResponse({'error': 'Missing "spots" data in request'}, status=400)
            if not image_src and not local_image_path:
                logger.error('Image source is missing')
                return JsonResponse({'error': 'Missing "imageSrc" data in request'}, status=400)

            # Check for the presence of both min and max lines
            min_line = next((line for line in lines if line['type'] == 'min'), None)
            max_line = next((line for line in lines if line['type'] == 'max'), None)

            if not min_line or not max_line:
                logger.error('Min or Max line is missing')
                return JsonResponse({'error': 'Please add "min" and "max" lines.'}, status=400)

            try:
                image = _load_image_from_payload(
                    is_local_image,
                    image_src=image_src,
                    image_data=image_data,
                    local_image_path=local_image_path,
                )
            except ValueError as exc:
                logger.error(str(exc))
                return JsonResponse({'error': str(exc)}, status=400)

            # Get the original image dimensions
            original_height, original_width = image.shape[:2]
            logger.info(f'Image dimensions: {original_width}x{original_height}')

            # ✅ Calculate scaling factors for coordinate conversion
            scale_x = original_width / canvas_width if canvas_width > 0 else 1
            scale_y = original_height / canvas_height if canvas_height > 0 else 1
            
            logger.info(f'Scaling factors: x={scale_x}, y={scale_y}')

            # Scale the min and max line coordinates
            # Semantics:
            # - min line = baseline/origin near the bottom of the plate
            # - max line = solvent front near the top of the plate
            min_y_scaled = min_line['y'] * scale_y
            max_y_scaled = max_line['y'] * scale_y
            
            logger.info(f'Min line: original y={min_line["y"]}, scaled y={min_y_scaled}')
            logger.info(f'Max line: original y={max_line["y"]}, scaled y={max_y_scaled}')

            if min_line['y'] <= max_line['y']:
                logger.error(
                    'Invalid line ordering: min line must be below max line. '
                    f'min_y={min_line["y"]}, max_y={max_line["y"]}'
                )
                return JsonResponse({
                    'error': 'Invalid line placement: "Min" must be the bottom baseline and "Max" must be the top solvent front.'
                }, status=400)

            # Calculate RF values based on spot positions
            rf_values_for_csv = []
            
            for i, spot in enumerate(spots):
                # Scale spot coordinates from canvas to image space
                x_original = spot['x']
                y_original = spot['y']
                x_scaled = int(x_original * scale_x)
                y_scaled = int(y_original * scale_y)
                             
                
                logger.info(f'Spot {i+1}: original=({x_original}, {y_original}), scaled=({x_scaled}, {y_scaled})')

                # Calculate the RF value using TLC semantics:
                # RF = distance traveled by spot from baseline / distance traveled by solvent front from baseline
                baseline_to_spot = min_line['y'] - y_original
                baseline_to_front = min_line['y'] - max_line['y']
                rf_value = baseline_to_spot / baseline_to_front
                rf_value = round(rf_value, 2)  # Round to two decimal places

                # Skip invalid RF values (outside the range 0.0 to 1.0)
                if rf_value <= 0.0 or rf_value >= 1.0:
                    logger.warning(f'Skipping invalid RF value: {rf_value} for spot at ({x_original}, {y_original})')
                    continue

                # Find closest vertical and horizontal lines for annotation
                closest_vertical_line = min(
                    (line for line in lines if line['type'] == 'vertical'),
                    key=lambda line: abs(line.get('x', 0) - x_original),
                    default=None
                )
                closest_horizontal_line = min(
                    (line for line in lines if line['type'] == 'horizontal'),
                    key=lambda line: abs(line.get('y', 0) - y_original),
                    default=None
                )

                solvent = closest_vertical_line['text'] if closest_vertical_line else 'Unknown'
                compound = closest_horizontal_line['text'] if closest_horizontal_line else 'Unknown'

                # Add the RF value, solvent, and compound to the CSV list
                rf_values_for_csv.append({
                    "compound": compound,
                    "solvent": solvent,
                    "rf": rf_value
                })

                # ✅ IMPROVED: Smart text positioning to keep within image bounds
                img_height, img_width = image.shape[:2]
                
                # Ensure coordinates are within image bounds
                x_pos = max(10, min(x_scaled, img_width - 120))
                y_pos = max(30, min(y_scaled, img_height - 80))
                
                # Draw circle at spot location (using scaled coordinates)
                cv2.circle(image, (x_scaled, y_scaled), 8, (0, 165, 255), -1)  # Filled circle
                cv2.circle(image, (x_scaled, y_scaled), 12, (255, 255, 255), 2)  # White border
                
                # ✅ IMPROVED: Better text positioning with bounds checking
                # Calculate text position relative to spot, but keep within bounds
                text_x = x_pos
                text_y_rf = max(25, y_pos - 20)      # RF value above spot
                text_y_compound = min(img_height - 35, y_pos + 20)  # Compound below spot
                text_y_solvent = min(img_height - 15, y_pos + 40)   # Solvent further below
                
                # If spot is too close to top, move all text below the spot
                if y_pos < 80:
                    text_y_rf = y_pos + 25
                    text_y_compound = y_pos + 45
                    text_y_solvent = y_pos + 65
                
                # If spot is too close to right edge, move text to left
                if x_pos > img_width - 150:
                    text_x = max(10, x_pos - 120)
                
                # If spot is too close to bottom, move text above the spot
                if y_pos > img_height - 80:
                    text_y_rf = y_pos - 60
                    text_y_compound = y_pos - 40
                    text_y_solvent = y_pos - 20
                
                # Prepare text strings
                rf_text = f'RF={rf_value}'
                compound_text = f'{compound}'
                solvent_text = f'{solvent}'
                
                # ✅ Draw RF value with background for better visibility
                # Background (shadow) for RF value
                cv2.putText(image, rf_text, (text_x + 2, text_y_rf + 2), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 3, cv2.LINE_AA)
                # Foreground (bright text) for RF value
                cv2.putText(image, rf_text, (text_x, text_y_rf), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
                
                # ✅ Draw compound with background
                # Background for compound
                cv2.putText(image, compound_text, (text_x + 2, text_y_compound + 2), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
                # Foreground for compound
                cv2.putText(image, compound_text, (text_x, text_y_compound), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2, cv2.LINE_AA)
                
                # ✅ Draw solvent with background
                # Background for solvent
                cv2.putText(image, solvent_text, (text_x + 2, text_y_solvent + 2), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
                # Foreground for solvent
                cv2.putText(image, solvent_text, (text_x, text_y_solvent), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2, cv2.LINE_AA)
                
                logger.info(f'Annotated spot {i+1}: RF={rf_value}, text positioned at ({text_x}, {text_y_rf})')

            # ✅ Draw the min and max lines on the annotated image for reference
            min_y_img = int(min_y_scaled)
            max_y_img = int(max_y_scaled)
            
            # Draw min line (green)
            cv2.line(image, (0, min_y_img), (original_width, min_y_img), (0, 255, 0), 2)
            cv2.putText(image, 'MIN', (10, min_y_img - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            # Draw max line (red)
            cv2.line(image, (0, max_y_img), (original_width, max_y_img), (0, 0, 255), 2)
            cv2.putText(image, 'MAX', (10, max_y_img + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

            # Save the annotated image
            annotated_image_name = f'annotated_{os.path.splitext(image_src)[0]}_{int(time.time())}.png'
            annotated_image_path = os.path.join(settings.MEDIA_ROOT, annotated_image_name)
            
            # Ensure media directory exists
            os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
            
            # Save with high quality
            cv2.imwrite(annotated_image_path, image, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            logger.info(f'Annotated image saved: {annotated_image_path}')

            # Save RF values to a CSV file
            csv_name = f'rf_values_{os.path.splitext(image_src)[0]}_{int(time.time())}.csv'
            csv_path = os.path.join(settings.MEDIA_ROOT, csv_name)
            
            with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['Solvent (X value)', 'Compound (Y value)', 'RF value']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for value in rf_values_for_csv:
                    writer.writerow({
                        'Solvent (X value)': value['solvent'],
                        'Compound (Y value)': value['compound'],
                        'RF value': value['rf']
                    })
            
            logger.info(f'CSV file saved: {csv_path}')

            # Return URLs for the annotated image and CSV
            annotated_image_url = settings.MEDIA_URL + annotated_image_name
            csv_url = settings.MEDIA_URL + csv_name

            return JsonResponse({
                'rf_values': rf_values_for_csv,
                'annotated_image_url': annotated_image_url,
                'csv_url': csv_url,
                'message': f'Successfully calculated RF values for {len(rf_values_for_csv)} spots',
                'debug_info': {
                    'original_image_size': f'{original_width}x{original_height}',
                    'canvas_size': f'{canvas_width}x{canvas_height}',
                    'scaling_factors': f'x={scale_x:.2f}, y={scale_y:.2f}',
                    'spots_processed': len(spots),
                    'valid_rf_values': len(rf_values_for_csv)
                }
            })

        except json.JSONDecodeError as e:
            logger.error(f'Invalid JSON format received: {str(e)}')
            return JsonResponse({'error': 'Invalid JSON format'}, status=400)
        except Exception as e:
            logger.error(f'Unexpected error in calculate_rf: {str(e)}')
            import traceback
            logger.error(f'Traceback: {traceback.format_exc()}')
            return JsonResponse({'error': f'Server error: {str(e)}'}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)


def fetch_and_save_image(request):
    """
    Fetch and save an image from a remote URL.

    This view fetches an image from the URL provided as a GET parameter,
    temporarily saves it to the server, and returns the file path.

    Args:
        request (HttpRequest): The HTTP request object containing the 'url' parameter.

    Returns:
        JsonResponse: A JSON response containing the local path of the saved image
                      or an error message if the fetch fails.
    """
    # Extract the image URL from the request
    image_url = request.GET.get('url')
    print(image_url)
    
    # Attempt to fetch the image from the remote URL
    response = requests.get(image_url)
    
    if response.status_code == 200:
        # If the request is successful, save the image to a temporary file
        temp_dir = tempfile.gettempdir()  # Get the system's temporary directory
        temp_file_path = os.path.join(settings.MEDIA_ROOT, 'downloaded_image.png')  # Set file name and path
        
        # Write the image content to the temp file
        with open(temp_file_path, 'wb') as temp_file:
            temp_file.write(response.content)
        
        # Return the file path of the saved image
        return JsonResponse({'imagePath': temp_file_path})
    else:
        # Return an error message if the image fetch fails
        return JsonResponse({'error': 'Failed to fetch image'}, status=400)


def serve_image(request):
    """
    Serve an image file to the client.

    This view reads an image from the local file system, given its path in the
    'file_path' GET parameter, and serves it as a PNG image.

    Args:
        request (HttpRequest): The HTTP request object containing the 'file_path' parameter.

    Returns:
        HttpResponse: A response containing the image file's binary content and correct
                      content type or an error message if the file is not found.
    """
    # Get the file path from the request
    file_path = request.GET.get('file_path')
    
    # Check if the file exists at the specified path
    if os.path.exists(file_path):
        # Serve the image if found
        with open(file_path, 'rb') as f:
            return HttpResponse(f.read(), content_type="image/png")
    else:
        # Return an error if the file does not exist
        return JsonResponse({'error': 'File not found'}, status=404)

@csrf_exempt
def auto_detect_spots(request):
    """
    Detect spots using ONNX model ONLY inside the Min/Max band.
    Expects minY/maxY in IMAGE coordinates (not display canvas coords).
    Returns spot centers in IMAGE coordinates.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)

    try:
        data = json.loads(request.body)

        is_local = bool(data.get("isLocalImage", False))
        image_src = data.get("imageSrc")
        image_data = data.get("imageData")
        local_image_path = data.get("localImagePath")

        # IMPORTANT: minY/maxY should be IMAGE-space coordinates
        min_y = data.get("minY")
        max_y = data.get("maxY")

        if min_y is None or max_y is None:
            return JsonResponse({"error": "minY/maxY missing. Draw Min/Max lines first."}, status=400)

        min_y = float(min_y)
        max_y = float(max_y)

        try:
            bgr = _load_image_from_payload(
                is_local,
                image_src=image_src,
                image_data=image_data,
                local_image_path=local_image_path,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        H, W = bgr.shape[:2]

        # Add small margin to band
        margin = int(getattr(settings, "TLC_BAND_MARGIN", 12))
        band_top = min(min_y, max_y)
        band_bottom = max(min_y, max_y)
        y1 = max(0, int(band_top) - margin)
        y2 = min(H, int(band_bottom) + margin)

        if y2 <= y1 + 2:
            return JsonResponse({"error": "Invalid ROI band (min/max too close)."}, status=400)

        roi = bgr[y1:y2, 0:W]

        conf = float(getattr(settings, "SPOT_CONF_DEFAULT", 0.15))

        # Run model on ROI
        dets = SPOT_MODEL.predict(roi, conf=conf)

        spots_out = []
        for d in dets:
            cx = (float(d["x1"]) + float(d["x2"])) / 2.0
            cy = (float(d["y1"]) + float(d["y2"])) / 2.0

            # convert ROI y back to full-image y
            cy_full = cy + y1

            # filter strictly inside band
            if cy_full < band_top or cy_full > band_bottom:
                continue

            spots_out.append({
                "x": cx,
                "y": cy_full,
                "conf": float(d.get("conf", 0.0))
            })

        return JsonResponse({
            "spots": spots_out,
            "debug": {
                "image_size": [W, H],
                "roi": [0, y1, W, y2],
                "minY": min_y,
                "maxY": max_y,
                "kept": len(spots_out),
            }
        })

    except Exception as e:
        logger.exception("auto_detect_spots failed")
        return JsonResponse({"error": str(e)}, status=500)
        
        
@csrf_exempt
def auto_detect_lines(request):
    """
    Detect min/max reference lines using line_best.onnx.
    Returns minY/maxY in IMAGE coordinates.
    Assumes model has two classes:
      0 -> min_line
      1 -> max_line
    If your class IDs differ, adjust CLASS_MIN / CLASS_MAX.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)

    try:
        data = json.loads(request.body)

        is_local = bool(data.get("isLocalImage", False))
        image_src = data.get("imageSrc")
        image_data = data.get("imageData")
        local_image_path = data.get("localImagePath")

        try:
            bgr = _load_image_from_payload(
                is_local,
                image_src=image_src,
                image_data=image_data,
                local_image_path=local_image_path,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        H, W = bgr.shape[:2]

        conf = float(getattr(settings, "LINE_CONF_DEFAULT", 0.15))

        dets = LINE_MODEL.predict(bgr, conf=conf)

        # ---- pick best per class (stops the “only one line” issue) ----
        CLASS_MIN = 0
        CLASS_MAX = 1

        best_min = None
        best_max = None

        for d in dets:
            if d["cls"] == CLASS_MIN:
                if best_min is None or d["conf"] > best_min["conf"]:
                    best_min = d
            elif d["cls"] == CLASS_MAX:
                if best_max is None or d["conf"] > best_max["conf"]:
                    best_max = d

        if best_min is None and best_max is None:
            return JsonResponse({"error": "No lines detected. Try lowering LINE_CONF_DEFAULT."}, status=400)

        # Convert a box to a line Y: use center y
        def box_center_y(det):
            return (float(det["y1"]) + float(det["y2"])) / 2.0

        # TLC semantics:
        # - min line = baseline near the bottom
        # - max line = solvent front near the top
        #
        # The model class labels are not always aligned with the TLC semantics,
        # so when both detections exist we map them by vertical position:
        #   * lower line (larger y) -> min
        #   * upper line (smaller y) -> max
        minY = None
        maxY = None

        detected_ys = [
            box_center_y(det)
            for det in (best_min, best_max)
            if det is not None
        ]

        if len(detected_ys) >= 2:
            minY = max(detected_ys)
            maxY = min(detected_ys)
        else:
            # fallback: use whichever exists + a simple rule
            only = best_min or best_max
            y = box_center_y(only)
            if y < H / 2:
                # likely max line near top; set max and estimate min near bottom
                maxY = y
                minY = float(H * 0.90)
            else:
                # likely min line near bottom; set min and estimate max near top
                minY = y
                maxY = float(H * 0.10)

        return JsonResponse({
            "minY": float(minY),
            "maxY": float(maxY),
            "debug": {
                "image_size": [W, H],
                "conf": conf,
                "raw_detections": len(dets),
                "best_min": best_min,
                "best_max": best_max,
            }
        })

    except Exception as e:
        logger.exception("auto_detect_lines failed")
        return JsonResponse({"error": str(e)}, status=500)
