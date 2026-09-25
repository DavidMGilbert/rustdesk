package main

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"strings"
	"time"
)

// Host wraps the YLTS Remote Host (branded RustDesk) command line.
type Host interface {
	ID() (string, error)
	SetPassword(pw string) error
	SetOption(key, value string) error
}

type rustdeskCLI struct {
	Exe string
}

var idPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{6,32}$`)

func (h rustdeskCLI) run(args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, h.Exe, args...)
	hideWindow(cmd)
	out, err := cmd.CombinedOutput()
	text := strings.TrimSpace(string(out))
	if ctx.Err() != nil {
		return text, fmt.Errorf("%s %s timed out", h.Exe, args[0])
	}
	return text, err
}

func (h rustdeskCLI) ID() (string, error) {
	out, err := h.run("--get-id")
	if err != nil {
		return "", fmt.Errorf("get-id: %v (%s)", err, out)
	}
	// Output can contain log noise; the ID is the last non-empty line.
	lines := strings.Split(strings.ReplaceAll(out, "\r", ""), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		l := strings.TrimSpace(lines[i])
		if idPattern.MatchString(l) {
			return l, nil
		}
	}
	return "", errors.New("host did not report an ID yet")
}

func (h rustdeskCLI) SetPassword(pw string) error {
	out, err := h.run("--password", pw)
	if err != nil || !strings.Contains(out, "Done") {
		return fmt.Errorf("set password failed: %v %s", err, out)
	}
	return nil
}

func (h rustdeskCLI) SetOption(key, value string) error {
	out, err := h.run("--option", key, value)
	if err != nil || strings.Contains(out, "disabled") || strings.Contains(out, "required") {
		return fmt.Errorf("set option %s failed: %v %s", key, err, out)
	}
	return nil
}
