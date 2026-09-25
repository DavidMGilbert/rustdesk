; YLTS Remote - client PC installer
; Installs the YLTS Remote Host (branded RustDesk, runs as a service) and the YLTS Agent
; (registration, password management and the notification-area icon).
;
; Interactive:  YLTS-Remote-Setup.exe                 (asks for the enrolment code)
; Silent:       YLTS-Remote-Setup.exe /S /ENROL=ylts_xxxxx
; Build:        makensis -DVERSION=1.0.0 -DPORTAL_URL=https://remote.ylts.com.au -DPAYLOAD=payload host.nsi

Unicode true
ManifestDPIAware true
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!ifndef PORTAL_URL
  !define PORTAL_URL "https://remote.ylts.com.au"
!endif
!ifndef PAYLOAD
  !define PAYLOAD "payload"
!endif
!define COMPANY "Your Local Tech Solutions"
!define HOST_APP "YLTS-Remote-Host"
!define AGENT_DIR "YLTS Agent"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\YLTS Remote"

!include MUI2.nsh
!include LogicLib.nsh
!include FileFunc.nsh
!include x64.nsh
!include nsDialogs.nsh

Name "YLTS Remote"
OutFile "YLTS-Remote-Setup.exe"
InstallDir "$PROGRAMFILES64\${AGENT_DIR}"
BrandingText "${COMPANY}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "YLTS Remote"
VIAddVersionKey "CompanyName" "${COMPANY}"
VIAddVersionKey "FileDescription" "YLTS Remote Support setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "${COMPANY}"

!define MUI_ICON "${PAYLOAD}\ylts.ico"
!define MUI_UNICON "${PAYLOAD}\ylts.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${PAYLOAD}\installer-side.bmp"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "YLTS Remote Support"
!define MUI_WELCOMEPAGE_TEXT "This installs YLTS Remote Support so ${COMPANY} can help you with this computer.$\r$\n$\r$\nWhat it does:$\r$\n  - Adds a YLTS icon near the clock with your Support ID$\r$\n  - Lets your YLTS technician connect to fix problems, including when you're away$\r$\n  - Shows a window whenever a technician is connected$\r$\n$\r$\nYou can require on-screen approval for every session from the YLTS icon, and uninstall at any time from Settings > Apps.$\r$\n$\r$\nPhone 0483 866 665$\r$\nhello@ylts.com.au$\r$\nylts.com.au"
!define MUI_FINISHPAGE_TITLE "YLTS Remote Support is ready"
!define MUI_FINISHPAGE_TEXT "Look for the YLTS icon near the clock (you may need to click the ^ arrow). Right-click it to see your Support ID or to ask us for help.$\r$\n$\r$\nPhone 0483 866 665 · hello@ylts.com.au · ylts.com.au"

Var EnrolToken
Var TokenField

!insertmacro MUI_PAGE_WELCOME
Page custom TokenPage TokenPageLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "YLTS Remote needs 64-bit Windows 10 or later." /SD IDOK
    Abort
  ${EndIf}
  SetRegView 64
  ${GetParameters} $0
  ${GetOptions} $0 "/ENROL=" $EnrolToken
  ${If} $EnrolToken == ""
    ${GetOptions} $0 "/ENROLL=" $EnrolToken
  ${EndIf}
  ; Allow re-running over an existing install (upgrade) without a token.
  ${If} ${Silent}
  ${AndIf} $EnrolToken == ""
  ${AndIfNot} ${FileExists} "$COMMONPROGRAMDATA\YLTS\agent.json"
    SetErrorLevel 2
    Abort
  ${EndIf}
FunctionEnd

Function TokenPage
  ${If} ${FileExists} "$COMMONPROGRAMDATA\YLTS\agent.json"
  ${AndIf} $EnrolToken == ""
    Abort ; already registered: this is an upgrade, skip the page
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "Enrolment code" "Enter the code YLTS gave you (it starts with ylts_)."
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 36u "This links the computer to your account with ${COMPANY}. If you don't have a code, contact us on the details from your technician."
  Pop $0
  ${NSD_CreateText} 0 44u 100% 14u "$EnrolToken"
  Pop $TokenField
  nsDialogs::Show
FunctionEnd

Function TokenPageLeave
  ${NSD_GetText} $TokenField $EnrolToken
  ${If} $EnrolToken == ""
    MessageBox MB_ICONEXCLAMATION "Please enter your enrolment code."
    Abort
  ${EndIf}
