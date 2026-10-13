# Kubernetes MCP Server

This project exposes Kubernetes cluster data through a Model Context Protocol (MCP) server. It includes generic tools for listing pods, listing nodes, reading pod logs, and identifying pods that are running Python-based workloads.

## Features

- `list_namespaces()`
- `list_pods(namespace="default")`
- `list_nodes()`
- `get_pod_logs(pod_name, namespace="default", container=None, tail_lines=200)`
- `find_pods_with_python(namespace="default")`

## Kubernetes access model

The server can authenticate in one of two ways:

1. In-cluster service account auth (default): if the container is running inside Kubernetes and the pod has a service account with the correct RBAC permissions.
2. Explicit env-based config: pass `KUBERNETES_API_SERVER`, `KUBERNETES_SERVICE_ACCOUNT_TOKEN`, and `KUBERNETES_CA_CERT_PATH` or mount a service account token file.

## Example RBAC

See [kubernetes-rbac.yaml](kubernetes-rbac.yaml) for a sample `ServiceAccount`, `ClusterRole`, and `ClusterRoleBinding` that allows read access to pods, pod logs, nodes, and namespaces.

## Environment variables

Use the variables below when the pod does not rely on the default service account token automatically mounted by Kubernetes:

```bash
export KUBERNETES_API_SERVER="https://kubernetes.default.svc"
export KUBERNETES_SERVICE_ACCOUNT_TOKEN="<bearer-token>"
export KUBERNETES_CA_CERT_PATH="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
```

You can also rely on the default in-cluster files mounted at:

```bash
/var/run/secrets/kubernetes.io/serviceaccount/token
/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
```

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m mcp_server.server
```

## Run in Docker

```bash
docker build -t kubernetes-mcp-server .
docker run --rm -it \
  -e KUBERNETES_API_SERVER="https://kubernetes.default.svc" \
  -e KUBERNETES_SERVICE_ACCOUNT_TOKEN="<token>" \
  -e KUBERNETES_CA_CERT_PATH="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt" \
  kubernetes-mcp-server
```

If the container runs inside Kubernetes with a bound service account, you can simply run:

```bash
docker run --rm -it kubernetes-mcp-server
```

## Deploy to Kubernetes

An example deployment could look like:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kubernetes-mcp-server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: kubernetes-mcp-server
  template:
    metadata:
      labels:
        app: kubernetes-mcp-server
    spec:
      serviceAccountName: mcp-k8s-reader
      containers:
        - name: mcp-server
          image: your-registry/kubernetes-mcp-server:latest
          imagePullPolicy: IfNotPresent
```

This works with the RBAC manifest in [kubernetes-rbac.yaml](kubernetes-rbac.yaml).
