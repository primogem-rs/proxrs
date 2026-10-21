# Own acceptance proxy sessions independently of Docker fixtures

Give each proxrs invocation in the acceptance harness its own owner for its process, captured output, observed local proxy address, and cleanup, while the reusable Docker fixture owns the SSH server and generated credentials.
This separates invocation state when multiple proxy sessions share one fixture and concentrates lifecycle bookkeeping across pipe and terminal launches.
Keep raw output, terminal interaction, exit status, and socket observations available to cases, which verify product behavior before emergency cleanup can hide a failure.

## Consequences

- Pipe and PTY adapters share lifetime management while retaining their distinct input/output and terminal-observation capabilities.
- Launch is separate from readiness: cases choose the complete export block for `up`, a controlled managed command's marker for `run`, or observations made before either appears.
- Ownership registration must withstand ordinary SIGINT/SIGTERM during acquisition, including the managed command's separate process group; controlled test helpers may report ownership earlier when needed.
- Cleanup has deadlines, attempts every owned resource, and reports cleanup failures alongside the original test failure.
- The session owner alone collects and caches its child's exit status and resource usage; the load cases retain per-invocation peak-memory measurements of the actual proxrs process.
- Migrate every Python acceptance path that launches proxrs, including terminal, recovery, cleanup, and load cases, then remove process tuples and shared fixture proxy-port state so callers identify each proxy session explicitly.