FunctionEnd

Section "Install"
  SetRegView 64
  SetShellVarContext all
  SetOutPath "$INSTDIR"
  DetailPrint "Stopping any running YLTS icon..."
  nsExec::Exec 'taskkill /F /IM ylts-agent.exe'
  File "${PAYLOAD}\ylts-agent.exe"
  File "${PAYLOAD}\ylts.ico"

  ; 1. The remote support host (branded RustDesk) installs itself as a Windows service.
  DetailPrint "Installing YLTS Remote Host..."
  InitPluginsDir
  File "/oname=$PLUGINSDIR\YLTS-Remote-Host.exe" "${PAYLOAD}\YLTS-Remote-Host.exe"
  ExecWait '"$PLUGINSDIR\YLTS-Remote-Host.exe" --silent-install' $0
  StrCpy $1 0
  ${Do}
    ${If} ${FileExists} "$PROGRAMFILES64\${HOST_APP}\${HOST_APP}.exe"
      ${ExitDo}
    ${EndIf}
    IntOp $1 $1 + 1
    ${If} $1 > 120
      DetailPrint "YLTS Remote Host did not finish installing."
      MessageBox MB_ICONSTOP "YLTS Remote Host couldn't be installed. Please contact YLTS." /SD IDOK
      SetErrorLevel 3
      Abort
    ${EndIf}
    Sleep 1000
  ${Loop}

  ; 2. The agent service registers the PC and keeps its access password up to date.
  DetailPrint "Registering this computer with YLTS..."
  ${If} $EnrolToken != ""
    nsExec::ExecToLog '"$INSTDIR\ylts-agent.exe" install --portal "${PORTAL_URL}" --enrol "$EnrolToken" --host-exe "$PROGRAMFILES64\${HOST_APP}\${HOST_APP}.exe"'
  ${Else}
    nsExec::ExecToLog '"$INSTDIR\ylts-agent.exe" install --portal "${PORTAL_URL}" --host-exe "$PROGRAMFILES64\${HOST_APP}\${HOST_APP}.exe"'
  ${EndIf}
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "The YLTS Agent couldn't be set up (code $0). Please contact YLTS." /SD IDOK
    SetErrorLevel 4
    Abort
  ${EndIf}

  ; 3. Tray icon for every user at sign-in, and now for the current user (started via
  ;    Explorer so it runs as the signed-in user rather than elevated).
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Run" "YLTS Agent" '"$INSTDIR\ylts-agent.exe"'
  ${IfNot} ${Silent}
    Exec '"$WINDIR\explorer.exe" "$INSTDIR\ylts-agent.exe"'
  ${EndIf}

  ; Start menu + Add/Remove Programs
  CreateDirectory "$SMPROGRAMS\YLTS"
  CreateShortCut "$SMPROGRAMS\YLTS\YLTS Support icon.lnk" "$INSTDIR\ylts-agent.exe" "" "$INSTDIR\ylts.ico"
  WriteUninstaller "$INSTDIR\Uninstall YLTS Remote.exe"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayName" "YLTS Remote Support"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINST_KEY}" "Publisher" "${COMPANY}"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\ylts.ico"
  WriteRegStr HKLM "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall YLTS Remote.exe"'
  WriteRegStr HKLM "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall YLTS Remote.exe" /S'
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetRegView 64
  SetShellVarContext all
  nsExec::Exec 'taskkill /F /IM ylts-agent.exe'
  nsExec::ExecToLog '"$INSTDIR\ylts-agent.exe" uninstall --purge'
  ${If} ${FileExists} "$PROGRAMFILES64\${HOST_APP}\${HOST_APP}.exe"
    DetailPrint "Removing YLTS Remote Host..."
    ExecWait '"$PROGRAMFILES64\${HOST_APP}\${HOST_APP}.exe" --uninstall'
  ${EndIf}
  DeleteRegValue HKLM "Software\Microsoft\Windows\CurrentVersion\Run" "YLTS Agent"
  Delete "$SMPROGRAMS\YLTS\YLTS Support icon.lnk"
  RMDir "$SMPROGRAMS\YLTS"
  Delete "$INSTDIR\ylts-agent.exe"
  Delete "$INSTDIR\ylts.ico"
  Delete "$INSTDIR\Uninstall YLTS Remote.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "${UNINST_KEY}"
SectionEnd
