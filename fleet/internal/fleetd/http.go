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
	_ = json.NewEncoder(writer).Encode(payload)
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

// NewHandler exposes the service as the env API.
func NewHandler(service *Service) http.Handler {
	api := &httpAPI{service: service}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", api.health)
	mux.HandleFunc("POST /v1/leases", api.leaseGroup)
	mux.HandleFunc("GET /v1/leases", api.listLeases)
	mux.HandleFunc("POST /v1/leases/{leaseID}/heartbeat", api.heartbeat)
	mux.HandleFunc("POST /v1/leases/{leaseID}/reset", api.reset)
	mux.HandleFunc("POST /v1/leases/{leaseID}/step", api.step)
	mux.HandleFunc("POST /v1/leases/{leaseID}/fork", api.fork)
	mux.HandleFunc("POST /v1/leases/{leaseID}/checkpoint", api.checkpoint)
	mux.HandleFunc("POST /v1/leases/{leaseID}/finish", api.finish)
	mux.HandleFunc("POST /v1/leases/{leaseID}/inject", api.inject)
	mux.HandleFunc("DELETE /v1/leases/{leaseID}", api.close)
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
	infos, err := api.service.LeaseGroup(request.Context(), GroupRequest{
		RunID: body.RunID, Tenant: body.Tenant, CostCenter: body.CostCenter, Pool: body.Pool,
		Priority: body.Priority, Endpoint: body.Endpoint, Snapshot: body.Snapshot,
		PerVM: scheduler.Resources{CPUMillicores: body.CPUMillicores, MemoryMiB: body.MemoryMiB, GPUs: body.GPUs},
		VCPUs: body.VCPUs, GroupSize: max(1, body.GroupSize), TTL: time.Duration(body.TTLSeconds) * time.Second,
		HostScopedLicenses: body.HostScopedLicenses,
	})
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
