// Package hostexec runs host commands for the node agent and starts the long lived VMM processes drivers need.
package hostexec

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"

	"rlworld/fleet/internal/vmm"
)

// Commands runs one command to completion, such as the clone network and disk overlay setup.
type Commands struct{}

// Run runs argv and folds its output into the error when it fails.
func (Commands) Run(ctx context.Context, argv []string) error {
	if len(argv) == 0 {
		return errors.New("empty command")
	}
	output, err := exec.CommandContext(ctx, argv[0], argv[1:]...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s: %w: %s", argv[0], err, strings.TrimSpace(string(output)))
	}
	return nil
}

// Processes starts VMM processes.
type Processes struct{}

type process struct {
	command *exec.Cmd
}

// PID is the process id.
func (running *process) PID() int {
	return running.command.Process.Pid
}

// Kill stops the process.
func (running *process) Kill() error {
	return running.command.Process.Kill()
}

// Wait reaps the process.
func (running *process) Wait() error {
	return running.command.Wait()
}

// Start launches argv detached from ctx, so the API request that restored a VM never takes it down when it ends.
func (Processes) Start(_ context.Context, argv []string) (vmm.Process, error) {
	if len(argv) == 0 {
		return nil, errors.New("empty command")
	}
	command := exec.Command(argv[0], argv[1:]...)
	if err := command.Start(); err != nil {
		return nil, fmt.Errorf("start %s: %w", argv[0], err)
	}
	return &process{command: command}, nil
}
