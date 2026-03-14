# Review-fix pipeline

dgov provides an automated "review-then-fix" pipeline. It dispatches a review agent to find problems, parses those findings into a structured list, and then dispatches a fix agent to resolve them one file at a time.

## Three phases

1. **Review**: A review agent scans targets and outputs structured JSON findings.
2. **Approve**: If not using `--auto-approve`, the user reviews the findings.
3. **Fix**: dgov dispatches fix workers for each file with findings. Each fix is merged and then validated by running your repository's test suite.

## Running the pipeline

```bash
# Review a directory and fix findings of medium or higher severity
dgov review-fix -t src/ \
  --review-agent claude \
  --fix-agent claude \
  --severity medium
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--targets` | `-t` | string | `None` | File or directory paths to review |
| `--review-agent` | | string | `claude` | Agent for the review phase |
| `--fix-agent` | | string | `claude` | Agent for the fix phase |
| `--auto-approve` | | bool | `False` | Dispatch fixes immediately without manual check |
| `--severity` | | string | `medium` | Threshold: `critical`, `medium`, or `low` |
| `--timeout` | | int | `600` | Timeout per phase (in seconds) |

## Severity threshold

Findings are filtered by the severity you specify:
- **critical**: Only critical bugs or security vulnerabilities.
- **medium**: Includes critical + logic errors and performance issues.
- **low**: Includes everything, including style nitpicks.

## Review prompt format

The review agent is instructed to output a JSON array of findings with specific fields:

```json
[
  {
    "file": "src/parser.py",
    "line": 42,
    "severity": "medium",
    "category": "bug",
    "description": "Off-by-one in loop",
    "suggested_fix": "Change < to <="
  }
]
```

## Fix prompt format

For each file, the fix agent receives a prompt summarizing all findings for that file. It is instructed to apply the fixes, run `ruff check --fix` and `ruff format`, and commit the changes.

## Output

After the pipeline finishes, dgov outputs a summary of findings, fixes applied, and the pass/fail status of your tests after the merges.

```json
{
  "phase": "complete",
  "findings_count": 12,
  "fixed_count": 10,
  "merged_count": 10,
  "failed_count": 2,
  "test_status": "pass"
}
```
