"""Integration tests for OpenSandboxBackend against a real OpenSandbox server.

Due to SDK compatibility issues with K8s deployments (metadata=null parsing bug),
sandbox lifecycle (create/delete) uses REST API directly, while command execution
and file operations go through the SDK via OpenSandboxBackend.connect().

Prerequisites:
    - OpenSandbox server running (default: http://localhost:8080)
    - Port forwarding:
        kubectl port-forward svc/opensandbox-server 8080:80 -n opensandbox-system

Usage:
    python -m pytest tests/test_integration.py -v

Environment variables:
    OPEN_SANDBOX_DOMAIN   - Server address (default: localhost:8080)
    OPEN_SANDBOX_PROTOCOL - http or https (default: http)
    OPEN_SANDBOX_IMAGE    - Sandbox image (see default below)
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error

import pytest

from langchain_opensandbox.sandbox import OpenSandboxBackend

# -- Configuration -------------------------------------------------------------

DEFAULT_IMAGE = os.environ.get(
    "OPEN_SANDBOX_IMAGE",
    "ezone.kingsoft.com/ksyun/ai-app-docker/release/python:3.12-bookworm-slim-uv0.8-patched",
)
DOMAIN = os.environ.get("OPEN_SANDBOX_DOMAIN", "localhost:8080")
PROTOCOL = os.environ.get("OPEN_SANDBOX_PROTOCOL", "http")
BASE_URL = f"{PROTOCOL}://{DOMAIN}"


# -- REST API helpers (bypass SDK lifecycle bug) --------------------------------

def _api_request(method: str, path: str, body: dict | None = None) -> dict | None:
    """Make a REST API call to OpenSandbox server."""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=30)
    if resp.status == 204:
        return None
    return json.loads(resp.read())


def _create_sandbox() -> str:
    """Create a sandbox via REST API, wait until Running, return sandbox_id."""
    resp = _api_request("POST", "/sandboxes", {
        "image": {"uri": DEFAULT_IMAGE},
        "timeout": 300,
        "resourceLimits": {"cpu": "0.5", "memory": "512Mi"},
        "entrypoint": ["tail", "-f", "/dev/null"],
    })
    sandbox_id = resp["id"]

    # Poll until Running
    for _ in range(60):
        info = _api_request("GET", f"/sandboxes/{sandbox_id}")
        state = info.get("status", {}).get("state", "")
        if state == "Running":
            return sandbox_id
        time.sleep(1)

    raise TimeoutError(f"Sandbox {sandbox_id} did not reach Running state")


def _delete_sandbox(sandbox_id: str) -> None:
    """Delete a sandbox via REST API."""
    try:
        url = f"{BASE_URL}/sandboxes/{sandbox_id}"
        req = urllib.request.Request(url, method="DELETE")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # best-effort cleanup


def _server_healthy() -> bool:
    try:
        resp = urllib.request.urlopen(f"{BASE_URL}/health", timeout=3)
        return resp.status == 200
    except Exception:
        return False


# Skip all tests if server is not available
pytestmark = pytest.mark.skipif(
    not _server_healthy(),
    reason=f"OpenSandbox server not reachable at {BASE_URL}",
)


# -- Fixtures ------------------------------------------------------------------

@pytest.fixture(scope="module")
def sandbox_id():
    """Create a sandbox via REST API, yield its ID, cleanup on teardown."""
    sid = _create_sandbox()
    yield sid
    _delete_sandbox(sid)


@pytest.fixture(scope="module")
def backend(sandbox_id):
    """Connect to the sandbox via SDK and yield an OpenSandboxBackend."""
    b = OpenSandboxBackend.connect(
        sandbox_id,
        domain=DOMAIN,
        protocol=PROTOCOL,
        use_server_proxy=True,
        working_directory="/tmp",
    )

    # Wait for execd to be ready
    for _ in range(30):
        try:
            result = b.execute("echo ready", timeout=10)
            if result.exit_code == 0 and "ready" in result.output:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        b.close()
        pytest.fail("Sandbox execd did not become ready within 30 seconds")

    yield b
    b.close()


# -- Lifecycle -----------------------------------------------------------------

class TestLifecycle:

    def test_sandbox_has_id(self, backend: OpenSandboxBackend):
        assert backend.id is not None
        assert len(backend.id) > 0

    def test_connect_by_id(self, backend: OpenSandboxBackend):
        """Verify we can create a second connection to the same sandbox."""
        connected = OpenSandboxBackend.connect(
            backend.id, domain=DOMAIN, protocol=PROTOCOL,
            use_server_proxy=True,
        )
        result = connected.execute("echo connected")
        assert result.exit_code == 0
        assert "connected" in result.output
        connected.close()


# -- Command Execution ---------------------------------------------------------

class TestExecute:

    def test_echo(self, backend):
        result = backend.execute("echo hello world")
        assert result.exit_code == 0
        assert "hello world" in result.output

    def test_exit_code_success(self, backend):
        result = backend.execute("true")
        assert result.exit_code == 0

    def test_exit_code_failure(self, backend):
        result = backend.execute("false")
        assert result.exit_code != 0

    def test_stderr_captured(self, backend):
        result = backend.execute("echo error_msg >&2")
        assert "error_msg" in result.output

    def test_multiline_output(self, backend):
        result = backend.execute("echo line1 && echo line2 && echo line3")
        assert "line1" in result.output
        assert "line2" in result.output
        assert "line3" in result.output

    def test_python_execution(self, backend):
        result = backend.execute("python3 -c \"print(sum(range(101)))\"")
        assert result.exit_code == 0
        assert "5050" in result.output

    def test_environment_variables(self, backend):
        result = backend.execute("export MY_VAR=test123 && echo $MY_VAR")
        assert "test123" in result.output

    def test_working_directory(self, backend):
        result = backend.execute("pwd")
        assert result.exit_code == 0
        assert "/tmp" in result.output

    def test_nonexistent_command(self, backend):
        result = backend.execute("this_command_does_not_exist_xyz")
        assert result.exit_code != 0

    def test_timeout_respected(self, backend):
        result = backend.execute("echo fast", timeout=30)
        assert result.exit_code == 0
        assert "fast" in result.output


# -- File Upload ---------------------------------------------------------------

class TestUploadFiles:

    def test_upload_single_file(self, backend):
        responses = backend.upload_files([
            ("/tmp/test_upload.txt", b"hello from integration test"),
        ])
        assert len(responses) == 1
        assert responses[0].error is None

        result = backend.execute("cat /tmp/test_upload.txt")
        assert result.exit_code == 0
        assert "hello from integration test" in result.output

    def test_upload_multiple_files(self, backend):
        responses = backend.upload_files([
            ("/tmp/multi_a.txt", b"content_a"),
            ("/tmp/multi_b.txt", b"content_b"),
        ])
        assert len(responses) == 2
        assert all(r.error is None for r in responses)

        result = backend.execute("cat /tmp/multi_a.txt /tmp/multi_b.txt")
        assert "content_a" in result.output
        assert "content_b" in result.output

    def test_upload_binary_file(self, backend):
        binary_data = bytes(range(256))
        responses = backend.upload_files([("/tmp/binary_test.bin", binary_data)])
        assert responses[0].error is None

        result = backend.execute("wc -c < /tmp/binary_test.bin")
        assert "256" in result.output

    def test_upload_and_run_python(self, backend):
        script = b"""\
