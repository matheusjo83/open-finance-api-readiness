# Local platform: a kind cluster with the Spark Operator, SeaweedFS (S3) and an
# Iceberg catalog in Postgres (CloudNativePG). The same targets run in CI.
# Tools are downloaded into .bin/ at pinned versions, checked by SHA-256
# (linux-amd64 only: WSL and the GitHub runners).

SHELL := /bin/bash
comma := ,
.SHELLFLAGS := -euo pipefail -c

BIN := $(CURDIR)/.bin
export PATH := $(BIN):$(PATH)
# Every download retries transient failures (a 503 from raw.githubusercontent.com failed a CI
# run on 2026-11-24); the checksums still decide what is kept (decision 36).
CURL := curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20

CLUSTER := open-finance
# Images go to the worker only: the control plane is tainted NoSchedule, so no pod of ours runs
# there, and loading it too costs a second copy of every image (decision 34).
KIND_LOAD := kind load docker-image --name $(CLUSTER) --nodes $(CLUSTER)-worker
SPARK_IMAGE := ofar/spark:local
TRANSMITTER_IMAGE := ofar/transmitter:local
PROXY_IMAGE := ofar/defect-proxy:local
VALIDATOR_IMAGE := ofar/contract-validator:local

KIND_VERSION := v0.33.0
KIND_SHA256 := aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
HELM_VERSION := v4.3.2
HELM_SHA256 := d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983
KUBECTL_VERSION := v1.37.0
KUBECTL_SHA256 := 6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
UV_VERSION := 0.13.6
UV_SHA256 := f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645

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

.PHONY: tools specs jars lint-specs check-requirements transmitter-test proxy-test validator-test cluster platform image \
  transmitter-image transmitter-deploy transmitter-smoke proxy-image proxy-deploy proxy-smoke \
  validator-image validate validator-smoke jobs-test business-rules report report-smoke campaign focused-campaign reports \
  up smoke diagnose down

tools: $(BIN)/kind $(BIN)/helm $(BIN)/kubectl

$(BIN)/kind:
	mkdir -p $(BIN)
	$(CURL) -o $@.tmp https://github.com/kubernetes-sigs/kind/releases/download/$(KIND_VERSION)/kind-linux-amd64
	echo "$(KIND_SHA256)  $@.tmp" | sha256sum -c --quiet
	chmod +x $@.tmp && mv $@.tmp $@

$(BIN)/helm:
	mkdir -p $(BIN)
	$(CURL) -o $(BIN)/helm.tgz https://get.helm.sh/helm-$(HELM_VERSION)-linux-amd64.tar.gz
	echo "$(HELM_SHA256)  $(BIN)/helm.tgz" | sha256sum -c --quiet
	tar -xzf $(BIN)/helm.tgz -C $(BIN) --strip-components=1 linux-amd64/helm
	rm $(BIN)/helm.tgz

# uv runs the Python services' tests (it also fetches the pinned Python 3.13).
$(BIN)/uv:
	mkdir -p $(BIN)
	$(CURL) -o $(BIN)/uv.tgz https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-x86_64-unknown-linux-gnu.tar.gz
	echo "$(UV_SHA256)  $(BIN)/uv.tgz" | sha256sum -c --quiet
	tar -xzf $(BIN)/uv.tgz -C $(BIN) --strip-components=1 uv-x86_64-unknown-linux-gnu/uv
	rm $(BIN)/uv.tgz

$(BIN)/kubectl:
	mkdir -p $(BIN)
	$(CURL) -o $@.tmp https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/linux/amd64/kubectl
	echo "$(KUBECTL_SHA256)  $@.tmp" | sha256sum -c --quiet
	chmod +x $@.tmp && mv $@.tmp $@

# Re-downloaded when the Makefile changes, so a new commit or checksum is always applied.
define fetch_spec
mkdir -p $(@D)
$(CURL) -o $@.tmp $(SPECS_URL)/$(1)
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

# The operational requirements (specs/operational) against the pinned specs and their own rules;
# every check must also notice a copy broken on purpose. Runs in the validator's environment (PyYAML).
check-requirements: $(BIN)/uv specs
	cd services/validator && uv run --locked python -I ../../scripts/check-requirements.py \
	  ../../specs/operational/requirements.json $(SPECS:%=../../%)

# Every transmitter response, happy and error paths, validated against the pinned specs.
transmitter-test: $(BIN)/uv specs
	cd services/transmitter && uv run --locked pytest

# Every defect class against the real transmitter in process: contract defects break the specs,
# business defects keep them and break a domain rule; labels, determinism, rates.
proxy-test: $(BIN)/uv specs
	cd services/proxy && uv run --locked pytest

# The validator over the proxy and the real transmitter, in process: no finding on clean
# responses, every contract defect found by its rule, business defects invisible to it.
validator-test: $(BIN)/uv specs
	cd services/validator && uv run --locked pytest

