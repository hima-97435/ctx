# Vision

## Problem

Work context is scattered across tools: OpenCode sessions, VS Code, terminal activity,
git history, and AI chats. Each tool remembers only its own slice, so questions that
span tools or time — "what was I working on Tuesday?", "why did I choose this
architecture?", "where did I hit this error?" — have no answer. ctx collects these
slices into one local store and builds retrieval on top.

## Target user

A single developer (me) working on a personal laptop across many projects (ML
research, web, tooling), who already generates rich context in coding-agent sessions
and wants it reusable — by themselves, and by other coding agents.

## Example questions ctx must answer

1. What was I working on last Tuesday / last week in project X?
2. Where did I hit error E, and how did I eventually fix it?
3. Why did I pick architecture/approach Y in project Z?
4. Give a new agent a handoff brief for project P: current state, open TODOs, recent decisions.
5. Which sessions touched file F (or directory D) and when?
6. What setup steps did project P need (env vars, installs, quirks)?
7. What did I try and **reject** for problem Q, so I don't retry it?
8. Summarize the last session in project P with links to the exact messages.

## Product shape

A local CLI (`ctx`) with an optional MCP server later, so other agents can query the
same memory. Search and timeline are deterministic; Q&A and synthesis use an LLM
only through one context-assembly seam, with evidence links back to raw events.

## Non-goals

- No knowledge graph, no vector DB server, no Docker, no GUI (v1).
- No network traffic interception, no keystroke capture, no full browser history
  (web AI chats come only from the user's own official data exports, ADR-025; a browser
  extension is deferred, not rejected).
- No cloud sync, no multi-user, no hosted anything.
- No fully-automated "memory" that asserts things I didn't decide: synthesized facts
  carry status (`suggested | confirmed | abandoned`) and must cite evidence.
- No silent LLM calls: cloud providers are per-project opt-in (`local_only=1` default).

See `docs/ARCHITECTURE.md` for how the above is enforced structurally.
