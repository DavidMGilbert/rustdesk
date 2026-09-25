//go:build windows

package main

import (
	_ "embed"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

//go:embed icons/ylts.ico
var iconNormal []byte

//go:embed icons/ylts-active.ico
var iconActive []byte

//go:embed icons/ylts-warn.ico
var iconWarn []byte

var (
	user32   = windows.NewLazySystemDLL("user32.dll")
	shell32  = windows.NewLazySystemDLL("shell32.dll")
	kernel32 = windows.NewLazySystemDLL("kernel32.dll")

	pRegisterClassExW            = user32.NewProc("RegisterClassExW")
	pCreateWindowExW             = user32.NewProc("CreateWindowExW")
	pDefWindowProcW              = user32.NewProc("DefWindowProcW")
	pGetMessageW                 = user32.NewProc("GetMessageW")
	pTranslateMessage            = user32.NewProc("TranslateMessage")
	pDispatchMessageW            = user32.NewProc("DispatchMessageW")
	pPostQuitMessage             = user32.NewProc("PostQuitMessage")
	pPostMessageW                = user32.NewProc("PostMessageW")
	pRegisterWindowMessageW      = user32.NewProc("RegisterWindowMessageW")
	pCreatePopupMenu             = user32.NewProc("CreatePopupMenu")
	pAppendMenuW                 = user32.NewProc("AppendMenuW")
	pDestroyMenu                 = user32.NewProc("DestroyMenu")
	pTrackPopupMenu              = user32.NewProc("TrackPopupMenu")
	pSetForegroundWindow         = user32.NewProc("SetForegroundWindow")
	pGetCursorPos                = user32.NewProc("GetCursorPos")
	pSetTimer                    = user32.NewProc("SetTimer")
	pLoadImageW                  = user32.NewProc("LoadImageW")
	pDestroyIcon                 = user32.NewProc("DestroyIcon")
	pOpenClipboard               = user32.NewProc("OpenClipboard")
	pEmptyClipboard              = user32.NewProc("EmptyClipboard")
	pSetClipboardData            = user32.NewProc("SetClipboardData")
	pCloseClipboard              = user32.NewProc("CloseClipboard")
	pChangeWindowMessageFilterEx = user32.NewProc("ChangeWindowMessageFilterEx")
	pMessageBoxW                 = user32.NewProc("MessageBoxW")
	pShellNotifyIconW            = shell32.NewProc("Shell_NotifyIconW")
	pShellExecuteW               = shell32.NewProc("ShellExecuteW")
	pGlobalAlloc                 = kernel32.NewProc("GlobalAlloc")
	pGlobalLock                  = kernel32.NewProc("GlobalLock")
	pGlobalUnlock                = kernel32.NewProc("GlobalUnlock")
	pGetModuleHandleW            = kernel32.NewProc("GetModuleHandleW")
	pCreateMutexW                = kernel32.NewProc("CreateMutexW")
	pRtlMoveMemory               = kernel32.NewProc("RtlMoveMemory")
)

const (
	wmDestroy     = 0x0002
	wmCommand     = 0x0111
	wmTimer       = 0x0113
	wmNull        = 0x0000
	wmContextMenu = 0x007B
	wmLButtonUp   = 0x0202
	wmRButtonUp   = 0x0205
	wmApp         = 0x8000
	wmTrayIcon    = wmApp + 1

	ninSelect    = 0x0400
	ninKeySelect = 0x0401

	nimAdd        = 0
	nimModify     = 1
	nimDelete     = 2
	nimSetVersion = 4

	nifMessage  = 0x01
	nifIcon     = 0x02
	nifTip      = 0x04
	nifInfo     = 0x10
	nifShowTip  = 0x80
	niifInfo    = 0x01
	niifUser    = 0x04
	niifLarge   = 0x20
	notifyV4    = 4
	mfString    = 0x0000
	mfGrayed    = 0x0001
	mfChecked   = 0x0008
	mfSeparator = 0x0800
	mfDefault   = 0x1000 // MFS_DEFAULT (bold)
	tpmRightBtn = 0x0002
	tpmReturn   = 0x0100
	tpmBottom   = 0x0020
	imageIcon   = 1
	lrLoadFile  = 0x0010
	lrDefSize   = 0x0040
	cfUnicode   = 13
	gmemMove    = 0x0002
	msgFltAllow = 1
	swShow      = 5
	mbIconInfo  = 0x40
	mbIconWarn  = 0x30
)

// Menu command IDs
const (
	cmdCopyID = iota + 100
	cmdHelp
	cmdCall
	cmdEmail
	cmdWebsite
	cmdApproval
	cmdOpenHost
	cmdHide
)

type wndClassEx struct {
	cbSize        uint32
	style         uint32
	lpfnWndProc   uintptr
	cbClsExtra    int32
	cbWndExtra    int32
	hInstance     windows.Handle
	hIcon         windows.Handle
	hCursor       windows.Handle
	hbrBackground windows.Handle
	lpszMenuName  *uint16
	lpszClassName *uint16
	hIconSm       windows.Handle
}

type point struct{ x, y int32 }

type msg struct {
	hwnd    windows.Handle
	message uint32
	wParam  uintptr
	lParam  uintptr
	time    uint32
	pt      point
	private uint32
}

type notifyIconData struct {
	cbSize           uint32
	hWnd             windows.Handle
	uID              uint32
	uFlags           uint32
	uCallbackMessage uint32
	hIcon            windows.Handle
	szTip            [128]uint16
	dwState          uint32
	dwStateMask      uint32
	szInfo           [256]uint16
	uVersion         uint32
	szInfoTitle      [64]uint16
	dwInfoFlags      uint32
	guidItem         windows.GUID
	hBalloonIcon     windows.Handle
}

type tray struct {
	paths          Paths
	hwnd           windows.Handle
	taskbarCreated uint32
	icons          map[string]windows.Handle
	current        string
	status         Status
	haveStatus     bool
	lastSessions   int
	lastState      string
	approvalWanted bool
}

var theTray *tray

func utf16(s string) *uint16 {
	p, _ := windows.UTF16PtrFromString(s)
	return p
}

func copyUTF16(dst []uint16, s string) {
	u, _ := windows.UTF16FromString(s)
	if len(u) > len(dst) {
		u = u[:len(dst)]
		u[len(u)-1] = 0
	}
	copy(dst, u)
}

// loadIcon writes the embedded .ico to a per-user cache and loads it at the small icon size.
func loadIcon(name string, data []byte) windows.Handle {
	dir := filepath.Join(os.TempDir(), "ylts-agent")
	_ = os.MkdirAll(dir, 0o755)
	path := filepath.Join(dir, name)
	if cur, err := os.ReadFile(path); err != nil || len(cur) != len(data) {
		_ = os.WriteFile(path, data, 0o644)
	}
	h, _, _ := pLoadImageW.Call(0, uintptr(unsafe.Pointer(utf16(path))), imageIcon, 0, 0, lrLoadFile|lrDefSize)
	return windows.Handle(h)
}

func runTray(paths Paths) {
	runtime.LockOSThread()
	// One tray per signed-in user.
	if h, _, err := pCreateMutexW.Call(0, 0, uintptr(unsafe.Pointer(utf16(`Local\YLTSAgentTray`)))); h != 0 &&
		err == windows.ERROR_ALREADY_EXISTS {
		return
	}
	t := &tray{paths: paths, icons: map[string]windows.Handle{}}
	theTray = t
	t.icons["normal"] = loadIcon("ylts.ico", iconNormal)
	t.icons["active"] = loadIcon("ylts-active.ico", iconActive)
	t.icons["warn"] = loadIcon("ylts-warn.ico", iconWarn)
	t.approvalWanted = fileExists(paths.ApprovalFlag())

	hInst, _, _ := pGetModuleHandleW.Call(0)
	className := utf16("YLTSAgentTrayWindow")
	wc := wndClassEx{
		lpfnWndProc:   windows.NewCallback(wndProc),
		hInstance:     windows.Handle(hInst),
		lpszClassName: className,
	}
	wc.cbSize = uint32(unsafe.Sizeof(wc))
	if r, _, err := pRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc))); r == 0 {
		fatalBox("Could not start the YLTS icon: " + err.Error())
		return
	}
	hwnd, _, err := pCreateWindowExW.Call(0, uintptr(unsafe.Pointer(className)),
		uintptr(unsafe.Pointer(utf16("YLTS Agent"))), 0, 0, 0, 0, 0, 0, 0, hInst, 0)
	if hwnd == 0 {
		fatalBox("Could not start the YLTS icon: " + err.Error())
		return
	}
	t.hwnd = windows.Handle(hwnd)
	tc, _, _ := pRegisterWindowMessageW.Call(uintptr(unsafe.Pointer(utf16("TaskbarCreated"))))
	t.taskbarCreated = uint32(tc)
	// If Explorer runs at a different integrity level, still accept its messages.
	for _, m := range []uint32{t.taskbarCreated, wmTrayIcon, wmCommand} {
		pChangeWindowMessageFilterEx.Call(hwnd, uintptr(m), msgFltAllow, 0)
	}

	t.refreshStatus()
	t.addIcon()
	pSetTimer.Call(hwnd, 1, 4000, 0)

	var m msg
	for {
		r, _, _ := pGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			break
		}
		pTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		pDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
	t.removeIcon()
}

