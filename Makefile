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

# Official Open Finance Brasil specifications, pinned by commit and SHA-256. Downloaded, not
# vendored: the upstream repository declares no license.
SPECS_COMMIT := 9116cf09c1c371782a46280eebda4a2b59244675
SPECS_URL := https://raw.githubusercontent.com/OpenBanking-Brasil/openapi/$(SPECS_COMMIT)/swagger-apis
CONSENTS_VERSION := 3.3.1
CONSENTS_SHA256 := 11aefe3b7a87e4ad8c0dcaf32b118a9b7f2b873bbbf171786ada6605809aa223
ACCOUNTS_VERSION := 2.4.2
ACCOUNTS_SHA256 := 0b5b92e11a932019497f285e49ecef57bc8c3a61664a172fabe9b2324e9d8e1f
SPECS := specs/upstream/consents-$(CONSENTS_VERSION).yml specs/upstream/accounts-$(ACCOUNTS_VERSION).yml

SPECTRAL_IMAGE := stoplight/spectral:6.17.0@sha256:c8ffae6ee561b70e80402f1ea669e8b37c74fee48a3dbe2147b71359ed987239
SPECTRAL := docker run --rm -v $(CURDIR)/specs:/specs:ro -w /specs $(SPECTRAL_IMAGE)

HELM_INSTALL := helm upgrade --install --wait --timeout 5m

.PHONY: tools specs lint-specs cluster platform image up smoke diagnose down

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

# Re-downloaded when the Makefile changes, so a new commit or checksum is always applied.
define fetch_spec
mkdir -p $(@D)
curl -fsSLo $@.tmp $(SPECS_URL)/$(1)
echo "$(2)  $@.tmp" | sha256sum -c --quiet
mv $@.tmp $@
endef

specs: $(SPECS)

specs/upstream/consents-$(CONSENTS_VERSION).yml: Makefile
	$(call fetch_spec,consents/$(CONSENTS_VERSION).yml,$(CONSENTS_SHA256))

specs/upstream/accounts-$(ACCOUNTS_VERSION).yml: Makefile
	$(call fetch_spec,accounts/$(ACCOUNTS_VERSION).yml,$(ACCOUNTS_SHA256))

# Lints the pinned specs (fails on errors, prints warnings), then checks that every ofb-* rule
# fires on a fixture built to break it.
lint-specs: specs
	$(SPECTRAL) lint --ruleset ruleset.yaml --fail-severity error $(SPECS:specs/%=%)
	$(SPECTRAL) lint --ruleset ruleset.yaml --format json --quiet fixtures/defective.yml > specs/upstream/defective.json || true
	python3 -I scripts/check-ruleset.py specs/ruleset.yaml specs/upstream/defective.json

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
