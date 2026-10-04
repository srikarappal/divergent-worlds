// Package egress is the forward proxy live internet eval goes through (DESIGN.md sections 6.6 and 7).
//
// An agent with a shell on the platform's addresses is the platform's legal
// exposure, so every request an env sends is checked against its allowlist,
// rate limited and logged as one JSON line. HTTPS travels as CONNECT tunnels,
// logged by host and bytes; plain HTTP is forwarded and logged with its path.
// Upstream addresses are checked after DNS resolution, so a name that resolves
// to a private, metadata or control plane address is refused even when the
// name is allowlisted. The log is the record of what envs did on the
// platform's addresses, so once a line cannot be written the proxy refuses
// everything.
package egress

import (
	"bufio"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// Decisions a record carries.
const (
	Allowed        = "allowed"
	NotAllowlisted = "not_allowlisted"
	PortNotAllowed = "port_not_allowed"
	RateLimited    = "rate_limited"
	TooManyTunnels = "too_many_tunnels"
	BlockedAddress = "blocked_address"
	UpstreamFailed = "upstream_failed"
	BadRequest     = "bad_request"
	LogUnavailable = "log_unavailable"
)

// dialTimeout bounds how long one upstream connection may take to open.
const dialTimeout = 15 * time.Second

// errBlockedAddress marks a dial refused because the resolved address is never reachable.
var errBlockedAddress = errors.New("the upstream address is not reachable from envs")

// hopHeaders are connection scoped and never forwarded.
var hopHeaders = []string{"Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization",
	"Proxy-Connection", "Te", "Trailer", "Transfer-Encoding", "Upgrade"}

// NonPublicPrefixes are never reachable through the proxy: this network, private, shared, loopback, link local
// (which holds the metadata service) and multicast ranges.
var NonPublicPrefixes = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"),
	netip.MustParsePrefix("10.0.0.0/8"),
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("127.0.0.0/8"),
	netip.MustParsePrefix("169.254.0.0/16"),
	netip.MustParsePrefix("172.16.0.0/12"),
	netip.MustParsePrefix("192.168.0.0/16"),
	netip.MustParsePrefix("224.0.0.0/4"),
	netip.MustParsePrefix("240.0.0.0/4"),
	netip.MustParsePrefix("::1/128"),
	netip.MustParsePrefix("fc00::/7"),
	netip.MustParsePrefix("fe80::/10"),
	netip.MustParsePrefix("ff00::/8"),
}

// Policy is what one env may reach and how fast.
type Policy struct {
	// Allowlist names hosts an env may reach; an entry also covers its subdomains.
	Allowlist []string
	// AllowAll lets any host through, for live mode; blocked addresses stay blocked.
	AllowAll bool
	// Ports an env may reach; empty allows every port.
	Ports []int
	// RequestsPerSecond and Burst bound new requests and tunnels; zero means unlimited.
	RequestsPerSecond float64
	Burst             int
	// MaxTunnels bounds open tunnels at once; zero means unlimited.
	MaxTunnels int
	// Blocked lists address ranges never dialed, checked after DNS resolution.
	Blocked []netip.Prefix
}

// Record is one logged request.
type Record struct {
	Time          time.Time `json:"time"`
	Env           string    `json:"env"`
	Method        string    `json:"method"`
	Host          string    `json:"host"`
	Port          int       `json:"port"`
	Path          string    `json:"path,omitempty"`
	Decision      string    `json:"decision"`
	Status        int       `json:"status"`
	BytesSent     int64     `json:"bytes_sent"`
	BytesReceived int64     `json:"bytes_received"`
	DurationMS    int64     `json:"duration_ms"`
}

// Proxy serves one env's egress.
type Proxy struct {
	env       string
	policy    Policy
	allowed   []string
	ports     map[int]bool
	bucket    *TokenBucket
	tunnels   chan struct{}
	dialer    *net.Dialer
	transport *http.Transport
	logMu     sync.Mutex
	log       *json.Encoder
	logFailed atomic.Bool
	now       func() time.Time
}

// New returns the proxy for env, logging one JSON line per request to log.
func New(env string, policy Policy, log io.Writer, now func() time.Time) *Proxy {
	proxy := &Proxy{env: env, policy: policy, ports: map[int]bool{}, log: json.NewEncoder(log), now: now,
		bucket: NewTokenBucket(policy.RequestsPerSecond, policy.Burst, now)}
	for _, entry := range policy.Allowlist {
		proxy.allowed = append(proxy.allowed, normalizedHost(entry))
	}
	for _, port := range policy.Ports {
		proxy.ports[port] = true
	}
	if policy.MaxTunnels > 0 {
		proxy.tunnels = make(chan struct{}, policy.MaxTunnels)
	}
	proxy.dialer = &net.Dialer{Timeout: dialTimeout, Control: proxy.checkAddress}
	proxy.transport = &http.Transport{DialContext: proxy.dialer.DialContext, ForceAttemptHTTP2: false,
		ResponseHeaderTimeout: time.Minute, MaxIdleConnsPerHost: 4}
	return proxy
}

