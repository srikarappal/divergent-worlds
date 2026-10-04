package egress

import (
	"bufio"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeClock moves only when a test says so.
type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 3, 14, 9, 0, 0, 0, time.UTC)}
}

func (clock *fakeClock) Now() time.Time {
	clock.mu.Lock()
	defer clock.mu.Unlock()
	return clock.now
}

func (clock *fakeClock) advance(duration time.Duration) {
	clock.mu.Lock()
	defer clock.mu.Unlock()
	clock.now = clock.now.Add(duration)
}

// recordLog hands every logged record to the test as it is written.
type recordLog struct {
	records chan Record
}

func newRecordLog() *recordLog {
	return &recordLog{records: make(chan Record, 16)}
}

func (log *recordLog) Write(data []byte) (int, error) {
	var record Record
	if err := json.Unmarshal(data, &record); err != nil {
		return 0, err
	}
	log.records <- record
	return len(data), nil
}

func (log *recordLog) next(t *testing.T) Record {
	t.Helper()
	select {
	case record := <-log.records:
		return record
	case <-time.After(5 * time.Second):
		t.Fatal("no record was logged")
		return Record{}
	}
}

// failingLog can never be written.
type failingLog struct{}

func (failingLog) Write(data []byte) (int, error) {
	return 0, errors.New("disk full")
}

func serveEcho(listener net.Listener) {
	for {
		connection, err := listener.Accept()
		if err != nil {
			return
		}
		go echo(connection)
	}
}

func echo(connection net.Conn) {
	defer connection.Close()
	if _, err := io.Copy(connection, connection); err != nil {
		return
	}
}

func startEcho(t *testing.T) (string, int) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(closer{listener}.close)
	go serveEcho(listener)
	address := listener.Addr().(*net.TCPAddr)
	return "127.0.0.1", address.Port
}

// closer adapts a Close method to the cleanup signature.
type closer struct {
	target io.Closer
}

func (wrapped closer) close() {
	wrapped.target.Close()
}

func portOf(t *testing.T, rawURL string) int {
	t.Helper()
	parsed, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(parsed.Port())
	if err != nil {
		t.Fatal(err)
	}
	return port
}

func startProxy(t *testing.T, policy Policy, log io.Writer, clock *fakeClock) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(New("env_000001", policy, log, clock.Now))
	t.Cleanup(server.Close)
	return server
}

// connect opens a tunnel through the proxy and returns the connection with the status line it got.
func connect(t *testing.T, proxyURL string, target string) (net.Conn, *bufio.Reader, string) {
	t.Helper()
	connection, err := net.Dial("tcp", strings.TrimPrefix(proxyURL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	requestLine := "CONNECT " + target + " HTTP/1.1\r\nHost: " + target + "\r\n\r\n"
	if _, err := connection.Write([]byte(requestLine)); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(connection)
	statusLine, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	return connection, reader, strings.TrimSpace(statusLine)
}

func TestTunnelsCarryBytesBothWaysAndLogThem(t *testing.T) {
	host, port := startEcho(t)
	log := newRecordLog()
	proxy := startProxy(t, Policy{AllowAll: true, Ports: []int{port}}, log, newFakeClock())
	connection, reader, status := connect(t, proxy.URL, net.JoinHostPort(host, strconv.Itoa(port)))
	defer connection.Close()
	if status != "HTTP/1.1 200 Connection Established" {
		t.Fatalf("status %q", status)
	}
	if blank, err := reader.ReadString('\n'); err != nil || blank != "\r\n" {
		t.Fatalf("after the status line %q, %v", blank, err)
	}
	if _, err := connection.Write([]byte("hello\n")); err != nil {
		t.Fatal(err)
	}
	reply, err := reader.ReadString('\n')
	if err != nil || reply != "hello\n" {
		t.Fatalf("echo %q, %v", reply, err)
	}
	if err := connection.(*net.TCPConn).CloseWrite(); err != nil {
		t.Fatal(err)
	}
	record := log.next(t)
	if record.Decision != Allowed || record.Method != "CONNECT" || record.Host != host || record.Port != port {
		t.Fatalf("record %+v", record)
	}
	if record.BytesSent != 6 || record.BytesReceived != 6 || record.Status != http.StatusOK ||
		record.Env != "env_000001" {
		t.Fatalf("record %+v", record)
	}
}

// backendHandler answers plain HTTP and reports what reached it.
type backendHandler struct {
	seen chan *http.Request
	body chan string
}

func (handler backendHandler) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	payload, err := io.ReadAll(request.Body)
	if err != nil {
		http.Error(writer, err.Error(), http.StatusBadRequest)
		return
	}
	handler.seen <- request
	handler.body <- string(payload)
	writer.Header().Set("Connection", "close")
	writer.Header().Set("X-Backend", "clone")
	writer.WriteHeader(http.StatusCreated)
	if _, err := writer.Write([]byte("stored")); err != nil {
		return
	}
}

func proxiedClient(t *testing.T, proxyURL string) *http.Client {
	t.Helper()
	parsed, err := url.Parse(proxyURL)
	if err != nil {
		t.Fatal(err)
	}
	return &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(parsed)}, Timeout: 10 * time.Second}
}

