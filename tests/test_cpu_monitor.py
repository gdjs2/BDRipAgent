from backend.app.cpu_monitor import CpuMonitor, read_counters, utilization


def test_utilization_uses_counter_deltas_excluding_guest_and_io_wait():
    before = read_counters("cpu 100 0 40 700 100 10 20 30 50 0\ncpu0 100 0 40 700 100 10 20 30 50 0\n")
    after = read_counters("cpu 120 0 50 750 110 10 25 35 70 0\ncpu0 120 0 50 750 110 10 25 35 70 0\n")
    assert utilization(before["cpu"], after["cpu"]) == 40.0
    assert utilization(None, after["cpu"]) is None
    assert utilization(after["cpu"], after["cpu"]) is None
    assert utilization(after["cpu"], before["cpu"]) is None


def test_sampling_handles_core_hotplug_recovery_and_never_reports_fake_zero(tmp_path):
    stat = tmp_path / "stat"
    monitor = CpuMonitor(stat)
    monitor.sample()
    assert not monitor.snapshot()["available"]
    stat.write_text("cpu 10 0 0 90\ncpu0 10 0 0 90\n")
    monitor.sample()
    assert monitor.snapshot()["percent"] is None
    stat.write_text("cpu 50 0 0 150\ncpu0 20 0 0 180\ncpu2 30 0 0 70\n")
    monitor.sample()
    sample = monitor.snapshot()
    assert sample["percent"] == 40
    assert sample["cores"] == [{"id": 0, "percent": 10}, {"id": 2, "percent": None}]
    assert sample["logical_cores"] == 2 and sample["interval_seconds"] is not None
    stat.write_text("unavailable")
    monitor.sample()
    assert not monitor.snapshot()["available"]
    stat.write_text("cpu 70 0 0 230\ncpu2 70 0 0 230\n")
    monitor.sample()
    assert monitor.snapshot()["percent"] is None
    assert monitor.snapshot()["logical_cores"] == 1


def test_cpu_endpoint_is_authenticated_and_browser_reads_do_not_change_baseline(client, tmp_path):
    from backend.app.main import app

    stat = tmp_path / "stat"
    monitor = CpuMonitor(stat)
    stat.write_text("cpu 10 0 0 90\ncpu0 10 0 0 90\n")
    monitor.sample()
    stat.write_text("cpu 50 0 0 150\ncpu0 50 0 0 150\n")
    monitor.sample()
    app.state.cpu_monitor = monitor
    first = client.get("/api/system/cpu")
    assert first.status_code == 200 and first.json()["percent"] == 40
    assert client.get("/api/system/cpu").json() == first.json()
    client.headers.clear()
    assert client.get("/api/system/cpu").status_code == 401


def test_load_and_average_frequency_are_live_independent_readings(tmp_path):
    from backend.app.cpu_monitor import read_frequency_mhz, read_load_average

    assert read_load_average("2.25 4.50 6.75 2/400 12345") == {
        "one_minute": 2.25,
        "five_minutes": 4.5,
        "fifteen_minutes": 6.75,
    }
    # The advertised model clock must never be used as a live measurement.
    info = "model name : Example CPU @ 4.0 GHz\ncpu MHz : 2100.000\ncpu MHz : 3900.000\n"
    assert read_frequency_mhz(info) == 3000
    assert read_frequency_mhz("model name : CPU @ 4.0 GHz") is None
    stat, load, cpuinfo = [tmp_path / name for name in ("stat", "loadavg", "cpuinfo")]
    monitor = CpuMonitor(stat, load, cpuinfo)
    stat.write_text("cpu 10 0 0 90\ncpu0 10 0 0 90\n")
    load.write_text("0.00 4.50 6.75 2/400 12345")
    cpuinfo.write_text(info)
    monitor.sample()
    assert monitor.snapshot()["load_average"]["one_minute"] == 0
    assert monitor.snapshot()["frequency_mhz"] == 3000
    cpuinfo.write_text("cpu MHz : 2400\n")
    load.write_text("10 8 6")
    monitor.sample()
    assert monitor.snapshot()["frequency_mhz"] == 2400
    assert monitor.snapshot()["load_average"]["one_minute"] == 10
    # A temporarily unreadable counter file does not hide independent metrics.
    stat.unlink()
    monitor.sample()
    assert not monitor.snapshot()["available"] and monitor.snapshot()["frequency_mhz"] == 2400
    # Missing or malformed readings clear stale metrics, never invent zero.
    load.unlink()
    cpuinfo.write_text("cpu MHz : nan")
    monitor.sample()
    assert monitor.snapshot()["load_average"] is None
    assert monitor.snapshot()["frequency_mhz"] is None


def test_load_and_frequency_reject_invalid_metrics():
    import pytest

    from backend.app.cpu_monitor import read_frequency_mhz, read_load_average

    for value in ("", "1 2", "1 -2 3", "NaN 1 2", "1 inf 2", "invalid 1 2"):
        with pytest.raises(ValueError):
            read_load_average(value)
    for value in ("-1", "0", "NaN", "inf", "invalid"):
        with pytest.raises(ValueError):
            read_frequency_mhz("cpu MHz : " + value)
