import json
import os
import sys
import logging
from typing import Any, Optional

# Configure logging from environment variable LOGGING
# Accepted values (case-insensitive): DEBUG, INFO, WARN, WARNING, ERROR
log_level_name = (os.getenv("LOGGING") or "INFO").strip().upper()
if log_level_name == "WARN":
    log_level_name = "WARNING"
_log_level = getattr(logging, log_level_name, None)
if not isinstance(_log_level, int):
    _log_level = logging.INFO

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
# Apply level to common libraries used by this app to make their logs respect LOGGING
for _logger in ("kubernetes", "mcp", "urllib3", "requests"):
    logging.getLogger(_logger).setLevel(_log_level)

logger = logging.getLogger(__name__)
logger.debug("Logging initialized at level %s", logging.getLevelName(_log_level))

from kubernetes import client, config

try:
    # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:
    # mcp 2.x renamed FastMCP to MCPServer — provide a compatibility alias
    from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP("kubernetes-tools")
logger.info("MCP server wrapper initialized using %s", type(mcp).__name__)


def _read_file_if_exists(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
            return value or None
    except OSError:
        return None


def _get_k8s_api_client() -> client.ApiClient:
    api_server = os.getenv("KUBERNETES_API_SERVER")
    token = os.getenv("KUBERNETES_SERVICE_ACCOUNT_TOKEN") or _read_file_if_exists(
        os.getenv("KUBERNETES_SERVICE_ACCOUNT_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token")
    )
    ca_cert = os.getenv("KUBERNETES_CA_CERT_PATH") or os.getenv(
        "KUBERNETES_SERVICE_ACCOUNT_CA_FILE",
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    )

    if api_server and token:
        logger.debug("Creating ApiClient using explicit API server %s", api_server)
        configuration = client.Configuration()
        configuration.host = api_server.rstrip("/")
        configuration.api_key_prefix["authorization"] = "Bearer"
        configuration.api_key["authorization"] = token
        configuration.verify_ssl = True
        if ca_cert and os.path.exists(ca_cert):
            configuration.ssl_ca_cert = ca_cert
        else:
            configuration.verify_ssl = False
        return client.ApiClient(configuration=configuration)

    try:
        logger.debug("Attempting in-cluster Kubernetes configuration")
        config.load_incluster_config()
        return client.ApiClient()
    except config.ConfigException:
        logger.debug("Falling back to kubeconfig file")
        config.load_kube_config()
        return client.ApiClient()


def _v1_api() -> client.CoreV1Api:
    return client.CoreV1Api(api_client=_get_k8s_api_client())


@mcp.tool()
def list_namespaces() -> str:
    """Return a JSON list of available Kubernetes namespaces."""
    v1 = _v1_api()
    namespaces = v1.list_namespace()
    data = [{
        "name": item.metadata.name,
        "status": item.status.phase if item.status and item.status.phase else "Unknown",
    } for item in (namespaces.items or [])]
    return json.dumps(data, indent=2)


@mcp.tool()
def list_pods(namespace: str = "default") -> str:
    """Return a JSON list of pods in the specified namespace."""
    v1 = _v1_api()
    pod_list = v1.list_namespaced_pod(namespace=namespace)
    data = [{
        "name": item.metadata.name,
        "status": item.status.phase if item.status and item.status.phase else "Unknown",
        "node": item.spec.node_name if item.spec else None,
        "containers": [container.name for container in (item.spec.containers if item.spec else [])],
    } for item in (pod_list.items or [])]
    return json.dumps(data, indent=2)


@mcp.tool()
def list_nodes() -> str:
    """Return a JSON list of cluster nodes and their readiness states."""
    v1 = _v1_api()
    nodes = v1.list_node()
    data = []
    for item in nodes.items or []:
        conditions = {}
        for condition in (item.status.conditions or []):
            conditions[condition.type] = condition.status
        data.append({
            "name": item.metadata.name,
            "status": item.status.phase if hasattr(item.status, "phase") and item.status.phase else "Unknown",
            "conditions": conditions,
            "roles": list(item.metadata.labels.get("kubernetes.io/role", "").split(",")) if item.metadata.labels else [],
        })
    return json.dumps(data, indent=2)


@mcp.tool()
def get_pod_logs(pod_name: str, namespace: str = "default", container: Optional[str] = None, tail_lines: int = 200) -> str:
    """Return the log stream from a pod, optionally filtered to a specific container."""
    if tail_lines <= 0:
        tail_lines = 200

    v1 = _v1_api()
    if container:
        logs = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, container=container, tail_lines=tail_lines)
    else:
        logs = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=tail_lines)
    return logs or "<no logs available>"


