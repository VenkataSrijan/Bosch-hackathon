#!/usr/bin/env bash
# Local cluster for the chaos platform:
#   kind (default) or minikube, Istio (sidecar injection on "default", 100% trace sampling),
#   Prometheus + Jaeger addons, Online Boutique from kubernetes-manifests/, Chaos Mesh,
#   paymentservice scaled to 2 replicas.
#
# Usage (from anywhere):  bash chaos/scripts/setup_cluster.sh
# Needs: docker, kubectl, helm, curl, and kind or minikube.
#
# Env overrides:
#   CLUSTER_TOOL=kind|minikube   CLUSTER_NAME=chaos   ISTIO_VERSION=1.24.2
#   CHAOS_MESH_VERSION=2.7.2     BOUTIQUE_IMAGE_REPO=us-central1-docker.pkg.dev/google-samples/microservices-demo
#   BOUTIQUE_IMAGE_TAG=v0.10.4   (prebuilt images; kubernetes-manifests/ itself has bare image names for skaffold)
set -euo pipefail

CLUSTER_TOOL="${CLUSTER_TOOL:-kind}"
CLUSTER_NAME="${CLUSTER_NAME:-chaos}"
ISTIO_VERSION="${ISTIO_VERSION:-1.24.2}"
CHAOS_MESH_VERSION="${CHAOS_MESH_VERSION:-2.7.2}"
BOUTIQUE_IMAGE_REPO="${BOUTIQUE_IMAGE_REPO:-us-central1-docker.pkg.dev/google-samples/microservices-demo}"
BOUTIQUE_IMAGE_TAG="${BOUTIQUE_IMAGE_TAG:-v0.10.4}"
NAMESPACE="default"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOLS_DIR="${REPO_ROOT}/chaos/.tools"
mkdir -p "${TOOLS_DIR}"

log() { printf '\n==> %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 1; }; }

need docker; need kubectl; need helm; need curl; need "${CLUSTER_TOOL}"

# ------------------------------------------------------------------ 1. cluster
log "Cluster (${CLUSTER_TOOL}/${CLUSTER_NAME})"
if [[ "${CLUSTER_TOOL}" == "kind" ]]; then
  if ! kind get clusters | grep -qx "${CLUSTER_NAME}"; then
    cat <<EOF | kind create cluster --name "${CLUSTER_NAME}" --config -
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
  fi
  kubectl config use-context "kind-${CLUSTER_NAME}"
  CHAOS_RUNTIME="containerd"
  CHAOS_SOCKET="/run/containerd/containerd.sock"
elif [[ "${CLUSTER_TOOL}" == "minikube" ]]; then
  minikube status -p "${CLUSTER_NAME}" >/dev/null 2>&1 \
    || minikube start -p "${CLUSTER_NAME}" --cpus=4 --memory=8g --container-runtime=containerd
  kubectl config use-context "${CLUSTER_NAME}"
  CHAOS_RUNTIME="containerd"
  CHAOS_SOCKET="/run/containerd/containerd.sock"
else
  echo "CLUSTER_TOOL must be kind or minikube" >&2; exit 1
fi

# ------------------------------------------------------------------ 2. Istio
log "Istio ${ISTIO_VERSION}"
ISTIO_DIR="${TOOLS_DIR}/istio-${ISTIO_VERSION}"
if [[ ! -x "${ISTIO_DIR}/bin/istioctl" && ! -x "${ISTIO_DIR}/bin/istioctl.exe" ]]; then
  (cd "${TOOLS_DIR}" && curl -sL https://istio.io/downloadIstio | ISTIO_VERSION="${ISTIO_VERSION}" sh -)
fi
ISTIOCTL="${ISTIO_DIR}/bin/istioctl"
[[ -x "${ISTIOCTL}" ]] || ISTIOCTL="${ISTIOCTL}.exe"

"${ISTIOCTL}" install -y --set profile=demo \
  --set meshConfig.enableTracing=true \
  --set meshConfig.defaultConfig.tracing.sampling=100.0 \
  --set meshConfig.extensionProviders[0].name=zipkin \
  --set meshConfig.extensionProviders[0].zipkin.service=zipkin.istio-system.svc.cluster.local \
  --set meshConfig.extensionProviders[0].zipkin.port=9411

kubectl apply -f - <<EOF
apiVersion: telemetry.istio.io/v1
kind: Telemetry
metadata:
  name: mesh-default
  namespace: istio-system
spec:
  tracing:
    - providers:
        - name: zipkin
      randomSamplingPercentage: 100.0
EOF

log "Prometheus and Jaeger addons"
kubectl apply -f "${ISTIO_DIR}/samples/addons/prometheus.yaml"
kubectl apply -f "${ISTIO_DIR}/samples/addons/jaeger.yaml"
kubectl -n istio-system rollout status deployment/prometheus --timeout=300s
kubectl -n istio-system rollout status deployment/jaeger --timeout=300s

# ------------------------------------------------------------------ 3. Online Boutique
log "Online Boutique in namespace ${NAMESPACE} (sidecar injection on)"
kubectl label namespace "${NAMESPACE}" istio-injection=enabled --overwrite
SERVICES="adservice|cartservice|checkoutservice|currencyservice|emailservice|frontend|loadgenerator|paymentservice|productcatalogservice|recommendationservice|shippingservice"
# kustomization.yaml leaves out loadgenerator (skaffold deploys it separately); add it so traffic flows.
{ kubectl kustomize "${REPO_ROOT}/kubernetes-manifests"; echo "---"; cat "${REPO_ROOT}/kubernetes-manifests/loadgenerator.yaml"; } \
  | sed -E "s#^([[:space:]]*image: )(${SERVICES})[[:space:]]*\$#\1${BOUTIQUE_IMAGE_REPO}/\2:${BOUTIQUE_IMAGE_TAG}#" \
  | kubectl apply -n "${NAMESPACE}" -f -

log "paymentservice -> 2 replicas"
kubectl -n "${NAMESPACE}" scale deployment paymentservice --replicas=2

for d in $(kubectl -n "${NAMESPACE}" get deploy -o name); do
  kubectl -n "${NAMESPACE}" rollout status "${d}" --timeout=600s
done

# ------------------------------------------------------------------ 4. Chaos Mesh
log "Chaos Mesh ${CHAOS_MESH_VERSION} (runtime ${CHAOS_RUNTIME}, socket ${CHAOS_SOCKET})"
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update chaos-mesh >/dev/null
kubectl create namespace chaos-mesh --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --version "${CHAOS_MESH_VERSION}" \
  --set chaosDaemon.runtime="${CHAOS_RUNTIME}" \
  --set chaosDaemon.socketPath="${CHAOS_SOCKET}" \
  --set dashboard.securityMode=false \
  --wait --timeout 10m

# ------------------------------------------------------------------ done
log "Ready"
kubectl -n "${NAMESPACE}" get pods -o wide
cat <<EOF

Port-forward telemetry in separate terminals, then export the URLs for the phases:
  kubectl -n istio-system port-forward svc/prometheus 9090:9090
  kubectl -n istio-system port-forward svc/tracing 16686:80
  export PROMETHEUS_URL=http://localhost:9090 JAEGER_URL=http://localhost:16686

Shop front end:
  kubectl -n ${NAMESPACE} port-forward svc/frontend 8081:80   # http://localhost:8081
EOF
