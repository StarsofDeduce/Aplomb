; Aplomb 安装程序（NSIS 3.x / MUI2）
; 按用户安装，不弹 UAC：装到 %LOCALAPPDATA%\Programs\Aplomb

Unicode true
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "1.2.3"
!endif

!define APP_NAME "Aplomb"
!define APP_NAME_CN "从容"
!define APP_EXE "Aplomb.exe"
!define APP_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Aplomb"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"

Name "${APP_NAME} ${APP_NAME_CN}"
BrandingText "Aplomb · 从容  v${APP_VERSION}"
OutFile "..\dist\Aplomb-Setup-${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel user

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} 安装程序"
VIAddVersionKey "FileVersion" "${APP_VERSION}.0"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "StarsofDeduce"

!define MUI_ICON "..\assets\aplomb.ico"
!define MUI_UNICON "..\assets\aplomb.ico"
!define MUI_ABORTWARNING
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP "header.bmp"
!define MUI_HEADERIMAGE_RIGHT
!define MUI_WELCOMEFINISHPAGE_BITMAP "welcome.bmp"
!define MUI_WELCOMEPAGE_TITLE "安装 ${APP_NAME}"
!define MUI_WELCOMEPAGE_TEXT "Aplomb（从容）是一个常驻托盘的窗口居中工具。$\r$\n$\r$\n三个全局快捷键，让窗口回到它该在的位置。$\r$\n$\r$\n安装到当前用户目录，不需要管理员权限。"

!define MUI_DIRECTORYPAGE_TEXT_TOP "Aplomb 会被安装到下面的目录。继续之前可以先改路径。"

!define MUI_FINISHPAGE_TITLE "${APP_NAME} 已经装好了"
!define MUI_FINISHPAGE_TEXT "程序会安静地待在系统托盘里。$\r$\n$\r$\n快捷键默认留空，不会占用系统组合键。$\r$\n请在「设置 → 快捷键」中按需设置。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APP_NAME}"
!define MUI_FINISHPAGE_LINK "查看使用说明"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\使用说明.txt"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; ------------------------------------------------------------------ 初始化
Function .onInit
  SetShellVarContext current
  ; 已经装过就沿用原来的目录
  ReadRegStr $0 HKCU "Software\${APP_NAME}" "InstallDir"
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
FunctionEnd

Function .onInstSuccess
  ; 安装完成后把目录记下来，方便下次升级/卸载
  WriteRegStr HKCU "Software\${APP_NAME}" "InstallDir" "$INSTDIR"
FunctionEnd

; ------------------------------------------------------------------ 组件
Section "Aplomb 主程序" SecMain
  SectionIn RO

  ; 先结束正在运行的实例，避免文件占用
  nsExec::Exec 'taskkill /F /IM ${APP_EXE}'
  Pop $0
  Sleep 400

  SetOutPath "$INSTDIR"
  File "..\dist\${APP_EXE}"
  File "..\assets\aplomb.ico"
  File "使用说明.txt"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\aplomb.ico" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "${APP_KEY}" "DisplayName" "${APP_NAME} · ${APP_NAME_CN}"
  WriteRegStr HKCU "${APP_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${APP_KEY}" "Publisher" "StarsofDeduce"
  WriteRegStr HKCU "${APP_KEY}" "DisplayIcon" "$INSTDIR\aplomb.ico"
  WriteRegStr HKCU "${APP_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${APP_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${APP_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${APP_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${APP_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${APP_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "桌面快捷方式" SecDesktop
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\aplomb.ico" 0
SectionEnd

Section "开机自动启动" SecAutostart
  WriteRegStr HKCU "${RUN_KEY}" "${APP_NAME}" '"$INSTDIR\${APP_EXE}" --minimized'
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "程序本体、开始菜单项和卸载程序。"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "在桌面上放一个快捷方式。"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutostart} "登录 Windows 后自动在托盘启动（之后也可以在设置里关掉）。"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ------------------------------------------------------------------ 卸载
Section "Uninstall"
  nsExec::Exec 'taskkill /F /IM ${APP_EXE}'
  Pop $0
  Sleep 400

  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\aplomb.ico"
  Delete "$INSTDIR\使用说明.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegValue HKCU "${RUN_KEY}" "${APP_NAME}"
  DeleteRegKey HKCU "${APP_KEY}"
  DeleteRegKey HKCU "Software\${APP_NAME}"
SectionEnd
