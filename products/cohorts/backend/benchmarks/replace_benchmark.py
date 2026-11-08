"""Benchmark for replacing the members of a query-populated static cohort (issue #121486).

Not part of CI, and not part of the replace PR: it lives on a linked branch with its results.

It measures two things:

- ``removal``: removing the same stale members from Postgres two ways, through the existing per-person
  delete in a loop (the baseline, which never ships) and through the bulk ``DeleteCohortMembersByPersonIds``
  RPC, one call per page.
- ``replace``: a full replace population end to end (mint, quorum insert, sync, Postgres prune, ClickHouse
  prune), at several cohort sizes and churn rates, checked against the bar in the engineering plan.

It needs the dev stack, a real personhog replica and router built from this branch, and these env vars:

    PERSONHOG_ADDR=127.0.0.1:50052  DEBUG=1  CLICKHOUSE_DATABASE=default
    PERSONS_DATABASE_URL=postgres://posthog:posthog@localhost:5432/posthog_persons

Usage:

    python "replace_benchmark.py" matrix --out results.json      # every cell, one subprocess each
    python "replace_benchmark.py" cell replace 10000 0.05         # one cell, prints a JSON line
    python "replace_benchmark.py" cell removal 1000 per_person
    python "replace_benchmark.py" repeats --n 3 --out results_repeats.json   # key cells, interleaved rounds

Production differs in ways the results must state: ClickHouse here is one node, so the insert runs
with insert_quorum=1 where production waits for a majority, and every personhog call is local, where
production crosses the network. The settle wait is forced to its production value of one second.
"""

from __future__ import annotations

import os
import sys
import json
import time
import resource
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from unittest.mock import patch

REPLACE_SIZES = (10_000, 100_000, 1_000_000)
REPLACE_CHURNS = (0.0, 0.05, 0.5, 1.0)
REMOVAL_SIZES = (100, 1_000, 10_000, 100_000)
REMOVAL_METHODS = ("per_person", "bulk")
CONSISTENCY_DEADLINE_SECONDS = 900
UTIL = "products.cohorts.backend.models.util"


def _setup_django() -> None:
    sys.path.insert(0, os.getcwd())
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "posthog.settings")
    import django

    django.setup()


def _persons_db():
    import psycopg

    return psycopg.connect(
        os.environ.get("PERSONS_DATABASE_URL", "postgres://posthog:posthog@localhost:5432/posthog_persons"),
        autocommit=True,
    )


@contextmanager
def _tagged(query_type: str) -> Iterator[None]:
    from posthog.clickhouse.query_tagging import Feature, tags_context
    from posthog.schema_enums import ProductKey

    with tags_context(product=ProductKey.COHORTS, feature=Feature.COHORT, query_type=query_type):
        yield


def _ch(sql: str, params: dict[str, Any] | None = None, query_type: str = "bench") -> Any:
    from posthog.clickhouse.client import sync_execute

    with _tagged(query_type):
        return sync_execute(sql, params or {})


# Deterministic person uuids, built the same way in both stores: the team in the first group, the
# person's index in the last, so a cell can address its persons without moving ids between stores.
PG_UUID = "(lpad(to_hex(%(team)s::int), 8, '0') || '-0000-4000-8000-' || lpad(to_hex(i::bigint), 12, '0'))::uuid"
# ClickHouse's hex() drops leading zero bytes, so both parts are padded explicitly.
CH_UUID = (
    "toUUID(concat(leftPad(lower(hex(toUInt32(%(team)s))), 8, '0'), '-0000-4000-8000-', "
    "leftPad(lower(hex(toUInt64(number))), 12, '0')))"
)


def _new_team_and_cohort(label: str):
    from posthog.models import Organization
    from posthog.models.project import Project

    from products.cohorts.backend.models.cohort import Cohort

    org = Organization.objects.create(name=f"replace benchmark {label}")
    _, team = Project.objects.create_with_team(organization=org, initiating_user=None, name=label)
    cohort = Cohort.objects.create(
        team=team,
        name=f"bench {label}",
        is_static=True,
        query={"kind": "HogQLQuery", "query": "SELECT id FROM persons WHERE properties.bench_in = '1'"},
    )
    return team, cohort