@mcp.tool()
def find_pods_with_python(namespace: str = "default") -> str:
    """Identify pod names whose container image or command reference Python."""
    v1 = _v1_api()
    pods = v1.list_namespaced_pod(namespace=namespace)
    matches = []
    for item in pods.items or []:
        if not item.spec:
            continue
        for container in item.spec.containers:
            haystack = " ".join(filter(None, [
                container.image,
                " ".join(container.command or []),
                " ".join(container.args or []),
            ])).lower()
            if "python" in haystack:
                matches.append({
                    "name": item.metadata.name,
                    "container": container.name,
                    "image": container.image,
                })
                break
    return json.dumps({
        "namespace": namespace,
        "count": len(matches),
        "pods": matches,
    }, indent=2)


def main():
    """Start the MCP server runtime with compatibility wrappers that keep the
    process alive when run() returns immediately. This extracts the previous
    if __name__ == '__main__' logic into a callable function so other entry
    points (for example, python -m mcp_server) can use the same startup
    behavior and avoid exiting immediately.
    """
    import asyncio
    import inspect
    import time
    import signal

    logger.info("Starting MCP tool runtime (compat-run wrapper)")

    # Enable faulthandler to dump tracebacks for low-level crashes
    try:
        import faulthandler

        faulthandler.enable(file=sys.stdout)
        logger.debug("faulthandler enabled")
    except Exception:
        logger.debug("faulthandler not available")

    # Install a global excepthook that logs uncaught exceptions and prints traceback
    def _excepthook(exc_type, exc_value, exc_tb):
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        import traceback as _tb

        _tb.print_exception(exc_type, exc_value, exc_tb)
        # exit with non-zero so Kubernetes sees the failure
        try:
            sys.exit(1)
        except SystemExit:
            pass

    sys.excepthook = _excepthook

    # Signal handler to log term/interrupt and dump tracebacks
    def _on_signal(signum, frame):
        logger.info("Received signal %s, shutting down", signum)
        try:
            import faulthandler as _fh

            _fh.dump_traceback(file=sys.stdout)
        except Exception:
            logger.debug("faulthandler.dump_traceback failed")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Introspect mcp object to help diagnose API differences
    try:
        logger.debug("mcp type: %s", type(mcp).__name__)
        public_attrs = [n for n in dir(mcp) if not n.startswith("_")]
        logger.debug("mcp public attributes: %s", public_attrs)
    except Exception:
        logger.debug("Failed to introspect mcp object")

    run_callable = getattr(mcp, "run", None)
    if run_callable is None or not callable(run_callable):
        logger.error("mcp has no callable run() method (found: %r); cannot start server", run_callable)
        # keep container alive for inspection
        while True:
            time.sleep(60)

    try:
        # If run is an async function
        if asyncio.iscoroutinefunction(run_callable):
            logger.debug("mcp.run is a coroutine function; calling asyncio.run(mcp.run())")
            asyncio.run(run_callable())
        else:
            logger.debug("Calling mcp.run() (synchronous call)")
            result = run_callable()
            logger.debug("mcp.run() returned: %r", result)

            # If it returned an awaitable, run it
            if inspect.isawaitable(result):
                logger.debug("mcp.run() returned awaitable; running it with asyncio.run()")
                asyncio.run(result)
            # server-like object with blocking method?
            elif hasattr(result, "serve_forever") and callable(result.serve_forever):
                logger.debug("mcp.run() returned object with serve_forever(); calling it")
                result.serve_forever()
            elif hasattr(result, "run_forever") and callable(result.run_forever):
                logger.debug("mcp.run() returned object with run_forever(); calling it")
                result.run_forever()
            else:
                # If result is None (common when run() registers services and returns), keep process alive
                logger.warning(
                    "mcp.run() returned without blocking (type=%s). Entering sleep loop to keep the process running for inspection.",
                    type(result).__name__,
                )
                while True:
                    time.sleep(60)

    except Exception:
        logger.exception("Exception while starting MCP server")
        raise


if __name__ == "__main__":
    main()
