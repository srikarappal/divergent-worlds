// Package clock lets fleet code read time through an interface so tests can move it by hand.
package clock

import (
	"sync"
	"time"
)

// Clock returns the current time.
type Clock interface {
	Now() time.Time
}

// Real reads the system clock.
type Real struct{}

// Now returns the current system time.
func (Real) Now() time.Time {
	return time.Now()
}

// Fake is a clock that only moves when a test advances it.
type Fake struct {
	mu      sync.Mutex
	current time.Time
}

// NewFake returns a fake clock set to start.
func NewFake(start time.Time) *Fake {
	return &Fake{current: start}
}

// Now returns the fake time.
func (fake *Fake) Now() time.Time {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	return fake.current
}

// Advance moves the fake time forward.
func (fake *Fake) Advance(duration time.Duration) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	fake.current = fake.current.Add(duration)
}