func fatalBox(text string) {
	pMessageBoxW.Call(0, uintptr(unsafe.Pointer(utf16(text))), uintptr(unsafe.Pointer(utf16("YLTS"))), mbIconWarn)
}

func wndProc(hwnd windows.Handle, message uint32, wParam, lParam uintptr) uintptr {
	t := theTray
	switch {
	case t != nil && message == t.taskbarCreated && message != 0:
		t.addIcon() // Explorer restarted
		return 0
	case message == wmTrayIcon:
		switch uint32(lParam & 0xFFFF) {
		case wmContextMenu, wmRButtonUp, ninSelect, ninKeySelect, wmLButtonUp:
			t.showMenu()
		}
		return 0
	case message == wmTimer:
		t.refreshStatus()
		return 0
	case message == wmDestroy:
		pPostQuitMessage.Call(0)
		return 0
	}
	r, _, _ := pDefWindowProcW.Call(uintptr(hwnd), uintptr(message), wParam, lParam)
	return r
}

func (t *tray) nid() notifyIconData {
	var n notifyIconData
	n.cbSize = uint32(unsafe.Sizeof(n))
	n.hWnd = t.hwnd
	n.uID = 1
	return n
}

func (t *tray) tooltip() string {
	company := t.status.Company
	if company == "" {
		company = "YLTS"
	}
	if !t.haveStatus {
		return company + " Remote Support - starting"
	}
	s := company + " Remote Support\n" + t.status.Message
	if t.status.RustDeskID != "" {
		s += "\nSupport ID: " + formatID(t.status.RustDeskID)
	}
	return s
}

