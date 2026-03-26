import gzip
import json
import logging
import os
import random
import sys
import time
import traceback
from glob import glob
from hashlib import md5

from PIL import Image
from selenium.common.exceptions import (
    MoveTargetOutOfBoundsException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import Firefox
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.exceptions import ReadTimeoutError

from ..config import BrowserParams, ManagerParams
from ..socket_interface import ClientSocket
from .types import BaseCommand
from .utils.webdriver_utils import (
    execute_in_all_frames,
    execute_script_with_retry,
    get_intra_links,
    is_displayed,
    scroll_down,
    wait_until_loaded,
)

# Constants for bot mitigation
NUM_MOUSE_MOVES = 10  # Times to randomly move the mouse
RANDOM_SLEEP_LOW = 1  # low (in sec) for random sleep between page loads
RANDOM_SLEEP_HIGH = 7  # high (in sec) for random sleep between page loads
logger = logging.getLogger("openwpm")


def bot_mitigation(webdriver):
    """Performs three optional commands for bot-detection mitigation when getting a site"""

    # bot mitigation 1: move the randomly around a number of times
    window_size = webdriver.get_window_size()
    num_moves = 0
    num_fails = 0
    while num_moves < NUM_MOUSE_MOVES + 1 and num_fails < NUM_MOUSE_MOVES:
        try:
            if num_moves == 0:  # move to the center of the screen
                x = int(round(window_size["height"] / 2))
                y = int(round(window_size["width"] / 2))
            else:  # move a random amount in some direction
                move_max = random.randint(0, 500)
                x = random.randint(-move_max, move_max)
                y = random.randint(-move_max, move_max)
            action = ActionChains(webdriver)
            action.move_by_offset(x, y)
            action.perform()
            num_moves += 1
        except MoveTargetOutOfBoundsException:
            num_fails += 1
            pass

    # bot mitigation 2: scroll in random intervals down page
    scroll_down(webdriver)

    # bot mitigation 3: randomly wait so page visits happen with irregularity
    time.sleep(random.randrange(RANDOM_SLEEP_LOW, RANDOM_SLEEP_HIGH))


def close_other_windows(webdriver):
    """
    close all open pop-up windows and tabs other than the current one
    """
    main_handle = webdriver.current_window_handle
    windows = webdriver.window_handles
    if len(windows) > 1:
        for window in windows:
            if window != main_handle:
                webdriver.switch_to.window(window)
                webdriver.close()
        webdriver.switch_to.window(main_handle)


def tab_restart_browser(webdriver):
    """kills the current tab and creates a new one to stop traffic"""
    # note: this technically uses windows, not tabs, due to problems with
    # chrome-targeted keyboard commands in Selenium 3 (intermittent
    # nonsense WebDriverExceptions are thrown). windows can be reliably
    # created, although we do have to detour into JS to do it.
    close_other_windows(webdriver)

    if webdriver.current_url.lower() == "about:blank":
        return

    # Create a new window.  Note that it is not practical to use
    # noopener here, as we would then be forced to specify a bunch of
    # other "features" that we don't know whether they are on or off.
    # Closing the old window will kill the opener anyway.
    webdriver.execute_script("window.open('')")

    # This closes the _old_ window, and does _not_ switch to the new one.
    webdriver.close()

    # The only remaining window handle will be for the new window;
    # switch to it.
    assert len(webdriver.window_handles) == 1
    webdriver.switch_to.window(webdriver.window_handles[0])

    instrumentation = getattr(webdriver, "openwpm_chrome_instrumentation", None)
    if instrumentation is not None:
        try:
            instrumentation.prepare_page_instrumentation()
        except Exception:
            logger.debug("Failed to prepare Chrome instrumentation after tab restart")


class GetCommand(BaseCommand):
    """goes to <url> using the given <webdriver> instance"""

    def __init__(self, url, sleep):
        self.url = url
        self.sleep = sleep
        self.max_retries = 3
        self.retry_delay = 2  # seconds

    def __repr__(self):
        return "GetCommand({},{})".format(self.url, self.sleep)

    def _get_alternate_url(self, url: str) -> str:
        """Generate alternate URL with different protocol (HTTP/HTTPS)"""
        if url.startswith("https://"):
            return url.replace("https://", "http://", 1)
        elif url.startswith("http://"):
            return url.replace("http://", "https://", 1)
        return url

    def _is_network_error(self, exception: Exception) -> bool:
        """Check if exception is a network/DNS resolution error"""
        error_msg = str(exception).lower()
        network_errors = [
            "err_name_not_resolved",
            "err_connection_failed",
            "err_connection_refused",
            "err_connection_reset",
            "err_connection_aborted",
            "err_network_unreachable",
            "err_address_unreachable",
            "err_net_error",
            "getaddrinfo failed",
            "nodename nor servname provided",
            "name or service not known",
        ]
        return any(error in error_msg for error in network_errors)

    def _try_load_url(self, webdriver, url: str, attempt: int = 1) -> bool:
        """
        Attempt to load a URL with retry logic.
        Returns True if successful, False otherwise.
        """
        try:
            logger.info(
                f"GetCommand: Attempting to load URL (attempt {attempt}): {url}"
            )
            webdriver.get(url)
            logger.info(f"GetCommand: Successfully loaded {url}")
            return True
        except TimeoutException:
            logger.warning(
                f"GetCommand: Timeout loading {url} (attempt {attempt})"
            )
            # TimeoutException is not critical - page may still load
            return True
        except ReadTimeoutError:
            logger.warning(
                "GetCommand: WebDriver transport timeout loading %s (attempt %d)",
                url,
                attempt,
            )
            return False
        except WebDriverException as e:
            if self._is_network_error(e):
                logger.warning(
                    f"GetCommand: Network error loading {url} (attempt {attempt}): {str(e)[:100]}"
                )
                return False
            else:
                # Re-raise non-network WebDriver exceptions
                raise

    def execute(
        self,
        webdriver: Firefox,
        browser_params: BrowserParams,
        manager_params: ManagerParams,
        extension_socket: ClientSocket,
    ) -> None:
        tab_restart_browser(webdriver)

        if extension_socket is not None:
            extension_socket.send(self.visit_id)

        # Execute a get through selenium with retry logic
        urls_to_try = [self.url]
        alternate_url = self._get_alternate_url(self.url)
        if alternate_url != self.url:
            urls_to_try.append(alternate_url)

        last_exception = None
        url_loaded = False

        for url in urls_to_try:
            for attempt in range(1, self.max_retries + 1):
                try:
                    if self._try_load_url(webdriver, url, attempt):
                        url_loaded = True
                        break
                except (WebDriverException, ReadTimeoutError) as e:
                    # Critical error, skip this URL
                    last_exception = e
                    logger.error(
                        f"GetCommand: Critical error with {url}: {str(e)[:200]}"
                    )
                    break

                if attempt < self.max_retries:
                    logger.debug(
                        f"GetCommand: Retrying in {self.retry_delay} seconds..."
                    )
                    time.sleep(self.retry_delay)

            if url_loaded:
                break

        if not url_loaded and last_exception:
            logger.error(
                f"GetCommand: Failed to load {self.url} after all attempts: {str(last_exception)[:200]}"
            )

        # Sleep after get attempt
        time.sleep(self.sleep)

        # Close modal dialog if exists
        try:
            WebDriverWait(webdriver, 0.5).until(EC.alert_is_present())
            alert = webdriver.switch_to.alert
            alert.dismiss()
            time.sleep(1)
        except (TimeoutException, WebDriverException):
            pass

        close_other_windows(webdriver)

        if browser_params.bot_mitigation:
            bot_mitigation(webdriver)


class BrowseCommand(BaseCommand):
    def __init__(self, url, num_links, sleep):
        self.url = url
        self.num_links = num_links
        self.sleep = sleep

    def __repr__(self):
        return "BrowseCommand({},{},{})".format(self.url, self.num_links, self.sleep)

    def execute(
        self,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
    ):
        """Calls get_website before visiting <num_links> present on the page.

        Note: the site_url in the site_visits table for the links visited will
        be the site_url of the original page and NOT the url of the links visited.
        """
        # First get the site
        get_command = GetCommand(self.url, self.sleep)
        get_command.set_visit_browser_id(self.visit_id, self.browser_id)
        get_command.execute(
            webdriver,
            browser_params,
            manager_params,
            extension_socket,
        )

        # Then visit a few subpages
        for _ in range(self.num_links):
            links = [
                x
                for x in get_intra_links(webdriver, self.url)
                if is_displayed(x) is True
            ]
            if not links:
                break
            r = int(random.random() * len(links))
            logger.info(
                "BROWSER %i: visiting internal link %s"
                % (browser_params.browser_id, links[r].get_attribute("href"))
            )

            try:
                links[r].click()
                wait_until_loaded(webdriver, 300)
                time.sleep(max(1, self.sleep))
                if browser_params.bot_mitigation:
                    bot_mitigation(webdriver)
                webdriver.back()
                wait_until_loaded(webdriver, 300)
            except Exception as e:
                logger.error(
                    "BROWSER %i: Error visitit internal link %s",
                    browser_params.browser_id,
                    links[r].get_attribute("href"),
                    exc_info=e,
                )
                pass


class SaveScreenshotCommand(BaseCommand):
    def __init__(self, suffix):
        self.suffix = suffix

    def __repr__(self):
        return "SaveScreenshotCommand({})".format(self.suffix)

    def execute(
        self,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
    ):
        if self.suffix != "":
            self.suffix = "-" + self.suffix

        urlhash = md5(webdriver.current_url.encode("utf-8")).hexdigest()
        outname = os.path.join(
            manager_params.screenshot_path,
            "%i-%s%s.png" % (self.visit_id, urlhash, self.suffix),
        )
        webdriver.save_screenshot(outname)


def _stitch_screenshot_parts(visit_id, browser_id, manager_params):
    # Read image parts and compute dimensions of output image
    total_height = -1
    max_scroll = -1
    max_width = -1
    images = dict()
    parts = list()
    for f in glob(
        os.path.join(
            manager_params.screenshot_path, "parts", "%i*-part-*.png" % visit_id
        )
    ):
        # Load image from disk and parse params out of filename
        img_obj = Image.open(f)
        width, height = img_obj.size
        parts.append((f, width, height))
        outname, _, index, curr_scroll = os.path.basename(f).rsplit("-", 3)
        curr_scroll = int(curr_scroll.split(".")[0])
        index = int(index)

        # Update output image size
        if curr_scroll > max_scroll:
            max_scroll = curr_scroll
            total_height = max_scroll + height

        if width > max_width:
            max_width = width

        # Save image parameters
        img = {}
        img["object"] = img_obj
        img["scroll"] = curr_scroll
        images[index] = img

    # Output filename same for all parts, so we can just use last filename
    outname = outname + ".png"
    outname = os.path.join(manager_params.screenshot_path, outname)
    output = Image.new("RGB", (max_width, total_height))

    # Compute dimensions for output image
    for i in range(max(images.keys()) + 1):
        img = images[i]
        output.paste(im=img["object"], box=(0, img["scroll"]))
        img["object"].close()
    try:
        output.save(outname)
    except SystemError:
        logger.error(
            "BROWSER %i: SystemError while trying to save screenshot %s. \n"
            "Slices of image %s \n Final size %s, %s."
            % (
                browser_id,
                outname,
                "\n".join([str(x) for x in parts]),
                max_width,
                total_height,
            )
        )
        pass


class ScreenshotFullPageCommand(BaseCommand):
    def __init__(self, suffix):
        self.suffix = suffix

    def __repr__(self):
        return "ScreenshotFullPageCommand({})".format(self.suffix)

    def execute(
        self,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
    ):
        self.outdir = os.path.join(manager_params.screenshot_path, "parts")
        if not os.path.isdir(self.outdir):
            os.mkdir(self.outdir)
        if self.suffix != "":
            self.suffix = "-" + self.suffix
        urlhash = md5(webdriver.current_url.encode("utf-8")).hexdigest()
        outname = os.path.join(
            self.outdir,
            "%i-%s%s-part-%%i-%%i.png" % (self.visit_id, urlhash, self.suffix),
        )

        try:
            part = 0
            max_height = execute_script_with_retry(
                webdriver, "return document.body.scrollHeight;"
            )
            inner_height = execute_script_with_retry(
                webdriver, "return window.innerHeight;"
            )
            curr_scrollY = execute_script_with_retry(
                webdriver, "return window.scrollY;"
            )
            prev_scrollY = -1
            webdriver.save_screenshot(outname % (part, curr_scrollY))
            while (
                curr_scrollY + inner_height
            ) < max_height and curr_scrollY != prev_scrollY:
                # Scroll down to bottom of previous viewport
                try:
                    webdriver.execute_script("window.scrollBy(0, window.innerHeight)")
                except WebDriverException:
                    logger.info(
                        "BROWSER %i: WebDriverException while scrolling, "
                        "screenshot may be misaligned!" % self.browser_id
                    )
                    pass

                # Update control variables
                part += 1
                prev_scrollY = curr_scrollY
                curr_scrollY = execute_script_with_retry(
                    webdriver, "return window.scrollY;"
                )

                # Save screenshot
                webdriver.save_screenshot(outname % (part, curr_scrollY))
        except WebDriverException:
            excp = traceback.format_exception(*sys.exc_info())
            logger.error(
                "BROWSER %i: Exception while taking full page screenshot \n %s"
                % (self.browser_id, "".join(excp))
            )
            return

        _stitch_screenshot_parts(self.visit_id, self.browser_id, manager_params)


class DumpPageSourceCommand(BaseCommand):
    def __init__(self, suffix):
        self.suffix = suffix

    def __repr__(self):
        return "DumpPageSourceCommand({})".format(self.suffix)

    def execute(
        self,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
    ):
        if self.suffix != "":
            self.suffix = "-" + self.suffix

        outname = md5(webdriver.current_url.encode("utf-8")).hexdigest()
        outfile = os.path.join(
            manager_params.source_dump_path,
            "%i-%s%s.html" % (self.visit_id, outname, self.suffix),
        )

        with open(outfile, "wb") as f:
            f.write(webdriver.page_source.encode("utf8"))
            f.write(b"\n")


class RecursiveDumpPageSourceCommand(BaseCommand):
    def __init__(self, suffix):
        self.suffix = suffix

    def __repr__(self):
        return "RecursiveDumpPageSourceCommand({})".format(self.suffix)

    def execute(
        self,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
    ):
        """Dump a compressed html tree for the current page visit"""
        if self.suffix != "":
            self.suffix = "-" + self.suffix

        outname = md5(webdriver.current_url.encode("utf-8")).hexdigest()
        outfile = os.path.join(
            manager_params.source_dump_path,
            "%i-%s%s.json.gz" % (self.visit_id, outname, self.suffix),
        )

        def collect_source(webdriver, frame_stack, rv={}):
            is_top_frame = len(frame_stack) == 1

            # Gather frame information
            doc_url = webdriver.execute_script("return window.document.URL;")
            if is_top_frame:
                page_source = rv
            else:
                page_source = dict()
            page_source["doc_url"] = doc_url
            source = webdriver.page_source
            if type(source) != str:
                source = str(source, "utf-8")
            page_source["source"] = source
            page_source["iframes"] = dict()

            # Store frame info in correct area of return value
            if is_top_frame:
                return
            out_dict = rv["iframes"]
            for frame in frame_stack[1:-1]:
                out_dict = out_dict[frame.id]["iframes"]
            out_dict[frame_stack[-1].id] = page_source

        page_source = dict()
        execute_in_all_frames(webdriver, collect_source, {"rv": page_source})

        with gzip.GzipFile(outfile, "wb") as f:
            f.write(json.dumps(page_source).encode("utf-8"))


class FinalizeCommand(BaseCommand):
    """This command is automatically appended to the end of a CommandSequence

    It's apperance means there won't be any more commands for this
    visit_id
    """

    def __init__(self, sleep):
        self.sleep = sleep

    def __repr__(self):
        return f"FinalizeCommand({self.sleep})"

    def execute(
        self,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
    ):
        """Informs the extension that a visit is done"""
        tab_restart_browser(webdriver)
        # This doesn't immediately stop data saving from the current
        # visit so we sleep briefly before unsetting the visit_id.
        time.sleep(self.sleep)
        if extension_socket is not None:
            msg = {"action": "Finalize", "visit_id": self.visit_id}
            extension_socket.send(msg)


class InitializeCommand(BaseCommand):
    """The command is automatically prepended to the beginning of a CommandSequence

    It initializes state both in the extensions as well in as the
    StorageController
    """

    def __repr__(self):
        return "InitializeCommand()"

    def execute(
        self,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
    ):
        if extension_socket is not None:
            msg = {"action": "Initialize", "visit_id": self.visit_id}
            extension_socket.send(msg)
