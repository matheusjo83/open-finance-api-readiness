# Local platform: a kind cluster with the Spark Operator, SeaweedFS (S3) and an
# Iceberg catalog in Postgres (CloudNativePG). The same targets run in CI.
# Tools are downloaded into .bin/ at pinned versions, checked by SHA-256
# (linux-amd64 only: WSL and the GitHub runners).

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

BIN := $(CURDIR)/.bin
export PATH := $(BIN):$(PATH)

CLUSTER := open-finance
SPARK_IMAGE := ofar/spark:local

KIND_VERSION := v0.33.0
KIND_SHA256 := aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
HELM_VERSION := v4.3.2
HELM_SHA256 := d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983
KUBECTL_VERSION := v1.37.0
KUBECTL_SHA256 := 6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f

CNPG_CHART_VERSION := 0.29.1
SEAWEEDFS_CHART_VERSION := 4.54.0
SPARK_OPERATOR_CHART_VERSION := 2.6.0

HELM_INSTALL := helm upgrade --install --wait --timeout 5m

.PHONY: tools cluster platform image up smoke diagnose down

tools: $(BIN)/kind $(BIN)/helm $(BIN)/kubectl

$(BIN)/kind:
	mkdir -p $(BIN)
	curl -fsSLo $@.tmp https://github.com/kubernetes-sigs/kind/releases/download/$(KIND_VERSION)/kind-linux-amd64
	echo "$(KIND_SHA256)  $@.tmp" | sha256sum -c --quiet
	chmod +x $@.tmp && mv $@.tmp $@

$(BIN)/helm:
	mkdir -p $(BIN)
	curl -fsSLo $(BIN)/helm.tgz https://get.helm.sh/helm-$(HELM_VERSION)-linux-amd64.tar.gz
	echo "$(HELM_SHA256)  $(BIN)/helm.tgz" | sha256sum -c --quiet
	tar -xzf $(BIN)/helm.tgz -C $(BIN) --strip-components=1 linux-amd64/helm
	rm $(BIN)/helm.tgz

$(BIN)/kubectl:
	mkdir -p $(BIN)
	curl -fsSLo $@.tmp https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/linux/amd64/kubectl
	echo "$(KUBECTL_SHA256)  $@.tmp" | sha256sum -c --quiet
	chmod +x $@.tmp && mv $@.tmp $@

cluster: tools
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config infra/kind/cluster.yaml
	kubectl config use-context kind-$(CLUSTER)
	kubectl apply -f infra/k8s/namespaces.yaml

platform: cluster
	kubectl apply -f infra/k8s/dev-credentials.yaml -f infra/k8s/lakehouse-config.yaml
	$(HELM_INSTALL) cnpg cloudnative-pg --repo https://cloudnative-pg.github.io/charts \
	  --version $(CNPG_CHART_VERSION) -n cnpg-system --create-namespace -f infra/helm/cloudnative-pg.yaml
	kubectl apply -f infra/k8s/catalog-db.yaml
	$(HELM_INSTALL) seaweedfs seaweedfs --repo https://seaweedfs.github.io/seaweedfs/helm \
	  --version $(SEAWEEDFS_CHART_VERSION) -n storage -f infra/helm/seaweedfs.yaml
	$(HELM_INSTALL) spark-operator spark-operator --repo https://kubeflow.github.io/spark-operator \
	  --version $(SPARK_OPERATOR_CHART_VERSION) -n spark-operator --create-namespace -f infra/helm/spark-operator.yaml
	kubectl -n storage wait cluster.postgresql.cnpg.io/iceberg-catalog --for=condition=Ready --timeout=5m

image: cluster
	docker build -f images/spark/Dockerfile -t $(SPARK_IMAGE) .
	kind load docker-image $(SPARK_IMAGE) --name $(CLUSTER)

up: platform image

# Writes an Iceberg table to SeaweedFS through the Postgres catalog and reads it back.
smoke:
	kubectl -n spark delete sparkapplication smoke --ignore-not-found --wait
	kubectl apply -f jobs/smoke/sparkapp.yaml
	scripts/wait-sparkapp.sh spark smoke 600

diagnose:
	-kubectl get pods -A -o wide
	-kubectl -n spark get sparkapplications -o yaml
	-kubectl get events -A --sort-by=.lastTimestamp | tail -50
	-for ns in storage spark spark-operator cnpg-system; do \
	  for p in $$(kubectl -n $$ns get pods -o name); do \
	    echo "=== $$ns/$$p"; kubectl -n $$ns logs $$p --all-containers --tail=80; done; done

down: tools
	kind delete cluster --name $(CLUSTER)
