# mcp-agent-sla

MCP server for the **Agent Failure-Recovery SLA Subscription** - contractual guarantees for
agent failures with measurable detection-to-recovery windows, credits that self-execute in the
billing token, adversarial test rights, and a watcher-heartbeat clause.

## Tools

### `get_sla_terms`
Quote the SLA terms for a tier.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tier` | string enum `base`/`standard`/`desk` | `base` | Tier to quote |
| `include_credit_schedule` | boolean | `true` | Include per-severity credit schedule and monthly cap |
| `include_history` | boolean | `false` | Also return persisted credit history |

Returns the four-clause boilerplate, tier windows (ack + recovery), credit schedule with
severity multipliers and monthly cap, heartbeat threshold, and a comparison of all tiers.

### `check_sla_compliance`
Evaluate incident records against a tier's recovery window and compute self-executing credits.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `incidents` | array of object (**required**) | - | `{id, service, severity, detected_at, recovered_at, notes}` |
| `tier` | string enum | `base` | SLA tier |
| `account_id` | string | `acct-default` | Account credits are issued against |
| `monthly_fee_usd` | number | tier default | Actual billed fee for the period |
| `persist` | boolean | `true` | Persist incidents and credit events |

Recovery time is measured operator-side detection to restoration of last-known-good behaviour.
Credits are proportional (`credit_pct_per_breach` x severity multiplier) and capped at the tier
monthly cap. Each credit carries a `billing_token_execution` block - it self-executes, no email
negotiation.

### `get_credit_history`
The credit ledger. Parameters: `account_id`, `tier`, `limit` (default 50).

### `set_alert_threshold`
Configure heartbeat escalation and alerting. Parameters: `watcher_heartbeat_gap_minutes`,
`auto_escalate_on_miss`, `notify_email`, `recovery_window_minutes` + `apply_to_tier`.

### `get_post_mortem_history`
Public post-mortems including misses. Parameters: `account_id`, `tier`, `include_misses_only`,
`limit`.

### `run_adversarial_test`
Execute an adversarial test. Parameters: `account_id`, `tier`, `scenario_id` (omit for rotation),
`seed` (reproducible), `window_announced` (default `false`), `monthly_fee_usd`, `persist`.

Goodhart controls are returned in `run.goodhart_controls`: scenarios rotate through an 8-scenario
catalogue, windows are unannounced by default, and no scenario is replayed verbatim inside the
rotation window. A miss self-executes a credit and publishes a post-mortem.

## Tiers

| Tier | Recovery window | Ack window | Credit/breach | Monthly cap | Fee |
|---|---|---|---|---|---|
| base | 240 min | 60 min | 5% | 30% | $250 |
| standard | 120 min | 30 min | 7% | 40% | $400 |
| desk | 45 min | 10 min | 10% | 50% | $650 |

Severity multipliers: sev1 x1.5, sev2 x1.0, sev3 x0.5.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

State is persisted as JSON in `./state/sla_state.json`. Override the location with
`MCP_AGENT_SLA_STATE_DIR`.

## Connecting to an MCP client

```json
{
  "mcpServers": {
    "agent-sla": {
      "command": "python3",
      "args": ["/tmp/v3-services/mcp-agent-sla/main.py"]
    }
  }
}
```

## License

MIT
