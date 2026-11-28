"""Bound automatic retries after repeated short-lived worker exits."""

import time


class RestartPolicy:
    SHORT_RUN_SECONDS = 10
    MAX_SHORT_RUNS = 5
    INITIAL_DELAY_SECONDS = 3

    def __init__(self):
        self._short_runs = 0

    def wait_before_restart(self, started_at, reason, wait):
        if time.monotonic() - started_at < self.SHORT_RUN_SECONDS:
            self._short_runs += 1
        else:
            self._short_runs = 0

        print(f"  {reason}", flush=True)
        if self._short_runs >= self.MAX_SHORT_RUNS:
            print(
                f"  Automatic restart stopped after {self._short_runs} consecutive exits "
                f"within {self.SHORT_RUN_SECONDS}s of launch. Check the worker output.",
                flush=True,
            )
            raise SystemExit(1)

        delay = self.INITIAL_DELAY_SECONDS * 2 ** max(0, self._short_runs - 1)
        print(f"  Restarting in {delay}s... (Ctrl+C to quit)", flush=True)
        wait(delay)
