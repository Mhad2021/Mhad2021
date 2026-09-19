"""Agent entrypoint: wire the engine, tray, window and OS hooks together."""
from __future__ import annotations

import argparse
import contextlib
import logging
import logging.handlers
import signal
import sys
from typing import Optional

from tracker.api_client import ApiClient, ApiError
from tracker.config import AGENT_VERSION, APP_NAME, AgentConfig, log_dir
from tracker.engine import TrackerEngine
from tracker.platform import autostart
from tracker.platform.session_events import SessionEventListener

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Rotating file log plus console. Never logs credentials or activity content."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir() / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)


def ensure_enrolled(config: AgentConfig) -> bool:
    """Run setup if this laptop has no working credentials yet."""
    if config.device_token:
        try:
            ApiClient(config).status()
            return True
        except ApiError as exc:
            if not exc.is_auth:
                # Server down or offline network — keep the token and carry on.
                logger.warning("Server unreachable at start-up: %s", exc.message)
                return True
            logger.info("Stored credentials rejected; re-running setup")
            config.clear_credentials()

    from tracker.ui.login import SetupWindow

    return SetupWindow(config).run() is not None


def run_gui(config: AgentConfig) -> int:
    from tracker.ui.tray import TrayIcon
    from tracker.ui.window import TrackerWindow

    engine = TrackerEngine(config)
    window = TrackerWindow(engine, config)

    def quit_agent() -> None:
        engine.stop("employee quit the tracker")
        tray.stop()
        try:
            window.root.quit()
            window.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    tray = TrayIcon(engine, on_open=lambda: window.root.after(0, window.show),
                    on_quit=lambda: window.root.after(0, quit_agent))

    listener = SessionEventListener(on_event=engine.report_event)
    listener.start()

    def handle_signal(signum, _frame) -> None:
        logger.info("Received signal %s", signum)
        engine.stop(f"received signal {signum}")
        tray.stop()
        with contextlib.suppress(Exception):
            window.root.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Signal handling is not available on every platform or thread.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handle_signal)

    autostart.ensure_enabled()
    engine.start()
    tray.run_detached()

    # With a tray icon the window starts hidden; without one it must stay
    # visible, otherwise the employee has no way to clock in.
    from tracker.ui.tray import TRAY_AVAILABLE

    if TRAY_AVAILABLE:
        window.hide()
    else:
        window.show()

    try:
        window.run()
    finally:
        listener.stop()
        engine.stop("window closed")
        tray.stop()

    return 0


def run_headless(config: AgentConfig) -> int:
    """No GUI — used for smoke-testing a deployment from the command line."""
    import time

    engine = TrackerEngine(config)
    engine.start()
    logger.info("Headless agent running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(10)
            state = engine.state
            logger.info(
                "state=%s connected=%s idle=%ss worked=%ss",
                state.state, state.connected, state.local_idle_seconds,
                state.worked_seconds,
            )
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop("headless agent interrupted")

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="presence-agent",
        description=f"{APP_NAME} attendance tracker v{AGENT_VERSION}",
    )
    parser.add_argument("--headless", action="store_true",
                        help="run without a window (for testing a deployment)")
    parser.add_argument("--server", help="override the server URL")
    parser.add_argument("--enroll", metavar="CODE",
                        help="enroll this laptop with a code, then exit")
    parser.add_argument("--check", action="store_true",
                        help="report configuration and connectivity, then exit")
    parser.add_argument("--install-autostart", action="store_true",
                        help="register the agent to start at logon, then exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    config = AgentConfig.load()
    if args.server:
        config.server_url = args.server

    logger.info("%s agent %s starting", APP_NAME, AGENT_VERSION)

    if args.install_autostart:
        return 0 if autostart.enable() else 1

    if args.enroll:
        try:
            result = ApiClient(config).enroll(args.enroll)
        except ApiError as exc:
            print(f"Enrollment failed: {exc.message}", file=sys.stderr)
            return 1
        print(f"Enrolled as {result['full_name']} ({result['employee_code']})")
        return 0

    if args.check:
        from tracker.idle import build_detector

        print(f"{APP_NAME} agent {AGENT_VERSION}")
        print(f"  server     : {config.server_url}")
        print(f"  enrolled   : {'yes' if config.device_token else 'no'}")
        print(f"  idle source: {build_detector().describe()}")
        print(f"  autostart  : {'enabled' if autostart.is_enabled() else 'not set'}")
        try:
            status = ApiClient(config).status()
            print(f"  connection : OK — currently {status.get('state')}")
            return 0
        except ApiError as exc:
            print(f"  connection : FAILED — {exc.message}")
            return 1

    if not args.headless and not ensure_enrolled(config):
        logger.info("Setup was cancelled; exiting")
        return 1

    return run_headless(config) if args.headless else run_gui(config)


if __name__ == "__main__":
    sys.exit(main())