cluster: tools
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config infra/kind/cluster.yaml
	kubectl config use-context kind-$(CLUSTER)
	# kind returns before the nodes are Ready; pods scheduled earlier start late.
	kubectl wait node --all --for=condition=Ready --timeout=3m
	kubectl apply -f infra/k8s/namespaces.yaml

platform: cluster
	kubectl apply -f infra/k8s/dev-credentials.yaml -f infra/k8s/lakehouse-config.yaml
	$(HELM_INSTALL) cnpg cloudnative-pg --repo https://cloudnative-pg.github.io/charts \
	  --version $(CNPG_CHART_VERSION) -n cnpg-system --create-namespace -f infra/helm/cloudnative-pg.yaml
	# helm --wait returns when the operator pod is Ready, which can be before the API server
	# reaches its admission webhook; retry until the webhook accepts the Cluster.
	for i in $$(seq 1 12); do \
	  kubectl apply -f infra/k8s/catalog-db.yaml && break; \
	  [ $$i -lt 12 ] || exit 1; echo "CNPG webhook not reachable yet ($$i/12), retrying in 5s"; sleep 5; \
	done
	$(HELM_INSTALL) seaweedfs seaweedfs --repo https://seaweedfs.github.io/seaweedfs/helm \
	  --version $(SEAWEEDFS_CHART_VERSION) -n storage -f infra/helm/seaweedfs.yaml
	$(HELM_INSTALL) spark-operator spark-operator --repo https://kubeflow.github.io/spark-operator \
	  --version $(SPARK_OPERATOR_CHART_VERSION) -n spark-operator --create-namespace -f infra/helm/spark-operator.yaml
	kubectl -n storage wait cluster.postgresql.cnpg.io/iceberg-catalog --for=condition=Ready --timeout=5m

# The Spark image's jars, checked against images/spark/jars.sha256 (kept when already right).
jars:
	scripts/fetch-jars.sh images/spark/jars.sha256 .cache/jars

image: cluster jars
	docker build -f images/spark/Dockerfile -t $(SPARK_IMAGE) .
	$(KIND_LOAD) $(SPARK_IMAGE)

transmitter-image: cluster
	docker build -t $(TRANSMITTER_IMAGE) services/transmitter
	$(KIND_LOAD) $(TRANSMITTER_IMAGE)

# Applies a manifest with a hash of the image's build inputs in its ofar/image-id annotation, so
# the pod is replaced only when they change. (Docker's image id is not used: with the containerd
# image store it changes on every build, even a fully cached one.)
define apply_with_image_id
sed "s|ofar/image-id: .*|ofar/image-id: \"$$(cd $(1) && find Dockerfile pyproject.toml uv.lock .python-version src -type f \
  ! -path '*/__pycache__/*' | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -c1-12)\"|" $(2) | kubectl apply -f -
endef

transmitter-deploy: transmitter-image
	$(call apply_with_image_id,services/transmitter,infra/k8s/transmitter.yaml)
	kubectl -n ofb rollout status deployment/transmitter --timeout=3m

# Calls the transmitter through its Service from inside the cluster.
transmitter-smoke:
	kubectl -n ofb exec -i deploy/transmitter -- python - < scripts/transmitter_smoke.py

proxy-image: cluster
	docker build -t $(PROXY_IMAGE) services/proxy
	$(KIND_LOAD) $(PROXY_IMAGE)

proxy-deploy: proxy-image
	$(call apply_with_image_id,services/proxy,infra/k8s/proxy.yaml)
	kubectl -n ofb rollout status deployment/proxy --timeout=3m

# Walks the seeded resources through the proxy and reads the labels back from SeaweedFS.
proxy-smoke:
	kubectl -n ofb exec -i deploy/proxy -- python - < scripts/proxy_smoke.py

# The business rules and the detection report over validation runs exported in process
# (transmitter, proxy, validator), with a local Spark (Python 3.10 and Java 17, as in the image).
jobs-test: $(BIN)/uv specs
	rm -rf jobs/tests/.runs
	cd services/validator && uv run --locked python -W ignore -m tests.export_runs ../../jobs/tests/.runs
	cd jobs && uv run --locked pytest

# The pinned specs go in as a named build context (they are outside services/validator).
validator-image: cluster specs
	docker build --build-context specs=specs/upstream -t $(VALIDATOR_IMAGE) services/validator
	$(KIND_LOAD) $(VALIDATOR_IMAGE)

# One validation run through the proxy, as a Job: results and exchanges in SeaweedFS.
validate: validator-image
	kubectl -n ofb delete job contract-validator --ignore-not-found --wait
	kubectl apply -f infra/k8s/validator.yaml
	scripts/wait-job.sh ofb contract-validator 900

# Joins the last run's results with the proxy's labels: the contract layer must be exact.
validator-smoke:
	kubectl -n ofb exec -i deploy/proxy -- python - < scripts/validator_smoke.py

