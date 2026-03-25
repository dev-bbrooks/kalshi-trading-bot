"""
engine.py — Plugin runner for the trading platform.

Usage: python3 engine.py btc_15m
"""

import importlib
import inspect
import logging
import signal
import sys
import threading

from config import LOG_FILE
from db import init_db, update_plugin_state, now_utc
from plugin_base import MarketPlugin


def setup_logging(plugin_id: str):
    """Configure console + file logging."""
    fmt = f"[%(asctime)s] [{plugin_id}] %(levelname)s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(console)

    # File
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(file_handler)


def load_plugin(plugin_id: str) -> MarketPlugin:
    """Import plugins.{plugin_id}.plugin and return the MarketPlugin subclass instance."""
    module_path = f"plugins.{plugin_id}.plugin"
    mod = importlib.import_module(module_path)

    # Find the MarketPlugin subclass in the module
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, MarketPlugin) and obj is not MarketPlugin:
            return obj()

    raise RuntimeError(f"No MarketPlugin subclass found in {module_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 engine.py <plugin_id>")
        sys.exit(1)

    plugin_id = sys.argv[1]
    setup_logging(plugin_id)
    log = logging.getLogger(__name__)

    # Initialize platform database
    init_db()

    # Load and initialize plugin
    log.info(f"Loading plugin: {plugin_id}")
    plugin = load_plugin(plugin_id)
    plugin.init_db()

    # Set initial state
    update_plugin_state(plugin_id, {
        "status": "starting",
        "status_detail": "initializing",
    })

    # Stop event for graceful shutdown
    stop_event = threading.Event()

    def handle_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info(f"Received {sig_name}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Start regime worker in daemon thread
    from regime import regime_worker
    regime_thread = threading.Thread(
        target=regime_worker,
        args=(plugin.asset, stop_event),
        daemon=True,
        name=f"regime-{plugin.asset}",
    )
    regime_thread.start()
    log.info(f"Regime worker started for {plugin.asset}")

    # Run plugin (blocks until stop_event)
    try:
        plugin.run(stop_event)
    except Exception:
        log.exception("Plugin crashed")
    finally:
        stop_event.set()
        update_plugin_state(plugin_id, {
            "status": "stopped",
            "status_detail": "",
        })
        log.info("Engine stopped")


if __name__ == "__main__":
    main()