import json
data = {"status": "ok", "value": 42}
print(json.dumps(data))
"""
        responses = backend.upload_files([("/tmp/test_script.py", script)])
        assert responses[0].error is None

        result = backend.execute("python3 /tmp/test_script.py")
        assert result.exit_code == 0
        assert '"status": "ok"' in result.output


# -- File Download -------------------------------------------------------------

class TestDownloadFiles:

    def test_download_existing_file(self, backend):
        backend.execute("echo 'download test content' > /tmp/test_download.txt")

        responses = backend.download_files(["/tmp/test_download.txt"])
        assert len(responses) == 1
        assert responses[0].error is None
        assert responses[0].content is not None
        assert b"download test content" in responses[0].content

    def test_download_nonexistent_file(self, backend):
        responses = backend.download_files(["/tmp/nonexistent_file_xyz.txt"])
        assert len(responses) == 1
        assert responses[0].error is not None
        assert responses[0].content is None

    def test_download_multiple_files(self, backend):
        backend.execute("echo aaa > /tmp/dl_a.txt && echo bbb > /tmp/dl_b.txt")

        responses = backend.download_files(["/tmp/dl_a.txt", "/tmp/dl_b.txt"])
        assert len(responses) == 2
        assert all(r.error is None for r in responses)
        assert b"aaa" in responses[0].content
        assert b"bbb" in responses[1].content

    def test_roundtrip(self, backend):
        """Upload then download, verify content matches."""
        original = b"roundtrip test data: \x00\xff\n"
        backend.upload_files([("/tmp/roundtrip.bin", original)])

        responses = backend.download_files(["/tmp/roundtrip.bin"])
        assert responses[0].error is None
        assert responses[0].content == original


# -- End-to-end workflow -------------------------------------------------------

class TestWorkflow:

    def test_agent_workflow(self, backend):
        """Simulate a real agent workflow: upload code → run → download result."""
        # 1. Upload
        script = b"""\
import json
result = {"fibonacci": [1, 1, 2, 3, 5, 8, 13, 21]}
with open("/tmp/result.json", "w") as f:
    json.dump(result, f)
print("done")
"""
        backend.upload_files([("/tmp/workflow_script.py", script)])

        # 2. Execute
        result = backend.execute("python3 /tmp/workflow_script.py")
        assert result.exit_code == 0
        assert "done" in result.output

        # 3. Download
        responses = backend.download_files(["/tmp/result.json"])
        assert responses[0].error is None
        data = json.loads(responses[0].content)
        assert data["fibonacci"] == [1, 1, 2, 3, 5, 8, 13, 21]
