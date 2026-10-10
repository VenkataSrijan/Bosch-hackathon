#!/usr/bin/env bash
# Local cluster for the chaos platform:
#   kind (default) or minikube, Istio (sidecar injection on "default", 100% trace sampling),
#   Prometheus + Jaeger addons, Online Boutique from kubernetes-manifests/, Chaos Mesh,
#   the services in REPLICATED_SERVICES scaled to 2 replicas (so they can be fault targets).
#
# Usage (from anywhere):  bash chaos/scripts/setup_cluster.sh
# Needs: docker, kubectl, curl, unzip or tar, and kind or minikube. helm and istioctl are downloaded
# into chaos/.tools when missing. Works from Linux, macOS and Git Bash on Windows, and is safe to re-run
# on an existing cluster (CLUSTER_NAME=<name> reuses it).
#
# Env overrides:
#   CLUSTER_TOOL=kind|minikube   CLUSTER_NAME=chaos   ISTIO_VERSION=1.24.2
#   CHAOS_MESH_VERSION=2.7.2     BOUTIQUE_IMAGE_REPO=us-central1-docker.pkg.dev/google-samples/microservices-demo
#   BOUTIQUE_IMAGE_TAG=v0.10.4   (prebuilt images; kubernetes-manifests/ itself has bare image names for skaffold)
#   HELM_VERSION=v3.16.4         REPLICATED_SERVICES="paymentservice checkoutservice ..."
set -euo pipefail

CLUSTER_TOOL="${CLUSTER_TOOL:-kind}"
CLUSTER_NAME="${CLUSTER_NAME:-chaos}"
ISTIO_VERSION="${ISTIO_VERSION:-1.24.2}"
CHAOS_MESH_VERSION="${CHAOS_MESH_VERSION:-2.7.2}"
BOUTIQUE_IMAGE_REPO="${BOUTIQUE_IMAGE_REPO:-us-central1-docker.pkg.dev/google-samples/microservices-demo}"
BOUTIQUE_IMAGE_TAG="${BOUTIQUE_IMAGE_TAG:-v0.10.4}"
HELM_VERSION="${HELM_VERSION:-v3.16.4}"
REPLICATED_SERVICES="${REPLICATED_SERVICES:-paymentservice checkoutservice currencyservice productcatalogservice shippingservice}"
NAMESPACE="default"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOLS_DIR="${REPO_ROOT}/chaos/.tools"
mkdir -p "${TOOLS_DIR}"

log() { printf '\n==> %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 1; }; }

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) HOST_OS=windows ;;
  Darwin) HOST_OS=darwin ;;
  *) HOST_OS=linux ;;
esac
case "$(uname -m)" in
  arm64|aarch64) HOST_ARCH=arm64 ;;
  *) HOST_ARCH=amd64 ;;
esac

sha256_of() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
# fetch_verified <url> <checksum url> <file>: download and compare against the published SHA-256.
fetch_verified() {
  curl -fsSL -o "$3" "$1"
  local want; want="$(curl -fsSL "$2" | awk '{print $1}')"
  [[ "$(sha256_of "$3")" == "${want}" ]] || { echo "checksum mismatch for $1" >&2; rm -f "$3"; exit 1; }
}

need docker; need kubectl; need curl; need "${CLUSTER_TOOL}"

# helm: use the one on PATH, else download a pinned release into chaos/.tools.
if ! command -v helm >/dev/null 2>&1; then
  HELM_DIR="${TOOLS_DIR}/helm-${HELM_VERSION}/${HOST_OS}-${HOST_ARCH}"
  if [[ ! -x "${HELM_DIR}/helm" && ! -x "${HELM_DIR}/helm.exe" ]]; then
    log "Downloading helm ${HELM_VERSION}"
    ext="tar.gz"; [[ "${HOST_OS}" == windows ]] && ext="zip"
    archive="${TOOLS_DIR}/helm-${HELM_VERSION}-${HOST_OS}-${HOST_ARCH}.${ext}"
    fetch_verified "https://get.helm.sh/helm-${HELM_VERSION}-${HOST_OS}-${HOST_ARCH}.${ext}" \
      "https://get.helm.sh/helm-${HELM_VERSION}-${HOST_OS}-${HOST_ARCH}.${ext}.sha256sum" "${archive}"
    mkdir -p "${TOOLS_DIR}/helm-${HELM_VERSION}"
    if [[ "${ext}" == zip ]]; then unzip -qo "${archive}" -d "${TOOLS_DIR}/helm-${HELM_VERSION}"
    else tar -xzf "${archive}" -C "${TOOLS_DIR}/helm-${HELM_VERSION}"; fi
  fi
  export PATH="${HELM_DIR}:${PATH}"
fi
need helm

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
  if [[ "${HOST_OS}" == windows ]]; then
    # istio.io/downloadIstio would fetch the Linux build under Git Bash; take the Windows release instead.
    archive="${TOOLS_DIR}/istio-${ISTIO_VERSION}-win.zip"
    base="https://github.com/istio/istio/releases/download/${ISTIO_VERSION}/istio-${ISTIO_VERSION}-win.zip"
    fetch_verified "${base}" "${base}.sha256" "${archive}"
    unzip -qo "${archive}" -d "${TOOLS_DIR}"
  else
    (cd "${TOOLS_DIR}" && curl -sL https://istio.io/downloadIstio | ISTIO_VERSION="${ISTIO_VERSION}" sh -)
  fi
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

log "2 replicas for: ${REPLICATED_SERVICES}"
for svc in ${REPLICATED_SERVICES}; do
  kubectl -n "${NAMESPACE}" scale deployment "${svc}" --replicas=2
done

# Sidecars are injected only when a pod is created. On a cluster that already ran these images,
# "kubectl apply" changes nothing, so restart every deployment that still has a pod without istio-proxy.
missing="$(kubectl -n "${NAMESPACE}" get pods -o jsonpath='{range .items[*]}{.metadata.labels.app}{" "}{.spec.containers[*].name}{"\n"}{end}' \
  | awk 'NF && $0 !~ /istio-proxy/ {print $1}' | sort -u)"
if [[ -n "${missing}" ]]; then
  log "Restarting deployments without sidecars: $(echo ${missing})"
  for app in ${missing}; do
    kubectl -n "${NAMESPACE}" get deployment "${app}" >/dev/null 2>&1 \
      && kubectl -n "${NAMESPACE}" rollout restart deployment "${app}"
  done
fi

for d in $(kubectl -n "${NAMESPACE}" get deploy -o name); do
  kubectl -n "${NAMESPACE}" rollout status "${d}" --timeout=600s
done

# ------------------------------------------------------------------ 4. Chaos Mesh
log "Chaos Mesh ${CHAOS_MESH_VERSION} (runtime ${CHAOS_RUNTIME}, socket ${CHAOS_SOCKET})"
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update chaos-mesh >/dev/null
kubectl create namespace chaos-mesh --dry-run=client -o yaml | kubectl apply -f -
# MSYS_NO_PATHCONV: Git Bash would rewrite the socket path to C:/Program Files/Git/run/containerd/...
MSYS_NO_PATHCONV=1 helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --version "${CHAOS_MESH_VERSION}" \
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
  export PROMETHEUS_URL=http://localhost:9090 JAEGER_URL=http://localhost:16686/jaeger

Shop front end:
  kubectl -n ${NAMESPACE} port-forward svc/frontend 8081:80   # http://localhost:8081
EOF
