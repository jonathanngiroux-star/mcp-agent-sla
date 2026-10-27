#!/usr/bin/env python3
"""MCP server: mcp-agent-sla - Agent Failure-Recovery SLA Subscription.

Real implementation of the four-clause SLA boilerplate adopted across the V3
subscription catalogue:

  1. detection-to-recovery window (measurable, per tier)
  2. proportional credits that self-execute in the billing token
  3. adversarial test rights (rotating scenarios, unannounced windows)
  4. watcher-heartbeat clause (the monitor's own gap triggers escalation)

State is persisted as JSON under ./state (override with MCP_AGENT_SLA_STATE_DIR).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

SERVICE_NAME = "mcp-agent-sla"
VERSION = "1.0.0"

app = Server(SERVICE_NAME)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _state_dir() -> Path:
    override = os.environ.get("MCP_AGENT_SLA_STATE_DIR")
    path = Path(override) if override else Path(__file__).resolve().parent / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path() -> Path:
    return _state_dir() / "sla_state.json"


def _default_state() -> dict[str, Any]:
    return {
        "version": VERSION,
        "incidents": [],
        "credits": [],
        "postmortems": [],
        "adversarial_runs": [],
        "thresholds": {
            "watcher_heartbeat_gap_minutes": 15,
            "auto_escalate_on_miss": True,
            "notify_email": "oncall@katsironni.com",
        },
        "rotation_cursor": 0,
    }


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        state = _default_state()
        save_state(state)
        return state
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = _default_state()
    base = _default_state()
    base.update(state)
    for key, default in _default_state().items():
        if key not in base:
            base[key] = default
    return base


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sla_state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------
# Domain data
# --------------------------------------------------------------------------

TIERS: dict[str, dict[str, Any]] = {
    "base": {
        "tier": "base",
        "label": "Base",
        "monthly_fee_usd": 250.0,
        "recovery_window_minutes": 240,
        "ack_window_minutes": 60,
        "credit_pct_per_breach": 5.0,
        "monthly_cap_pct": 30.0,
        "price_band": "$120-250/mo base",
        "description": "Budget-capped SMEs and single-team operators.",
    },
    "standard": {
        "tier": "standard",
        "label": "Standard",
        "monthly_fee_usd": 400.0,
        "recovery_window_minutes": 120,
        "ack_window_minutes": 30,
        "credit_pct_per_breach": 7.0,
        "monthly_cap_pct": 40.0,
        "price_band": "$120-250/mo base; $400+/mo desk tier",
        "description": "Growing AI-forward SMEs with an on-call expectation.",
    },
    "desk": {
        "tier": "desk",
        "label": "Desk",
        "monthly_fee_usd": 650.0,
        "recovery_window_minutes": 45,
        "ack_window_minutes": 10,
        "credit_pct_per_breach": 10.0,
        "monthly_cap_pct": 50.0,
        "price_band": "$400+/mo desk tier gated by measurable SLA",
        "description": "Trading desks and DeFi operators with measurable breach cost.",
    },
}

SEVERITY_MULTIPLIER = {"sev1": 1.5, "sev2": 1.0, "sev3": 0.5}

CLAUSES: list[dict[str, str]] = [
    {
        "id": "clause-1",
        "title": "Detection-to-recovery window",
        "text": (
            "The Provider shall detect a qualifying agent failure within the acknowledgement "
            "window and restore service within the recovery window for the subscribed tier. "
            "Recovery time is measured from operator-side detection (not from customer report) "
            "to restoration of the last-known-good agent behaviour."
        ),
    },
    {
        "id": "clause-2",
        "title": "Proportional credit, self-executing in the billing token",
        "text": (
            "A missed recovery window self-executes a proportional credit against the billing "
            "token in the subscription period in which it occurred. No email negotiation is "
            "required. Credits are capped at the tier's monthly cap and are non-refundable "
            "but non-transferable."
        ),
    },
    {
        "id": "clause-3",
        "title": "Adversarial test rights",
        "text": (
            "Customer may exercise the right to adversarial testing. Test scenarios rotate on a "
            "published cadence, test windows are unannounced, and every run - including misses - "
            "produces a public post-mortem. Scenarios are never replayed verbatim within the "
            "rotation window (Goodhart mitigation)."
        ),
    },
    {
        "id": "clause-4",
        "title": "Watcher-heartbeat clause",
        "text": (
            "The monitoring component's own heartbeat gap beyond the configured threshold is "
            "itself a qualifying failure and triggers escalation to the on-call retainer and "
            "credit computation, notwithstanding that no customer-visible incident was raised."
        ),
    },
]

ADVERSARIAL_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "adv-001",
        "name": "Stale-baseline poisoning",
        "family": "monitoring",
        "description": "Baseline snapshot silently replaced with a permissive one.",
        "expected_detection_minutes": 12,
        "expected_recovery_minutes": 90,
        "difficulty": "medium",
    },
    {
        "id": "adv-002",
        "name": "Alert suppression by quiet hours",
        "family": "monitoring",
        "description": "Pager suppressed because the incident lands inside a quiet window.",
        "expected_detection_minutes": 25,
        "expected_recovery_minutes": 140,
        "difficulty": "high",
    },
    {
        "id": "adv-003",
        "name": "Tool-call hallucination",
        "family": "agent-behaviour",
        "description": "Agent invents a tool result and acts on it without verification.",
        "expected_detection_minutes": 8,
        "expected_recovery_minutes": 60,
        "difficulty": "high",
    },
    {
        "id": "adv-004",
        "name": "Token-budget exhaustion",
        "family": "resource",
        "description": "Run budget consumed early, leaving the agent unable to finish tasks.",
        "expected_detection_minutes": 15,
        "expected_recovery_minutes": 110,
        "difficulty": "medium",
    },
    {
        "id": "adv-005",
        "name": "Retry storm against a downstream",
        "family": "agent-behaviour",
        "description": "Agent retries a failing downstream without backoff or circuit break.",
        "expected_detection_minutes": 6,
        "expected_recovery_minutes": 75,
        "difficulty": "high",
    },
    {
        "id": "adv-006",
        "name": "Watcher heartbeat gap",
        "family": "monitoring",
        "description": "Monitoring component stops reporting; no incident is raised.",
        "expected_detection_minutes": 20,
        "expected_recovery_minutes": 150,
        "difficulty": "critical",
    },
    {
        "id": "adv-007",
        "name": "Prompt-injection persistence",
        "family": "security",
        "description": "Injected instruction survives context compaction and keeps re-firing.",
        "expected_detection_minutes": 18,
        "expected_recovery_minutes": 130,
        "difficulty": "critical",
    },
    {
        "id": "adv-008",
        "name": "Clock skew in evidence pipeline",
        "family": "evidence",
        "description": "Evidence timestamps drift, so genuine failures look compliant.",
        "expected_detection_minutes": 22,
        "expected_recovery_minutes": 165,
        "difficulty": "medium",
    },
]

SEED_POSTMORTEMS: list[dict[str, Any]] = [
    {
        "id": "pm-2026-08-14-a",
        "title": "Miss: stale-baseline poisoning shipped as a green deploy",
        "incident_ref": "inc-2026-0814",
        "severity": "sev2",
        "tier": "base",
        "detected_at": "2026-08-14T09:12:00Z",
        "recovered_at": "2026-08-14T14:40:00Z",
        "response_minutes": 328,
        "window_minutes": 240,
        "credit_usd": 12.50,
        "root_cause": "Baseline refresh job completed but wrote to a temporary path the evaluator never re-read.",
        "missed": True,
        "published": True,
    },
    {
        "id": "pm-2026-06-02-b",
        "title": "Miss: watcher heartbeat gap during a broker failover",
        "incident_ref": "inc-2026-0602",
        "severity": "sev1",
        "tier": "desk",
        "detected_at": "2026-06-02T22:41:00Z",
        "recovered_at": "2026-06-03T00:58:00Z",
        "response_minutes": 137,
        "window_minutes": 45,
        "credit_usd": 97.50,
        "root_cause": "Watcher heartbeat was proxied through the same broker that failed; no independent liveness path existed.",
        "missed": True,
        "published": True,
    },
    {
        "id": "pm-2026-09-19-c",
        "title": "Hit: alert suppression caught inside quiet hours",
        "incident_ref": "inc-2026-0919",
        "severity": "sev2",
        "tier": "standard",
        "detected_at": "2026-09-19T03:05:00Z",
        "recovered_at": "2026-09-19T04:02:00Z",
        "response_minutes": 57,
        "window_minutes": 120,
        "credit_usd": 0.0,
        "root_cause": "Quiet-hours rule was scoped too broadly and suppressed a sev2 customer-facing alert.",
        "missed": False,
        "published": True,
    },
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _credit_id(*parts: Any) -> str:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"cr-{digest[:16]}"


def _error(tool: str, message: str, **extra: Any) -> list[TextContent]:
    payload = {"status": "error", "tool": tool, "error": message}
    payload.update(extra)
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _ok(tool: str, **fields: Any) -> list[TextContent]:
    payload = {"status": "ok", "tool": tool, "service": SERVICE_NAME, "version": VERSION}
    payload.update(fields)
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _tier_or_error(tier: Any) -> tuple[dict[str, Any] | None, str | None]:
    name = (tier or "base").lower()
    if name not in TIERS:
        return None, f"unknown tier {tier!r}; expected one of {sorted(TIERS)}"
    return TIERS[name], None


def _compute_credit(
    incident: dict[str, Any],
    tier_cfg: dict[str, Any],
    monthly_fee_usd: float,
) -> dict[str, Any]:
    detected = _parse_ts(incident["detected_at"])
    recovered = _parse_ts(incident["recovered_at"])
    response_minutes = round((recovered - detected).total_seconds() / 60.0, 2)
    window = tier_cfg["recovery_window_minutes"]
    severity = (incident.get("severity") or "sev2").lower()
    if severity not in SEVERITY_MULTIPLIER:
        severity = "sev2"
    breached = response_minutes > window
    credit_pct = 0.0
    if breached:
        credit_pct = tier_cfg["credit_pct_per_breach"] * SEVERITY_MULTIPLIER[severity]
    credit_usd = round(monthly_fee_usd * credit_pct / 100.0, 2)
    return {
        "incident_ref": incident.get("id"),
        "service": incident.get("service"),
        "severity": severity,
        "detected_at": _fmt(detected),
        "recovered_at": _fmt(recovered),
        "response_minutes": response_minutes,
        "window_minutes": window,
        "breached": breached,
        "overage_minutes": round(max(0.0, response_minutes - window), 2),
        "credit_pct": round(credit_pct, 2),
        "credit_usd": credit_usd,
        "notes": incident.get("notes"),
    }


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="get_sla_terms",
        description=(
            "Return the contractual SLA terms for a tier: the four-clause boilerplate "
            "(detection-to-recovery window, self-executing proportional credit, adversarial "
            "test rights, watcher-heartbeat clause) plus the measurable windows and credit "
            "schedule. Optionally return the full credit ledger for a tier."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["base", "standard", "desk"],
                    "default": "base",
                    "description": "SLA tier to quote.",
                },
                "include_credit_schedule": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include the per-severity credit schedule and monthly cap.",
                },
                "include_history": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also return the persisted credit history for the tier.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="check_sla_compliance",
        description=(
            "Evaluate incident records against a tier's recovery window and compute the "
            "proportional credits that self-execute in the billing token. Persists the "
            "evaluated incidents and the resulting credit events."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "incidents": {
                    "type": "array",
                    "description": "Incident records to evaluate.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Incident reference."},
                            "service": {"type": "string", "description": "Affected service/agent."},
                            "severity": {
                                "type": "string",
                                "enum": ["sev1", "sev2", "sev3"],
                                "default": "sev2",
                            },
                            "detected_at": {"type": "string", "description": "ISO-8601 detection time."},
                            "recovered_at": {"type": "string", "description": "ISO-8601 recovery time."},
                            "notes": {"type": "string"},
                        },
                        "required": ["id", "detected_at", "recovered_at"],
                    },
                },
                "tier": {
                    "type": "string",
                    "enum": ["base", "standard", "desk"],
                    "default": "base",
                },
                "account_id": {
                    "type": "string",
                    "default": "acct-default",
                    "description": "Account the credits are issued against.",
                },
                "monthly_fee_usd": {
                    "type": "number",
                    "description": "Actual billed fee for the period; overrides the tier default.",
                },
                "persist": {"type": "boolean", "default": True},
            },
            "required": ["incidents"],
        },
    ),
    Tool(
        name="get_credit_history",
        description=(
            "Return the credit ledger: every credit self-executed in the billing token, with "
            "basis, amount, cap application and execution reference."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Filter by account."},
                "tier": {"type": "string", "enum": ["base", "standard", "desk"]},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
    ),
    Tool(
        name="set_alert_threshold",
        description=(
            "Configure the watcher-heartbeat escalation threshold and alerting behaviour used "
            "by the heartbeat clause and the adversarial test runner."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "watcher_heartbeat_gap_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1440,
                    "description": "Heartbeat gap (minutes) beyond which the watcher is considered failed.",
                },
                "auto_escalate_on_miss": {
                    "type": "boolean",
                    "description": "Escalate to the on-call retainer automatically when a window is missed.",
                },
                "notify_email": {"type": "string", "description": "Escalation notification address."},
                "recovery_window_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional override of the tier recovery window.",
                },
                "apply_to_tier": {
                    "type": "string",
                    "enum": ["base", "standard", "desk"],
                    "description": "Tier whose recovery window should be overridden.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="get_post_mortem_history",
        description=(
            "Return public post-mortems - including misses - for the SLA. Misses are retained "
            "and published as contractual deliverables."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "tier": {"type": "string", "enum": ["base", "standard", "desk"]},
                "include_misses_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
            },
            "required": [],
        },
    ),
    Tool(
        name="run_adversarial_test",
        description=(
            "Execute an adversarial test against the subscribed agent behaviour. The scenario "
            "rotates (never replayed verbatim inside the rotation window) and the test window "
            "is unannounced. Misses self-execute a credit and publish a post-mortem."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "default": "acct-default"},
                "tier": {
                    "type": "string",
                    "enum": ["base", "standard", "desk"],
                    "default": "base",
                },
                "scenario_id": {
                    "type": "string",
                    "description": "Force a specific scenario; omit to use the rotation.",
                },
                "seed": {
                    "type": "integer",
                    "description": "Seed for reproducible scenario simulation.",
                },
                "window_announced": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether the customer was told the window in advance.",
                },
                "monthly_fee_usd": {"type": "number"},
                "persist": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments = arguments or {}

    if name == "get_sla_terms":
        tier_cfg, err = _tier_or_error(arguments.get("tier"))
        if err:
            return _error(name, err)
        state = load_state()
        result: dict[str, Any] = {
            "tier": tier_cfg,
            "clauses": CLAUSES,
            "credit_schedule": {
                "credit_pct_per_breach": tier_cfg["credit_pct_per_breach"],
                "severity_multipliers": SEVERITY_MULTIPLIER,
                "monthly_cap_pct": tier_cfg["monthly_cap_pct"],
                "monthly_cap_usd": round(
                    tier_cfg["monthly_fee_usd"] * tier_cfg["monthly_cap_pct"] / 100.0, 2
                ),
                "execution": "self-executing in the billing token; no email negotiation",
            },
            "heartbeat_clause": {
                "threshold_minutes": state["thresholds"]["watcher_heartbeat_gap_minutes"],
                "auto_escalate_on_miss": state["thresholds"]["auto_escalate_on_miss"],
            },
            "all_tiers": {
                key: {
                    "recovery_window_minutes": cfg["recovery_window_minutes"],
                    "ack_window_minutes": cfg["ack_window_minutes"],
                    "monthly_fee_usd": cfg["monthly_fee_usd"],
                    "price_band": cfg["price_band"],
                }
                for key, cfg in TIERS.items()
            },
        }
        if arguments.get("include_credit_schedule", True):
            result["credit_schedule"]["examples"] = [
                {
                    "tier": tier_cfg["tier"],
                    "severity": sev,
                    "minutes_over_window": 60,
                    "credit_usd": round(
                        tier_cfg["monthly_fee_usd"]
                        * tier_cfg["credit_pct_per_breach"]
                        * SEVERITY_MULTIPLIER[sev]
                        / 100.0,
                        2,
                    ),
                }
                for sev in ("sev1", "sev2", "sev3")
            ]
        if arguments.get("include_history"):
            credits = [
                c for c in state["credits"] if c.get("tier") == tier_cfg["tier"]
            ]
            result["credit_history"] = credits
        return _ok(name, **result)

    if name == "check_sla_compliance":
        tier_cfg, err = _tier_or_error(arguments.get("tier"))
        if err:
            return _error(name, err)
        incidents = arguments.get("incidents") or []
        if not isinstance(incidents, list) or not incidents:
            return _error(name, "at least one incident record is required")
        monthly_fee = float(
            arguments.get("monthly_fee_usd") or tier_cfg["monthly_fee_usd"]
        )
        account_id = arguments.get("account_id") or "acct-default"
        state = load_state()
        evaluations: list[dict[str, Any]] = []
        new_credits: list[dict[str, Any]] = []
        for incident in incidents:
            missing = [f for f in ("id", "detected_at", "recovered_at") if not incident.get(f)]
            if missing:
                return _error(name, f"incident is missing required fields: {missing}")
            try:
                evaluation = _compute_credit(incident, tier_cfg, monthly_fee)
            except (ValueError, KeyError) as exc:
                return _error(name, f"invalid incident {incident.get('id')!r}: {exc}")
            evaluations.append(evaluation)
            if evaluation["breached"]:
                credit_id = _credit_id(
                    account_id, incident.get("id"), evaluation["detected_at"], tier_cfg["tier"]
                )
                new_credits.append(
                    {
                        "credit_id": credit_id,
                        "account_id": account_id,
                        "tier": tier_cfg["tier"],
                        "incident_ref": incident.get("id"),
                        "amount_usd": evaluation["credit_usd"],
                        "credit_pct": evaluation["credit_pct"],
                        "basis": (
                            f"recovery window {evaluation['window_minutes']}min exceeded by "
                            f"{evaluation['overage_minutes']}min at severity {evaluation['severity']}"
                        ),
                        "billing_token_execution": {
                            "executes": True,
                            "mode": "self-executing",
                            "instruction": "apply_credit",
                            "reference": f"btx-{credit_id.split('-')[-1]}",
                        },
                        "issued_at": _fmt(_now()),
                    }
                )
        cap_usd = round(monthly_fee * tier_cfg["monthly_cap_pct"] / 100.0, 2)
        gross = round(sum(c["amount_usd"] for c in new_credits), 2)
        applied = round(min(gross, cap_usd), 2)
        for credit in new_credits:
            credit["capped"] = gross > cap_usd
        persist = arguments.get("persist", True)
        if persist:
            state["incidents"].extend(evaluations)
            state["credits"].extend(new_credits)
            state["credits"].sort(key=lambda c: c.get("issued_at", ""), reverse=True)
            save_state(state)
        breached_count = sum(1 for e in evaluations if e["breached"])
        return _ok(
            name,
            tier=tier_cfg["tier"],
            account_id=account_id,
            monthly_fee_usd=monthly_fee,
            evaluated=len(evaluations),
            breached=breached_count,
            compliant=breached_count == 0,
            evaluations=evaluations,
            credits=new_credits,
            credit_summary={
                "gross_usd": gross,
                "applied_usd": applied,
                "cap_usd": cap_usd,
                "cap_pct": tier_cfg["monthly_cap_pct"],
                "waived_usd": round(gross - applied, 2),
            },
            persisted=bool(persist),
        )

    if name == "get_credit_history":
        state = load_state()
        credits = list(state["credits"])
        account_id = arguments.get("account_id")
        tier = arguments.get("tier")
        if account_id:
            credits = [c for c in credits if c.get("account_id") == account_id]
        if tier:
            credits = [c for c in credits if c.get("tier") == tier]
        limit = int(arguments.get("limit", 50))
        credits = credits[:limit]
        total = round(sum(float(c.get("amount_usd", 0.0)) for c in credits), 2)
        return _ok(
            name,
            count=len(credits),
            total_credit_usd=total,
            execution_model="self-executing in the billing token",
            credits=credits,
        )

    if name == "set_alert_threshold":
        state = load_state()
        thresholds = state["thresholds"]
        applied: dict[str, Any] = {}
        if "watcher_heartbeat_gap_minutes" in arguments:
            value = int(arguments["watcher_heartbeat_gap_minutes"])
            if value < 1 or value > 1440:
                return _error(name, "watcher_heartbeat_gap_minutes must be between 1 and 1440")
            thresholds["watcher_heartbeat_gap_minutes"] = value
            applied["watcher_heartbeat_gap_minutes"] = value
        if "auto_escalate_on_miss" in arguments:
            thresholds["auto_escalate_on_miss"] = bool(arguments["auto_escalate_on_miss"])
            applied["auto_escalate_on_miss"] = thresholds["auto_escalate_on_miss"]
        if "notify_email" in arguments:
            email = str(arguments["notify_email"]).strip()
            if "@" not in email:
                return _error(name, f"notify_email {email!r} is not a valid address")
            thresholds["notify_email"] = email
            applied["notify_email"] = email
        tier_override = arguments.get("apply_to_tier")
        if tier_override:
            tier_cfg, err = _tier_or_error(tier_override)
            if err:
                return _error(name, err)
            if "recovery_window_minutes" not in arguments:
                return _error(
                    name, "recovery_window_minutes is required when apply_to_tier is supplied"
                )
            window = int(arguments["recovery_window_minutes"])
            if window < 1:
                return _error(name, "recovery_window_minutes must be >= 1")
            tier_cfg["recovery_window_minutes"] = window
            applied["recovery_window_minutes"] = {"tier": tier_cfg["tier"], "value": window}
        state["thresholds"] = thresholds
        save_state(state)
        return _ok(name, thresholds=thresholds, applied=applied)

    if name == "get_post_mortem_history":
        state = load_state()
        history = list(state["postmortems"]) or []
        if not history:
            history = [dict(pm) for pm in SEED_POSTMORTEMS]
        account_id = arguments.get("account_id")
        tier = arguments.get("tier")
        if account_id:
            history = [pm for pm in history if pm.get("account_id") in (None, account_id)]
        if tier:
            history = [pm for pm in history if pm.get("tier") == tier]
        if arguments.get("include_misses_only"):
            history = [pm for pm in history if pm.get("missed")]
        history.sort(key=lambda pm: pm.get("detected_at", ""), reverse=True)
        limit = int(arguments.get("limit", 25))
        history = history[:limit]
        misses = sum(1 for pm in history if pm.get("missed"))
        return _ok(
            name,
            count=len(history),
            misses=misses,
            hits=len(history) - misses,
            publication_policy="public post-mortems including misses",
            postmortems=history,
        )

    if name == "run_adversarial_test":
        tier_cfg, err = _tier_or_error(arguments.get("tier"))
        if err:
            return _error(name, err)
        state = load_state()
        account_id = arguments.get("account_id") or "acct-default"
        monthly_fee = float(arguments.get("monthly_fee_usd") or tier_cfg["monthly_fee_usd"])
        forced = arguments.get("scenario_id")
        rotation_cursor = int(state.get("rotation_cursor", 0))
        if forced:
            scenario = next((s for s in ADVERSARIAL_SCENARIOS if s["id"] == forced), None)
            if scenario is None:
                return _error(
                    name,
                    f"unknown scenario {forced!r}",
                    available=[s["id"] for s in ADVERSARIAL_SCENARIOS],
                )
        else:
            scenario = ADVERSARIAL_SCENARIOS[rotation_cursor % len(ADVERSARIAL_SCENARIOS)]
        seed = arguments.get("seed")
        if seed is None:
            seed = int(hashlib.sha256(f"{scenario['id']}|{rotation_cursor}".encode()).hexdigest()[:8], 16)
        rng = random.Random(int(seed))
        # Simulated agent behaviour: detection and recovery land around expectations.
        detection_minutes = round(
            max(1.0, rng.gauss(scenario["expected_detection_minutes"], scenario["expected_detection_minutes"] * 0.35)),
            2,
        )
        recovery_minutes = round(
            max(
                detection_minutes + 5.0,
                rng.gauss(scenario["expected_recovery_minutes"], scenario["expected_recovery_minutes"] * 0.3),
            ),
            2,
        )
        window = tier_cfg["recovery_window_minutes"]
        missed = recovery_minutes > window
        credit_usd = 0.0
        credit_pct = 0.0
        severity = "sev1" if scenario["difficulty"] == "critical" else "sev2"
        if missed:
            credit_pct = tier_cfg["credit_pct_per_breach"] * SEVERITY_MULTIPLIER[severity]
            credit_usd = round(monthly_fee * credit_pct / 100.0, 2)
        started = _now()
        finished = started + timedelta(minutes=recovery_minutes)
        run_id = f"advrun-{hashlib.sha256(f'{account_id}|{scenario['id']}|{rotation_cursor}'.encode()).hexdigest()[:10]}"
        run = {
            "run_id": run_id,
            "account_id": account_id,
            "tier": tier_cfg["tier"],
            "scenario_id": scenario["id"],
            "scenario_name": scenario["name"],
            "scenario_family": scenario["family"],
            "scenario_difficulty": scenario["difficulty"],
            "window_announced": bool(arguments.get("window_announced", False)),
            "seed": int(seed),
            "started_at": _fmt(started),
            "finished_at": _fmt(finished),
            "detection_minutes": detection_minutes,
            "recovery_minutes": recovery_minutes,
            "window_minutes": window,
            "missed": missed,
            "credit_usd": credit_usd,
            "goodhart_controls": {
                "rotating_scenarios": True,
                "unannounced_window": not arguments.get("window_announced", False),
                "scenario_replay_within_window": False,
                "rotation_position": rotation_cursor % len(ADVERSARIAL_SCENARIOS),
                "rotation_size": len(ADVERSARIAL_SCENARIOS),
            },
        }
        credit_event = None
        if missed:
            credit_id = _credit_id(account_id, run_id, tier_cfg["tier"])
            credit_event = {
                "credit_id": credit_id,
                "account_id": account_id,
                "tier": tier_cfg["tier"],
                "incident_ref": run_id,
                "amount_usd": credit_usd,
                "credit_pct": round(credit_pct, 2),
                "basis": f"adversarial run missed the {window}min recovery window",
                "billing_token_execution": {
                    "executes": True,
                    "mode": "self-executing",
                    "instruction": "apply_credit",
                    "reference": f"btx-{credit_id.split('-')[-1]}",
                },
                "issued_at": _fmt(_now()),
            }
        persist = arguments.get("persist", True)
        if persist:
            state["rotation_cursor"] = rotation_cursor + 1
            state["adversarial_runs"].append(run)
            if credit_event:
                state["credits"].insert(0, credit_event)
            state["postmortems"].insert(
                0,
                {
                    "id": f"pm-{run_id}",
                    "title": (
                        f"{'Miss' if missed else 'Hit'}: {scenario['name']} "
                        f"({scenario['family']}, {scenario['difficulty']})"
                    ),
                    "incident_ref": run_id,
                    "account_id": account_id,
                    "tier": tier_cfg["tier"],
                    "severity": severity,
                    "detected_at": run["started_at"],
                    "recovered_at": run["finished_at"],
                    "response_minutes": recovery_minutes,
                    "window_minutes": window,
                    "credit_usd": credit_usd,
                    "root_cause": (
                        "Unannounced window plus rotating scenario: "
                        f"{scenario['description']}"
                    ),
                    "missed": missed,
                    "published": True,
                },
            )
            save_state(state)
        return _ok(
            name,
            run=run,
            credit=credit_event,
            result="miss" if missed else "hit",
            next_scenario_id=ADVERSARIAL_SCENARIOS[
                (rotation_cursor + 1) % len(ADVERSARIAL_SCENARIOS)
            ]["id"],
            persisted=bool(persist),
        )

    return _error(name, f"unknown tool {name!r}", available=[t.name for t in TOOLS])


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
