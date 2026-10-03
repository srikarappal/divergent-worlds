// Package admission decides whether a new env may start (DESIGN.md section 6.7).
//
// Three limits apply. Priority classes reserve capacity instead of preempting,
// because preempting an RL episode wastes it. Tenants have quotas. Each policy
// endpoint caps how many envs it can keep busy: by Little's law an env spends
// tokensPerEnvMinute against the endpoint's tokens per minute, so starting more
// envs than that only makes them idle behind the rate limit.
package admission

import (
	"errors"
	"fmt"
	"sync"
)

// Priority orders classes; a lower rank is more important.
type Priority string

const (
	// Interactive covers teleop, debugging and dry runs.
	Interactive Priority = "interactive"
	// Eval covers evaluation runs.
	Eval Priority = "eval"
	// Training covers RL and data runs.
	Training Priority = "training"
)

var (
	// ErrFleetFull means no capacity is left for the request's class.
	ErrFleetFull = errors.New("fleet capacity is exhausted for this priority class")
	// ErrTenantQuota means the tenant already runs its maximum number of envs.
	ErrTenantQuota = errors.New("tenant quota is exhausted")
	// ErrNoInferenceHeadroom means the policy endpoint cannot keep another env busy.
	ErrNoInferenceHeadroom = errors.New("policy endpoint has no inference headroom")
	// ErrUnknownPriority means the request names no known priority class.
	ErrUnknownPriority = errors.New("unknown priority class")
)

// EndpointBudget describes one policy endpoint's rate limit and what one env consumes.
type EndpointBudget struct {
	TokensPerMinute    int
	TokensPerEnvMinute int
}

// MaxEnvs is how many envs the endpoint can keep busy at once.
func (budget EndpointBudget) MaxEnvs() int {
	if budget.TokensPerEnvMinute <= 0 {
		return 0
	}
	return budget.TokensPerMinute / budget.TokensPerEnvMinute
}

// Config sets capacity, reservations, quotas and endpoint budgets.
type Config struct {
	Capacity           int
	Reserved           map[Priority]int
	TenantQuotas       map[string]int
	DefaultTenantQuota int
	EndpointBudgets    map[string]EndpointBudget
}

// Request describes the env that wants to start.
type Request struct {
	Tenant   string
	Priority Priority
	Endpoint string
}

// Usage reports envs in use.
type Usage struct {
	Total      int              `json:"total"`
	ByClass    map[Priority]int `json:"by_class"`
	ByTenant   map[string]int   `json:"by_tenant"`
	ByEndpoint map[string]int   `json:"by_endpoint"`
}

// Status is what the admin console shows: every limit next to what is in use against it.
type Status struct {
	Capacity           int              `json:"capacity"`
	Reserved           map[Priority]int `json:"reserved"`
	DefaultTenantQuota int              `json:"default_tenant_quota"`
	TenantQuotas       map[string]int   `json:"tenant_quotas"`
	EndpointMaxEnvs    map[string]int   `json:"endpoint_max_envs"`
	Usage              Usage            `json:"usage"`
}

// Controller tracks envs in use and admits new ones.
type Controller struct {
	mu                 sync.Mutex
	config             Config
	inUseByClass       map[Priority]int
	inUseByTenant      map[string]int
	inUseByEndpoint    map[string]int
	knownPriorityRanks map[Priority]int
}

// NewController validates the config and returns a controller with nothing in use.
func NewController(config Config) (*Controller, error) {
	reservedTotal := 0
	for priority, reserved := range config.Reserved {
		if reserved < 0 {
			return nil, fmt.Errorf("reservation for %s is negative", priority)
		}
		reservedTotal += reserved
	}
	if reservedTotal > config.Capacity {
		return nil, fmt.Errorf("reservations total %d exceed capacity %d", reservedTotal, config.Capacity)
	}
	return &Controller{
		config:             config,
		inUseByClass:       map[Priority]int{},
		inUseByTenant:      map[string]int{},
		inUseByEndpoint:    map[string]int{},
		knownPriorityRanks: map[Priority]int{Interactive: 0, Eval: 1, Training: 2},
	}, nil
}

// SetCapacity updates the slot count as hosts join or leave; running envs are never evicted.
func (controller *Controller) SetCapacity(capacity int) {
	controller.mu.Lock()
	defer controller.mu.Unlock()
	controller.config.Capacity = capacity
}

func (controller *Controller) totalInUse() int {
	total := 0
	for _, inUse := range controller.inUseByClass {
		total += inUse
	}
	return total
}

