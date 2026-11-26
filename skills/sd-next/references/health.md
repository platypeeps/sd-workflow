# Health check

Run when the operator says "health check": this machine's health and its open findings.

1. Run `sd today` and read the `Operations, this machine` and `Attention` sections.
   Run `sd reports list --open` for its job alerts.
2. Run `machine-setup.sh status`, `dashboard.sh health` and `sd gate status --json`.
   Check load, swap and disk.
3. For each problem: fix it if you can, file an sd item in the owning repo if you can't, or ask the operator if it needs their decision.
   Note which you did on the alert.

End with a table: finding, source, severity, action (fixed, filed sd:N, or needs the operator), reason.
List only real problems; don't list healthy checks.
