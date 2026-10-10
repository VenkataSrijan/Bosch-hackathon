"""Operations console: read-only API over the artifacts folder, plus quarantine acknowledgement."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from common.config import FIXTURES_DIR
from common.contracts import ARTIFACT_MODELS, Quarantine
from common.io import read_json, write_artifact, write_quarantine
from console.server import serve

EID = "exp-boutique-pay-089"


def seed(root, names=("ExperimentPlan.json",), eid=EID):
    for name in names:
        data = read_json(FIXTURES_DIR / name)
        if isinstance(data, dict):
            data["experiment_id"] = eid
        write_artifact(root / eid, ARTIFACT_MODELS[name].model_validate(data))
    return root / eid


@pytest.fixture
def server(tmp_path):
    httpd = serve(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_list_and_detail_with_partial_artifacts(server):
    root, base = server
    seed(root, ("ExperimentPlan.json", "InjectionReceipt.json"))
    code, body = call(f"{base}/api/v1/experiments")
    rows = json.loads(body)
    assert code == 200 and rows[0]["experiment_id"] == EID and rows[0]["status"] == "RUNNING"
    code, body = call(f"{base}/api/v1/experiments/{EID}")
    detail = json.loads(body)
    assert code == 200
    assert set(detail) == {"experiment_id", "ExperimentPlan", "InjectionReceipt"}  # missing ones are simply absent


def test_full_experiment_and_page(server):
    root, base = server
    seed(root, tuple(ARTIFACT_MODELS))
    detail = json.loads(call(f"{base}/api/v1/experiments/{EID}")[1])
    assert {"RootCauseAnalysis", "Recommendation", "HealthSeries", "AbortSignal"} <= set(detail)
    code, page = call(f"{base}/")
    assert code == 200 and b"Chaos Console" in page


def test_unknown_and_traversal_ids_are_404(server):
    root, base = server
    seed(root)
    assert call(f"{base}/api/v1/experiments/nope")[0] == 404
    assert call(f"{base}/api/v1/experiments/..%2F..%2Fetc")[0] == 404
    assert call(f"{base}/api/v1/experiments/..")[0] == 404


def test_ack_lifts_quarantine(server):
    root, base = server
    exp = seed(root)
    write_quarantine(exp, Quarantine(experiment_id=EID, quarantined_at="2026-10-11T00:50:38Z", reason="abort"))
    assert json.loads(call(f"{base}/api/v1/experiments")[1])[0]["quarantined"] is True
    assert "Quarantine" in json.loads(call(f"{base}/api/v1/experiments/{EID}")[1])
    code, body = call(f"{base}/api/v1/experiments/{EID}/ack", "POST")
    assert code == 200 and json.loads(body)["acknowledged"] is True
    assert not (root / "QUARANTINE.json").exists()
    assert call(f"{base}/api/v1/experiments/{EID}/ack", "POST")[0] == 404  # nothing left to acknowledge


def test_ack_for_another_experiment_is_refused(server):
    root, base = server
    seed(root)
    other = seed(root, eid="exp-boutique-cart-001")
    write_quarantine(other, Quarantine(experiment_id="exp-boutique-cart-001",
                                       quarantined_at="2026-10-11T00:50:38Z", reason="abort"))
    assert call(f"{base}/api/v1/experiments/{EID}/ack", "POST")[0] == 409
    assert (root / "QUARANTINE.json").exists()


def test_empty_artifacts_folder(tmp_path):
    httpd = serve(tmp_path / "does-not-exist", "127.0.0.1", 0)
    from console.server import Console
    assert Console(tmp_path / "does-not-exist").list_experiments() == []
    httpd.server_close()
