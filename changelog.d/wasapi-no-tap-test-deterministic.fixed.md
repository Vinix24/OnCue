- The WASAPI no_tap outage test now waits on the reader thread reaching the
  post-grace re-open instead of polling a state that only exists for
  microseconds, so it no longer depends on thread interleaving or machine load.
- The CI workflow resolves one effective runner for the job, so a manual
  `-f runner=self-hosted` run on a repo whose `CI_RUNNER` is not self-hosted
  runs the self-hosted setup only instead of both setups.
- The recorder tap-watchdog threshold test pumps just past the 30 s boundary
  instead of exactly on it, because `(t0 + 32) - (t0 + 2)` can round a few ULP
  below 30.0 on some hosts.
