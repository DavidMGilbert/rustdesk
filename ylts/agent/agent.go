package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Agent is the service's brain: enrol, check in, apply passwords and approval mode,
// forward help requests, and publish status for the tray. Everything platform specific
// is injected so it can be tested anywhere.
type Agent struct {
	Paths     Paths
	Host      Host
	NewPortal func(base, token string) *Portal
	Hostname  func() string
	OSName    func() string
	Log       *log.Logger
}

const (
	minInterval = 15 * time.Second
	maxInterval = 10 * time.Minute
)

func clampInterval(secs int) time.Duration {
	d := time.Duration(secs) * time.Second
	if d < minInterval {
		return minInterval
	}
	if d > maxInterval {
		return maxInterval
	}
	return d
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}

func (a *Agent) logf(format string, args ...any) {
	if a.Log != nil {
		a.Log.Printf(format, args...)
	}
}

func (a *Agent) applyConfig(st *Status, c ClientConfig) {
	st.Company, st.SupportPhone, st.SupportEmail, st.SupportURL = c.Company, c.SupportPhone, c.SupportEmail, c.SupportURL
	st.ClientName, st.DeviceLabel = c.ClientName, c.DeviceLabel
}

// Tick runs one cycle and returns how long to wait before the next one.
func (a *Agent) Tick() time.Duration {
	cfg, err := LoadConfig(a.Paths)
	st, _ := LoadStatus(a.Paths)
	st.HostExe = cfg.HostExe
	save := func(state, msg string) {
		st.State, st.Message = state, msg
		if err := SaveStatus(a.Paths, st); err != nil {
			a.logf("write status: %v", err)
		}
	}
	if err != nil {
		a.logf("read config: %v", err)
		save("error", "The YLTS agent's settings are damaged. Please reinstall YLTS Remote.")
		return maxInterval
	}
	if cfg.PortalURL == "" {
		save("error", "YLTS Remote is not configured. Please reinstall it.")
		return maxInterval
	}

	id, err := a.Host.ID()
	if err != nil {
		a.logf("host id: %v", err)
		save("starting", "Waiting for YLTS Remote Host to start…")
		return minInterval
	}
	st.RustDeskID = id

	// "Ask me before a technician connects": click-to-accept instead of password.
	approval := fileExists(a.Paths.ApprovalFlag())
	want := "password"
	if approval {
		want = "click"
	}
	if cfg.ApprovalMode != want {
		if err := a.Host.SetOption("approve-mode", want); err != nil {
			a.logf("approve-mode: %v", err)
		} else {
			cfg.ApprovalMode = want
			a.saveConfig(cfg)
			a.logf("approval mode set to %s", want)
		}
	}
	st.ApprovalRequired = cfg.ApprovalMode == "click"

	if cfg.DeviceToken == "" {
		if cfg.EnrolToken == "" {
			save("error", "This PC isn't registered with YLTS. Please contact YLTS to set it up again.")
			return maxInterval
		}
		portal := a.NewPortal(cfg.PortalURL, "")
		resp, err := portal.Enrol(cfg.EnrolToken, id, a.Hostname(), a.OSName(), "")
		if err != nil {
			a.logf("enrol: %v", err)
			var pe *PortalError
			if errors.As(err, &pe) && pe.Status == 403 {
				save("error", "The enrolment code was rejected ("+pe.Message+"). Please contact YLTS.")
				return maxInterval
			}
			save("enrolling", "Registering this PC with YLTS…")
			return time.Minute
		}
		cfg.DeviceToken, cfg.EnrolToken, cfg.AppliedVersion = resp.DeviceToken, "", 0
		a.saveConfig(cfg)
		a.logf("enrolled as %s", id)
		portal.Token = resp.DeviceToken
		if resp.SetPassword != nil {
			a.applyPassword(portal, &cfg, *resp.SetPassword)
		}
		a.applyConfig(&st, resp.Config)
		st.LastCheckin = time.Now()
		save("ready", "Ready for support")
		return clampInterval(resp.CheckinSeconds)
	}

	portal := a.NewPortal(cfg.PortalURL, cfg.DeviceToken)
	resp, err := portal.Checkin(CheckinRequest{
		RustDeskID: id, Hostname: a.Hostname(), AgentVersion: Version,
		AppliedVersion: cfg.AppliedVersion, ApprovalRequired: st.ApprovalRequired,
	})
	if errors.Is(err, ErrUnauthorized) {
		a.logf("device token rejected; this PC was removed from the portal")
		cfg.DeviceToken = ""
		a.saveConfig(cfg)
		save("error", "This PC was removed from YLTS Remote. Contact YLTS if you still need support.")
		return maxInterval
	}
	if err != nil {
		a.logf("checkin: %v", err)
		var pe *PortalError
		if errors.As(err, &pe) && pe.Status == 403 {
			save("error", "Remote support for this PC has been turned off by YLTS.")
			return maxInterval
		}
		save("offline", "Can't reach YLTS right now. Check your internet connection.")
		return time.Minute
	}
	if resp.SetPassword != nil {
		a.applyPassword(portal, &cfg, *resp.SetPassword)
	}
	a.forwardHelpRequests(portal)
	a.applyConfig(&st, resp.Config)
	st.Sessions = resp.Sessions
	st.LastCheckin = time.Now()
	msg := "Ready for support"
	if st.Sessions > 0 {
		msg = "A YLTS technician is connected"
	}
	save("ready", msg)
	return clampInterval(resp.CheckinSeconds)
}

