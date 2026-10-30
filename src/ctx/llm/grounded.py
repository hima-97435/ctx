"""Run a provider over a `ContextPackage` and check the answer's citations.

Flow for every LLM feature (ask, handoff --llm, summarize): context comes from
`build_context`; the messages from `assemble_prompt`; the policy gate (`authorize`) runs
over the projects of the items actually sent; then the provider is called. The model's
inline `[E<event id>]` citations are checked against the package: an id that was not in
the context is a hallucinated citation and is flagged, never silently kept.
"""

import re
import sqlite3
from dataclasses import dataclass, field

from ctx.core.context import INSUFFICIENT, ContextPackage, assemble_prompt
from ctx.core.redact import redact
from ctx.core.search import Citation, resolve_project
from ctx.llm.policy import authorize
from ctx.llm.providers import Provider

_GROUP = re.compile(r"\[([^\[\]]*)\]")
_ID = re.compile(r"\bE(\d+)\b")


@dataclass
class Grounded:
    text: str
    cited: list[int] = field(default_factory=list)  # valid ids, first-mention order
    hallucinated: list[int] = field(default_factory=list)  # cited but never in the context
    citations: list[Citation] = field(default_factory=list)
    insufficient: bool = False
    provider: str = ""
    model: str = ""

    @property
    def ok(self) -> bool:
        """Grounded: no hallucinated ids, and either cited evidence or an honest refusal."""
        return not self.hallucinated and (bool(self.cited) or self.insufficient)

    def warnings(self) -> list[str]:
        out = []
        if self.hallucinated:
            ids = ", ".join(f"E{i}" for i in self.hallucinated)
            out.append(f"hallucinated citation(s) not in the supplied context: {ids}")
        if not self.cited and not self.insufficient:
            out.append("answer cites no evidence; treat it as ungrounded")
        return out


def extract_citation_ids(text: str) -> list[int]:
    seen: dict[int, None] = {}
    for group in _GROUP.findall(text):
        for m in _ID.finditer(group):
            seen.setdefault(int(m.group(1)), None)
    return list(seen)


def ground(text: str, package: ContextPackage) -> Grounded:
    text = redact(text).strip()
    by_id = {i.citation.event_id: i.citation for i in package.items}
    ids = extract_citation_ids(text)
    cited = [i for i in ids if i in by_id]
    return Grounded(
        text=text,
        cited=cited,
        hallucinated=[i for i in ids if i not in by_id],
        citations=[by_id[i] for i in cited],
        insufficient=INSUFFICIENT in text.upper() and not cited,
    )


def package_scope(
    conn: sqlite3.Connection, package: ContextPackage, project: str | None
) -> set[str | None]:
    scope = {i.citation.project_id for i in package.items}
    if project is not None:
        scope.add(resolve_project(conn, project))
    return scope


def run_grounded(
    conn: sqlite3.Connection,
    provider: Provider,
    package: ContextPackage,
    task: str,
    *,
    project: str | None = None,
    max_tokens: int = 1024,
) -> Grounded:
    """Gate, call, ground. Raises CloudRefused / LLMError; sends nothing if refused."""
    clearance = authorize(conn, provider, package_scope(conn, package, project))
    reply = provider.chat(
        assemble_prompt(package, task), clearance=clearance, max_tokens=max_tokens
    )
    g = ground(reply, package)
    g.provider, g.model = provider.name, provider.model
    return g


def render_citations(g: Grounded) -> str:
    lines = []
    for c in g.citations:
        lines.append(
            f"[E{c.event_id}] {c.source} session={c.source_session_id or '-'} "
            f"message={c.message_id or '-'} project={c.project_id or '-'}"
        )
    return "\n".join(lines)


def ask(
    conn: sqlite3.Connection,
    provider: Provider,
    package: ContextPackage,
    *,
    project: str | None = None,
) -> Grounded:
    """Answer `package.query` from the package only. No evidence -> honest refusal, no LLM call."""
    if not package.items:
        return Grounded(text=INSUFFICIENT, insufficient=True)
    return run_grounded(
        conn,
        provider,
        package,
        "Answer the question from the evidence only.",
        project=project,
    )
