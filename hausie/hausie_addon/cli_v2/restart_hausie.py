import argparse

from ..core.flow_logger import get_logger
from .create_base import main as run_create_base
from .create_hausie import main as run_create_hausie


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restart Hausie (base + create_hausie).")
    return parser.parse_args()


def main() -> None:
    log = get_logger("core")
    _parse_args()
    log.start("Starting restart_hausie workflow.")
    run_create_base()
    run_create_hausie()
    log.ok("Restart_hausie workflow complete.")


if __name__ == "__main__":
    main()
