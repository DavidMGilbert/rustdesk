//go:build windows

package main

import (
	"fmt"
	"os"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

const (
	serviceName    = "YLTSAgent"
	serviceDisplay = "YLTS Agent"
	serviceDesc    = "Keeps YLTS Remote support ready on this PC: registration, security updates and the tray icon status."
)

type serviceHandler struct{ agent *Agent }

func (h *serviceHandler) Execute(_ []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		h.agent.Run(stop)
	}()
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for c := range req {
		switch c.Cmd {
		case svc.Interrogate:
			status <- c.CurrentStatus
		case svc.Stop, svc.Shutdown:
			status <- svc.Status{State: svc.StopPending}
			close(stop)
			select {
			case <-done:
			case <-time.After(10 * time.Second):
			}
			return false, 0
		}
	}
	return false, 0
}

func installService() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to service manager (run as administrator): %w", err)
	}
	defer m.Disconnect()
	cfg := mgr.Config{
		DisplayName:      serviceDisplay,
		Description:      serviceDesc,
		StartType:        mgr.StartAutomatic,
		DelayedAutoStart: true,
	}
	s, err := m.OpenService(serviceName)
	if err == nil {
		// Upgrade: point the existing service at this binary and restart it.
		cur, err := s.Config()
		if err == nil {
			cur.BinaryPathName = fmt.Sprintf(`"%s" service`, exe)
			cur.DisplayName, cur.Description, cur.StartType, cur.DelayedAutoStart =
				cfg.DisplayName, cfg.Description, cfg.StartType, cfg.DelayedAutoStart
			_ = s.UpdateConfig(cur)
		}
		defer s.Close()
		return restartOpen(s)
	}
	s, err = m.CreateService(serviceName, exe, cfg, "service")
	if err != nil {
		return fmt.Errorf("create service: %w", err)
	}
	defer s.Close()
	// Restart automatically if it ever crashes.
	_ = s.SetRecoveryActions([]mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 10 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 30 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
	}, 86400)
	return s.Start()
}

func restartOpen(s *mgr.Service) error {
	if st, err := s.Query(); err == nil && st.State != svc.Stopped {
		if _, err := s.Control(svc.Stop); err == nil {
			for i := 0; i < 30; i++ {
				time.Sleep(500 * time.Millisecond)
				if st, err := s.Query(); err == nil && st.State == svc.Stopped {
					break
				}
			}
		}
	}
	return s.Start()
}

func restartService() error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to service manager (run as administrator): %w", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(serviceName)
	if err != nil {
		return fmt.Errorf("the YLTS Agent service isn't installed: %w", err)
	}
	defer s.Close()
	return restartOpen(s)
}

func removeService() error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to service manager (run as administrator): %w", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(serviceName)
	if err != nil {
		if err == windows.ERROR_SERVICE_DOES_NOT_EXIST {
			return nil
		}
		return nil
	}
	defer s.Close()
	_, _ = s.Control(svc.Stop)
	time.Sleep(2 * time.Second)
	return s.Delete()
}
