package fleetd

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/lease"
	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/snapshot"
)

type leaseGroupBody struct {
	RunID              string             `json:"run_id"`
	Tenant             string             `json:"tenant"`
	CostCenter         string             `json:"cost_center"`
	Pool               string             `json:"pool"`
	Priority           admission.Priority `json:"priority"`
	Endpoint           string             `json:"endpoint"`
	Snapshot           Snapshot           `json:"snapshot"`
	CPUMillicores      int                `json:"cpu_millicores"`
	MemoryMiB          int                `json:"memory_mib"`
	GPUs               int                `json:"gpus"`
	VCPUs              int                `json:"vcpus"`
	GroupSize          int                `json:"group_size"`
	TTLSeconds         int                `json:"ttl_seconds"`
	HostScopedLicenses []string           `json:"host_scoped_licenses"`
	WorldTime          *time.Time         `json:"world_time"`
	ObservationKinds   []string           `json:"observation_kinds"`
}

type stepBody struct {
	Action json.RawMessage `json:"action"`
}

type forkBody struct {
	Count int `json:"count"`
}

type finishBody struct {
	Apps []string `json:"apps"`
}

type quotaBody struct {
	Quota int `json:"quota"`
}

type stepResponse struct {
	Observation    json.RawMessage `json:"observation"`
	EnvSeconds     float64         `json:"env_seconds"`
	SettleSeconds  float64         `json:"settle_seconds"`
	SettleCapped   bool            `json:"settle_capped"`
	EnvironmentErr string          `json:"env_error,omitempty"`
}

type errorResponse struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeJSON(writer http.ResponseWriter, status int, payload any) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	json.NewEncoder(writer).Encode(payload)
}

// statusFor maps errors to HTTP codes; anything unrecognized is an env error, never an agent failure.
func statusFor(err error) (int, string) {
	switch {
	case errors.Is(err, admission.ErrFleetFull):
		return http.StatusTooManyRequests, "fleet_full"
	case errors.Is(err, admission.ErrTenantQuota):
		return http.StatusTooManyRequests, "tenant_quota"
	case errors.Is(err, admission.ErrNoInferenceHeadroom):
		return http.StatusTooManyRequests, "no_inference_headroom"
	case errors.Is(err, scheduler.ErrNoCapacity):
		return http.StatusServiceUnavailable, "no_capacity"
	case errors.Is(err, ErrUnknownLease), errors.Is(err, lease.ErrNotFound), errors.Is(err, nodeagent.ErrUnknownEnv):
		return http.StatusNotFound, "unknown_lease"
	case errors.Is(err, ErrInputFrozen):
		return http.StatusConflict, "input_frozen"
	case errors.Is(err, ErrTakenOver):
		return http.StatusConflict, "taken_over"
	case errors.Is(err, ErrNoTakeover):
		return http.StatusConflict, "no_takeover"
	case errors.Is(err, ErrUnknownTakeover):
		return http.StatusNotFound, "unknown_takeover"
	case errors.Is(err, snapshot.ErrNotFound):
		return http.StatusNotFound, "unknown_snapshot"
	case errors.Is(err, snapshot.ErrIncompatible):
		return http.StatusConflict, "incompatible_snapshot"
	case errors.Is(err, snapshot.ErrBadDigest), errors.Is(err, ErrBadSnapshotRequest):
		return http.StatusBadRequest, "bad_request"
	case errors.Is(err, ErrBadImageBuild):
		return http.StatusUnprocessableEntity, "bad_image_build"
	case errors.Is(err, ErrNoSnapshots), errors.Is(err, nodeagent.ErrNoSnapshotStore):
		return http.StatusServiceUnavailable, "no_snapshot_store"
	default:
		return http.StatusInternalServerError, "env_error"
	}
}

func writeError(writer http.ResponseWriter, err error) {
	status, code := statusFor(err)
	writeJSON(writer, status, errorResponse{Code: code, Message: err.Error()})
}

func decode(writer http.ResponseWriter, request *http.Request, target any) bool {
	if err := json.NewDecoder(request.Body).Decode(target); err != nil {
		writeJSON(writer, http.StatusBadRequest, errorResponse{Code: "bad_request", Message: err.Error()})
		return false
	}
	return true
}

func stepPayload(result nodeagent.StepResult) stepResponse {
	return stepResponse{Observation: result.Observation, EnvSeconds: result.EnvDuration.Seconds(),
		SettleSeconds: result.SettleDuration.Seconds(), SettleCapped: result.SettleCapped}
}

// httpAPI holds the service for the handler methods below.
type httpAPI struct {
	service *Service
}

