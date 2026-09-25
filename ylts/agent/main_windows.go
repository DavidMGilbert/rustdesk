//go:build windows

package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
)

const usage = `YLTS Agent %s

Usage:
  ylts-agent                      start the notification-area icon (normal use)
  ylts-agent install --portal URL [--enrol TOKEN] [--host-exe PATH]
                                  configure and install the background service (admin)
  ylts-agent enrol TOKEN          register this PC again with a new enrolment token (admin)
  ylts-agent uninstall [--purge]  remove the background service (admin)
  ylts-agent status               show the current status
  ylts-agent service              run the service in this console (for troubleshooting)
`

func main() {
	paths := NewPaths(defaultDataDir())
	args := os.Args[1:]
	if len(args) == 0 || args[0] == "tray" {
		runTray(paths)
		return
	}
	attachParentConsole()
	var err error
	switch args[0] {
	case "service":
		err = runService(paths)
	case "install":
		err = cmdInstall(paths, args[1:])
	case "enrol", "enroll":
		if len(args) < 2 {
			err = fmt.Errorf("usage: ylts-agent enrol TOKEN")
			break
		}
		err = cmdEnrol(paths, args[1])
	case "uninstall":
		err = cmdUninstall(paths, len(args) > 1 && args[1] == "--purge")
	case "status":
		err = cmdStatus(paths)
	case "version", "--version", "-v":
		fmt.Println(Version)
	default:
		fmt.Printf(usage, Version)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
}

func openLog(paths Paths) *log.Logger {
	_ = os.MkdirAll(paths.Dir, 0o755)
	logPath := filepath.Join(paths.Dir, "agent.log")
	if fi, err := os.Stat(logPath); err == nil && fi.Size() > 2<<20 {
		_ = os.Rename(logPath, logPath+".old")
	}
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return log.New(os.Stderr, "", log.LstdFlags)
	}
	return log.New(f, "", log.LstdFlags)
}

func newAgent(paths Paths, lg *log.Logger) *Agent {
	cfg, _ := LoadConfig(paths)
	exe := cfg.HostExe
	if exe == "" {
		exe = defaultHostExe()
	}
	return &Agent{
		Paths:     paths,
		Host:      rustdeskCLI{Exe: exe},
		NewPortal: NewPortal,
		Hostname:  func() string { h, _ := os.Hostname(); return h },
		OSName:    osName,
		Log:       lg,
	}
}

func runService(paths Paths) error {
	lg := openLog(paths)
	isSvc, err := svc.IsWindowsService()
	if err != nil {
		return err
	}
	agent := newAgent(paths, lg)
	if !isSvc {
		lg.SetOutput(os.Stdout)
		fmt.Println("Running in the console. Press Ctrl+C to stop.")
		agent.Run(make(chan struct{}))
		return nil
	}
	lg.Printf("YLTS Agent %s service starting", Version)
	return svc.Run(serviceName, &serviceHandler{agent: agent})
}

func cmdInstall(paths Paths, args []string) error {
	fs := flag.NewFlagSet("install", flag.ContinueOnError)
	portal := fs.String("portal", "", "portal URL, e.g. https://remote.ylts.com.au")
	enrol := fs.String("enrol", "", "enrolment token from the YLTS Portal")
	hostExe := fs.String("host-exe", "", "path to YLTS Remote Host.exe")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := os.MkdirAll(paths.ControlDir, 0o755); err != nil {
		return err
	}
	if err := allowUsersModify(paths.ControlDir); err != nil {
		return fmt.Errorf("set permissions on %s: %w", paths.ControlDir, err)
	}
	cfg, err := LoadConfig(paths)
	if err != nil {
		cfg = Config{}
	}
	if *portal != "" {
		if !strings.HasPrefix(*portal, "https://") && !strings.HasPrefix(*portal, "http://localhost") {
			return fmt.Errorf("the portal URL must start with https://")
		}
		cfg.PortalURL = strings.TrimRight(*portal, "/")
	}
	if cfg.PortalURL == "" {
		return fmt.Errorf("--portal is required")
	}
	if *enrol != "" {
		cfg.EnrolToken = strings.TrimSpace(*enrol)
		cfg.DeviceToken = "" // re-register with the new token
	}
	if *hostExe != "" {
		cfg.HostExe = *hostExe
	} else if cfg.HostExe == "" {
		cfg.HostExe = defaultHostExe()
	}
	if err := SaveConfig(paths, cfg); err != nil {
		return err
	}
	if err := installService(); err != nil {
		return err
	}
	fmt.Println("YLTS Agent service installed and started.")
	return nil
}

func cmdEnrol(paths Paths, token string) error {
	cfg, err := LoadConfig(paths)
	if err != nil {
		return err
	}
	cfg.EnrolToken, cfg.DeviceToken = strings.TrimSpace(token), ""
	if err := SaveConfig(paths, cfg); err != nil {
		return err
	}
	if err := restartService(); err != nil {
		return err
	}
	fmt.Println("Enrolment token saved. Checking in with YLTS…")
	for i := 0; i < 20; i++ {
		time.Sleep(3 * time.Second)
		st, err := LoadStatus(paths)
		if err == nil && st.State == "ready" {
			fmt.Printf("Registered. Support ID: %s\n", st.RustDeskID)
			return nil
		}
	}
	return fmt.Errorf("not registered yet; run 'ylts-agent status' in a minute")
}

func cmdUninstall(paths Paths, purge bool) error {
	if err := removeService(); err != nil {
		return err
	}
	if purge {
		_ = os.RemoveAll(paths.Dir)
	}
	fmt.Println("YLTS Agent service removed.")
	return nil
}

func cmdStatus(paths Paths) error {
	st, err := LoadStatus(paths)
	if err != nil {
		return fmt.Errorf("no status yet (is the service running?): %w", err)
	}
	fmt.Printf("State:       %s\nMessage:     %s\nSupport ID:  %s\nClient:      %s\nApproval:    %v\nSessions:    %d\nLast check:  %s\nAgent:       %s\n",
		st.State, st.Message, st.RustDeskID, st.ClientName, st.ApprovalRequired, st.Sessions,
		st.LastCheckin.Local().Format(time.RFC1123), st.AgentVersion)
	return nil
}

// The exe is built as a GUI program (no console flash for the tray), so command-line
// use has to attach to the console of whoever started it to print anything.
func attachParentConsole() {
	const attachParent = ^uint32(0) // ATTACH_PARENT_PROCESS
	r, _, _ := kernel32.NewProc("AttachConsole").Call(uintptr(attachParent))
	if r == 0 {
		return
	}
	if h, err := windows.GetStdHandle(windows.STD_OUTPUT_HANDLE); err == nil && h != 0 {
		os.Stdout = os.NewFile(uintptr(h), "stdout")
	}
	if h, err := windows.GetStdHandle(windows.STD_ERROR_HANDLE); err == nil && h != 0 {
		os.Stderr = os.NewFile(uintptr(h), "stderr")
	}
	fmt.Println()
}
