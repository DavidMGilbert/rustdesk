package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"
)

// Version is overridden at build time with -ldflags "-X main.Version=1.2.3".
var Version = "1.0.0"

// Paths groups every file the agent touches so tests can redirect them.
type Paths struct {
	Dir        string // C:\ProgramData\YLTS
	Config     string // agent.json (SYSTEM/Administrators only: holds the device token)
	Status     string // status.json (readable by users: shown in the tray)
	ControlDir string // control\ (writable by users: tray requests)
}

func NewPaths(dir string) Paths {
	return Paths{
		Dir:        dir,
		Config:     filepath.Join(dir, "agent.json"),
		Status:     filepath.Join(dir, "status.json"),
		ControlDir: filepath.Join(dir, "control"),
	}
}

// ApprovalFlag exists while the signed-in user wants to approve each session on screen.
func (p Paths) ApprovalFlag() string { return filepath.Join(p.ControlDir, "approval-required.flag") }

// Config is the agent's private state.
type Config struct {
	PortalURL      string `json:"portal_url"`
	EnrolToken     string `json:"enrol_token,omitempty"` // cleared after a successful enrolment
	DeviceToken    string `json:"device_token,omitempty"`
	HostExe        string `json:"host_exe"`
	AppliedVersion int    `json:"applied_version"`
	ApprovalMode   string `json:"approval_mode_applied,omitempty"` // "click" or "password"
}

// Status is what the tray shows.
type Status struct {
	State            string    `json:"state"` // starting, enrolling, ready, error
	Message          string    `json:"message"`
	RustDeskID       string    `json:"rustdesk_id"`
	Company          string    `json:"company"`
	SupportPhone     string    `json:"support_phone"`
	SupportEmail     string    `json:"support_email"`
	SupportURL       string    `json:"support_url"`
	ClientName       string    `json:"client_name"`
	DeviceLabel      string    `json:"device_label"`
	ApprovalRequired bool      `json:"approval_required"`
	Sessions         int       `json:"sessions"`
	LastCheckin      time.Time `json:"last_checkin"`
	HostExe          string    `json:"host_exe"`
	AgentVersion     string    `json:"agent_version"`
	UpdatedAt        time.Time `json:"updated_at"`
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

// writeJSON writes atomically so the tray never reads a half-written file.
func writeJSON(path string, v any, perm os.FileMode) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, perm); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func LoadConfig(p Paths) (Config, error) {
	var c Config
	err := readJSON(p.Config, &c)
	if errors.Is(err, os.ErrNotExist) {
		return c, nil
	}
	return c, err
}

func SaveConfig(p Paths, c Config) error {
	if err := writeJSON(p.Config, c, 0o600); err != nil {
		return err
	}
	return restrictToAdmins(p.Config)
}

func LoadStatus(p Paths) (Status, error) {
	var s Status
	err := readJSON(p.Status, &s)
	return s, err
}

func SaveStatus(p Paths, s Status) error {
	s.UpdatedAt = time.Now()
	s.AgentVersion = Version
	return writeJSON(p.Status, s, 0o644)
}