// sharedInUse counts envs running beyond their class's reservation.
func (controller *Controller) sharedInUse() int {
	shared := 0
	for priority, inUse := range controller.inUseByClass {
		if beyondReservation := inUse - controller.config.Reserved[priority]; beyondReservation > 0 {
			shared += beyondReservation
		}
	}
	return shared
}

func (controller *Controller) reservedTotal() int {
	total := 0
	for _, reserved := range controller.config.Reserved {
		total += reserved
	}
	return total
}

func (controller *Controller) tenantQuota(tenant string) int {
	if quota, found := controller.config.TenantQuotas[tenant]; found {
		return quota
	}
	return controller.config.DefaultTenantQuota
}

// TryAdmit counts the env in if every limit allows it; otherwise it returns the first limit that does not.
func (controller *Controller) TryAdmit(request Request) error {
	controller.mu.Lock()
	defer controller.mu.Unlock()
	if _, known := controller.knownPriorityRanks[request.Priority]; !known {
		return ErrUnknownPriority
	}
	if controller.totalInUse() >= controller.config.Capacity {
		return ErrFleetFull
	}
	withinReservation := controller.inUseByClass[request.Priority] < controller.config.Reserved[request.Priority]
	sharedCapacity := controller.config.Capacity - controller.reservedTotal()
	if !withinReservation && controller.sharedInUse() >= sharedCapacity {
		return ErrFleetFull
	}
	if quota := controller.tenantQuota(request.Tenant); quota > 0 && controller.inUseByTenant[request.Tenant] >= quota {
		return ErrTenantQuota
	}
	if budget, limited := controller.config.EndpointBudgets[request.Endpoint]; limited {
		if controller.inUseByEndpoint[request.Endpoint] >= budget.MaxEnvs() {
			return ErrNoInferenceHeadroom
		}
	}
	controller.inUseByClass[request.Priority]++
	controller.inUseByTenant[request.Tenant]++
	controller.inUseByEndpoint[request.Endpoint]++
	return nil
}

// Release returns the env's slot, quota and inference share.
func (controller *Controller) Release(request Request) {
	controller.mu.Lock()
	defer controller.mu.Unlock()
	decrement(controller.inUseByClass, request.Priority)
	decrement(controller.inUseByTenant, request.Tenant)
	decrement(controller.inUseByEndpoint, request.Endpoint)
}

func decrement[Key comparable](counts map[Key]int, key Key) {
	if counts[key] <= 1 {
		delete(counts, key)
		return
	}
	counts[key]--
}

// Usage returns a copy of what is in use.
func (controller *Controller) Usage() Usage {
	controller.mu.Lock()
	defer controller.mu.Unlock()
	return controller.usage()
}

func (controller *Controller) usage() Usage {
	return Usage{
		Total:      controller.totalInUse(),
		ByClass:    copyCounts(controller.inUseByClass),
		ByTenant:   copyCounts(controller.inUseByTenant),
		ByEndpoint: copyCounts(controller.inUseByEndpoint),
	}
}

// Status reports the limits together with the usage.
func (controller *Controller) Status() Status {
	controller.mu.Lock()
	defer controller.mu.Unlock()
	endpointMaxEnvs := make(map[string]int, len(controller.config.EndpointBudgets))
	for endpoint, budget := range controller.config.EndpointBudgets {
		endpointMaxEnvs[endpoint] = budget.MaxEnvs()
	}
	return Status{
		Capacity:           controller.config.Capacity,
		Reserved:           copyCounts(controller.config.Reserved),
		DefaultTenantQuota: controller.config.DefaultTenantQuota,
		TenantQuotas:       copyCounts(controller.config.TenantQuotas),
		EndpointMaxEnvs:    endpointMaxEnvs,
		Usage:              controller.usage(),
	}
}

// SetTenantQuota changes one tenant's concurrent env quota, zero meaning unlimited; running envs are never evicted.
func (controller *Controller) SetTenantQuota(tenant string, quota int) error {
	if tenant == "" || quota < 0 {
		return fmt.Errorf("a tenant quota needs a tenant and a quota of zero or more, got %q and %d", tenant, quota)
	}
	controller.mu.Lock()
	defer controller.mu.Unlock()
	if controller.config.TenantQuotas == nil {
		controller.config.TenantQuotas = map[string]int{}
	}
	controller.config.TenantQuotas[tenant] = quota
	return nil
}

func copyCounts[Key comparable](counts map[Key]int) map[Key]int {
	copied := make(map[Key]int, len(counts))
	for key, value := range counts {
		copied[key] = value
	}
	return copied
}