func normalizedHost(host string) string {
	return strings.TrimSuffix(strings.ToLower(host), ".")
}

// HostAllowed reports whether the policy lets an env reach host.
func (proxy *Proxy) HostAllowed(host string) bool {
	if proxy.policy.AllowAll {
		return true
	}
	host = normalizedHost(host)
	for _, entry := range proxy.allowed {
		if host == entry || strings.HasSuffix(host, "."+entry) {
			return true
		}
	}
	return false
}

// AddressBlocked reports whether address is never dialed: it is not unicast or sits in a blocked range.
func (proxy *Proxy) AddressBlocked(address netip.Addr) bool {
	address = address.Unmap()
	if !address.IsValid() || address.IsUnspecified() || address.IsMulticast() {
		return true
	}
	for _, prefix := range proxy.policy.Blocked {
		if prefix.Contains(address) {
			return true
		}
	}
	return false
}

// checkAddress runs before every upstream connection, after DNS resolution picked the address.
func (proxy *Proxy) checkAddress(network, address string, connection syscall.RawConn) error {
	addressPort, err := netip.ParseAddrPort(address)
	if err != nil {
		return err
	}
	if proxy.AddressBlocked(addressPort.Addr()) {
		return errBlockedAddress
	}
	return nil
}

func (proxy *Proxy) admit(host string, port int) (string, int) {
	if proxy.logFailed.Load() {
		return LogUnavailable, http.StatusServiceUnavailable
	}
	if len(proxy.ports) > 0 && !proxy.ports[port] {
		return PortNotAllowed, http.StatusForbidden
	}
	if !proxy.HostAllowed(host) {
		return NotAllowlisted, http.StatusForbidden
	}
	if !proxy.bucket.Take() {
		return RateLimited, http.StatusTooManyRequests
	}
	return Allowed, 0
}

func (proxy *Proxy) write(record Record) {
	record.DurationMS = proxy.now().Sub(record.Time).Milliseconds()
	proxy.logMu.Lock()
	defer proxy.logMu.Unlock()
	if err := proxy.log.Encode(record); err != nil {
		proxy.logFailed.Store(true)
	}
}

func (proxy *Proxy) refuse(writer http.ResponseWriter, record Record, decision string, status int) {
	record.Decision, record.Status = decision, status
	http.Error(writer, "rlworld egress: "+decision, status)
	proxy.write(record)
}

func dialFailure(err error) (string, int) {
	if errors.Is(err, errBlockedAddress) {
		return BlockedAddress, http.StatusForbidden
	}
	return UpstreamFailed, http.StatusBadGateway
}

// ServeHTTP tunnels CONNECT requests and forwards plain HTTP requests in absolute form.
func (proxy *Proxy) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	if request.Method == http.MethodConnect {
		proxy.tunnel(writer, request)
		return
	}
	proxy.forward(writer, request)
}

func splitHostPort(hostPort string) (string, int, error) {
	host, portText, err := net.SplitHostPort(hostPort)
	if err != nil {
		return "", 0, err
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 {
		return "", 0, errors.New("bad port")
	}
	return host, port, nil
}

func (proxy *Proxy) acquireTunnel() bool {
	if proxy.tunnels == nil {
		return true
	}
	select {
	case proxy.tunnels <- struct{}{}:
		return true
	default:
		return false
	}
}

func (proxy *Proxy) releaseTunnel() {
	if proxy.tunnels != nil {
		<-proxy.tunnels
	}
}

func (proxy *Proxy) tunnel(writer http.ResponseWriter, request *http.Request) {
	record := Record{Time: proxy.now(), Env: proxy.env, Method: http.MethodConnect, Host: request.Host}
	host, port, err := splitHostPort(request.Host)
	if err != nil {
		proxy.refuse(writer, record, BadRequest, http.StatusBadRequest)
		return
	}
	record.Host, record.Port = host, port
	if decision, status := proxy.admit(host, port); decision != Allowed {
		proxy.refuse(writer, record, decision, status)
		return
	}
	if !proxy.acquireTunnel() {
		proxy.refuse(writer, record, TooManyTunnels, http.StatusTooManyRequests)
		return
	}
	defer proxy.releaseTunnel()
	upstream, err := proxy.dialer.DialContext(request.Context(), "tcp", net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		decision, status := dialFailure(err)
		proxy.refuse(writer, record, decision, status)
		return
	}
	defer upstream.Close()
	hijacker, canHijack := writer.(http.Hijacker)
	if !canHijack {
		proxy.refuse(writer, record, BadRequest, http.StatusInternalServerError)
		return
	}
	client, buffered, err := hijacker.Hijack()
	if err != nil {
		return
	}
	defer client.Close()
	if _, err := client.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n")); err != nil {
		return
	}
	sent, received := pipeBothWays(client, buffered, upstream)
	record.Decision, record.Status, record.BytesSent, record.BytesReceived = Allowed, http.StatusOK, sent, received
	proxy.write(record)
}

