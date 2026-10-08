from storage_validator.backends.lustre import health
from storage_validator.models import Pool, Target, Topology


def make_topology(**overrides) -> Topology:
    defaults = dict(
        fsname="scratch",
        mdts=[Target(name="scratch-MDT0000", kind="mdt", uuid="mdt0_uuid", state="UP")],
        osts=[
            Target(name="scratch-OST0000", kind="ost", uuid="ost0_uuid", state="UP"),
            Target(name="scratch-OST0001", kind="ost", uuid="ost1_uuid", state="IN"),
        ],
        pools=[Pool(name="flash", osts=["ost0_uuid"])],
        mounts=["/mnt/scratch"],
        df={
            "ost0_uuid": {"use_pct": 50},
            "ost1_uuid": {"use_pct": 97},
        },
    )
    defaults.update(overrides)
    return Topology(**defaults)


def test_check_target_states():
    results = health.check_target_states(make_topology())
    by_target = {r.target: r for r in results}
    assert by_target["scratch-MDT0000"].status == "PASS"
    assert by_target["scratch-OST0000"].status == "PASS"
    assert by_target["scratch-OST0001"].status == "FAIL"


def test_check_usage_thresholds():
    results = health.check_usage_thresholds(make_topology())
    by_target = {r.target: r for r in results}
    assert by_target["scratch-OST0000"].status == "PASS"
    assert by_target["scratch-OST0001"].status == "FAIL"


def test_check_usage_thresholds_warn_band():
    topo = make_topology(df={"ost0_uuid": {"use_pct": 85}})
    results = health.check_usage_thresholds(topo)
    assert results[0].status == "WARN"


def test_check_pool_membership_unknown_ost():
    topo = make_topology(pools=[Pool(name="flash", osts=["ost0_uuid", "missing_uuid"])])
    results = health.check_pool_membership(topo)
    assert results[0].status == "WARN"
    assert "missing_uuid" in results[0].details["unknown_osts"]


def test_check_pool_membership_ok():
    results = health.check_pool_membership(make_topology())
    assert results[0].status == "PASS"


def test_check_client_mounts_empty():
    topo = make_topology(mounts=[])
    results = health.check_client_mounts(topo)
    assert results[0].status == "WARN"


def test_check_client_mounts_present():
    results = health.check_client_mounts(make_topology())
    assert results[0].status == "PASS"


def test_run_health_checks_aggregates_all():
    results = health.run_health_checks(make_topology())
    names = {r.name for r in results}
    assert names == {
        "target_state",
        "usage_threshold",
        "pool_membership",
        "client_mount",
    }
