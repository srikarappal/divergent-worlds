// Command egressproxy is the logging, rate limited forward proxy live internet eval goes through
// (DESIGN.md section 6.6).
//
// One process serves one env: the node agent starts it beside the env's
// namespace with the allowlist and limits of the env's network plan; its log
// is the record of everything the env reached. Private, shared, loopback,
// link local and multicast ranges are always blocked; -block adds the control
// plane's ranges.
package main

import (
	"errors"
	"flag"
	"io"
	"log"
	"net/http"
	"net/netip"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"rlworld/fleet/internal/egress"
)

const readHeaderTimeout = 30 * time.Second

type options struct {
	listen  string
	env     string
	logPath string
	policy  egress.Policy
}

func splitList(text string) []string {
	var items []string
	for _, item := range strings.Split(text, ",") {
		if trimmed := strings.TrimSpace(item); trimmed != "" {
			items = append(items, trimmed)
		}
	}
	return items
}

func parsePorts(text string) ([]int, error) {
	var ports []int
	for _, item := range splitList(text) {
		port, err := strconv.Atoi(item)
		if err != nil || port < 1 || port > 65535 {
			return nil, errors.New("bad port " + strconv.Quote(item))
		}
		ports = append(ports, port)
	}
	return ports, nil
}

func parseBlocked(text string) ([]netip.Prefix, error) {
	blocked := slices.Clone(egress.NonPublicPrefixes)
	for _, item := range splitList(text) {
		prefix, err := netip.ParsePrefix(item)
		if err != nil {
			return nil, err
		}
		blocked = append(blocked, prefix.Masked())
	}
	return blocked, nil
}

func parseOptions(arguments []string, output io.Writer) (options, error) {
	flags := flag.NewFlagSet("egressproxy", flag.ContinueOnError)
	flags.SetOutput(output)
	listen := flags.String("listen", ":3128", "address to listen on")
	env := flags.String("env", "", "the env whose egress this is")
	allow := flags.String("allow", "", "comma separated hosts the env may reach, with their subdomains")
	allowAll := flags.Bool("allow-all", false, "let any public host through, for live mode")
	ports := flags.String("ports", "80,443", "comma separated ports the env may reach")
	rate := flags.Float64("rate", 10, "new requests and tunnels per second")
	burst := flags.Int("burst", 20, "requests allowed at once above the rate")
	maxTunnels := flags.Int("max-tunnels", 64, "tunnels open at once")
	block := flags.String("block", "", "comma separated CIDRs never dialed besides the non public ranges")
	logPath := flags.String("log", "", "file the JSON request log is appended to; standard output when empty")
	if err := flags.Parse(arguments); err != nil {
		return options{}, err
	}
	if *env == "" {
		return options{}, errors.New("-env names the env this proxy serves")
	}
	allowlist := splitList(*allow)
	if (len(allowlist) > 0) == *allowAll {
		return options{}, errors.New("give either -allow hosts for allowlist mode or -allow-all for live mode")
	}
	portList, err := parsePorts(*ports)
	if err != nil {
		return options{}, err
	}
	blocked, err := parseBlocked(*block)
	if err != nil {
		return options{}, err
	}
	if *rate < 0 || *burst < 0 || *maxTunnels < 0 {
		return options{}, errors.New("-rate, -burst and -max-tunnels may not be negative")
	}
	return options{listen: *listen, env: *env, logPath: *logPath, policy: egress.Policy{
		Allowlist: allowlist, AllowAll: *allowAll, Ports: portList, RequestsPerSecond: *rate, Burst: *burst,
		MaxTunnels: *maxTunnels, Blocked: blocked}}, nil
}

func openLog(path string) (io.Writer, error) {
	if path == "" {
		return os.Stdout, nil
	}
	return os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
}

func main() {
	parsed, err := parseOptions(os.Args[1:], os.Stderr)
	if err != nil {
		log.Fatal(err)
	}
	requestLog, err := openLog(parsed.logPath)
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{Addr: parsed.listen, ReadHeaderTimeout: readHeaderTimeout,
		Handler: egress.New(parsed.env, parsed.policy, requestLog, time.Now)}
	log.Fatal(server.ListenAndServe())
}
