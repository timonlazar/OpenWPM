import argparse
import logging
from pathlib import Path
from typing import Literal
from datetime import datetime
import tranco
import os
from dotenv import load_dotenv

from custom_command import LinkCountingCommand
from openwpm.commands.cookie_consent import AcceptCookieConsentCommand
from openwpm.command_sequence import CommandSequence
from openwpm.commands.browser_commands import GetCommand
from openwpm.config import BrowserParams, ManagerParams
from openwpm.storage.postgres_storage_provider import PostgresStorageProvider
from openwpm.task_manager import TaskManager

parser = argparse.ArgumentParser()
parser.add_argument("--tranco", action="store_true", default=False)
parser.add_argument("--headless", action="store_true", default=False)
parser.add_argument("--firefox", action="store_true", default=False,
                    help="Use Firefox browser instances")
parser.add_argument("--chrome", action="store_true", default=False,
                    help="Use Chrome browser instances")

args = parser.parse_args()

# Determine which browsers to use. Default to Firefox if neither flag is given.
selected_browsers = []
if args.firefox:
    selected_browsers.append("firefox")
if args.chrome:
    selected_browsers.append("chrome")
if not selected_browsers:
    selected_browsers = ["firefox"]

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
# Resolve schema file from env or fallback to committed db/init schema location
env_schema = os.environ.get("POSTGRES_SCHEMA_FILE")
if env_schema:
    schema_file = Path(env_schema).expanduser().resolve()
else:
    schema_file = (Path(__file__).parent / "db" / "init" / "postgre_schema.sql").resolve()

if not schema_file.exists():
    logging.getLogger("openwpm").warning("Postgres schema file %s does not exist.", schema_file)

# Build DSN from OPENWPM_PG_DSN or PG_*/POSTGRES_* env vars
pg_dsn = os.environ.get("OPENWPM_PG_DSN")
if not pg_dsn and (
    os.environ.get("PG_HOST")
    or os.environ.get("POSTGRES_HOST")
    or os.environ.get("PG_USER")
    or os.environ.get("POSTGRES_USER")
    or os.environ.get("PG_DB")
    or os.environ.get("POSTGRES_DB")
):
    pg_user = os.environ.get("PG_USER") or os.environ.get("POSTGRES_USER") or "postgres"
    pg_pass = os.environ.get("PG_PASS") or os.environ.get("POSTGRES_PASSWORD") or ""
    pg_host = os.environ.get("PG_HOST") or os.environ.get("POSTGRES_HOST") or "localhost"
    pg_port = os.environ.get("PG_PORT") or os.environ.get("POSTGRES_PORT") or "5432"
    pg_db = os.environ.get("PG_DB") or os.environ.get("POSTGRES_DB") or "openwpm"
    if pg_pass:
        pg_dsn = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
    else:
        pg_dsn = f"postgresql://{pg_user}@{pg_host}:{pg_port}/{pg_db}"

if not pg_dsn:
    raise SystemExit(
        "Postgres DSN is not configured. Set OPENWPM_PG_DSN or PG_HOST (and related PG_* variables)."
    )

storage_provider = PostgresStorageProvider(dsn=pg_dsn, schema_file=schema_file)

sites = [
    "http://www.example.com",
    "http://www.princeton.edu",
    "http://citp.princeton.edu/",
]
if args.tranco:
    # Load the latest tranco list. See https://tranco-list.eu/
    print("Loading tranco top sites list...")
    t = tranco.Tranco(cache=True, cache_dir=".tranco")
    latest_list = t.list(date="2025-08-05")
    sites = ["http://" + x for x in latest_list.top(200)]

date_str = datetime.now().strftime("%Y-%m-%d")
sqlite_path = Path(f"./datadir/crawl-data-{date_str}.sqlite")

display_mode: Literal["native", "headless", "xvfb"] = "native"
if args.headless:
    display_mode = "headless"

# Loads the default ManagerParams
# and NUM_BROWSERS copies of the default BrowserParams
# One browser instance is created per selected browser type.
NUM_BROWSERS = len(selected_browsers)
manager_params = ManagerParams(num_browsers=NUM_BROWSERS)
browser_params = [BrowserParams(display_mode=display_mode, browser=b) for b in selected_browsers]

# Update browser configuration (use this for per-browser settings)
for browser_param in browser_params:
    if browser_param.browser == "chrome":
        # Chrome uses CDP-based instrumentation instead of the Firefox WebExtension.
        # HTTP requests/responses, cookies, navigations, JS events, and DNS records
        # are collected via the Chrome instrumentation pipeline.
        browser_param.http_instrument = True
        browser_param.cookie_instrument = True
        browser_param.navigation_instrument = True
        browser_param.js_instrument = True
        browser_param.dns_instrument = True
    else:
        # Record HTTP Requests and Responses
        browser_param.http_instrument = True
        # Record cookie changes
        browser_param.cookie_instrument = True
        # Record Navigations
        browser_param.navigation_instrument = True
        # Record JS Web API calls
        browser_param.js_instrument = True
        # Record the callstack of all WebRequests made
        # browser_param.callstack_instrument = True
        # Record DNS resolution
        browser_param.dns_instrument = True
    # Set this value as appropriate for the size of your temp directory
    # if you are running out of space
    browser_param.maximum_profile_size = 50 * (10**20)  # 50 MB = 50 * 2^20 Bytes

# Update TaskManager configuration (use this for crawl-wide settings)
manager_params.data_directory = Path("./datadir/")
manager_params.log_path = Path("./datadir/openwpm.log")

# memory_watchdog and process_watchdog are useful for large scale cloud crawls.
# Please refer to docs/Configuration.md#platform-configuration-options for more information
# manager_params.memory_watchdog = True
# manager_params.process_watchdog = True


# Commands time out by default after 60 seconds
with TaskManager(
        manager_params,
        browser_params,
        storage_provider,
        None,
) as manager:
    # Visits the sites
    for index, site in enumerate(sites):

        def callback(success: bool, val: str = site) -> None:
            print(
                f"CommandSequence for {val} ran {'successfully' if success else 'unsuccessfully'}"
            )

        # Parallelize sites over all number of browsers set above.
        command_sequence = CommandSequence(
            site,
            site_rank=index,
            callback=callback,
        )

        # Start by visiting the page
        command_sequence.append_command(GetCommand(url=site, sleep=3), timeout=60)
        # Have a look at custom_command.py to see how to implement your own command
        command_sequence.append_command(AcceptCookieConsentCommand(), timeout=30)
        command_sequence.append_command(LinkCountingCommand())

        # Run commands across all browsers (simple parallelization)
        manager.execute_command_sequence(command_sequence)
