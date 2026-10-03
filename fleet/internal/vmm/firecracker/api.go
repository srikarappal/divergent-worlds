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

// API is the subset of the Firecracker API the driver uses.
type API interface {
	LoadSnapshot(ctx context.Context, statePath, memoryPath string, useUffd, resume bool) error
	SetPaused(ctx context.Context, paused bool) error
	CreateSnapshot(ctx context.Context, kind, statePath, memoryPath string) error
	InstanceInfo(ctx context.Context) (InstanceInfo, error)
}

// APIClient talks HTTP over the VM's unix socket.
type APIClient struct {
	httpClient *http.Client
}

// NewAPIClient returns a client bound to one API socket.
func NewAPIClient(socketPath string) *APIClient {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var dialer net.Dialer
			return dialer.DialContext(ctx, "unix", socketPath)
		},
	}
	return &APIClient{httpClient: &http.Client{Transport: transport, Timeout: 60 * time.Second}}
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
		_ = json.Unmarshal(responseBody, &fault)
		return &APIError{StatusCode: response.StatusCode, FaultMessage: fault.FaultMessage}
	}
	if decoded != nil && len(responseBody) > 0 {
		return json.Unmarshal(responseBody, decoded)
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