# The business-rules job over one run (RUN_ID, default the proxy's current run), as a
# SparkApplication: bronze, silver and gold Iceberg tables in lake.ofb.
PROXY_RUN_ID = kubectl -n ofb exec deploy/proxy -- python -c \
  "import json, urllib.request; print(json.load(urllib.request.urlopen('http://localhost:8080/_proxy/run'))['run_id'])"

# $(call spark_app,<name>,<manifest>,<sed expressions>): replaces the SparkApplication and waits.
define spark_app
kubectl -n spark delete sparkapplication $(1) --ignore-not-found --wait
sed $(3) $(2) | kubectl apply -f -
scripts/wait-sparkapp.sh spark $(1) 600
endef

business-rules:
	@run_id="$${RUN_ID:-$$($(PROXY_RUN_ID))}"; echo "run $$run_id"; \
	$(MAKE) --no-print-directory _business RUN_ID="$$run_id"

_business:
	$(call spark_app,business-rules,jobs/business/sparkapp.yaml,-e 's|"RUN_ID"|"$(RUN_ID)"|')

# The detection report over RUNS (comma-separated, default the proxy's current run), named
# REPORT_NAME (default: the first run): lake.ofb.detection_* and s3a://lakehouse/reports/<name>/report.md.
report:
	@runs="$${RUNS:-$$($(PROXY_RUN_ID))}"; echo "runs $$runs"; \
	$(MAKE) --no-print-directory _report RUNS="$$runs" REPORT_NAME="$(REPORT_NAME)" REPORT_ARGS="$(REPORT_ARGS)"

# The same, failing when an invariant breaks (precision 1, conditional recall 1).
report-smoke:
	$(MAKE) --no-print-directory report REPORT_ARGS='"--gate", '

_report:
	$(call spark_app,detection-report,jobs/report/sparkapp.yaml,-e 's|"RUNS"|"$(RUNS)"$(if $(REPORT_NAME),$(comma) "--name"$(comma) "$(REPORT_NAME)")|' -e 's|arguments: \[|arguments: [$(REPORT_ARGS)|')

# A campaign: SEEDS runs (one per defect seed) with CUSTOMERS customers, validated, checked and
# reported together as REPORT_NAME (scripts/campaign.sh). Restores the default settings at the end.
SEEDS ?= 3
CUSTOMERS ?= 50
campaign:
	scripts/campaign.sh "$(SEEDS)" "$(CUSTOMERS)" "$(or $(REPORT_NAME),campaign-$$(date -u +%Y%m%d))"

# A focused campaign: the same, with the contract classes off and the business classes boosted
# (FOCUSED_RATES), to measure each business rule on many cases. Reported apart from the
# reference campaign: the mix is artificial (docs/report.md, decision 37).
FOCUSED_RATES ?= {"missing-required-field": 0, "invalid-format": 0, "missing-interaction-id": 0, "undeclared-field": 0, "inconsistent-balance": 0.3, "broken-pagination": 0.25, "consent-not-enforced": 0.1, "transaction-outside-window": 0.05, "broken-extension-history": 0.15, "extension-not-reflected": 0.15}
focused-campaign:
	scripts/campaign.sh "$(SEEDS)" "$(CUSTOMERS)" "$(or $(REPORT_NAME),focused-$$(date -u +%Y%m%d))" '$(FOCUSED_RATES)'

up: platform image transmitter-deploy proxy-deploy

# Writes an Iceberg table to SeaweedFS through the Postgres catalog and reads it back.
smoke:
	kubectl -n spark delete sparkapplication smoke --ignore-not-found --wait
	kubectl apply -f jobs/smoke/sparkapp.yaml
	scripts/wait-sparkapp.sh spark smoke 600

# The current run's data (labels, contract results, exchanges, detection report, from the
# object store) and the logs of its jobs, in reports/: what CI uploads as the `reports` artifact.
reports:
	rm -rf reports && mkdir -p reports/logs
	kubectl -n ofb exec -i deploy/proxy -- python - < scripts/collect_reports.py | tar -xzf - -C reports
	-kubectl -n ofb logs job/contract-validator > reports/logs/contract-validator.log
	-kubectl -n spark logs business-rules-driver > reports/logs/business-rules.log
	-kubectl -n spark logs detection-report-driver > reports/logs/detection-report.log
	find reports -type f | LC_ALL=C sort

diagnose:
	-kubectl get pods -A -o wide
	-kubectl -n spark get sparkapplications -o yaml
	-kubectl get events -A --sort-by=.lastTimestamp | tail -50
	-kubectl -n ofb get jobs -o wide
	-for ns in storage spark spark-operator cnpg-system ofb; do \
	  for p in $$(kubectl -n $$ns get pods -o name); do \
	    echo "=== $$ns/$$p"; kubectl -n $$ns logs $$p --all-containers --tail=80; done; done

down: tools
	kind delete cluster --name $(CLUSTER)
