# ruff: noqa: E501
"""Synthetic multi-source corpus for the retrieval eval (M4). Entirely invented text.

Four sources (claude_code, codex, opencode, chatgpt), two linked projects plus one unlinked
web-chat source. Each session lists (message_id, role, kind, text). The eval set that
targets it is `eval_synthetic.jsonl` (same directory). Web chats are unlinked, so the eval
runs with include_all=True / `--all`.
"""

from pathlib import Path

from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.projects import load_projects_config
from ctx.core.sync import sync

EVAL_SET_PATH = Path(__file__).with_name("eval_synthetic.jsonl")

PROJECTS_TOML = """
[projects.alpha]
name = "Alpha"
allowed = true
roots = ["/work/alpha"]

[projects.beta]
name = "Beta"
allowed = true
roots = ["/work/beta"]
"""

T, A, R = "text", "assistant", "user"

# source -> [(session_id, cwd, title, [(message_id, role, kind, text), ...])]
CORPUS: dict[str, list[tuple[str, str | None, str, list[tuple[str, str, str, str]]]]] = {
    "claude_code": [
        (
            "cc1",
            "/work/alpha",
            "Fix login timeouts",
            [
                (
                    "m1",
                    R,
                    T,
                    "Users get logged out after 5 minutes. The session cookie seems to "
                    "expire way too early.",
                ),
                (
                    "m2",
                    A,
                    T,
                    "The cookie max-age was set to 300 seconds in src/auth/session.py. "
                    "I raised SESSION_TTL to 86400 and made it configurable through the "
                    "SESSION_TTL_SECONDS environment variable.",
                ),
            ],
        ),
        (
            "cc2",
            "/work/alpha",
            "Slow reports",
            [
                ("m1", R, T, "The /reports endpoint takes 8 seconds. Can you figure out why?"),
                (
                    "m2",
                    A,
                    T,
                    "EXPLAIN shows a sequential scan on the orders table. I added a "
                    "composite index on (customer_id, created_at) in "
                    "migrations/0042_add_orders_index.sql and the query dropped to 40 ms.",
                ),
            ],
        ),
        (
            "cc3",
            "/work/alpha",
            "Job queue storage",
            [
                ("m1", R, T, "Should we use Postgres or stay on SQLite for the job queue?"),
                (
                    "m2",
                    A,
                    T,
                    "We decided to stay on SQLite. Rationale: single-node deployment, "
                    "zero ops overhead, WAL mode handles our write rate, and Postgres would add a "
                    "container to maintain. Revisit if we ever need multiple writers.",
                ),
            ],
        ),
        (
            "cc4",
            "/work/alpha",
            "Docker build failure",
            [
                (
                    "m1",
                    R,
                    T,
                    'docker build fails with: ERROR: failed to solve: process "/bin/sh '
                    '-c pip install -r requirements.txt" did not complete successfully: exit code: 1',
                ),
                (
                    "m2",
                    A,
                    T,
                    "The failure is psycopg2 needing pg_config. I picked psycopg2-binary "
                    "in requirements.txt because it ships prebuilt wheels and keeps the image small; "
                    "the alternative was apt-get install libpq-dev in the builder stage.",
                ),
                (
                    "m3",
                    A,
                    "tool:Bash",
                    "output: Error: pg_config executable not found.\n"
                    "pg_config is required to build psycopg2 from source.",
                ),
            ],
        ),
        (
            "cc5",
            "/work/alpha",
            "Docs index",
            [
                ("m1", R, T, "Create an index page for the docs"),
                ("m2", A, T, "Added docs/index.md linking all the guides and the changelog."),
            ],
        ),
    ],
    "codex": [
        (
            "cx1",
            "/work/alpha",
            "Webhook retries",
            [
                ("m1", R, T, "Add retries for flaky webhook deliveries"),
                (
                    "m2",
                    A,
                    T,
                    "Implemented exponential backoff with jitter in "
                    "webhooks/dispatcher.py: base delay 2s, factor 2, max 5 attempts. I chose jitter "
                    "to avoid a thundering herd when the receiver recovers.",
                ),
            ],
        ),
        (
            "cx2",
            "/work/alpha",
            "Type errors",
            [
                (
                    "m1",
                    R,
                    T,
                    'mypy complains: Argument 1 to "parse_invoice" has incompatible '
                    'type "Optional[str]"; expected "str"',
                ),
                (
                    "m2",
                    A,
                    T,
                    "Guard the None case before calling parse_invoice in "
                    "billing/invoices.py, or change the signature to accept Optional[str]. I added "
                    "an early return that raises a ValueError.",
                ),
            ],
        ),
        (
            "cx3",
            "/work/alpha",
            "CI cache",
            [
                ("m1", R, T, "GitHub Actions takes 14 minutes on every push, can we cut that?"),
                (
                    "m2",
                    A,
                    T,
                    "Added actions/cache for the uv cache keyed on the uv.lock hash. "
                    "Pipeline runs dropped from 14 minutes to about 4.",
                ),
            ],
        ),
        (
            "cx4",
            "/work/alpha",
            "User analytics sessions",
            [
                (
                    "m1",
                    R,
                    T,
                    "In the analytics job, define a user session as 30 minutes of inactivity",
                ),
                (
                    "m2",
                    A,
                    T,
                    "Done: sessionize() in analytics/sessions.py closes a session after "
                    "a 30 minute gap between events.",
                ),
            ],
        ),
    ],
    "opencode": [
        (
            "oc1",
            "/work/beta",
            "React rerenders",
            [
                ("m1", R, T, "The dashboard feels laggy when I type in the filter box."),
                (
                    "m2",
                    A,
                    T,
                    "Every keystroke re-rendered the whole table. I debounced the input "
                    "by 250ms, wrapped TableRow in React.memo and added useMemo for the sorted rows "
                    "in components/DataTable.tsx.",
                ),
            ],
        ),
        (
            "oc2",
            "/work/beta",
            "Bundler choice",
            [
                ("m1", R, T, "Why did we pick Vite over webpack for the frontend?"),
                (
                    "m2",
                    A,
                    T,
                    "Chose Vite because dev server cold start is under a second thanks "
                    "to native ESM, the config is tiny, and HMR is faster. The webpack.config.js "
                    "was 200 lines of loaders we would have to keep maintaining.",
                ),
            ],
        ),
        (
            "oc3",
            "/work/beta",
            "Dev server crash",
            [
                (
                    "m1",
                    R,
                    T,
                    "npm run dev crashes: Error: ENOSPC: System limit for number of "
                    "file watchers reached, watch '/home/dev/beta/src'",
                ),
                (
                    "m2",
                    A,
                    T,
                    "Raise the inotify limit: echo fs.inotify.max_user_watches=524288 | "
                    "sudo tee -a /etc/sysctl.conf, then run sudo sysctl -p.",
                ),
            ],
        ),
        (
            "oc4",
            "/work/beta",
            "CSV import",
            [
                ("m1", R, T, "parse the uploaded csv and drop duplicate rows"),
                (
                    "m2",
                    A,
                    T,
                    "Used pandas.read_csv with dtype=str then "
                    "drop_duplicates(subset=['email']) in ingest/clean_csv.py. The encoding errors "
                    "went away with encoding='utf-8-sig' because Excel adds a byte order mark.",
                ),
            ],
        ),
    ],
    "chatgpt": [
        (
            "gp1",
            None,
            "Mutex vs semaphore",
            [
                ("m1", R, T, "What's the difference between a mutex and a semaphore?"),
                (
                    "m2",
                    A,
                    T,
                    "A mutex is owned by the thread that locks it; a semaphore is a "
                    "counter that any thread can signal. Use a mutex for mutual exclusion and a "
                    "semaphore to limit concurrent access to N resources.",
                ),
            ],
        ),
        (
            "gp2",
            None,
            "Kyoto trip",
            [
                ("m1", R, T, "Planning a trip to Kyoto in November, what should I pack?"),
                (
                    "m2",
                    A,
                    T,
                    "Layers: mornings are cool, afternoons mild. Bring a light rain "
                    "jacket, comfortable walking shoes and a small umbrella.",
                ),
            ],
        ),
        (
            "gp3",
            None,
            "License choice",
            [
                ("m1", R, T, "Which license should I use for my open source CLI tool?"),
                (
                    "m2",
                    A,
                    T,
                    "I recommend MIT: it is the simplest and gives contributors zero "
                    "friction. Apache-2.0 adds a patent grant, but for a small personal CLI that is "
                    "unnecessary, so the decision is MIT.",
                ),
            ],
        ),
    ],
}


