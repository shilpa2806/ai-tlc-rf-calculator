"""Gunicorn configuration, driven by environment variables.

All values have sensible defaults but can be tuned per deployment, e.g.::

    GUNICORN_WORKERS=4 GUNICORN_TIMEOUT=120 gunicorn config.wsgi -c gunicorn.conf.py

Sizing: each worker process loads its own copy of the two ONNX models on first
use (~130 MB with ONNX Runtime's memory arena off, the default; ~430 MB with
it), so memory scales with workers while threads are nearly free. Hence few
workers with several threads each; ``InferenceSession.run`` is thread-safe.
Image jobs are admitted by free memory (tlc/admission.py): measured with 2
workers x 4 threads and 50 MP photos, 1.5 GB served 8 at once (peak 1.26 GB);
1 GB refused them with 503 "busy" instead of running out of memory. CPU use
is roughly workers x threads x ONNX_INTRA_OP_THREADS at peak. Deliberately
no ``preload_app``: ONNX Runtime thread pools do not survive fork(), which
shows up as an intermittent hang inside inference.
"""

import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8050")
workers = int(os.getenv("GUNICORN_WORKERS", os.getenv("WEB_CONCURRENCY", "2")))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "4"))
# The first detection request in a worker also loads a model.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
# Recycle workers now and then, bounding any slow growth in native memory.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "500"))
max_requests_jitter = 50
# Log to stdout/stderr so a container platform collects them.
accesslog = "-"
errorlog = "-"