func (a *Agent) saveConfig(cfg Config) {
	if err := SaveConfig(a.Paths, cfg); err != nil {
		a.logf("save config: %v", err)
	}
}

func (a *Agent) applyPassword(p *Portal, cfg *Config, cmd PasswordCmd) {
	err := a.Host.SetPassword(cmd.Password)
	msg := ""
	if err != nil {
		msg = err.Error()
		// Never log the password itself; host errors don't contain it.
		a.logf("apply password v%d failed: %s", cmd.Version, strings.ReplaceAll(msg, cmd.Password, "***"))
		msg = strings.ReplaceAll(msg, cmd.Password, "***")
	} else {
		cfg.AppliedVersion = cmd.Version
		a.saveConfig(*cfg)
		a.logf("password v%d applied", cmd.Version)
	}
	if ackErr := p.Ack(cmd.Version, err == nil, msg); ackErr != nil {
		a.logf("ack v%d: %v", cmd.Version, ackErr)
	}
}

type helpFile struct {
	Message string `json:"message"`
	Contact string `json:"contact"`
}

// forwardHelpRequests sends requests the tray dropped into the control folder.
func (a *Agent) forwardHelpRequests(p *Portal) {
	matches, _ := filepath.Glob(filepath.Join(a.Paths.ControlDir, "help-*.json"))
	sort.Strings(matches)
	for i, path := range matches {
		if i >= 5 {
			break
		}
		// The folder is user-writable: only read small regular files, never follow links.
		fi, err := os.Lstat(path)
		if err != nil || !fi.Mode().IsRegular() || fi.Size() > 16<<10 {
			_ = os.Remove(path)
			continue
		}
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		raw, _ := io.ReadAll(io.LimitReader(f, 16<<10))
		f.Close()
		var h helpFile
		if json.Unmarshal(raw, &h) != nil {
			_ = os.Remove(path)
			continue
		}
		if err := p.Help(truncate(h.Message, 2000), truncate(h.Contact, 200)); err != nil {
			a.logf("help request: %v", err)
			return // keep the file and retry next cycle
		}
		_ = os.Remove(path)
		a.logf("help request forwarded")
	}
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// QueueHelpRequest is used by the tray (running as the signed-in user).
func QueueHelpRequest(p Paths, message, contact string) error {
	if err := os.MkdirAll(p.ControlDir, 0o777); err != nil {
		return err
	}
	name := filepath.Join(p.ControlDir, fmt.Sprintf("help-%d.json", time.Now().UnixNano()))
	return writeJSON(name, helpFile{Message: message, Contact: contact}, 0o644)
}

func SetApprovalRequired(p Paths, on bool) error {
	if on {
		if err := os.MkdirAll(p.ControlDir, 0o777); err != nil {
			return err
		}
		return os.WriteFile(p.ApprovalFlag(), []byte("1"), 0o644)
	}
	err := os.Remove(p.ApprovalFlag())
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// Run loops until stop is closed. It wakes early when the tray asks for something,
// so ticking "ask me first" or sending a help request takes effect within seconds.
func (a *Agent) Run(stop <-chan struct{}) {
	for {
		wait := a.Tick()
		deadline := time.Now().Add(wait)
		flag := fileExists(a.Paths.ApprovalFlag())
		for time.Now().Before(deadline) {
			select {
			case <-stop:
				return
			case <-time.After(3 * time.Second):
			}
			help, _ := filepath.Glob(filepath.Join(a.Paths.ControlDir, "help-*.json"))
			if fileExists(a.Paths.ApprovalFlag()) != flag || len(help) > 0 {
				break
			}
		}
	}
}
