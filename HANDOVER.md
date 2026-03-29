# Handover: Intelligence Matrix Canonicalized

## Session context
- Date: 2026-03-29
- Branch: main @ f41f84a
- Last commit: Handover: dgov skills reorganized for multi-agent access

## Open panes
| Slug | State | Description |
|------|-------|-------------|
| — | — | No active panes |

## Open bugs/issues
- None

## Blockers/debt
- None

## Next steps
1. Commit the intelligence matrix changes (CLAUDE.md, .dgov/agents.toml)
2. Test routing with `dgov plan run` on a small plan to verify T3 Kimi saturation
3. Monitor 429 frequency — tune `retry_policy` if Fireworks throttles

## Changes made this session
- **CLAUDE.md**: Replaced role-based routing with (DecisionKind, CapabilityTier) matrix
- **.dgov/agents.toml**: Full matrix routing with T3 Kimi 5× pool for greedy Fire Pass usage
- **Ledger #200**: Recorded canonical intelligence hierarchy decision

## Configuration summary
| Tier | Models | Use |
|------|--------|-----|
| T1 | River 9B, MLX 9B | Fast, parsing |
| T2 | River 35B | Validation |
| T3 | Kimi K2.5 ×5 (Fire Pass) | Default intelligence, greedy concurrency |
| T4 | Gemini, Codex, Claude | Frontier: planning, eval writing, audit |

Escalation: 2 attempts per cell → Up → Across → Governor
