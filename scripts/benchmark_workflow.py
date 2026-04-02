#!/usr/bin/env python3
"""ContextHub Multi-Agent Workflow Benchmark

Validates MVP correctness through two realistic multi-agent workflow scenarios:
  Suite A — Non-SQL: enterprise knowledge collaboration (promotion campaign)
  Suite B — SQL:     data-lake context management (SQL patterns + catalog)

Each suite tests: isolation, promotion, cross-agent visibility, skill versioning,
change propagation, and semantic retrieval — covering both positive ("should work")
and negative ("should NOT work") assertions.

Prerequisites:
  PostgreSQL + alembic upgrade head
  uvicorn contexthub.main:app --port 8000

Usage:
  python scripts/benchmark_workflow.py              # run both suites
  python scripts/benchmark_workflow.py --suite a    # non-SQL only
  python scripts/benchmark_workflow.py --suite b    # SQL only
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field

import httpx

BASE_URL = "http://localhost:8000"
API_KEY = "changeme"
ACCOUNT = "acme"

RUN_ID = str(int(time.time()))


def _headers(agent_id: str) -> dict:
    return {
        "X-API-Key": API_KEY,
        "X-Account-Id": ACCOUNT,
        "X-Agent-Id": agent_id,
    }


# ── Result tracking ─────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    passed: bool
    duration_ms: float
    detail: str = ""


@dataclass
class Suite:
    name: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    def report(self):
        print(f"\n{'━' * 64}")
        print(f"  {self.name}")
        print(f"  {self.passed}/{len(self.results)} passed, {self.failed} failed")
        print(f"{'━' * 64}")
        for r in self.results:
            icon = "✓" if r.passed else "✗"
            line = f"  {icon} {r.name}  ({r.duration_ms:.0f} ms)"
            if r.detail:
                line += f"  — {r.detail}"
            print(line)


async def check(suite: Suite, name: str, coro):
    """Execute an async check, record result, return (passed, return_value)."""
    t0 = time.monotonic()
    try:
        result = await coro
        ms = (time.monotonic() - t0) * 1000
        if isinstance(result, tuple):
            ok, detail = result[0], result[1]
            extra = result[2] if len(result) > 2 else None
        else:
            ok, detail, extra = result, "", None
        suite.results.append(CheckResult(name, ok, ms, detail))
        return ok, extra
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        suite.results.append(CheckResult(name, False, ms, str(exc)))
        return False, None


# ── Suite A: Non-SQL Multi-Agent Collaboration ───────────────


async def suite_a(http: httpx.AsyncClient) -> Suite:
    """Enterprise knowledge collaboration — spring promotion campaign.

    Workflow:
      ops-agent  (query-agent)     → stores promotion rules + confidential note
      analyst    (analysis-agent)  → stores user-behavior insight
      Both promote selectively to shared space; isolation + bidirectional sharing verified.
      Skill versioning with breaking change propagation tested.
    """
    s = Suite("Suite A · Non-SQL Multi-Agent Collaboration")
    qa = _headers("query-agent")
    aa = _headers("analysis-agent")
    uris: dict[str, str] = {}

    # A1 — ops stores promotion rules (will be promoted later)
    async def a1():
        r = await http.post("/api/v1/memories", json={
            "content": (
                "Spring campaign: spend 300 get 50 off, stackable with member discount, "
                "not combinable with new-user coupon. Valid Apr 1–15."
            ),
            "tags": ["promotion", "rules", f"bench-{RUN_ID}"],
        }, headers=qa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["promo"] = r.json()["uri"]
        return True, uris["promo"]

    await check(s, "A1  ops stores promotion rules", a1())

    # A2 — ops stores confidential supplier memo (MUST NOT be promoted)
    async def a2():
        r = await http.post("/api/v1/memories", json={
            "content": "Supplier floor: cost must not drop below 60% of retail. Confidential.",
            "tags": ["confidential", "supplier", f"bench-{RUN_ID}"],
        }, headers=qa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["confidential"] = r.json()["uri"]
        return True, ""

    await check(s, "A2  ops stores confidential memo", a2())

    # A3 — ops promotes promotion rules to team
    async def a3():
        r = await http.post("/api/v1/memories/promote", json={
            "uri": uris["promo"],
            "target_team": "engineering",
        }, headers=qa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["promo_team"] = r.json()["uri"]
        return True, uris["promo_team"]

    await check(s, "A3  ops promotes rules to team", a3())

    # A4 — analyst stores private insight
    async def a4():
        r = await http.post("/api/v1/memories", json={
            "content": (
                "User behavior: weekend 20:00-22:00 peak ordering, "
                "recommend push notification at 19:30 for best conversion."
            ),
            "tags": ["analytics", "user-behavior", f"bench-{RUN_ID}"],
        }, headers=aa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["insight"] = r.json()["uri"]
        return True, ""

    await check(s, "A4  analyst stores private insight", a4())

    # A5 — NEGATIVE: analyst CANNOT see ops' confidential memo
    async def a5():
        r = await http.get("/api/v1/memories", headers=aa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        for m in r.json():
            content = m.get("content", "").lower()
            if "supplier" in content or "floor" in content or "60%" in content:
                return False, "ISOLATION LEAK: analyst sees ops' confidential memo"
        return True, "no leak detected"

    await check(s, "A5  isolation — analyst cannot see confidential", a5())

    # A6 — POSITIVE: analyst CAN see promoted promotion rules
    async def a6():
        r = await http.get("/api/v1/memories", headers=aa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        shared = [m for m in r.json() if "shared_knowledge" in m.get("uri", "")]
        found = any(
            "300" in m.get("content", "") or "spring" in m.get("content", "").lower()
            for m in shared
        )
        if not found:
            return False, f"promoted rules not found (shared count={len(shared)})"
        return True, f"{len(shared)} shared memories visible"

    await check(s, "A6  sharing — analyst sees promoted rules", a6())

    # A7 — analyst promotes insight to team
    async def a7():
        r = await http.post("/api/v1/memories/promote", json={
            "uri": uris["insight"],
            "target_team": "engineering",
        }, headers=aa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["insight_team"] = r.json()["uri"]
        return True, ""

    await check(s, "A7  analyst promotes insight to team", a7())

    # A8 — BIDIRECTIONAL: ops can see analyst's promoted insight
    async def a8():
        r = await http.get("/api/v1/memories", headers=qa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        shared = [m for m in r.json() if "shared_knowledge" in m.get("uri", "")]
        found = any(
            "19:30" in m.get("content", "") or "peak" in m.get("content", "").lower()
            for m in shared
        )
        if not found:
            return False, "ops cannot see analyst's promoted insight"
        return True, ""

    await check(s, "A8  bidirectional — ops sees analyst's insight", a8())

    # A9 — NEGATIVE: confidential memo still NOT in shared space
    async def a9():
        r = await http.post("/api/v1/tools/ls", json={
            "uri": "ctx://team/engineering/shared_knowledge",
        }, headers=qa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        entries = r.json() if isinstance(r.json(), list) else r.json().get("entries", [])
        for e in entries:
            text = str(e).lower()
            if "supplier" in text or "floor" in text:
                return False, "LEAK: confidential in shared space"
        return True, "confidential not leaked"

    await check(s, "A9  selective promotion — confidential stays private", a9())

    # A10–A12: Skill versioning + propagation
    skill_uri = f"ctx://team/engineering/skills/campaign-planner-{RUN_ID}"

    async def a10():
        r = await http.post("/api/v1/contexts", json={
            "uri": skill_uri,
            "context_type": "skill",
            "scope": "team",
            "owner_space": "engineering",
            "l2_content": "Campaign planner skill for promotion scheduling",
        }, headers=qa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}: {r.text}"
        r2 = await http.post("/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v1: Basic campaign planner — dates + discount rules",
            "changelog": "Initial release",
            "is_breaking": False,
        }, headers=qa)
        if r2.status_code != 201:
            return False, f"v1 publish HTTP {r2.status_code}"
        return True, ""

    await check(s, "A10 create skill + publish v1", a10())

    async def a11():
        r = await http.post("/api/v1/skills/subscribe", json={
            "skill_uri": skill_uri,
            "pinned_version": 1,
        }, headers=aa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text}"
        return True, ""

    await check(s, "A11 analyst subscribes pinned v1", a11())

    async def a12():
        r = await http.post("/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v2: Campaign planner with A/B test groups + CTE",
            "changelog": "Breaking: new output format with test groups",
            "is_breaking": True,
        }, headers=qa)
        if r.status_code != 201:
            return False, f"v2 publish HTTP {r.status_code}"

        await asyncio.sleep(2)
        t0 = time.monotonic()
        for _ in range(30):
            r2 = await http.post("/api/v1/tools/read", json={
                "uri": skill_uri,
            }, headers=aa)
            if r2.status_code == 200:
                data = r2.json()
                if data.get("advisory"):
                    conv = (time.monotonic() - t0) * 1000
                    if data.get("version") == 1:
                        return True, f"pinned v1 + advisory, convergence {conv:.0f} ms"
                    return False, f"expected pinned v1, got v{data.get('version')}"
            await asyncio.sleep(0.2)
        return False, "advisory not received within timeout"

    await check(s, "A12 breaking v2 → pinned v1 + advisory", a12())

    s.report()
    return s


# ── Suite B: SQL / Data-Lake Context Benchmark ───────────────


async def suite_b(http: httpx.AsyncClient) -> Suite:
    """Data-lake scenario: catalog sync → SQL patterns → cross-agent retrieval.

    Tests ContextHub's value for SQL-oriented agents: catalog metadata management,
    SQL pattern sharing, schema-change propagation, and SQL-context search.
    """
    s = Suite("Suite B · SQL Data-Lake Context")
    qa = _headers("query-agent")
    aa = _headers("analysis-agent")
    uris: dict[str, str] = {}

    # B1 — catalog sync
    async def b1():
        r = await http.post("/api/v1/datalake/sync", json={"catalog": "mock"}, headers=qa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text}"
        data = r.json()
        synced = data.get("tables_synced", 0)
        if synced == 0:
            return False, "zero tables synced"
        return True, f"{synced} tables synced", synced

    ok, synced = await check(s, "B1  catalog sync (mock)", b1())

    # B2 — list tables
    async def b2():
        r = await http.get("/api/v1/datalake/mock/prod", headers=qa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        tables = r.json().get("tables", [])
        if not tables:
            return False, "no tables listed"
        return True, f"{len(tables)} tables"

    await check(s, "B2  list tables in mock/prod", b2())

    # B3 — query-agent stores SQL pattern as memory
    async def b3():
        r = await http.post("/api/v1/memories", json={
            "content": (
                "Monthly sales: SELECT date_trunc('month', o.order_date) AS month, "
                "SUM(o.amount) FROM orders o JOIN products p ON o.product_id = p.id "
                "GROUP BY 1 ORDER BY 1. Ensure index on order_date."
            ),
            "tags": ["sql", "monthly-sales", f"bench-{RUN_ID}"],
        }, headers=qa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["sql_pattern"] = r.json()["uri"]
        return True, ""

    await check(s, "B3  query-agent stores SQL pattern", b3())

    # B4 — promote SQL pattern
    async def b4():
        r = await http.post("/api/v1/memories/promote", json={
            "uri": uris["sql_pattern"],
            "target_team": "engineering",
        }, headers=qa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["sql_pattern_team"] = r.json()["uri"]
        return True, ""

    await check(s, "B4  promote SQL pattern to team", b4())

    # B5 — analyst sees promoted SQL pattern (cross-agent data-lake knowledge)
    async def b5():
        r = await http.get("/api/v1/memories", headers=aa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        shared = [m for m in r.json() if "shared_knowledge" in m.get("uri", "")]
        found = any(
            "order_date" in m.get("content", "") or "monthly" in m.get("content", "").lower()
            for m in shared
        )
        if not found:
            return False, "analyst cannot see promoted SQL pattern"
        return True, ""

    await check(s, "B5  analyst sees promoted SQL pattern", b5())

    # B6 — sql-context search (retrieval combines catalog metadata + memories)
    async def b6():
        r = await http.post("/api/v1/search/sql-context", json={
            "query": "How many orders per user per month?",
            "catalog": "mock",
            "top_k": 3,
        }, headers=qa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        total = data.get("total_tables_found", 0)
        if total == 0:
            return False, "no relevant tables found"
        return True, f"{total} tables found"

    await check(s, "B6  sql-context retrieval", b6())

    # B7–B9: SQL generator skill versioning
    skill_uri = f"ctx://team/engineering/skills/sql-gen-{RUN_ID}"

    async def b7():
        r = await http.post("/api/v1/contexts", json={
            "uri": skill_uri,
            "context_type": "skill",
            "scope": "team",
            "owner_space": "engineering",
            "l2_content": "SQL generator for e-commerce analytics queries",
        }, headers=qa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}: {r.text}"
        r2 = await http.post("/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v1: SELECT with JOIN for orders/products, GROUP BY month",
            "changelog": "Initial release",
            "is_breaking": False,
        }, headers=qa)
        if r2.status_code != 201:
            return False, f"v1 publish HTTP {r2.status_code}"
        return True, ""

    await check(s, "B7  create SQL generator skill + v1", b7())

    async def b8():
        r = await http.post("/api/v1/skills/subscribe", json={
            "skill_uri": skill_uri,
            "pinned_version": 1,
        }, headers=aa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text}"
        return True, ""

    await check(s, "B8  analyst subscribes pinned v1", b8())

    async def b9():
        r = await http.post("/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v2: CTE-based SQL generator with window functions",
            "changelog": "Breaking: switched from flat JOIN to CTE pattern",
            "is_breaking": True,
        }, headers=qa)
        if r.status_code != 201:
            return False, f"v2 publish HTTP {r.status_code}"

        await asyncio.sleep(2)
        t0 = time.monotonic()
        for _ in range(30):
            r2 = await http.post("/api/v1/tools/read", json={
                "uri": skill_uri,
            }, headers=aa)
            if r2.status_code == 200:
                data = r2.json()
                if data.get("advisory"):
                    conv = (time.monotonic() - t0) * 1000
                    if data.get("version") == 1:
                        return True, f"pinned v1 + advisory, convergence {conv:.0f} ms"
                    return False, f"expected v1, got v{data.get('version')}"
            await asyncio.sleep(0.2)
        return False, "advisory timeout"

    await check(s, "B9  breaking v2 → pinned v1 + advisory", b9())

    # B10 — unified search (memories + catalog metadata in one query)
    async def b10():
        r = await http.post("/api/v1/search", json={
            "query": "monthly sales orders products join",
            "top_k": 5,
        }, headers=aa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        if not items:
            return False, "no search results"
        return True, f"{len(items)} results"

    await check(s, "B10 unified search (memories + metadata)", b10())

    s.report()
    return s


# ── Entrypoint ───────────────────────────────────────────────


async def _ensure_team_membership():
    """Seed: make query-agent a direct member of engineering team."""
    try:
        import asyncpg
    except ImportError:
        print("  (asyncpg not installed — skipping membership seed)")
        return
    try:
        conn = await asyncpg.connect(
            "postgresql://contexthub:contexthub@localhost:5432/contexthub"
        )
        try:
            await conn.execute("SET app.account_id = 'acme'")
            await conn.execute("""
                INSERT INTO team_memberships
                    (agent_id, team_id, role, access, is_primary)
                VALUES
                    ('query-agent',
                     '00000000-0000-0000-0000-000000000002',
                     'member', 'read_write', FALSE)
                ON CONFLICT DO NOTHING
            """)
        finally:
            await conn.close()
    except Exception as exc:
        print(f"  Warning: membership seed failed ({exc})")


async def main():
    suite_filter = "all"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--suite" and i < len(sys.argv) - 1:
            suite_filter = sys.argv[i + 1].lower()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as http:
        r = await http.get("/health")
        if r.status_code != 200:
            print(f"ContextHub server not reachable at {BASE_URL}")
            sys.exit(1)
        print(f"Server healthy.  Run ID: {RUN_ID}")

        await _ensure_team_membership()

        suites: list[Suite] = []
        if suite_filter in ("a", "all"):
            suites.append(await suite_a(http))
        if suite_filter in ("b", "all"):
            suites.append(await suite_b(http))

        total_pass = sum(s.passed for s in suites)
        total_fail = sum(s.failed for s in suites)
        total = total_pass + total_fail

        print(f"\n{'═' * 64}")
        print(f"  BENCHMARK SUMMARY   (run {RUN_ID})")
        print(f"  {total_pass}/{total} checks passed, {total_fail} failed")
        print(f"{'─' * 64}")
        for suite in suites:
            icon = "✓" if suite.failed == 0 else "✗"
            print(f"  {icon} {suite.name}: {suite.passed}/{len(suite.results)}")
        print(f"{'═' * 64}")

        sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
