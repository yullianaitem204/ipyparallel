import asyncio
import os
import signal
import sys
import time

import pytest

import ipyparallel as ipp
from .clienttest import raises_remote
from ipyparallel import cluster
from ipyparallel.cluster.launcher import find_launcher_class

_timeout = 30


def _raise_interrupt(*frame_info):
    raise KeyboardInterrupt()


def _prepare_signal():
    """Register signal handler to test with

    Registers SIGUSR1 to raise KeyboardInterrupt where available
    """
    if hasattr(signal, 'SIGUSR1'):
        signal.signal(signal.SIGUSR1, _raise_interrupt)
    # returns the remote value of the signal
    return int(TEST_SIGNAL)


try:
    TEST_SIGNAL = signal.SIGUSR1
except AttributeError:
    # Windows
    TEST_SIGNAL = signal.CTRL_C_EVENT


async def test_cluster_id(Cluster):
    cluster_ids = set()
    for i in range(3):
        cluster = Cluster()
        cluster_ids.add(cluster.cluster_id)
    assert len(cluster_ids) == 3
    cluster = Cluster(cluster_id='abc')
    assert cluster.cluster_id == 'abc'


async def test_ipython_log(ipython):
    c = cluster.Cluster(parent=ipython)
    assert c.log.name == f"{cluster.Cluster.__module__}.{c.cluster_id}"
    assert len(c.log.handlers) == 1
    assert c.log.handlers[0].stream is sys.stdout


@pytest.fixture
def engine_launcher_class():
    return 'Local'


async def test_start_stop_controller(Cluster):
    cluster = Cluster()
    await cluster.start_controller()
    with pytest.raises(RuntimeError):
        await cluster.start_controller()
    assert cluster.config is not None
    assert cluster._controller.config is cluster.config
    assert cluster._controller is not None
    proc = cluster._controller.process
    assert proc.is_running()
    with await cluster.connect_client() as rc:
        assert rc.queue_status() == {'unassigned': 0}

    await cluster.stop_controller()
    proc.wait(timeout=3)
    assert cluster._controller is None
    # stop is idempotent
    await cluster.stop_controller()
    # TODO: test file cleanup


async def test_start_stop_engines(Cluster, engine_launcher_class):
    cluster = Cluster(engine_launcher_class=engine_launcher_class)
    await cluster.start_controller()

    n = 2
    engine_set_id = await cluster.start_engines(n)
    assert engine_set_id in cluster._engine_sets
    engine_set = cluster._engine_sets[engine_set_id]
    launcher_class = find_launcher_class(engine_launcher_class, "EngineSet")
    assert isinstance(engine_set, launcher_class)

    with await cluster.connect_client() as rc:
        rc.wait_for_engines(n, timeout=_timeout)

    await cluster.stop_engines(engine_set_id)
    assert cluster._engine_sets == {}
    with pytest.raises(KeyError):
        await cluster.stop_engines(engine_set_id)

    await cluster.stop_controller()


async def test_start_stop_cluster(Cluster, engine_launcher_class):
    n = 2
    cluster = Cluster(engine_launcher_class=engine_launcher_class, n=n)
    await cluster.start_cluster()
    controller = cluster._controller
    assert controller is not None
    assert len(cluster._engine_sets) == 1

    with await cluster.connect_client() as rc:
        rc.wait_for_engines(n, timeout=_timeout)
    await cluster.stop_cluster()
    assert cluster._controller is None
    assert cluster._engine_sets == {}


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Signal tests don't pass on Windows yet"
)
async def test_signal_engines(request, Cluster, engine_launcher_class):
    cluster = Cluster(engine_launcher_class=engine_launcher_class)
    await cluster.start_controller()
    engine_set_id = await cluster.start_engines(n=2)
    rc = await cluster.connect_client()
    request.addfinalizer(rc.close)
    rc.wait_for_engines(2)
    # seems to be a problem if we start too soon...
    await asyncio.sleep(1)
    # ensure responsive
    rc[:].apply_async(lambda: None).get(timeout=_timeout)
    # register signal handler
    signals = rc[:].apply_async(_prepare_signal).get(timeout=_timeout)
    # get test signal from engines in case of cross-platform mismatch,
    # e.g. SIGUSR1 on mac (30) -> linux (10)
    test_signal = signals[0]
    # submit request to be interrupted
    ar = rc[:].apply_async(time.sleep, 3)
    # wait for it to be running
    await asyncio.sleep(0.5)
    # send signal
    await cluster.signal_engines(test_signal, engine_set_id)

    # wait for result, which should raise KeyboardInterrupt
    with raises_remote(KeyboardInterrupt) as e:
        ar.get(timeout=_timeout)
    rc.close()

    await cluster.stop_engines()
    await cluster.stop_controller()


