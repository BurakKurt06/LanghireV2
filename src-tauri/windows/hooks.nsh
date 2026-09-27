; LangHire NSIS installer hooks (wired up in tauri.conf.json -> bundle.windows.nsis.installerHooks).

; A running backend sidecar locks its own exe, so a reinstall could not replace
; it and an uninstall left it behind. Stop it, and the browsers it started (/T),
; before touching any files.
!macro NSIS_HOOK_PREINSTALL
  nsExec::Exec 'taskkill /F /T /IM langhirev2-backend.exe'
  Pop $0
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  nsExec::Exec 'taskkill /F /T /IM langhirev2-backend.exe'
  Pop $0
!macroend

; Tauri's "Delete the application data" checkbox only removes the
; $APPDATA\<bundle id> folders. The backend keeps everything else (API keys,
; profile, jobs, logs, browser logins) in %APPDATA%\langhirev2
; (backend/core/config.py get_data_dir), so remove that too. Skipped during
; updates, like Tauri's own cleanup.
;
; RmDir /r silently skips any file that is still open (a backend or browser
; still shutting down, an antivirus scan), which once left the API token, the
; ChatGPT sign-in and the memory database behind. So keep trying for up to
; 10 seconds. (Deleting at the next restart instead would need admin rights,
; which this per-user uninstaller doesn't have.)
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    SetShellVarContext current
    StrCpy $R9 0
    ${Do}
      RmDir /r "$APPDATA\langhirev2"
      ${IfNot} ${FileExists} "$APPDATA\langhirev2\*.*"
      ${OrIf} $R9 >= 10
        ${Break}
      ${EndIf}
      IntOp $R9 $R9 + 1
      Sleep 1000
    ${Loop}
  ${EndIf}
!macroend
