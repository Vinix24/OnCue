- The app smoke test (`scripts/smoke_app.sh`) now explains a failed wait: its diagnostics print
  whether the app process is alive, the process tree with state, the listeners on the port and
  the last curl error, so a missing `/status` no longer hides why. The fake app used by the tests
  writes a start line and a listening line to the app log.
- The CI test job can be dispatched on a chosen runner label (`workflow_dispatch` input
  `runner`); normal push and pull-request runs keep using the `CI_RUNNER` default.
- The fake app in the smoke tests binds without `socket.getfqdn()`. `http.server.HTTPServer`
  resolves the bound address there, and on a GitHub-hosted macOS runner that reverse lookup
  stalls past the timeout with the socket bound but not listening, so the tests failed on
  hosted runners only. A regression test refuses the lookup and shows the server still starts.