class CorpusConnector:
    """Source-agnostic stand-in connector serving one source of CORPUS."""

    schema_version_pin = "eval-corpus-v1"
    denylist_paths: tuple[str, ...] = ()
    denylist_tables: frozenset[str] = frozenset()

    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.root = root
        self.allowed_paths = [root]

    def default_source_path(self) -> Path:
        return self.root

    def check_version(self, source_path: Path) -> str:
        return "eval-corpus-1"

    def fetch(self, source_path, *, blob_store, tmp_dir, state=None):
        ts = 1_760_000_000_000 + 86_400_000 * list(CORPUS).index(self.name)
        for sid, cwd, title, messages in CORPUS[self.name]:
            yield RawSession(self.name, sid, ts, ts + 60_000, cwd, None, None, title)
            for n, (mid, role, kind, text) in enumerate(messages):
                yield RawEvent(
                    self.name,
                    f"{sid}:{mid}",
                    ts + n * 1000,
                    sid,
                    role,
                    kind,
                    None,
                    False,
                    text,
                    len(text) // 4,
                    False,
                    None,
                    None,
                    "0" * 64,
                    mid,
                )
            ts += 3_600_000


def build_eval_db(tmp_path: Path, db_file: Path | None = None):
    """Sync the corpus into a fresh DB (default: tmp_path/eval.db); return the connection."""
    root = tmp_path / "corpus_src"
    root.mkdir(parents=True)
    cfg = tmp_path / "eval_projects.toml"
    cfg.write_text(PROJECTS_TOML)
    conn = connect(db_file or tmp_path / "eval.db")
    connectors = {name: CorpusConnector(name, root) for name in CORPUS}
    sync(
        conn,
        connectors,
        project_configs=load_projects_config(cfg),
        data_dir=tmp_path / "data",
    )
    return conn
