# Resilience And Recovery Test Plan

## 5.5.2 Claude Failover

### Proactive Usage Swap

The account usage refresh sweep should protect primary Claude controller
headroom before a hard limit is reached:

- With `[pollypm].failover_usage_threshold_pct = 85`, primary `used_pct = 84`
  does not switch the operator account.
- Primary `used_pct >= 85` switches the operator session to the first
  configured `failover_accounts` entry with `used_pct < 85`.
- The sweep emits `account.failover.proactive` with `from`, `to`, `reason`,
  and `threshold`.
- A successful soft swap raises `proactive_failover_active` while the operator
  is on the backup account, and returning to primary clears it.
- If every failover account is also at or above the threshold, the sweep raises
  the `proactive_failover_no_capacity` alert.
- When the primary drops below the threshold again, the next sweep switches the
  operator session back to the configured primary account.