def _seed_postgres(team_id: int, cohort_id: int, total: int, churned: int, members: int) -> None:
    """Persons 0..total-1, matching the query from index ``churned`` on; members are persons 0..members-1."""
    with _persons_db() as conn:
        conn.execute(
            f"""
            INSERT INTO posthog_person (created_at, properties, uuid, version, team_id)
            SELECT now(), jsonb_build_object('bench_i', i, 'bench_in', CASE WHEN i >= %(churned)s THEN '1' ELSE '0' END),
                   {PG_UUID}, 1, %(team)s
            FROM generate_series(0, %(total)s - 1) AS i
            """,
            {"team": team_id, "total": total, "churned": churned},
        )
        conn.execute(
            """
            INSERT INTO posthog_cohortpeople (cohort_id, person_id)
            SELECT %(cohort)s, id FROM posthog_person
            WHERE team_id = %(team)s AND (properties->>'bench_i')::int < %(members)s
            """,
            {"cohort": cohort_id, "team": team_id, "members": members},
        )


def _seed_clickhouse(team_id: int, cohort_id: int, total: int, churned: int, members: int) -> None:
    _ch(
        f"""
        INSERT INTO person (id, created_at, team_id, properties, is_identified, is_deleted, version)
        SELECT {CH_UUID}, now(), %(team)s,
               concat('{{"bench_i":', toString(number), ',"bench_in":"', if(number >= %(churned)s, '1', '0'), '"}}'),
               0, 0, 1
        FROM numbers(%(total)s)
        """,
        {"team": team_id, "total": total, "churned": churned},
        "bench_seed_person",
    )
    # Existing members, stamped an hour ago, as an earlier append population would have left them.
    _ch(
        f"""
        INSERT INTO person_static_cohort (id, person_id, cohort_id, team_id, _timestamp, _offset)
        SELECT generateUUIDv4(), {CH_UUID}, %(cohort)s, %(team)s, now() - INTERVAL 1 HOUR, 0
        FROM numbers(%(members)s)
        """,
        {"team": team_id, "cohort": cohort_id, "members": members},
        "bench_seed_static_cohort",
    )