// route is one pattern of the env API and the method that serves it.
type route struct {
	pattern string
	handler http.HandlerFunc
}

// routes lists the env API; the console's operations manifest names the admission routes too.
func (api *httpAPI) routes() []route {
	return []route{
		{"GET /healthz", api.health},
		{"POST /v1/leases", api.leaseGroup},
		{"GET /v1/leases", api.listLeases},
		{"POST /v1/leases/{leaseID}/heartbeat", api.heartbeat},
		{"POST /v1/leases/{leaseID}/reset", api.reset},
		{"POST /v1/leases/{leaseID}/step", api.step},
		{"POST /v1/leases/{leaseID}/fork", api.fork},
		{"POST /v1/leases/{leaseID}/checkpoint", api.checkpoint},
		{"POST /v1/leases/{leaseID}/finish", api.finish},
		{"POST /v1/leases/{leaseID}/inject", api.inject},
		{"DELETE /v1/leases/{leaseID}", api.close},
		{"POST /v1/leases/{leaseID}/takeover", api.startTakeover},
		{"DELETE /v1/leases/{leaseID}/takeover", api.endTakeover},
		{"GET /v1/takeover/{sessionID}", api.takeoverRelay},
		{"POST /v1/snapshots/build", api.buildSnapshot},
		{"POST /v1/snapshots/collect", api.collectSnapshots},
		{"GET /v1/snapshots", api.listSnapshots},
		{"GET /v1/snapshots/current", api.currentSnapshot},
		{"GET /v1/snapshots/{digest}", api.getSnapshot},
		{"POST /v1/snapshots/{digest}/prefetch", api.prefetchSnapshot},
		{"PUT /v1/snapshots/{digest}/pins/{holder}", api.pinSnapshot},
		{"DELETE /v1/snapshots/{digest}/pins/{holder}", api.unpinSnapshot},
		{"GET /v1/admission", api.admissionStatus},
		{"PUT /v1/admission/tenants/{tenant}", api.setTenantQuota},
	}
}

// NewHandler exposes the service as the env API.
func NewHandler(service *Service) http.Handler {
	api := &httpAPI{service: service}
	mux := http.NewServeMux()
	for _, entry := range api.routes() {
		mux.HandleFunc(entry.pattern, entry.handler)
	}
	return mux
}

func (api *httpAPI) health(writer http.ResponseWriter, _ *http.Request) {
	writeJSON(writer, http.StatusOK, map[string]string{"status": "ok"})
}

func (api *httpAPI) leaseGroup(writer http.ResponseWriter, request *http.Request) {
	var body leaseGroupBody
	if !decode(writer, request, &body) {
		return
	}
	group := GroupRequest{
		RunID: body.RunID, Tenant: body.Tenant, CostCenter: body.CostCenter, Pool: body.Pool,
		Priority: body.Priority, Endpoint: body.Endpoint, Snapshot: body.Snapshot,
		PerVM: scheduler.Resources{CPUMillicores: body.CPUMillicores, MemoryMiB: body.MemoryMiB, GPUs: body.GPUs},
		VCPUs: body.VCPUs, GroupSize: max(1, body.GroupSize), TTL: time.Duration(body.TTLSeconds) * time.Second,
		HostScopedLicenses: body.HostScopedLicenses, ObservationKinds: body.ObservationKinds,
	}
	if body.WorldTime != nil {
		group.WorldTime = *body.WorldTime
	}
	infos, err := api.service.LeaseGroup(request.Context(), group)
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusCreated, map[string]any{"leases": infos})
}

func (api *httpAPI) listLeases(writer http.ResponseWriter, request *http.Request) {
	writeJSON(writer, http.StatusOK, map[string]any{"leases": api.service.Leases(request.URL.Query().Get("run_id"))})
}

func (api *httpAPI) heartbeat(writer http.ResponseWriter, request *http.Request) {
	info, err := api.service.Heartbeat(request.PathValue("leaseID"))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, info)
}

func (api *httpAPI) reset(writer http.ResponseWriter, request *http.Request) {
	result, err := api.service.Reset(request.Context(), request.PathValue("leaseID"))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, stepPayload(result))
}

func (api *httpAPI) step(writer http.ResponseWriter, request *http.Request) {
	var body stepBody
	if !decode(writer, request, &body) {
		return
	}
	result, err := api.service.Step(request.Context(), request.PathValue("leaseID"), body.Action)
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, stepPayload(result))
}

