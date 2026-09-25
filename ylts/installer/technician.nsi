; YLTS Technician - installer for YLTS staff PCs.
; Build: makensis -DVERSION=1.0.0 -DPAYLOAD=payload technician.nsi

Unicode true
ManifestDPIAware true
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!ifndef PAYLOAD
  !define PAYLOAD "payload"
!endif
!define COMPANY "Your Local Tech Solutions"
!define APP "YLTS Technician"

!include MUI2.nsh
!include LogicLib.nsh
!include x64.nsh

Name "${APP}"
OutFile "YLTS-Technician-Setup.exe"
BrandingText "${COMPANY}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP}"
VIAddVersionKey "CompanyName" "${COMPANY}"
VIAddVersionKey "FileDescription" "${APP} setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "${COMPANY}"

!define MUI_ICON "${PAYLOAD}\ylts.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${PAYLOAD}\installer-side.bmp"
!define MUI_WELCOMEPAGE_TITLE "${APP}"
!define MUI_WELCOMEPAGE_TEXT "Installs the YLTS Technician app for connecting to client computers.$\r$\n$\r$\nAfter installing, sign in with your YLTS Portal account (menu next to your ID > Log in). The client computers you've been given access to appear in the Address Book tab, one book per client."
!define MUI_FINISHPAGE_RUN "$PROGRAMFILES64\${APP}\${APP}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open ${APP} now"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "${APP} needs 64-bit Windows 10 or later." /SD IDOK
    Abort
  ${EndIf}
FunctionEnd

Section "Install"
  InitPluginsDir
  File "/oname=$PLUGINSDIR\YLTS-Technician.exe" "${PAYLOAD}\YLTS-Technician.exe"
  DetailPrint "Installing ${APP}..."
  ExecWait '"$PLUGINSDIR\YLTS-Technician.exe" --silent-install' $0
  StrCpy $1 0
  ${Do}
    ${If} ${FileExists} "$PROGRAMFILES64\${APP}\${APP}.exe"
      ${ExitDo}
    ${EndIf}
    IntOp $1 $1 + 1
    ${If} $1 > 120
      MessageBox MB_ICONSTOP "${APP} couldn't be installed." /SD IDOK
      SetErrorLevel 3
      Abort
    ${EndIf}
    Sleep 1000
  ${Loop}
SectionEnd