func (t *tray) iconKey() string {
	switch {
	case !t.haveStatus:
		return "normal"
	case t.status.Sessions > 0:
		return "active"
	case t.status.State == "error" || t.status.State == "offline":
		return "warn"
	}
	return "normal"
}

func (t *tray) addIcon() {
	n := t.nid()
	n.uFlags = nifMessage | nifIcon | nifTip | nifShowTip
	n.uCallbackMessage = wmTrayIcon
	t.current = t.iconKey()
	n.hIcon = t.icons[t.current]
	copyUTF16(n.szTip[:], t.tooltip())
	pShellNotifyIconW.Call(nimAdd, uintptr(unsafe.Pointer(&n)))
	n.uVersion = notifyV4
	pShellNotifyIconW.Call(nimSetVersion, uintptr(unsafe.Pointer(&n)))
}

func (t *tray) updateIcon() {
	n := t.nid()
	n.uFlags = nifIcon | nifTip | nifShowTip
	t.current = t.iconKey()
	n.hIcon = t.icons[t.current]
	copyUTF16(n.szTip[:], t.tooltip())
	pShellNotifyIconW.Call(nimModify, uintptr(unsafe.Pointer(&n)))
}

func (t *tray) removeIcon() {
	n := t.nid()
	pShellNotifyIconW.Call(nimDelete, uintptr(unsafe.Pointer(&n)))
}

