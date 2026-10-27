# mcp-agent-sla

MCP server for Agent Failure-Recovery SLA - SLA terms, compliance checking, credit history, adversarial testing.

## Tools

### get_sla_terms
Get SLA terms for a tier (basic, professional, enterprise).

**Parameters:**
- `tier` (string, optional): SLA tier - "basic", "professional", or "enterprise"

### check_sla_compliance
Check SLA compliance for an agent over a period.

**Parameters:**
- `agent_id` (string, required): Agent identifier
- `period_days` (integer, optional): Period in days (default: 30)

### get_credit_history
Get SLA credit history for an agent.

**Parameters:**
- `agent_id` (string, required): Agent identifier
- `months` (integer, optional): Months of history (default: 6)

### run_adversarial_test
Run adversarial test on an agent (chaos engineering).

**Parameters:**
- `agent_id` (string, required): Agent identifier
- `test_type` (string, required): Test type - "chaos_network_partition", "chaos_cpu_stress", "chaos_memory_pressure", "chaos_disk_failure", "chaos_dependency_failure"

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

## Connecting to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-sla": {
      "command": "python",
      "args": ["/path/to/mcp-agent-sla/main.py"],
      "cwd": "/path/to/mcp-agent-sla"
    }
  }
}
```

## License

MIT