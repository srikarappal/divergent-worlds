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

// NewHandler exposes the service as the env API.
func NewHandler(service *Service) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(writer http.ResponseWriter, _ *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /v1/leases", func(writer http.ResponseWriter, request *http.Request) {
		var body leaseGroupBody
		if !decode(writer, request, &body) {
			return
		}
		if body.GroupSize < 1 {
			body.GroupSize = 1
		}
		infos, err := service.LeaseGroup(request.Context(), GroupRequest{
			RunID: body.RunID, Tenant: body.Tenant, CostCenter: body.CostCenter, Pool: body.Pool,
			Priority: body.Priority, Endpoint: body.Endpoint, Snapshot: body.Snapshot,
			PerVM: scheduler.Resources{CPUMillicores: body.CPUMillicores, MemoryMiB: body.MemoryMiB, GPUs: body.GPUs},
			VCPUs: body.VCPUs, GroupSize: body.GroupSize, TTL: time.Duration(body.TTLSeconds) * time.Second,
			HostScopedLicenses: body.HostScopedLicenses,
		})
		if err != nil {
			writeError(writer, err)
			return
		}
		writeJSON(writer, http.StatusCreated, map[string]any{"leases": infos})
	})
	mux.HandleFunc("GET /v1/leases", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]any{"leases": service.Leases(request.URL.Query().Get("run_id"))})
	})
	mux.HandleFunc("POST /v1/leases/{leaseID}/heartbeat", func(writer http.ResponseWriter, request *http.Request) {
		info, err := service.Heartbeat(request.PathValue("leaseID"))
		if err != nil {
			writeError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, info)
	})
	mux.HandleFunc("POST /v1/leases/{leaseID}/reset", func(writer http.ResponseWriter, request *http.Request) {
		result, err := service.Reset(request.Context(), request.PathValue("leaseID"))
		if err != nil {
			writeError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, stepPayload(result))
	})
	mux.HandleFunc("POST /v1/leases/{leaseID}/step", func(writer http.ResponseWriter, request *http.Request) {
		var body stepBody
		if !decode(writer, request, &body) {
			return
		}
		result, err := service.Step(request.Context(), request.PathValue("leaseID"), body.Action)
		if err != nil {
			writeError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, stepPayload(result))
	})
	mux.HandleFunc("POST /v1/leases/{leaseID}/fork", func(writer http.ResponseWriter, request *http.Request) {
		var body forkBody
		if !decode(writer, request, &body) {
			return
		}
		infos, err := service.Fork(request.Context(), request.PathValue("leaseID"), max(1, body.Count))
		if err != nil {
			writeError(writer, err)
			return
		}
		writeJSON(writer, http.StatusCreated, map[string]any{"leases": infos})
	})
	mux.HandleFunc("POST /v1/leases/{leaseID}/checkpoint", func(writer http.ResponseWriter, request *http.Request) {
		snapshot, err := service.Checkpoint(request.Context(), request.PathValue("leaseID"))
		if err != nil {
			writeError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, map[string]string{"state_path": snapshot.StatePath,
			"memory_path": snapshot.MemoryPath, "kind": string(snapshot.Kind)})
	})
	mux.HandleFunc("POST /v1/leases/{leaseID}/finish", func(writer http.ResponseWriter, request *http.Request) {
		var body finishBody
		if !decode(writer, request, &body) {
			return
		}
		finalState, err := service.Finish(request.Context(), request.PathValue("leaseID"), body.Apps)
		if err != nil {
			writeError(writer, err)
			return
		}
		writeJSON(writer, http.StatusOK, map[string]any{"frozen": true, "final_state": finalState})
	})
	mux.HandleFunc("POST /v1/leases/{leaseID}/inject", func(writer http.ResponseWriter, request *http.Request) {
		var payload json.RawMessage
		if !decode(writer, request, &payload) {
			return
		}
		if err := service.Inject(request.Context(), request.PathValue("leaseID"), payload); err != nil {
			writeError(writer, err)
			return
		}
		writer.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("DELETE /v1/leases/{leaseID}", func(writer http.ResponseWriter, request *http.Request) {
		if err := service.Close(request.PathValue("leaseID")); err != nil {
			writeError(writer, err)
			return
		}
		writer.WriteHeader(http.StatusNoContent)
	})
	return mux
}
