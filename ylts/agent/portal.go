package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type PasswordCmd struct {
	Password string `json:"password"`
	Version  int    `json:"version"`
}

type ClientConfig struct {
	Company      string `json:"company"`
	SupportPhone string `json:"support_phone"`
	SupportEmail string `json:"support_email"`
	SupportURL   string `json:"support_url"`
	ClientName   string `json:"client_name"`
	DeviceLabel  string `json:"device_label"`
}

type EnrolResponse struct {
	DeviceToken    string       `json:"device_token"`
	SetPassword    *PasswordCmd `json:"set_password"`
	CheckinSeconds int          `json:"checkin_seconds"`
	Config         ClientConfig `json:"config"`
}

type CheckinRequest struct {
	RustDeskID       string `json:"rustdesk_id"`
	Hostname         string `json:"hostname"`
	OSUsername       string `json:"os_username,omitempty"`
	AgentVersion     string `json:"agent_version"`
	AppliedVersion   int    `json:"applied_version"`
	ApprovalRequired bool   `json:"approval_required"`
}

type CheckinResponse struct {
	SetPassword    *PasswordCmd `json:"set_password"`
	CheckinSeconds int          `json:"checkin_seconds"`
	Sessions       int          `json:"sessions"`
	Config         ClientConfig `json:"config"`
}

// ErrUnauthorized means the portal no longer recognises this device's token.
var ErrUnauthorized = errors.New("portal rejected the device token")

type PortalError struct {
	Status  int
	Message string
}

func (e *PortalError) Error() string { return fmt.Sprintf("portal HTTP %d: %s", e.Status, e.Message) }

type Portal struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

func NewPortal(base, token string) *Portal {
	return &Portal{BaseURL: strings.TrimRight(base, "/"), Token: token, HTTP: &http.Client{Timeout: 20 * time.Second}}
}

func (p *Portal) post(path string, in, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, p.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "ylts-agent/"+Version)
	if p.Token != "" {
		req.Header.Set("Authorization", "Bearer "+p.Token)
	}
	resp, err := p.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		var e struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(raw, &e)
		if resp.StatusCode == http.StatusUnauthorized {
			return ErrUnauthorized
		}
		return &PortalError{Status: resp.StatusCode, Message: e.Error}
	}
	if out != nil {
		return json.Unmarshal(raw, out)
	}
	return nil
}

func (p *Portal) Enrol(token, id, hostname, osName, user string) (*EnrolResponse, error) {
	var r EnrolResponse
	err := p.post("/agent/enrol", map[string]string{
		"enrol_token": token, "rustdesk_id": id, "hostname": hostname, "os": osName,
		"os_username": user, "agent_version": Version,
	}, &r)
	return &r, err
}

func (p *Portal) Checkin(req CheckinRequest) (*CheckinResponse, error) {
	var r CheckinResponse
	err := p.post("/agent/checkin", req, &r)
	return &r, err
}

func (p *Portal) Ack(version int, ok bool, msg string) error {
	return p.post("/agent/ack", map[string]any{"version": version, "ok": ok, "error": msg}, nil)
}

func (p *Portal) Help(message, contact string) error {
	return p.post("/agent/help", map[string]string{"message": message, "contact": contact}, nil)
}