func TestPlainHTTPIsForwardedWithoutHopHeadersAndLoggedWithItsPath(t *testing.T) {
	handler := backendHandler{seen: make(chan *http.Request, 1), body: make(chan string, 1)}
	backend := httptest.NewServer(handler)
	defer backend.Close()
	log := newRecordLog()
	proxy := startProxy(t, Policy{Allowlist: []string{"127.0.0.1"}, Ports: []int{portOf(t, backend.URL)}}, log,
		newFakeClock())
	request, err := http.NewRequest(http.MethodPost, backend.URL+"/invoices/7?status=open",
		strings.NewReader("total=1250"))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Proxy-Authorization", "Basic c2VjcmV0")
	response, err := proxiedClient(t, proxy.URL).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusCreated || string(payload) != "stored" ||
		response.Header.Get("X-Backend") != "clone" {
		t.Fatalf("response %d %q %v", response.StatusCode, payload, response.Header)
	}
	seen := <-handler.seen
	if seen.Header.Get("Proxy-Authorization") != "" || seen.URL.RawQuery != "status=open" ||
		<-handler.body != "total=1250" {
		t.Fatalf("backend saw %v %q", seen.Header, seen.URL.RawQuery)
	}
	record := log.next(t)
	if record.Decision != Allowed || record.Method != http.MethodPost || record.Path != "/invoices/7" {
		t.Fatalf("record %+v", record)
	}
	if record.Status != http.StatusCreated || record.BytesSent != 10 || record.BytesReceived != 6 {
		t.Fatalf("record %+v", record)
	}
}

func TestRequestsOutsideThePolicyAreRefusedAndLogged(t *testing.T) {
	host, port := startEcho(t)
	target := net.JoinHostPort(host, strconv.Itoa(port))
	cases := []struct {
		name     string
		policy   Policy
		target   string
		status   int
		decision string
	}{
		{"not allowlisted", Policy{Allowlist: []string{"docs.vendor.example"}}, target, 403, NotAllowlisted},
		{"port not allowed", Policy{AllowAll: true, Ports: []int{443}}, target, 403, PortNotAllowed},
		{"metadata and loopback", Policy{AllowAll: true, Blocked: NonPublicPrefixes}, target, 403, BlockedAddress},
		{"no port", Policy{AllowAll: true}, "127.0.0.1", 400, BadRequest},
	}
	for _, testCase := range cases {
		log := newRecordLog()
		proxy := startProxy(t, testCase.policy, log, newFakeClock())
		connection, _, status := connect(t, proxy.URL, testCase.target)
		connection.Close()
		if !strings.Contains(status, strconv.Itoa(testCase.status)) {
			t.Fatalf("%s: status %q", testCase.name, status)
		}
		if record := log.next(t); record.Decision != testCase.decision || record.Status != testCase.status {
			t.Fatalf("%s: record %+v", testCase.name, record)
		}
	}
}

func TestUpstreamsThatDoNotAnswerAreBadGateways(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	listener.Close()
	log := newRecordLog()
	proxy := startProxy(t, Policy{AllowAll: true}, log, newFakeClock())
	connection, _, status := connect(t, proxy.URL, "127.0.0.1:"+strconv.Itoa(port))
	connection.Close()
	if !strings.Contains(status, "502") || log.next(t).Decision != UpstreamFailed {
		t.Fatalf("status %q", status)
	}
}

func TestRequestRatesAndOpenTunnelsAreLimited(t *testing.T) {
	host, port := startEcho(t)
	target := net.JoinHostPort(host, strconv.Itoa(port))
	clock := newFakeClock()
	log := newRecordLog()
	proxy := startProxy(t, Policy{AllowAll: true, RequestsPerSecond: 1, Burst: 1, MaxTunnels: 1}, log, clock)
	open, _, status := connect(t, proxy.URL, target)
	defer open.Close()
	if !strings.Contains(status, "200") {
		t.Fatalf("first tunnel %q", status)
	}
	limited, _, status := connect(t, proxy.URL, target)
	limited.Close()
	if !strings.Contains(status, "429") || log.next(t).Decision != RateLimited {
		t.Fatalf("second tunnel %q", status)
	}
	clock.advance(time.Second)
	crowded, _, status := connect(t, proxy.URL, target)
	crowded.Close()
	if !strings.Contains(status, "429") || log.next(t).Decision != TooManyTunnels {
		t.Fatalf("third tunnel %q", status)
	}
}

