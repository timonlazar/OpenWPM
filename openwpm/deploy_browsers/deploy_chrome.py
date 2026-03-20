import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from multiprocess import Queue
from pyvirtualdisplay import Display
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from ..config import BrowserParamsInternal, ManagerParamsInternal
from ..utilities.platform_utils import get_chrome_binary_path
from .chrome_instrumentation import ChromeInstrumentation

DEFAULT_SCREEN_RES = (1366, 768)
logger = logging.getLogger("openwpm")


def deploy_chrome(
    status_queue: Queue,
    browser_params: BrowserParamsInternal,
    manager_params: ManagerParamsInternal,
    crash_recovery: bool,
) -> Tuple[webdriver.Chrome, Path, Optional[Display], Optional[ChromeInstrumentation]]:
    """
    Launches a Chrome instance with parameters set by the input browser_params.
    Instrumentation (HTTP, cookies, navigation, JS, DNS) is collected via the Chrome
    DevTools Protocol (CDP) instead of the Firefox WebExtension.
    """
    assert browser_params.browser_id is not None

    chrome_binary_path = get_chrome_binary_path()

    browser_profile_path = Path(
        tempfile.mkdtemp(prefix="chrome_profile_", dir=browser_params.tmp_profile_dir)
    )
    status_queue.put(("STATUS", "Profile Created", browser_profile_path))

    # Profile tar loading is not supported for Chrome – signal completion anyway
    status_queue.put(("STATUS", "Profile Tar", None))

    display_pid = None
    display_port = None
    display = None
    display_mode = browser_params.display_mode

    if display_mode == "xvfb":
        try:
            display = Display(visible=False, size=DEFAULT_SCREEN_RES)
            display.start()
            display_pid, display_port = display.pid, display.display
        except Exception as e:
            raise RuntimeError(
                "Xvfb could not be started. "
                "Please ensure it's on your path. "
                "See www.X.org for full details. "
                "Commonly solved on ubuntu with `sudo apt install xvfb`"
            ) from e

    status_queue.put(("STATUS", "Display", (display_pid, display_port)))

    co = Options()
    co.binary_location = chrome_binary_path

    # Use custom profile directory
    co.add_argument(f"--user-data-dir={browser_profile_path}")

    # Headless mode
    if display_mode == "headless":
        co.add_argument("--headless=new")
        co.add_argument(f"--window-size={DEFAULT_SCREEN_RES[0]},{DEFAULT_SCREEN_RES[1]}")

    # Privacy / speed optimisations
    co.add_argument("--no-first-run")
    co.add_argument("--no-default-browser-check")
    co.add_argument("--disable-background-networking")
    co.add_argument("--disable-sync")
    co.add_argument("--disable-translate")
    co.add_argument("--disable-extensions")
    co.add_argument("--disable-infobars")
    co.add_argument("--disable-notifications")
    co.add_argument("--metrics-recording-only")
    co.add_argument("--safebrowsing-disable-auto-update")
    co.add_argument("--password-store=basic")
    co.add_argument("--use-mock-keychain")
    # Required for CDP event listeners to work correctly
    co.add_argument("--remote-allow-origins=*")

    # Enable performance logs so ChromeInstrumentation can consume
    # Network.* events (including redirect chains and response bodies).
    co.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    co.add_experimental_option(
        "perfLoggingPrefs",
        {
            "enableNetwork": True,
            "enablePage": False,
        },
    )

    # Third-party cookies
    if browser_params.tp_cookies.lower() == "never":
        co.add_argument("--block-new-web-contents")

    # DNT header
    if browser_params.donottrack:
        co.add_experimental_option("prefs", {"enable_do_not_track": True})

    # Apply any user-specified prefs
    if browser_params.prefs:
        for name, value in browser_params.prefs.items():
            logger.info(
                "BROWSER %i: Setting custom Chrome preference: %s = %s"
                % (browser_params.browser_id, name, value)
            )
            co.add_argument(f"--{name}={value}")

    status_queue.put(("STATUS", "Launch Attempted", None))

    # Try to locate chromedriver automatically via shutil.which or selenium's built-in manager
    chromedriver_path = shutil.which("chromedriver")
    if chromedriver_path:
        service = Service(executable_path=chromedriver_path)
    else:
        # Fall back to selenium's built-in driver management (selenium >= 4.6)
        service = Service()

    driver = webdriver.Chrome(options=co, service=service)
    driver.set_window_size(*DEFAULT_SCREEN_RES)

    logger.debug(
        "BROWSER %i: Chrome launched, enabling CDP instrumentation."
        % browser_params.browser_id
    )

    # Get browser process pid
    if hasattr(driver, "service") and hasattr(driver.service, "process"):
        pid = driver.service.process.pid
    else:
        raise RuntimeError("Unable to identify Chrome process ID.")

    status_queue.put(("STATUS", "Browser Launched", int(pid)))

    # Set up CDP-based instrumentation (replaces the Firefox WebExtension)
    instrumentation: Optional[ChromeInstrumentation] = None
    any_instrument = (
        browser_params.http_instrument
        or browser_params.cookie_instrument
        or browser_params.navigation_instrument
        or browser_params.js_instrument
        or browser_params.dns_instrument
    )
    if any_instrument:
        try:
            instrumentation = ChromeInstrumentation(driver, browser_params, manager_params)
            setattr(driver, "openwpm_chrome_instrumentation", instrumentation)
            logger.debug(
                "BROWSER %i: CDP instrumentation initialized." % browser_params.browser_id
            )
        except Exception as e:
            logger.warning(
                "BROWSER %i: Could not initialize CDP instrumentation: %s",
                browser_params.browser_id,
                e,
            )

    return driver, browser_profile_path, display, instrumentation
