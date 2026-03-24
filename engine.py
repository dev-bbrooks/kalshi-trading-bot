"""
engine.py — Universal plugin launcher.
Usage: python3 engine.py btc_15m
Loads the specified plugin, initializes DB, starts regime worker, runs plugin.
"""

import sys
import signal
import logging
import importlib
from threading import Event, Thread

from config import PLATFORM_DIR, LOG_FILE
from db import init_db, update_plugin_state, insert_log
from regime import regime_worker


# ── Logging ────────────────────────────────────────────────────

def setup_logging(plugin_id: str):
    fmt = f"%(asctime)s [{plugin_id}] %(name)s %(levelname)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, mode="a"),
        ],
    )


# ── Plugin Discovery ──────────────────────────────────────────

def load_plugin(plugin_id: str):
    """
    Load a plugin by ID. Expects plugins/{plugin_id}/plugin.py with a
    class that subclasses MarketPlugin. Returns an instance.
    """
    module_path = f"plugins.{plugin_id}.plugin"
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        print(f"Error: Could not find plugin '{plugin_id}' at {module_path}: {e}")
        sys.exit(1)

    # Find the MarketPlugin subclass
    from plugin_base import MarketPlugin
    plugin_cls = None
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if (isinstance(attr, type) and issubclass(attr, MarketPlugin)
                and attr is not MarketPlugin):
            plugin_cls = attr
            break

    if plugin_cls is None:
        print(f"Error: No MarketPlugin subclass found in {module_path}")
        sys.exit(1)

    return plugin_cls()


# ── Main ───────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 engine.py <plugin_id>")
        print("Example: python3 engine.py btc_15m")
        sys.exit(1)

    plugin_id = sys.argv[1]
    setup_logging(plugin_id)
    log = logging.getLogger("engine")

    log.info(f"Starting engine for plugin: {plugin_id}")

    # Initialize platform DB
    init_db()
    log.info("Platform DB initialized")

    # Load plugin
    plugin = load_plugin(plugin_id)
    log.info(f"Loaded plugin: {plugin.display_name} (asset={plugin.asset})")

    # Initialize plugin DB tables
    plugin.init_db()
    log.info("Plugin DB tables initialized")

    # Set initial state
    update_plugin_state(plugin_id, {
        "status": "starting",
        "status_detail": "Initializing regime engine...",
    })

    # Stop event for clean shutdown
    stop_event = Event()

    def handle_signal(signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Start regime worker in background thread
    regime_thread = Thread(
        target=regime_worker,
        args=(plugin.asset, stop_event, plugin_id),
        daemon=True,
        name=f"regime-{plugin.asset}",
    )
    regime_thread.start()
    log.info(f"Regime worker started for {plugin.asset}")

    # Run plugin (blocks until stop_event)
    try:
        insert_log("INFO", f"Engine started: {plugin.display_name}", plugin_id)
        plugin.run(stop_event)
    except Exception as e:
        log.error(f"Plugin crashed: {e}", exc_info=True)
        insert_log("ERROR", f"Plugin crashed: {e}", plugin_id)
    finally:
        stop_event.set()
        update_plugin_state(plugin_id, {
            "status": "stopped",
            "status_detail": "Engine shut down",
        })
        insert_log("INFO", f"Engine stopped: {plugin.display_name}", plugin_id)
        log.info("Engine stopped")


if __name__ == "__main__":
    # Ensure platform dir is on path for imports
    if PLATFORM_DIR not in sys.path:
        sys.path.insert(0, PLATFORM_DIR)
    main()