async def test_restart_engines(Cluster, engine_launcher_class):
    n = 2
    async with Cluster(engine_launcher_class=engine_launcher_class, n=n) as rc:
        cluster = rc.cluster
        engine_set_id = next(iter(cluster._engine_sets))
        engine_set = cluster._engine_sets[engine_set_id]
        assert rc.ids[:n] == list(range(n))
        before_pids = rc[:].apply_sync(os.getpid)
        await cluster.restart_engines()
        # wait for unregister
        while any(eid in rc.ids for eid in range(n)):
            await asyncio.sleep(0.1)
        # wait for register
        rc.wait_for_engines(n, timeout=_timeout)
        after_pids = rc[:].apply_sync(os.getpid)
        assert set(after_pids).intersection(before_pids) == set()


async def test_async_with(Cluster):
    async with Cluster(n=5) as rc:
        assert sorted(rc.ids) == list(range(5))
        rc[:]['a'] = 5
        assert rc[:]['a'] == [5] * 5


def test_sync_with(Cluster):
    with Cluster(log_level=10, n=5) as rc:
        assert sorted(rc.ids) == list(range(5))
        rc[:]['a'] = 5
        assert rc[:]['a'] == [5] * 5


@pytest.mark.parametrize(
    "classname, expected_class",
    [
        ("MPI", cluster.launcher.MPIEngineSetLauncher),
        ("SGE", cluster.launcher.SGEEngineSetLauncher),
        (
            "ipyparallel.cluster.launcher.LocalEngineSetLauncher",
            cluster.launcher.LocalEngineSetLauncher,
        ),
    ],
)
def test_cluster_abbreviations(classname, expected_class):
    c = cluster.Cluster(engine_launcher_class=classname)
    assert c.engine_launcher_class is expected_class


async def test_cluster_repr(Cluster):
    c = Cluster(cluster_id="test", profile_dir='/tmp')
    assert repr(c) == "<Cluster(cluster_id='test', profile_dir='/tmp')>"
    await c.start_controller()
    assert (
        repr(c)
        == "<Cluster(cluster_id='test', profile_dir='/tmp', controller=<running>)>"
    )
    await c.start_engines(1, 'engineid')
    assert (
        repr(c)
        == "<Cluster(cluster_id='test', profile_dir='/tmp', controller=<running>, engine_sets=['engineid'])>"
    )


async def test_cluster_manager():
    m = cluster.ClusterManager()
    assert m.list_clusters() == []
    c = m.new_cluster(profile_dir="/tmp")
    assert c.profile_dir == "/tmp"
    assert m.get_cluster(c.cluster_id) is c
    with pytest.raises(KeyError):
        m.get_cluster("nosuchcluster")

    with pytest.raises(KeyError):
        m.new_cluster(cluster_id=c.cluster_id)

    assert m.list_clusters() == [c.cluster_id]
    m.remove_cluster(c.cluster_id)
    assert m.list_clusters() == []
    with pytest.raises(KeyError):
        m.remove_cluster("nosuchcluster")


async def test_to_from_dict(Cluster, engine_launcher_class):
    cluster = Cluster(engine_launcher_class=engine_launcher_class, n=2)
    print(cluster.config, cluster.controller_args)
    async with cluster as rc:
        d = cluster.to_dict()
        cluster2 = ipp.Cluster.from_dict(d)
        assert not cluster2.shutdown_atexit
        assert cluster2._controller is not None
        assert cluster2._controller.process.pid == cluster._controller.process.pid
        assert list(cluster2._engine_sets) == list(cluster._engine_sets)

        es1 = next(iter(cluster._engine_sets.values()))
        es2 = next(iter(cluster2._engine_sets.values()))
        # ensure responsive
        rc[:].apply_async(lambda: None).get(timeout=_timeout)
        if not sys.platform.startswith("win"):
            # signal tests doesn't work yet on Windows
            # register signal handler
            signals = rc[:].apply_async(_prepare_signal).get(timeout=_timeout)
            # get test signal from engines in case of cross-platform mismatch,
            # e.g. SIGUSR1 on mac (30) -> linux (10)
            test_signal = signals[0]
            # submit request to be interrupted
            ar = rc[:].apply_async(time.sleep, 3)
            await asyncio.sleep(0.5)
            # send signal
            await cluster2.signal_engines(test_signal)

            # wait for result, which should raise KeyboardInterrupt
            with raises_remote(KeyboardInterrupt) as e:
                ar.get(timeout=_timeout)
        assert es1.n == es2.n
        assert cluster2.engine_launcher_class is cluster.engine_launcher_class

        # shutdown from cluster2, shouldn't raise in cluster1
        await cluster2.stop_cluster()


async def test_default_from_file(Cluster):
    cluster = Cluster(n=1, profile="default", cluster_id="")
    async with cluster:
        cluster2 = ipp.Cluster.from_file()
        assert cluster2.cluster_file == cluster.cluster_file
        with await cluster.connect_client() as rc:
            assert len(rc) == 1