// copier copies one direction of a tunnel, then half closes its destination so the far side sees the end.
type copier struct {
	destination net.Conn
	source      io.Reader
	copied      int64
	done        chan struct{}
}

func (direction *copier) run() {
	direction.copied, _ = io.Copy(direction.destination, direction.source)
	if halfCloser, canHalfClose := direction.destination.(interface{ CloseWrite() error }); canHalfClose {
		if err := halfCloser.CloseWrite(); err != nil {
			direction.destination.Close()
		}
	} else {
		direction.destination.Close()
	}
	close(direction.done)
}

// pipeBothWays copies until both directions end and returns the bytes sent upstream and received from it.
func pipeBothWays(client net.Conn, buffered *bufio.ReadWriter, upstream net.Conn) (int64, int64) {
	outbound := &copier{destination: upstream, source: buffered.Reader, done: make(chan struct{})}
	inbound := &copier{destination: client, source: upstream, done: make(chan struct{})}
	go outbound.run()
	go inbound.run()
	<-outbound.done
	<-inbound.done
	return outbound.copied, inbound.copied
}

// countingReader counts the request body bytes sent upstream.
type countingReader struct {
	reader io.ReadCloser
	count  int64
}

func (counter *countingReader) Read(buffer []byte) (int, error) {
	read, err := counter.reader.Read(buffer)
	counter.count += int64(read)
	return read, err
}

func (counter *countingReader) Close() error {
	return counter.reader.Close()
}

func removeHopHeaders(header http.Header) {
	for _, name := range hopHeaders {
		header.Del(name)
	}
}

func (proxy *Proxy) forward(writer http.ResponseWriter, request *http.Request) {
	record := Record{Time: proxy.now(), Env: proxy.env, Method: request.Method, Host: request.URL.Hostname(),
		Path: request.URL.Path}
	if !request.URL.IsAbs() || request.URL.Scheme != "http" || request.URL.Hostname() == "" {
		proxy.refuse(writer, record, BadRequest, http.StatusBadRequest)
		return
	}
	record.Port = 80
	if portText := request.URL.Port(); portText != "" {
		port, err := strconv.Atoi(portText)
		if err != nil {
			proxy.refuse(writer, record, BadRequest, http.StatusBadRequest)
			return
		}
		record.Port = port
	}
	if decision, status := proxy.admit(record.Host, record.Port); decision != Allowed {
		proxy.refuse(writer, record, decision, status)
		return
	}
	outbound := request.Clone(request.Context())
	outbound.RequestURI = ""
	removeHopHeaders(outbound.Header)
	body := &countingReader{reader: http.NoBody}
	if request.Body != nil {
		body.reader = request.Body
	}
	outbound.Body = body
	response, err := proxy.transport.RoundTrip(outbound)
	if err != nil {
		decision, status := dialFailure(err)
		proxy.refuse(writer, record, decision, status)
		return
	}
	defer response.Body.Close()
	removeHopHeaders(response.Header)
	for name, values := range response.Header {
		for _, value := range values {
			writer.Header().Add(name, value)
		}
	}
	writer.WriteHeader(response.StatusCode)
	received, _ := io.Copy(writer, response.Body)
	record.Decision, record.Status, record.BytesSent, record.BytesReceived = Allowed, response.StatusCode,
		body.count, received
	proxy.write(record)
}

// TokenBucket admits Rate events per second with bursts of Burst; a zero rate admits everything.
type TokenBucket struct {
	mu     sync.Mutex
	rate   float64
	burst  float64
	tokens float64
	last   time.Time
	now    func() time.Time
}

// NewTokenBucket starts full.
func NewTokenBucket(rate float64, burst int, now func() time.Time) *TokenBucket {
	capacity := float64(max(burst, 1))
	return &TokenBucket{rate: rate, burst: capacity, tokens: capacity, last: now(), now: now}
}

// Take spends one token, or reports that none is left.
func (bucket *TokenBucket) Take() bool {
	if bucket.rate <= 0 {
		return true
	}
	bucket.mu.Lock()
	defer bucket.mu.Unlock()
	current := bucket.now()
	bucket.tokens = min(bucket.burst, bucket.tokens+current.Sub(bucket.last).Seconds()*bucket.rate)
	bucket.last = current
	if bucket.tokens < 1 {
		return false
	}
	bucket.tokens--
	return true
}
