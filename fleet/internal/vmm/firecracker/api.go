// Package firecracker drives Firecracker microVMs through the jailer and the API socket.
package firecracker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// APIError is a non success answer from the Firecracker API.
type APIError struct {
	StatusCode   int
	FaultMessage string
}

func (apiError *APIError) Error() string {
	return fmt.Sprintf("firecracker api returned %d: %s", apiError.StatusCode, apiError.FaultMessage)
}

// InstanceInfo is the answer to GET /.
type InstanceInfo struct {
	ID         string `json:"id"`
	State      string `json:"state"`
	VMMVersion string `json:"vmm_version"`
}

// BootConfig is everything a cold boot sets before InstanceStart; paths are inside the jail.
type BootConfig struct {
	VCPUs       int
	MemoryMiB   int
	CPUTemplate json.RawMessage
	KernelPath  string
	BootArgs    string
	DiskPath    string
	VsockPath   string
	GuestCID    int
	TapName     string
	GuestMAC    string
}

// API is the subset of the Firecracker API the driver uses.
type API interface {
	BootVM(ctx context.Context, config BootConfig) error
	LoadSnapshot(ctx context.Context, statePath, memoryPath string, useUffd, resume bool) error
	SetPaused(ctx context.Context, paused bool) error
	CreateSnapshot(ctx context.Context, kind, statePath, memoryPath string) error
	InstanceInfo(ctx context.Context) (InstanceInfo, error)
}

// APIClient talks HTTP over the VM's unix socket.
type APIClient struct {
	httpClient *http.Client
}

// unixSocketDialer sends every HTTP connection to one unix socket.
type unixSocketDialer struct {
	socketPath string
}

func (socketDialer unixSocketDialer) dial(ctx context.Context, _, _ string) (net.Conn, error) {
	var dialer net.Dialer
	return dialer.DialContext(ctx, "unix", socketDialer.socketPath)
}

// NewAPIClient returns a client bound to one API socket.
func NewAPIClient(socketPath string) *APIClient {
	transport := &http.Transport{DialContext: unixSocketDialer{socketPath: socketPath}.dial}
	return &APIClient{httpClient: &http.Client{Transport: transport, Timeout: 60 * time.Second}}
}

// NewAPI is the default APIFactory: a real client on the socket.
func NewAPI(socketPath string) API {
	return NewAPIClient(socketPath)
}

func (client *APIClient) call(ctx context.Context, method, path string, body any, decoded any) error {
	var requestBody io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		requestBody = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, "http://localhost"+path, requestBody)
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	response, err := client.httpClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(response.Body)
	if err != nil {
		return err
	}
	if response.StatusCode >= 300 {
		var fault struct {
			FaultMessage string `json:"fault_message"`
		}
		json.Unmarshal(responseBody, &fault)
		return &APIError{StatusCode: response.StatusCode, FaultMessage: fault.FaultMessage}
	}
	if decoded != nil && len(responseBody) > 0 {
		return json.Unmarshal(responseBody, decoded)
	}
	return nil
}

// apiCall is one request of a cold boot.
type apiCall struct {
	method string
	path   string
	body   any
}

// BootVM configures the machine, its CPU template, kernel, root disk, vsock and NIC, then starts it.
func (client *APIClient) BootVM(ctx context.Context, config BootConfig) error {
	calls := []apiCall{{http.MethodPut, "/machine-config", map[string]any{"vcpu_count": config.VCPUs,
		"mem_size_mib": config.MemoryMiB, "smt": false, "track_dirty_pages": true}}}
	if len(config.CPUTemplate) > 0 {
		calls = append(calls, apiCall{http.MethodPut, "/cpu-config", config.CPUTemplate})
	}
	calls = append(calls,
		apiCall{http.MethodPut, "/boot-source", map[string]string{"kernel_image_path": config.KernelPath,
			"boot_args": config.BootArgs}},
		apiCall{http.MethodPut, "/drives/rootfs", map[string]any{"drive_id": "rootfs", "path_on_host": config.DiskPath,
			"is_root_device": true, "is_read_only": false}},
		apiCall{http.MethodPut, "/vsock", map[string]any{"guest_cid": config.GuestCID, "uds_path": config.VsockPath}},
		apiCall{http.MethodPut, "/network-interfaces/eth0", map[string]string{"iface_id": "eth0",
			"host_dev_name": config.TapName, "guest_mac": config.GuestMAC}},
		apiCall{http.MethodPut, "/actions", map[string]string{"action_type": "InstanceStart"}},
	)
	for _, call := range calls {
		if err := client.call(ctx, call.method, call.path, call.body, nil); err != nil {
			return fmt.Errorf("%s %s: %w", call.method, call.path, err)
		}
	}
	return nil
}

// LoadSnapshot restores memory from a file mapped private (shared page cache) or through UFFD.
func (client *APIClient) LoadSnapshot(ctx context.Context, statePath, memoryPath string, useUffd, resume bool) error {
	backendType := "File"
	if useUffd {
		backendType = "Uffd"
	}
	return client.call(ctx, http.MethodPut, "/snapshot/load", map[string]any{
		"snapshot_path":         statePath,
		"mem_backend":           map[string]string{"backend_type": backendType, "backend_path": memoryPath},
		"enable_diff_snapshots": true,
		"resume_vm":             resume,
	}, nil)
}

// SetPaused pauses or resumes the vCPUs.
func (client *APIClient) SetPaused(ctx context.Context, paused bool) error {
	state := "Resumed"
	if paused {
		state = "Paused"
	}
	return client.call(ctx, http.MethodPatch, "/vm", map[string]string{"state": state}, nil)
}

// CreateSnapshot writes a Full or Diff snapshot of a paused VM.
func (client *APIClient) CreateSnapshot(ctx context.Context, kind, statePath, memoryPath string) error {
	return client.call(ctx, http.MethodPut, "/snapshot/create", map[string]string{
		"snapshot_type": kind,
		"snapshot_path": statePath,
		"mem_file_path": memoryPath,
	}, nil)
}

// InstanceInfo reads the instance description.
func (client *APIClient) InstanceInfo(ctx context.Context) (InstanceInfo, error) {
	var info InstanceInfo
	err := client.call(ctx, http.MethodGet, "/", nil, &info)
	return info, err
}