func (t *tray) balloon(title, text string, warn bool) {
	n := t.nid()
	n.uFlags = nifInfo
	copyUTF16(n.szInfoTitle[:], title)
	copyUTF16(n.szInfo[:], text)
	n.dwInfoFlags = niifUser | niifLarge
	n.hBalloonIcon = t.icons["normal"]
	if warn {
		n.dwInfoFlags = niifInfo
	}
	pShellNotifyIconW.Call(nimModify, uintptr(unsafe.Pointer(&n)))
}

func (t *tray) refreshStatus() {
	st, err := LoadStatus(t.paths)
	if err != nil {
		t.haveStatus = false
		t.updateIcon()
		return
	}
	first := !t.haveStatus
	t.status, t.haveStatus = st, true
	// Status older than 3 minutes means the service isn't running.
	if time.Since(st.UpdatedAt) > 3*time.Minute {
		t.status.State, t.status.Message = "error", "The YLTS Agent service isn't running."
		t.status.Sessions = 0
	}
	if !first {
		if t.status.Sessions > 0 && t.lastSessions == 0 {
			t.balloon("A YLTS technician has connected",
				"Someone from "+t.companyShort()+" is now working on this computer. You'll see a small window while the session is active.", false)
		} else if t.status.Sessions == 0 && t.lastSessions > 0 {
			t.balloon("Support session ended", "The YLTS technician has disconnected.", false)
		}
	}
	t.lastSessions, t.lastState = t.status.Sessions, t.status.State
	t.updateIcon()
}

func (t *tray) companyShort() string {
	if t.status.Company != "" {
		return t.status.Company
	}
	return "YLTS"
}

func formatID(id string) string {
	if len(id) == 9 && strings.Trim(id, "0123456789") == "" {
		return id[0:3] + " " + id[3:6] + " " + id[6:9]
	}
	return id
}

func appendItem(menu uintptr, id int, text string, flags uint32) {
	pAppendMenuW.Call(menu, uintptr(flags), uintptr(id), uintptr(unsafe.Pointer(utf16(text))))
}

func (t *tray) showMenu() {
	t.refreshStatus()
	menu, _, _ := pCreatePopupMenu.Call()
	defer pDestroyMenu.Call(menu)
	st := t.status
	title := t.companyShort() + " Remote Support"
	appendItem(menu, 0, title, mfString|mfGrayed)
	if st.RustDeskID != "" {
		appendItem(menu, cmdCopyID, "Support ID: "+formatID(st.RustDeskID)+"   (click to copy)", mfString)
	}
	state := "Starting…"
	if t.haveStatus {
		state = st.Message
	}
	appendItem(menu, 0, state, mfString|mfGrayed)
	pAppendMenuW.Call(menu, mfSeparator, 0, 0)
	appendItem(menu, cmdHelp, "Request help from "+t.companyShort()+"…", mfString|mfDefault)
	phone, email, site := t.contact()
	appendItem(menu, cmdCall, "Call us: "+phone, mfString)
	appendItem(menu, cmdEmail, "Email us: "+email, mfString)
	appendItem(menu, cmdWebsite, "Visit "+strings.TrimPrefix(strings.TrimPrefix(site, "https://"), "http://"), mfString)
	pAppendMenuW.Call(menu, mfSeparator, 0, 0)
	flags := uint32(mfString)
	if fileExists(t.paths.ApprovalFlag()) {
		flags |= mfChecked
	}
	appendItem(menu, cmdApproval, "Ask me before a technician connects", flags)
	appendItem(menu, cmdOpenHost, "Open YLTS Remote", mfString)
	appendItem(menu, cmdHide, "Hide this icon until I sign in again", mfString)

	var pt point
	pGetCursorPos.Call(uintptr(unsafe.Pointer(&pt)))
	pSetForegroundWindow.Call(uintptr(t.hwnd))
	cmd, _, _ := pTrackPopupMenu.Call(menu, tpmRightBtn|tpmReturn|tpmBottom, uintptr(pt.x), uintptr(pt.y), 0, uintptr(t.hwnd), 0)
	pPostMessageW.Call(uintptr(t.hwnd), wmNull, 0, 0)
	t.handle(int(cmd))
}

