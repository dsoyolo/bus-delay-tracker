"""
Bus Delay Tracker
-----------------
Monitors configured transit routes via Google Maps Directions API and
sends SMS alerts via AWS SNS when delays exceed the configured threshold.

Usage:
  # Check routes once right now:
  python tracker.py --check-now

  # Run continuously on a schedule (uses config.yaml polling interval):
  python tracker.py --daemon

  # Learn baseline journey times (run this on a normal day first):
  python tracker.py --learn-baseline
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import googlemaps
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from notifier import SNSNotifier

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"
BASELINE_PATH = Path(__file__).parent / "baseline.json"
ALERT_STATE_PATH = Path(__file__).parent / ".alert_state.json"

# North Bay, Ontario timezone
LOCAL_TZ = ZoneInfo("America/Toronto")

DAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def load_baseline() -> dict:
    if BASELINE_PATH.exists():
        with BASELINE_PATH.open() as f:
            return json.load(f)
    return {}


def save_baseline(baseline: dict) -> None:
    with BASELINE_PATH.open("w") as f:
        json.dump(baseline, f, indent=2)
    logger.info("Baseline saved to %s", BASELINE_PATH)


def load_alert_state() -> dict:
    if ALERT_STATE_PATH.exists():
        with ALERT_STATE_PATH.open() as f:
            return json.load(f)
    return {}


def save_alert_state(state: dict) -> None:
    with ALERT_STATE_PATH.open("w") as f:
        json.dump(state, f, indent=2)


def is_today_monitored(days: list[str]) -> bool:
    today = DAY_ABBR[datetime.now(LOCAL_TZ).weekday()]
    return today in days


def cooldown_active(state: dict, route_name: str, cooldown_hours: int) -> bool:
    last_sent = state.get(route_name)
    if not last_sent:
        return False
    elapsed = time.time() - last_sent
    return elapsed < cooldown_hours * 3600


def record_alert_sent(state: dict, route_name: str) -> dict:
    state[route_name] = time.time()
    return state


# ---------------------------------------------------------------------------
# Google Maps query
# ---------------------------------------------------------------------------

def get_transit_duration(client: googlemaps.Client, origin: str,
                          destination: str, departure_dt: datetime,
                          waypoints: list[str] | None = None) -> int | None:
    """
    Returns the transit travel duration in minutes, or None if no result.
    departure_dt should be timezone-aware.
    """
    try:
        result = client.directions(
            origin,
            destination,
            mode="transit",
            departure_time=departure_dt,
            waypoints=waypoints or [],
            alternatives=False,
        )
    except Exception as e:
        logger.error("Google Maps API error: %s", e)
        return None

    if not result:
        return None

    leg = result[0]["legs"][0]
    return leg["duration"]["value"] // 60  # seconds -> minutes


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

def check_route(route: dict, baseline: dict, config: dict,
                gmaps: googlemaps.Client, notifier: SNSNotifier,
                alert_state: dict) -> None:
    name = route["name"]
    logger.info("Checking route: %s", name)

    if not is_today_monitored(route.get("days", DAY_ABBR)):
        logger.info("  Skipping — not a monitored day.")
        return

    # Build departure datetime: today + check_time
    now = datetime.now(LOCAL_TZ)
    check_h, check_m = map(int, route["check_time"].split(":"))
    departure_dt = now.replace(hour=check_h, minute=check_m, second=0, microsecond=0)

    # If check_time already passed more than lead_minutes ago, skip
    alert_lead = config["alerts"].get("alert_lead_minutes", 15)
    if now > departure_dt - timedelta(minutes=alert_lead) + timedelta(minutes=1):
        logger.info("  Skipping — check window has passed for today.")
        return

    current_minutes = get_transit_duration(
        gmaps,
        route["origin"],
        route["destination"],
        departure_dt,
        route.get("waypoints"),
    )

    if current_minutes is None:
        logger.warning("  No transit result for route: %s", name)
        if not cooldown_active(alert_state, name, config["alerts"]["cooldown_hours"]):
            phone = os.environ.get("ALERT_PHONE_NUMBER") or config["alerts"]["phone_number"]
            notifier.send_no_service_alert(phone, name)
            alert_state = record_alert_sent(alert_state, name)
            save_alert_state(alert_state)
        return

    baseline_minutes = baseline.get(name)
    if baseline_minutes is None:
        logger.warning(
            "  No baseline for '%s'. Run with --learn-baseline first. "
            "Using current duration (%d min) as temporary baseline.",
            name, current_minutes,
        )
        baseline[name] = current_minutes
        save_baseline(baseline)
        return

    delay = current_minutes - baseline_minutes
    threshold = route.get("delay_threshold_minutes", 5)
    logger.info(
        "  Baseline: %d min | Current: %d min | Delay: %+d min (threshold: %d min)",
        baseline_minutes, current_minutes, delay, threshold,
    )

    if delay >= threshold:
        if cooldown_active(alert_state, name, config["alerts"]["cooldown_hours"]):
            logger.info("  Alert suppressed (cooldown active).")
            return
        phone = os.environ.get("ALERT_PHONE_NUMBER") or config["alerts"]["phone_number"]
        sent = notifier.send_delay_alert(phone, name, baseline_minutes, current_minutes, delay)
        if sent:
            alert_state = record_alert_sent(alert_state, name)
            save_alert_state(alert_state)
    else:
        logger.info("  No significant delay detected.")


def check_all_routes() -> None:
    config = load_config()
    baseline = load_baseline()
    alert_state = load_alert_state()

    gmaps = googlemaps.Client(key=os.environ["GOOGLE_MAPS_API_KEY"])
    notifier = SNSNotifier()

    for route in config["routes"]:
        check_route(route, baseline, config, gmaps, notifier, alert_state)


# ---------------------------------------------------------------------------
# Baseline learning
# ---------------------------------------------------------------------------

def learn_baseline() -> None:
    """
    Queries current journey times for all routes and saves them as the baseline.
    Run this on a normal (non-delayed) day during typical commute conditions.
    """
    config = load_config()
    gmaps = googlemaps.Client(key=os.environ["GOOGLE_MAPS_API_KEY"])
    baseline = load_baseline()

    for route in config["routes"]:
        name = route["name"]
        now = datetime.now(LOCAL_TZ)
        check_h, check_m = map(int, route["check_time"].split(":"))
        departure_dt = now.replace(hour=check_h, minute=check_m, second=0, microsecond=0)
        # If time already passed today, check from now instead
        if departure_dt < now:
            departure_dt = now + timedelta(minutes=5)

        minutes = get_transit_duration(
            gmaps,
            route["origin"],
            route["destination"],
            departure_dt,
            route.get("waypoints"),
        )
        if minutes is not None:
            baseline[name] = minutes
            logger.info("Baseline for '%s': %d minutes", name, minutes)
        else:
            logger.warning("Could not get baseline for '%s'", name)

    save_baseline(baseline)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Bus Delay Tracker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-now", action="store_true",
                       help="Check all routes once and exit")
    group.add_argument("--daemon", action="store_true",
                       help="Run continuously on the polling schedule")
    group.add_argument("--learn-baseline", action="store_true",
                       help="Record current journey times as baselines")
    args = parser.parse_args()

    missing = [v for v in ("GOOGLE_MAPS_API_KEY", "AWS_ACCESS_KEY_ID",
                            "AWS_SECRET_ACCESS_KEY", "AWS_REGION")
               if not os.environ.get(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    if args.learn_baseline:
        learn_baseline()
        return

    if args.check_now:
        check_all_routes()
        return

    # Daemon mode
    config = load_config()
    interval = config["polling"]["interval_minutes"]
    logger.info("Starting daemon mode — checking every %d minutes.", interval)

    scheduler = BlockingScheduler(timezone=str(LOCAL_TZ))
    scheduler.add_job(check_all_routes, "interval", minutes=interval,
                      next_run_time=datetime.now(LOCAL_TZ))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