func (api *httpAPI) fork(writer http.ResponseWriter, request *http.Request) {
	var body forkBody
	if !decode(writer, request, &body) {
		return
	}
	infos, err := api.service.Fork(request.Context(), request.PathValue("leaseID"), max(1, body.Count))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusCreated, map[string]any{"leases": infos})
}

func (api *httpAPI) checkpoint(writer http.ResponseWriter, request *http.Request) {
	snapshot, err := api.service.Checkpoint(request.Context(), request.PathValue("leaseID"))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, map[string]string{"state_path": snapshot.StatePath,
		"memory_path": snapshot.MemoryPath, "kind": string(snapshot.Kind)})
}

func (api *httpAPI) finish(writer http.ResponseWriter, request *http.Request) {
	var body finishBody
	if !decode(writer, request, &body) {
		return
	}
	finalState, err := api.service.Finish(request.Context(), request.PathValue("leaseID"), body.Apps)
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, map[string]any{"frozen": true, "final_state": finalState})
}

func (api *httpAPI) inject(writer http.ResponseWriter, request *http.Request) {
	var payload json.RawMessage
	if !decode(writer, request, &payload) {
		return
	}
	if err := api.service.Inject(request.Context(), request.PathValue("leaseID"), payload); err != nil {
		writeError(writer, err)
		return
	}
	writer.WriteHeader(http.StatusNoContent)
}

func (api *httpAPI) close(writer http.ResponseWriter, request *http.Request) {
	if err := api.service.Close(request.PathValue("leaseID")); err != nil {
		writeError(writer, err)
		return
	}
	writer.WriteHeader(http.StatusNoContent)
}

func (api *httpAPI) admissionStatus(writer http.ResponseWriter, _ *http.Request) {
	writeJSON(writer, http.StatusOK, api.service.AdmissionStatus())
}

func (api *httpAPI) setTenantQuota(writer http.ResponseWriter, request *http.Request) {
	var body quotaBody
	if !decode(writer, request, &body) {
		return
	}
	if err := api.service.SetTenantQuota(request.PathValue("tenant"), body.Quota); err != nil {
		writeJSON(writer, http.StatusBadRequest, errorResponse{Code: "bad_request", Message: err.Error()})
		return
	}
	writeJSON(writer, http.StatusOK, api.service.AdmissionStatus())
}

// buildSnapshot runs for minutes: the image boots, waits for its readiness probe and its snapshot is published.
func (api *httpAPI) buildSnapshot(writer http.ResponseWriter, request *http.Request) {
	var body SnapshotBuild
	if !decode(writer, request, &body) {
		return
	}
	manifest, err := api.service.BuildSnapshot(request.Context(), body)
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusCreated, manifest)
}

func (api *httpAPI) collectSnapshots(writer http.ResponseWriter, request *http.Request) {
	report, err := api.service.CollectSnapshots(request.Context())
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, report)
}

func (api *httpAPI) listSnapshots(writer http.ResponseWriter, request *http.Request) {
	currents, err := api.service.CurrentSnapshots(request.Context())
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, map[string]any{"current": append([]snapshot.Current{}, currents...)})
}

func (api *httpAPI) currentSnapshot(writer http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	current, err := api.service.CurrentSnapshot(request.Context(), query.Get("image_digest"), query.Get("pool"))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, current)
}

func (api *httpAPI) getSnapshot(writer http.ResponseWriter, request *http.Request) {
	detail, err := api.service.Snapshot(request.Context(), request.PathValue("digest"))
	if err != nil {
		writeError(writer, err)
		return
	}
	detail.Pins = append([]snapshot.Pin{}, detail.Pins...)
	writeJSON(writer, http.StatusOK, detail)
}

func (api *httpAPI) prefetchSnapshot(writer http.ResponseWriter, request *http.Request) {
	var body PrefetchRequest
	if !decode(writer, request, &body) {
		return
	}
	hostIDs, err := api.service.PrefetchSnapshot(request.Context(), request.PathValue("digest"), body)
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusAccepted, map[string]any{"hosts": append([]string{}, hostIDs...)})
}

func (api *httpAPI) pinSnapshot(writer http.ResponseWriter, request *http.Request) {
	pin, err := api.service.PinSnapshot(request.Context(), request.PathValue("digest"), request.PathValue("holder"))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, pin)
}

func (api *httpAPI) unpinSnapshot(writer http.ResponseWriter, request *http.Request) {
	if err := api.service.UnpinSnapshot(request.Context(), request.PathValue("digest"),
		request.PathValue("holder")); err != nil {
		writeError(writer, err)
		return
	}
	writer.WriteHeader(http.StatusNoContent)
}
