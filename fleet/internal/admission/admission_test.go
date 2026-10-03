package admission

import (
	"errors"
	"testing"
)

func mustController(t *testing.T, config Config) *Controller {
	t.Helper()
	controller, err := NewController(config)
	if err != nil {
		t.Fatalf("new controller: %v", err)
	}
	return controller
}

func admitN(t *testing.T, controller *Controller, request Request, count int) {
	t.Helper()
	for index := 0; index < count; index++ {
		if err := controller.TryAdmit(request); err != nil {
			t.Fatalf("admit %d of %d: %v", index+1, count, err)
		}
	}
}

func TestReservationsHoldCapacityForHigherClasses(t *testing.T) {
	controller := mustController(t, Config{Capacity: 10, Reserved: map[Priority]int{Interactive: 2, Eval: 3}})
	training := Request{Tenant: "lab", Priority: Training}
	admitN(t, controller, training, 5)
	if err := controller.TryAdmit(training); !errors.Is(err, ErrFleetFull) {
		t.Fatalf("training beyond the shared capacity: got %v, want ErrFleetFull", err)
	}
	admitN(t, controller, Request{Tenant: "lab", Priority: Eval}, 3)
	admitN(t, controller, Request{Tenant: "lab", Priority: Interactive}, 2)
	if err := controller.TryAdmit(Request{Tenant: "lab", Priority: Interactive}); !errors.Is(err, ErrFleetFull) {
		t.Fatalf("a full fleet: got %v, want ErrFleetFull", err)
	}
}

func TestClassesBorrowFromTheSharedPoolOnly(t *testing.T) {
	controller := mustController(t, Config{Capacity: 10, Reserved: map[Priority]int{Interactive: 2, Eval: 3}})
	admitN(t, controller, Request{Tenant: "lab", Priority: Eval}, 8)
	if err := controller.TryAdmit(Request{Tenant: "lab", Priority: Eval}); !errors.Is(err, ErrFleetFull) {
		t.Fatalf("eval beyond its reservation and the shared pool: got %v", err)
	}
	if err := controller.TryAdmit(Request{Tenant: "lab", Priority: Interactive}); err != nil {
		t.Fatalf("interactive inside its reservation: %v", err)
	}
}

func TestTenantQuotas(t *testing.T) {
	controller := mustController(t, Config{Capacity: 100, TenantQuotas: map[string]int{"small": 2}, DefaultTenantQuota: 5})
	admitN(t, controller, Request{Tenant: "small", Priority: Training}, 2)
	if err := controller.TryAdmit(Request{Tenant: "small", Priority: Training}); !errors.Is(err, ErrTenantQuota) {
		t.Fatalf("got %v, want ErrTenantQuota", err)
	}
	admitN(t, controller, Request{Tenant: "other", Priority: Training}, 5)
	if err := controller.TryAdmit(Request{Tenant: "other", Priority: Training}); !errors.Is(err, ErrTenantQuota) {
		t.Fatalf("default quota: got %v, want ErrTenantQuota", err)
	}
}

func TestEndpointBudgetsCapEnvsByLittlesLaw(t *testing.T) {
	budget := EndpointBudget{TokensPerMinute: 2_000_000, TokensPerEnvMinute: 450_000}
	if budget.MaxEnvs() != 4 {
		t.Fatalf("max envs: got %d, want 4", budget.MaxEnvs())
	}
	controller := mustController(t, Config{Capacity: 100, EndpointBudgets: map[string]EndpointBudget{"frontier": budget}})
	frontierRequest := Request{Tenant: "lab", Priority: Eval, Endpoint: "frontier"}
	admitN(t, controller, frontierRequest, 4)
	if err := controller.TryAdmit(frontierRequest); !errors.Is(err, ErrNoInferenceHeadroom) {
		t.Fatalf("got %v, want ErrNoInferenceHeadroom", err)
	}
	admitN(t, controller, Request{Tenant: "lab", Priority: Eval, Endpoint: "self_hosted"}, 10)
}

func TestReleaseFreesEveryCounter(t *testing.T) {
	controller := mustController(t, Config{Capacity: 1, DefaultTenantQuota: 1})
	request := Request{Tenant: "lab", Priority: Training, Endpoint: "policy"}
	admitN(t, controller, request, 1)
	controller.Release(request)
	usage := controller.Usage()
	if usage.Total != 0 || len(usage.ByTenant) != 0 || len(usage.ByEndpoint) != 0 {
		t.Fatalf("usage after release: %+v", usage)
	}
	admitN(t, controller, request, 1)
}

func TestConfigAndRequestValidation(t *testing.T) {
	if _, err := NewController(Config{Capacity: 4, Reserved: map[Priority]int{Eval: 5}}); err == nil {
		t.Fatal("reservations above capacity must be rejected")
	}
	controller := mustController(t, Config{Capacity: 4})
	if err := controller.TryAdmit(Request{Tenant: "lab", Priority: "urgent"}); !errors.Is(err, ErrUnknownPriority) {
		t.Fatalf("got %v, want ErrUnknownPriority", err)
	}
	controller.SetCapacity(0)
	if err := controller.TryAdmit(Request{Tenant: "lab", Priority: Training}); !errors.Is(err, ErrFleetFull) {
		t.Fatalf("zero capacity: got %v", err)
	}
}

func TestStatusReportsLimitsAndQuotasChangeLive(t *testing.T) {
	controller := mustController(t, Config{Capacity: 10, Reserved: map[Priority]int{Interactive: 2},
		EndpointBudgets: map[string]EndpointBudget{"policy": {TokensPerMinute: 1000, TokensPerEnvMinute: 100}}})
	request := Request{Tenant: "lab", Priority: Training, Endpoint: "policy"}
	admitN(t, controller, request, 3)
	if err := controller.SetTenantQuota("lab", 3); err != nil {
		t.Fatalf("set quota: %v", err)
	}
	if err := controller.TryAdmit(request); !errors.Is(err, ErrTenantQuota) {
		t.Fatalf("admit past a lowered quota: got %v, want ErrTenantQuota", err)
	}
	status := controller.Status()
	if status.Capacity != 10 || status.Reserved[Interactive] != 2 || status.TenantQuotas["lab"] != 3 ||
		status.EndpointMaxEnvs["policy"] != 10 || status.Usage.ByTenant["lab"] != 3 || status.Usage.Total != 3 {
		t.Fatalf("unexpected status %+v", status)
	}
	if err := controller.SetTenantQuota("lab", -1); err == nil {
		t.Fatal("a negative quota was accepted")
	}
	if err := controller.SetTenantQuota("lab", 0); err != nil {
		t.Fatalf("clear quota: %v", err)
	}
	admitN(t, controller, request, 1)
}
