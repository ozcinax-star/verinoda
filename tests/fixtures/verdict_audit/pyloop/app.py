"""Entry point: start a loop, plan the nightly jobs, run one pass."""

from loop.base import BaseLoop
from loop.scheduler import plan_hourly, plan_nightly


def main():
    loop = BaseLoop()
    queue = []
    plan_nightly(queue)
    plan_hourly(queue)
    loop.run_once()
    return queue


if __name__ == "__main__":
    main()
