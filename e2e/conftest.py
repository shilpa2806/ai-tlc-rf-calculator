"""Fixtures for the Playwright browser tests: a Django development server
running the app, a browser, and generated plate images.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
import pytest
from playwright.sync_api import Browser, Page, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - polling until the server is ready
            last_err = exc
            time.sleep(0.4)
    raise RuntimeError(f"Server at {url} did not start in time: {last_err}")


def _start_server(settings_module: str) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {
        **os.environ,
        "DEBUG": "True",
        "DJANGO_SETTINGS_MODULE": settings_module,
        "ALLOWED_HOSTS": "127.0.0.1,localhost",
        # As in production: lets the page post results to the (stubbed) ELN.
        "ELN_ORIGIN": "http://eln.example",
    }
    proc = subprocess.Popen(
        ["uv", "run", "python", "manage.py", "runserver", "--noreload", f"127.0.0.1:{port}"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    _wait_until_up(base_url + "/")
    return proc, base_url


@pytest.fixture(scope="session")
def servers() -> Iterator[dict[str, str]]:
    """Start the app's server; yield its base URL as ``servers["new"]``."""
    procs: list[subprocess.Popen] = []
    urls: dict[str, str] = {}
    try:
        proc, url = _start_server("config.settings")
        procs.append(proc)
        urls["new"] = url
        yield urls
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture(scope="session")
def test_image_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A 300x300 PNG. Height < 500 keeps the canvas scaleFactor at 1."""
    path = tmp_path_factory.mktemp("img") / "tlc_plate.png"
    image = np.full((300, 300, 3), 240, dtype=np.uint8)
    cv2.imwrite(str(path), image)
    return str(path)


@pytest.fixture(scope="session")
def blob_image_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A 400x300 light image with three dark filled circles for auto-detection."""
    path = tmp_path_factory.mktemp("img") / "blobs.png"
    image = np.full((300, 400, 3), 245, dtype=np.uint8)
    for cx, cy in [(100, 100), (200, 150), (300, 220)]:
        cv2.circle(image, (cx, cy), 22, (40, 40, 40), -1)
    cv2.imwrite(str(path), image)
    return str(path)


@pytest.fixture(scope="session")
def wide_image_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A 2000x500 image — wider than the panel, so the canvas is CSS-scaled
    down (exercises the delete-icon display-scaling path)."""
    path = tmp_path_factory.mktemp("img") / "wide.png"
    image = np.full((500, 2000, 3), 235, dtype=np.uint8)
    cv2.imwrite(str(path), image)
    return str(path)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1600, "height": 1200})
    pg = context.new_page()
    # A broken ES-module import fails silently: the page renders but its JS
    # never initialises, so a narrow assertion can still pass. Fail the test on
    # any uncaught exception (TDZ / import-cycle bugs) or module-load failure —
    # cheap insurance for the ES modules. Handled network errors the app
    # logs deliberately (e.g. a blocked ELN URL) are intentionally ignored.
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

    def _on_console(msg: object) -> None:
        text = getattr(msg, "text", "")
        if getattr(msg, "type", "") == "error" and (
            "module script" in text or "module specifier" in text
        ):
            errors.append(f"module-load error: {text}")

    pg.on("console", _on_console)
    yield pg
    context.close()
    assert not errors, "Browser reported JS errors:\n" + "\n".join(errors)
