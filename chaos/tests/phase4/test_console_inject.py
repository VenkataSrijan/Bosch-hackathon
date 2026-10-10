"""Console fault injection: options, validation, a real mock run, emergency stop, and cross-site refusal."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from common.io import read_artifact, write_quarantine
from common.contracts import Quarantine
from console.runner import RunManager
from console.server import serve



def call(url, method="GET", headers=None, body=None):
    if method == "POST" and headers is None:
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


@pytest.fixture
def mock_console(tmp_path):
    runner = RunManager(tmp_path, mock=True)
    httpd = serve(tmp_path, "127.0.0.1", 0, mock=True, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}", runner
    if runner.run and runner.run.process.poll() is None:
        runner.run.process.kill()
    httpd.shutdown()
    httpd.server_close()


def test_options_list_faults_and_eligible_targets(mock_console):
    _, base, _ = mock_console
    code, body = call(f"{base}/api/v1/inject")
    opts = json.loads(body)
    assert code == 200 and opts["mode"] == "mock" and opts["problems"] == []
    assert [f["id"] for f in opts["faults"]] == ["latency", "loss", "pod-kill"]
    targets = {t["service"]: t for t in opts["targets"]}
    assert targets["paymentservice"]["eligible"] is True
    assert targets["checkoutservice"]["eligible"] is False and "--replicas=2" in targets["checkoutservice"]["reason"]


@pytest.mark.parametrize("request_body,error", [
    ({"target": "paymentservice", "fault": "cpu"}, "fault must be one of"),
    ({"target": "checkoutservice", "fault": "latency"}, "needs 2 replicas"),
    ({"target": "nope", "fault": "latency"}, "unknown target"),
    ({"target": "paymentservice", "fault": "loss", "loss_percent": 150}, "loss_percent must be between"),
    ({"target": "paymentservice", "fault": "latency", "duration": 5}, "duration must be between"),
])
def test_bad_requests_are_refused(mock_console, request_body, error):
    _, base, _ = mock_console
    code, body = call(f"{base}/api/v1/runs", "POST", body=request_body)
    assert code == 400 and error in json.loads(body)["error"]


def test_cross_site_posts_are_refused(mock_console):
    _, base, _ = mock_console
    req = {"target": "paymentservice", "fault": "latency"}
    assert call(f"{base}/api/v1/runs", "POST", headers={"Content-Type": "text/plain"}, body=req)[0] == 403
    evil = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert call(f"{base}/api/v1/runs", "POST", headers=evil, body=req)[0] == 403


def test_quarantine_blocks_a_run(mock_console):
    root, base, _ = mock_console
    write_quarantine(root / "exp-boutique-pay-089", Quarantine(experiment_id="exp-boutique-pay-089",
                                                              quarantined_at="2026-10-11T00:50:38Z", reason="abort"))
    code, body = call(f"{base}/api/v1/runs", "POST", body={"target": "paymentservice", "fault": "latency"})
    assert code == 409 and "acknowledge" in json.loads(body)["error"]


def test_mock_run_end_to_end(mock_console):
    root, base, runner = mock_console
    code, body = call(f"{base}/api/v1/runs", "POST",
                      body={"target": "paymentservice", "fault": "loss", "loss_percent": 40, "duration": 30})
    assert code == 202, body
    assert json.loads(body)["request"] == {"target": "paymentservice", "fault": "loss", "loss_percent": 40, "duration": 30}
    second = call(f"{base}/api/v1/runs", "POST", body={"target": "paymentservice", "fault": "latency"})
    assert second[0] == 409  # one run at a time
    assert runner.wait(300) == 0
    run = json.loads(call(f"{base}/api/v1/runs/current")[1])["run"]
    assert run["state"] == "finished" and run["experiment_id"]
    plan = read_artifact(root / run["experiment_id"], "ExperimentPlan.json")
    assert plan.fault_profile.action == "loss" and plan.fault_profile.parameters["loss_percent"] == 40
    assert read_artifact(root / run["experiment_id"], "Recommendation.json") is not None
    listed = json.loads(call(f"{base}/api/v1/experiments")[1])
    assert run["experiment_id"] in {e["experiment_id"] for e in listed}


def test_emergency_stop(mock_console):
    _, base, runner = mock_console
    assert call(f"{base}/api/v1/runs", "POST", body={"target": "paymentservice", "fault": "latency"})[0] == 202
    code, body = call(f"{base}/api/v1/runs/current/stop", "POST")
    stopped = json.loads(body)
    assert code == 200 and stopped["state"] == "stopped" and stopped["stop_note"].startswith("stopped")
    assert call(f"{base}/api/v1/runs/current/stop", "POST")[0] == 404


def test_live_stop_deletes_the_experiments_chaos(tmp_path):
    calls = []
    runner = RunManager(tmp_path, mock=False, kubectl=lambda args: calls.append(args) or (0, "deleted"),
                        reachable=lambda url: None)

    class Proc:
        pid = 0
        def poll(self):
            return None if not calls else 1
        def wait(self, timeout=None):
            return 1
        def kill(self):
            pass

    from console.runner import Run
    log = tmp_path / "run.log"
    log.write_text("[phase1] ...\nartifacts/exp-boutique-payment-123\n", encoding="utf-8")
    runner.run = Run(run_id="r", request={}, log_path=log, process=Proc())
    import console.runner as mod
    mod_subprocess_run = mod.subprocess.run
    mod.subprocess.run = lambda *a, **k: None  # no taskkill in a unit test
    try:
        if mod.os.name != "nt":
            mod.os.killpg = lambda *a: None
        result = runner.stop()
    finally:
        mod.subprocess.run = mod_subprocess_run
    assert calls[-1] == ["delete", "networkchaos,podchaos,stresschaos", "-n", "default", "-l",
                         "chaos.ai/experiment-id=exp-boutique-payment-123", "--now", "--ignore-not-found"]
    assert "exp-boutique-payment-123" in result["stop_note"]
