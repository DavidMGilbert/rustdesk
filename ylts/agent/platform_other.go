//go:build !windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"runtime"
)

func hideWindow(*exec.Cmd)          {}
func restrictToAdmins(string) error { return nil }
func osName() string                { return runtime.GOOS }
func defaultDataDir() string        { return "/tmp/ylts" }
func defaultHostExe() string        { return "rustdesk" }

func main() {
	fmt.Fprintln(os.Stderr, "ylts-agent only runs on Windows; this build exists for tests.")
	os.Exit(1)
}
