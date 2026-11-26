# Health check

Run when the operator says "health check": this machine's health and its open findings.
Budget: about 20 minutes. Follow `references/decide-ask-record.md` to decide, ask, record and report; this run's tag is `health`.

0. Report what the previous health check's items became, as that file says.
1. Run `sd today` and read the `Operations, this machine` and `Attention` sections.
   Run `sd reports list --open` for its job alerts.
2. Run `machine-setup.sh status`, `dashboard.sh health` and `sd gate status --json`.
   Check load, swap and disk.
3. Act only on this machine's problems; an alert from another host gets one line in the table, and its session fixes it.
   For each problem: fix it if you can, file an sd item in the owning repo with `--ref health:<date>:<slug>` if you can't, or ask the operator if it needs their decision.
   Note which you did on the alert.

End with a table: finding, source, severity, action (fixed, filed sd:N, or needs the operator), reason.
List only real problems; don't list healthy checks.
End with the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.
