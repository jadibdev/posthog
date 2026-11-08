"""Print one line per benchmark cell from a results file."""

import sys
import json

results = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "results.json"))
for c in results:
    if "error" in c:
        print("ERROR", c["cell"], c["error"][-600:].replace("\n", " | "))  # noqa: T201
        continue
    if c["kind"] == "removal":
        print(  # noqa: T201
            f"removal {c['method']:10} stale={c['stale']:>7} secs={c['seconds']:>9} calls={c['calls']:>7} "
            f"ms/member={c['ms_per_member']} ok={c['removed_all']}"
        )
        continue
    ph = c["phase_seconds"]
    rss_delta = round(c["peak_rss_mb"] - c["peak_rss_mb_before"], 1)
    print(  # noqa: T201
        f"replace n={c['members']:>8} churn={c['churn']:<4} total={c['replace_seconds']:>7}s "
        f"insert={ph['insert_cohort_query_actors_into_ch']} sync={ph['insert_cohort_people_into_pg']} "
        f"pg_prune={ph['prune_members_absent_from_replace']} ch_prune={ph['prune_static_cohort_rows_before_marker']} "
        f"rss_delta={rss_delta}MB pg_exact={c['postgres_exact']} ch_exact_after={c['clickhouse_exact_after_seconds']}s "
        f"mutations={c['clickhouse_mutations']} delete_rpcs={c['calls'].get('delete_rpc', 0)} "
        f"failed={c['run_failed']} cleared={c['fields_cleared']} calculating={c['is_calculating']}"
    )