func shellOpen(target string) {
	pShellExecuteW.Call(0, uintptr(unsafe.Pointer(utf16("open"))), uintptr(unsafe.Pointer(utf16(target))), 0, 0, swShow)
}

func (t *tray) contact() (phone, email, site string) {
	phone, email, site = t.status.SupportPhone, t.status.SupportEmail, t.status.SupportURL
	if phone == "" {
		phone = "0483 866 665"
	}
	if email == "" {
		email = "hello@ylts.com.au"
	}
	if site == "" {
		site = "https://ylts.com.au"
	}
	return
}

func (t *tray) handle(cmd int) {
	st := t.status
	phone, email, site := t.contact()
	switch cmd {
	case cmdCopyID:
		if setClipboard(formatID(st.RustDeskID)) {
			t.balloon("Support ID copied", "Read or paste "+formatID(st.RustDeskID)+" to your technician.", false)
		}
	case cmdHelp:
		go t.requestHelp()
	case cmdCall:
		shellOpen("tel:" + strings.ReplaceAll(phone, " ", ""))
	case cmdEmail:
		subject := "Help needed"
		if st.DeviceLabel != "" {
			subject += " - " + st.DeviceLabel
		}
		shellOpen("mailto:" + email + "?subject=" + strings.ReplaceAll(subject, " ", "%20"))
	case cmdWebsite:
		if strings.HasPrefix(site, "https://") || strings.HasPrefix(site, "http://") {
			shellOpen(site)
		}
	case cmdApproval:
		on := !fileExists(t.paths.ApprovalFlag())
		if err := SetApprovalRequired(t.paths, on); err != nil {
			t.balloon("Couldn't change the setting", err.Error(), true)
			return
		}
		if on {
			t.balloon("You'll be asked first", "When a technician connects, a window will ask you to accept before they can see your screen.", false)
		} else {
			t.balloon("Unattended support allowed", "Your YLTS technician can connect without you needing to be at the computer.", false)
		}
	case cmdOpenHost:
		exe := st.HostExe
		if exe == "" {
			exe = defaultHostExe()
		}
		shellOpen(exe)
	case cmdHide:
		pPostMessageW.Call(uintptr(t.hwnd), wmDestroy, 0, 0)
	}
}

// requestHelp asks for a short description with a standard Windows input box.
func (t *tray) requestHelp() {
	script := `Add-Type -AssemblyName Microsoft.VisualBasic;` +
		`[Microsoft.VisualBasic.Interaction]::InputBox('Briefly describe what you need help with. We will get back to you as soon as we can.', '` +
		strings.ReplaceAll(t.companyShort(), "'", "") + ` - Request help', '')`
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	out, err := cmd.Output()
	if err != nil {
		t.balloon("Couldn't open the help window", "Please call or email us instead.", true)
		return
	}
	message := strings.TrimSpace(string(out))
	if message == "" {
		return // cancelled
	}
	user := os.Getenv("USERNAME")
	if err := QueueHelpRequest(t.paths, message, user); err != nil {
		t.balloon("Couldn't send your request", "Please call or email us instead. ("+err.Error()+")", true)
		return
	}
	t.balloon("Request sent", fmt.Sprintf("Thanks%s. %s has been notified and will be in touch.", greetingName(user), t.companyShort()), false)
}

func greetingName(user string) string {
	if user == "" {
		return ""
	}
	return ", " + user
}

func setClipboard(text string) bool {
	u, err := windows.UTF16FromString(text)
	if err != nil {
		return false
	}
	if r, _, _ := pOpenClipboard.Call(0); r == 0 {
		return false
	}
	defer pCloseClipboard.Call()
	pEmptyClipboard.Call()
	size := uintptr(len(u) * 2)
	h, _, _ := pGlobalAlloc.Call(gmemMove, size)
	if h == 0 {
		return false
	}
	p, _, _ := pGlobalLock.Call(h)
	if p == 0 {
		return false
	}
	pRtlMoveMemory.Call(p, uintptr(unsafe.Pointer(&u[0])), size)
	pGlobalUnlock.Call(h)
	r, _, _ := pSetClipboardData.Call(cfUnicode, h)
	return r != 0
}