func TestOnlyAbsoluteHTTPRequestsAreForwarded(t *testing.T) {
	log := newRecordLog()
	proxy := startProxy(t, Policy{AllowAll: true}, log, newFakeClock())
	response, err := http.Get(proxy.URL + "/direct")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusBadRequest || log.next(t).Decision != BadRequest {
		t.Fatalf("direct request got %d", response.StatusCode)
	}
}

func TestBlockedAddressesAreRefusedAfterResolution(t *testing.T) {
	handler := backendHandler{seen: make(chan *http.Request, 1), body: make(chan string, 1)}
	backend := httptest.NewServer(handler)
	defer backend.Close()
	log := newRecordLog()
	policy := Policy{AllowAll: true, Blocked: []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8")}}
	proxy := startProxy(t, policy, log, newFakeClock())
	response, err := proxiedClient(t, proxy.URL).Get(backend.URL + "/latest/meta-data")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusForbidden || log.next(t).Decision != BlockedAddress || len(handler.seen) != 0 {
		t.Fatalf("blocked backend answered %d", response.StatusCode)
	}
}

func TestAProxyThatCannotLogRefusesEverything(t *testing.T) {
	host, port := startEcho(t)
	target := net.JoinHostPort(host, strconv.Itoa(port))
	proxy := startProxy(t, Policy{Allowlist: []string{"docs.vendor.example"}}, failingLog{}, newFakeClock())
	first, _, status := connect(t, proxy.URL, target)
	first.Close()
	if !strings.Contains(status, "403") {
		t.Fatalf("first request %q", status)
	}
	second, _, status := connect(t, proxy.URL, "docs.vendor.example:443")
	second.Close()
	if !strings.Contains(status, "503") {
		t.Fatalf("after a failed log write %q", status)
	}
}

func TestAllowlistEntriesCoverTheirSubdomainsOnly(t *testing.T) {
	proxy := New("env", Policy{Allowlist: []string{"Docs.Vendor.example."}}, io.Discard, time.Now)
	for host, allowed := range map[string]bool{
		"docs.vendor.example": true, "api.docs.vendor.example": true, "DOCS.vendor.example.": true,
		"evildocs.vendor.example": false, "vendor.example": false, "docs.vendor.example.attacker.example": false,
	} {
		if proxy.HostAllowed(host) != allowed {
			t.Fatalf("%s allowed should be %v", host, allowed)
		}
	}
	if !New("env", Policy{AllowAll: true}, io.Discard, time.Now).HostAllowed("anything.example") {
		t.Fatal("live mode lets any host through")
	}
}

func TestNonPublicAndControlPlaneAddressesAreBlocked(t *testing.T) {
	controlPlane := netip.MustParsePrefix("203.0.113.0/24")
	proxy := New("env", Policy{AllowAll: true, Blocked: append(NonPublicPrefixes, controlPlane)}, io.Discard,
		time.Now)
	for address, blocked := range map[string]bool{
		"169.254.169.254": true, "10.1.2.3": true, "172.20.0.1": true, "192.168.1.1": true, "127.0.0.1": true,
		"100.64.0.9": true, "0.0.0.0": true, "224.0.0.1": true, "::1": true, "fd00::1": true, "fe80::1": true,
		"::ffff:10.0.0.1": true, "203.0.113.7": true, "8.8.8.8": false, "2606:4700::1111": false,
	} {
		if proxy.AddressBlocked(netip.MustParseAddr(address)) != blocked {
			t.Fatalf("%s blocked should be %v", address, blocked)
		}
	}
	if !New("env", Policy{AllowAll: true}, io.Discard, time.Now).AddressBlocked(netip.Addr{}) {
		t.Fatal("an invalid address is never dialed")
	}
}

func TestTokenBucketsRefillAtTheirRate(t *testing.T) {
	clock := newFakeClock()
	bucket := NewTokenBucket(2, 2, clock.Now)
	if !bucket.Take() || !bucket.Take() || bucket.Take() {
		t.Fatal("a full bucket holds its burst")
	}
	clock.advance(500 * time.Millisecond)
	if !bucket.Take() || bucket.Take() {
		t.Fatal("half a second at two per second refills one")
	}
	clock.advance(time.Hour)
	if !bucket.Take() || !bucket.Take() || bucket.Take() {
		t.Fatal("refills stop at the burst")
	}
	unlimited := NewTokenBucket(0, 0, clock.Now)
	for range 100 {
		if !unlimited.Take() {
			t.Fatal("a zero rate admits everything")
		}
	}
}
