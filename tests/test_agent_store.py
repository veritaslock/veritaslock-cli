"""The agent table's store functions (vl-agent-cli-spec.md §3, §6)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from _helpers import add_service_account
from vl.lib import store


@pytest.fixture
def acct() -> store.Identity:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "2026-01-01T00:00:00Z")
    store.upsert_organization("local", "acme", "org-2", "Acme", True, "2026-01-01T00:00:00Z")
    return add_service_account("globo-agent", "globo", role="NODE_AGENT")


def test_add_agent_creates_a_new_row_with_no_pid(acct: store.Identity) -> None:
    agent = store.add_agent("local", "globo", acct.id)
    assert (agent.environment_name, agent.org_name, agent.svc_acct_id) == ("local", "globo", acct.id)
    assert agent.state == "NEW"
    assert agent.pid is None
    assert store.get_agent("local", "globo") == agent


def test_one_agent_per_org_and_environment(acct: store.Identity) -> None:
    store.add_agent("local", "globo", acct.id)
    with pytest.raises(store.AgentExistsError, match="already has an agent"):
        store.add_agent("local", "globo", acct.id)
    other = store.add_agent("local", "acme", acct.id)
    assert [a.org_name for a in store.list_agents("local")] == ["acme", "globo"]
    assert other.org_name == "acme"


def test_get_agent_unknown_org_points_at_create(acct: store.Identity) -> None:
    with pytest.raises(store.AgentNotFoundError, match="vl agent create globo"):
        store.get_agent("local", "globo")


def test_set_agent_pid_then_stopped(acct: store.Identity) -> None:
    agent = store.add_agent("local", "globo", acct.id)
    running = store.set_agent_pid(agent, 4242)
    assert (running.state, running.pid) == ("RUNNING", 4242)
    assert store.get_agent("local", "globo") == running
    stopped = store.set_agent_stopped(running)
    assert (stopped.state, stopped.pid) == ("STOPPED", None)
    assert store.get_agent("local", "globo") == stopped


def test_set_agent_pid_on_a_deleted_row_says_so(acct: store.Identity, isolated_store: Path) -> None:
    agent = store.add_agent("local", "globo", acct.id)
    with sqlite3.connect(isolated_store) as conn:
        conn.execute("DELETE FROM agent")
    with pytest.raises(store.AgentNotFoundError):
        store.set_agent_pid(agent, 1)


def test_add_agent_takes_the_write_lock_before_it_reads(acct: store.Identity, isolated_store: Path) -> None:
    """Another writer inserts the row while add_agent waits: BEGIN IMMEDIATE makes it
    read after that writer commits and refuse cleanly, never an IntegrityError."""
    holding = threading.Event()

    def other_writer() -> None:
        conn = sqlite3.connect(isolated_store, timeout=10, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        holding.set()
        time.sleep(0.4)
        conn.execute(
            "INSERT INTO agent (environment_name, org_name, svc_acct_id, state, created_at) "
            "VALUES ('local', 'globo', ?, 'NEW', 'x')", (acct.id,))
        conn.execute("COMMIT")
        conn.close()

    t = threading.Thread(target=other_writer)
    store.get_environment("local")  # the store exists, migrated and in WAL, before the race
    t.start()
    holding.wait(5)
    with pytest.raises(store.AgentExistsError):
        store.add_agent("local", "globo", acct.id)
    t.join()


def test_the_list_repair_never_clobbers_a_start_that_came_in_between() -> None:
    store.ensure_local_environment_seeded()
    store.upsert_organization("local", "globo", "org-1", "Globo", True, "t")
    sa = store.add_identity("local", "SERVICE_ACCOUNT", "sa-a", "globo-agent", "globo-agent")
    store.set_svc_acct(sa.id, "local", "globo", "client-a", "secret", role="NODE_AGENT")
    seen = store.set_agent_pid(store.add_agent("local", "globo", sa.id), 1111)  # read by `list`: pid 1111 dead
    store.set_agent_pid(seen, 2222)  # meanwhile `vl agent start` recorded a new one
    after = store.set_agent_stopped_if_unchanged(seen)
    assert (after.state, after.pid) == ("RUNNING", 2222)
    unchanged = store.get_agent("local", "globo")
    assert store.set_agent_stopped_if_unchanged(unchanged).state == "STOPPED"
