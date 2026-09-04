# Project Review

> **Point-in-time review, written 2026-07-10.** The assessment below reflects the
> codebase as it was then; the counts it cites have since moved. As of
> 2026-09-04 (v0.12.0): **37 MCP tools**, **777 tests** passing, `server.py` is
> ~4,470 lines, and implementation is ~15,800 lines across `src/ask_fable/`.
> The review's own "stale counts" observation referred to 16-vs-19-tool drift,
> since corrected.

## Overall assessment

`ask-fable` is an unusually polished and thoughtful solo project. It feels like a
real tool built from genuine use, not a portfolio project searching for a purpose.
For a non-commercial project, it is very successful.

## What stands out

- The core idea is genuinely useful: expose several reasoning models through one
  guarded MCP interface.
- Council, chain, and debate are meaningfully different workflows, not superficial
  provider wrappers.
- Failure handling is mature: timeouts, partial degradation, circuit breaking,
  atomic persistence, session locking, audit logs, context limits, and structured
  errors.
- The structured sidecar and context bus show strong agent-oriented design thinking.
- Documentation is exceptionally thorough, including the visual architecture.
- The implementation is well tested: 356 tests pass, and Ruff reports no issues.
- The project has roughly 6,600 lines of implementation and 4,800 lines of tests,
  which is serious coverage for solo work.

## Main concern: concentrated complexity

The biggest weakness is concentration of complexity. `src/ask_fable/server.py` is
about 2,050 lines and owns schemas, routing, orchestration, consensus, debates,
context handling, and MCP handlers. It has become the project's gravity well.

It would be reasonable to split it eventually, but only along concepts that already
exist:

- Tool schemas
- Single-oracle handlers
- Council and chain orchestration
- Debate orchestration

This does not need a new framework or additional abstraction layers.

## Product focus

The product surface is approaching "too many good ideas at once." Nineteen tools,
numerous providers, councils, chains, debates, context storage, statistics, caching,
sessions, skills, and personas make the project impressive, but harder to explain.

The strongest identity is:

> A local MCP reasoning router that lets coding agents consult and reconcile
> independent models.

Everything should reinforce that sentence. Personas and elaborate presentation are
fun, but they slightly blur the center.

## Smaller observations

- The README is excellent as a reference, but long as a first encounter. A short
  opening path with one installation command, one example, and one representative
  response would help.
- Documentation contains some stale counts: places say 16 tools while the README
  now describes 19.
- Running `python -m pytest` from an uninstalled source checkout cannot import the
  `src` package unless the project is installed or `PYTHONPATH=src` is supplied.
  The guide tells users to install it, so this is mainly contributor ergonomics.
- Type annotations are present but often broad (`dict`, untyped payloads), and no
  static type checker is configured. That is acceptable for a personal project,
  though increasingly risky as orchestration grows.
- The abstractions mostly look earned rather than speculative. This is overbuilt
  compared with a tiny personal utility, but not nonsensically over-engineered.

## Suggested direction

Rather than pursuing commercialization, provider completeness, or more modes now:

1. Use the project heavily and measure which tools actually earn their existence.
2. Freeze features temporarily.
3. Break up `server.py` without changing behavior.
4. Shorten the onboarding path and reconcile documentation drift.
5. Keep the unusual personality and visual identity. They make the project distinct.

## Conclusion

The project is technically strong, distinctive, and more complete than most solo
projects. Its current danger is not amateurism; it is success-driven accretion. The
next level is subtraction and consolidation, not another feature.