def _postgres_membership(team_id: int, cohort_id: int) -> tuple[int, int]:
    """Members in Postgres, and how many of them the query matches."""
    with _persons_db() as conn:
        row = conn.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE p.properties->>'bench_in' = '1')
            FROM posthog_cohortpeople cp JOIN posthog_person p ON p.id = cp.person_id AND p.team_id = %(team)s
            WHERE cp.cohort_id = %(cohort)s
            """,
            {"team": team_id, "cohort": cohort_id},
        ).fetchone()
    return int(row[0]), int(row[1])


def _cleanup_postgres(team_id: int, cohort_id: int) -> None:
    with _persons_db() as conn:
        conn.execute("DELETE FROM posthog_cohortpeople WHERE cohort_id = %(cohort)s", {"cohort": cohort_id})
        conn.execute("DELETE FROM posthog_person WHERE team_id = %(team)s", {"team": team_id})


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class _Recorder:
    """Wraps functions the replace calls, to time phases and count calls without changing behavior."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = defaultdict(float)
        self.calls: dict[str, int] = defaultdict(int)
        self.items: dict[str, int] = defaultdict(int)

    def timed(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.seconds[name] += time.perf_counter() - started
                self.calls[name] += 1

        return wrapper

    def counted(self, name: str, fn: Callable[..., Any], size: Callable[..., int]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            self.items[name] += size(*args, **kwargs)
            return fn(*args, **kwargs)

        return wrapper


def _clickhouse_work(started_at: float, finished_at: float) -> dict[str, Any]:
    """Rows read and written, and query time, for this cell's queries, from the query log."""
    _ch("SYSTEM FLUSH LOGS", query_type="bench_flush_logs")
    rows = _ch(
        """
        SELECT query_kind, count(), sum(read_rows), sum(written_rows), sum(query_duration_ms)
        FROM system.query_log
        WHERE type = 'QueryFinish' AND event_time >= toDateTime(%(started)s) AND event_time <= toDateTime(%(finished)s)
          AND query NOT LIKE '%%system.%%'
        GROUP BY query_kind
        """,
        {"started": int(started_at), "finished": int(finished_at) + 1},
        "bench_query_log",
    )
    return {kind: {"queries": q, "read_rows": r, "written_rows": w, "ms": ms} for kind, q, r, w, ms in rows}


def _mutations(cohort_id: int, started_at: float) -> int:
    rows = _ch(
        """
        SELECT count() FROM system.mutations
        WHERE table = 'person_static_cohort' AND database = currentDatabase()
          AND create_time >= toDateTime(%(started)s) AND command LIKE %(pattern)s
        """,
        {"started": int(started_at), "pattern": f"%cohort_id = {cohort_id}%"},
        "bench_mutations",
    )
    return int(rows[0][0])


def _seconds_until_clickhouse_exact(
    team_id: int, cohort_id: int, expected: int, task_finished_at: float
) -> float | None:
    """Poll until the cohort's rows are exactly the expected members: no stale rows and no duplicates."""
    while True:
        rows = _ch(
            "SELECT count(), uniqExact(person_id) FROM person_static_cohort WHERE team_id = %(team)s AND cohort_id = %(cohort)s",
            {"team": team_id, "cohort": cohort_id},
            "bench_consistency_poll",
        )
        if int(rows[0][0]) == expected and int(rows[0][1]) == expected:
            return round(time.monotonic() - task_finished_at, 1)
        if time.monotonic() - task_finished_at > CONSISTENCY_DEADLINE_SECONDS:
            return None
        time.sleep(2)


def run_replace_cell(members: int, churn: float) -> dict[str, Any]:
    from posthog.tasks.calculate_cohort import insert_cohort_from_query

    from products.cohorts.backend.models import util
    from products.cohorts.backend.models.calculation_history import CohortCalculationHistory
    from products.cohorts.backend.models.cohort import Cohort, CohortPopulationMode

    churned = int(members * churn)
    total = members + churned
    team, cohort = _new_team_and_cohort(f"replace-{members}-{churn}")

    seed_started = time.perf_counter()
    _seed_postgres(team.id, cohort.id, total, churned, members)
    _seed_clickhouse(team.id, cohort.id, total, churned, members)
    seed_seconds = time.perf_counter() - seed_started
    rss_before = _peak_rss_mb()

    # What the API claim would write for a replace.
    Cohort.objects.filter(pk=cohort.pk).update(
        is_calculating=True, pending_population_mode=CohortPopulationMode.REPLACE
    )

    recorder = _Recorder()
    phases = (
        "mint_population_marker",
        "insert_cohort_query_actors_into_ch",
        "insert_cohort_people_into_pg",
        "prune_members_absent_from_replace",
        "prune_static_cohort_rows_before_marker",
    )
    with ExitStack() as stack:
        for name in phases:
            stack.enter_context(patch(f"{UTIL}.{name}", recorder.timed(name, getattr(util, name))))
        stack.enter_context(
            patch(
                f"{UTIL}.delete_cohort_members_by_person_ids",
                recorder.counted(
                    "delete_rpc", util.delete_cohort_members_by_person_ids, lambda team_id, cohort_id, ids: len(ids)
                ),
            )
        )
        stack.enter_context(
            patch(
                f"{UTIL}._resolve_person_uuids",
                recorder.counted("resolve_pages", util._resolve_person_uuids, lambda team_id, ids: len(ids)),
            )
        )
        stack.enter_context(patch(f"{UTIL}.REPLACE_PRUNE_SETTLE_SECONDS", 1))
        real_sleep = time.sleep
        stack.enter_context(
            patch(
                f"{UTIL}.time.sleep", recorder.counted("settle_sleep", real_sleep, lambda seconds: int(seconds * 1000))
            )
        )

        started_wall = time.time()
        started = time.perf_counter()
        insert_cohort_from_query(cohort.id, team.id)
        replace_seconds = time.perf_counter() - started
        finished_wall = time.time()
        finished_mono = time.monotonic()

    cohort.refresh_from_db()
    history = CohortCalculationHistory.objects.filter(cohort=cohort).order_by("-started_at").first()
    pg_total, pg_matching = _postgres_membership(team.id, cohort.id)
    consistency_seconds = _seconds_until_clickhouse_exact(team.id, cohort.id, members, finished_mono)
    result = {
        "kind": "replace",
        "members": members,
        "churn": churn,
        "stale": churned,
        "added": churned,
        "seed_seconds": round(seed_seconds, 1),
        "replace_seconds": round(replace_seconds, 1),
        "phase_seconds": {name: round(recorder.seconds[name], 2) for name in phases},
        "calls": dict(recorder.calls),
        "items": dict(recorder.items),
        "peak_rss_mb_before": round(rss_before, 1),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "postgres_exact": pg_total == members and pg_matching == members,
        "postgres_members": pg_total,
        "clickhouse_exact_after_seconds": consistency_seconds,
        "clickhouse_mutations": _mutations(cohort.id, started_wall),
        "clickhouse_work": _clickhouse_work(started_wall, finished_wall),
        "run_failed": bool(history and history.error_code),
        "fields_cleared": cohort.pending_population_mode is None and cohort.population_marker is None,
        "is_calculating": cohort.is_calculating,
    }
    _cleanup_postgres(team.id, cohort.id)
    return result


def run_removal_cell(stale: int, method: str) -> dict[str, Any]:
    from products.cohorts.backend.models.util import (
        PRUNE_PAGE_SIZE,
        delete_cohort_member,
        delete_cohort_members_by_person_ids,
    )

    team, cohort = _new_team_and_cohort(f"removal-{stale}-{method}")
    _seed_postgres(team.id, cohort.id, stale, 0, stale)
    with _persons_db() as conn:
        person_ids = [
            row[0]
            for row in conn.execute(
                "SELECT person_id FROM posthog_cohortpeople WHERE cohort_id = %(cohort)s ORDER BY person_id",
                {"cohort": cohort.id},
            )
        ]

    calls = 0
    started = time.perf_counter()
    if method == "per_person":
        for person_id in person_ids:
            delete_cohort_member(team.id, cohort.id, person_id)
            calls += 1
    else:
        for start in range(0, len(person_ids), PRUNE_PAGE_SIZE):
            delete_cohort_members_by_person_ids(team.id, cohort.id, person_ids[start : start + PRUNE_PAGE_SIZE])
            calls += 1
    seconds = time.perf_counter() - started

    remaining, _ = _postgres_membership(team.id, cohort.id)
    _cleanup_postgres(team.id, cohort.id)
    return {
        "kind": "removal",
        "method": method,
        "stale": stale,
        "seconds": round(seconds, 3),
        "calls": calls,
        "ms_per_member": round(seconds * 1000 / stale, 4),
        "removed_all": remaining == 0,
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


def _run_cell_subprocess(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "cell", *args], capture_output=True, text=True, env=os.environ
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if completed.returncode != 0 or not lines:
        return {"cell": args, "error": (completed.stderr or completed.stdout)[-2000:]}
    return json.loads(lines[-1])


def run_matrix(out_path: str, kinds: set[str]) -> None:
    cells: list[list[str]] = []
    if "removal" in kinds:
        cells += [["removal", str(size), method] for size in REMOVAL_SIZES for method in REMOVAL_METHODS]
    if "replace" in kinds:
        cells += [["replace", str(size), str(churn)] for size in REPLACE_SIZES for churn in REPLACE_CHURNS]
    results = []
    for cell in cells:
        print(f"running {' '.join(cell)}", file=sys.stderr, flush=True)  # noqa: T201
        results.append(_run_cell_subprocess(cell))
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)


# The cells the argument rests on, repeated to measure run-to-run variance.
REPEAT_CELLS = (
    ["replace", "1000000", "0.0"],
    ["replace", "1000000", "1.0"],
    ["removal", "100000", "per_person"],
    ["removal", "100000", "bulk"],
    ["replace", "10000", "0.05"],
)


def run_repeats(out_path: str, repeats: int) -> None:
    # Interleaved, one round of every cell before the next, so slow drift in the machine spreads across
    # cells rather than landing on one.
    results = []
    for round_index in range(repeats):
        for cell in REPEAT_CELLS:
            print(f"round {round_index + 1}: {' '.join(cell)}", file=sys.stderr, flush=True)  # noqa: T201
            result = _run_cell_subprocess(cell)
            result["round"] = round_index + 1
            results.append(result)
            with open(out_path, "w") as f:
                json.dump(results, f, indent=2)


def main() -> None:
    command = sys.argv[1]
    if command == "repeats":
        out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "results_repeats.json"
        run_repeats(out_path, int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 3)
        return
    if command == "matrix":
        out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "results.json"
        kinds = (
            set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else {"removal", "replace"}
        )
        run_matrix(out_path, kinds)
        return
    _setup_django()
    kind, size, arg = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    result = run_replace_cell(size, float(arg)) if kind == "replace" else run_removal_cell(size, arg)
    print(json.dumps(result))  # noqa: T201


if __name__ == "__main__":
    main()
