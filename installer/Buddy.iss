; Buddy - Windows installer (Inno Setup 7). Build with:  python build_installer.py
;
; A double-click install, per user, no admin prompt. It:
;   1. puts Buddy.py + buddy.zip in Resolve's Scripts\Utility folder, so
;      Buddy appears under Workspace > Scripts in DaVinci Resolve;
;   2. makes sure there is a Python Resolve can run it with. Resolve's script
;      host (fuscript.exe) uses the Python the "py" launcher finds (py -3), so
;      that is exactly what's checked. If there's none (or it's older than
;      3.10), the official python.org 3.13 installer is downloaded - its
;      SHA-256 pinned below - and installed for this user only;
;   3. installs Buddy's packages into that same Python with pip (installer\
;      requirements.txt, ~300 MB, mostly PySide6), skipping any already there.
;      Resolve / Buddy being open is checked for first (they lock the files
;      pip replaces), and pip's output goes to the setup log in %TEMP%, so a
;      failure says why instead of guessing.
;
; Nothing compiled is installed: Buddy stays plain Python source inside a zip.
; (The earlier tools' installer found Windows Defender deleting an unsigned
; PyInstaller exe on a fresh PC; this avoids that class of problem.) The
; installer itself is unsigned, so Windows SmartScreen may say "Windows
; protected your PC" - More info > Run anyway.
;
; Uninstalling removes Buddy's two files, and the "Start Buddy automatically
; when Resolve starts" helper if it was turned on (its HKCU Run value, the
; running watcher, and its files in {app} - see app/core/startup_manager.py).
; Python, its packages, and Buddy's settings/models in %USERPROFILE%\.buddy
; are left alone - other things may use that Python, and the models are
; large downloads.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Buddy"
#define AppPublisher "Buddy"
#define ScriptsDir "{userappdata}\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
#define PythonVersion "3.13.15"
#define PythonUrl "https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe"
; SHA-256 of that exact file, checked by the downloader before it runs.
; (Also Authenticode-signed by the Python Software Foundation.)
#define PythonSha256 "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403"

[Setup]
AppId={{3F6C2B1E-9A47-4D8B-B6E2-5C1D7A90E4F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Buddy
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
OutputDir=..\dist
OutputBaseFilename=BuddySetup-{#AppVersion}
SetupIconFile=buddy.ico
UninstallDisplayIcon={app}\buddy.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
PrivilegesRequired=lowest
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoCopyright=(c) {#AppPublisher}
CloseApplications=no
; Always write %TEMP%\Setup Log <date> #NNN.txt - pip's full output goes in
; it, so a failed package install can be diagnosed afterwards.
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedLabel=Buddy is installed.%n%nOpen DaVinci Resolve and choose Workspace > Scripts > Buddy. If Resolve was already open, restart it so the menu picks Buddy up. If Buddy was running, quit it from the system tray first.

[Files]
Source: "..\build\Buddy.py"; DestDir: "{#ScriptsDir}"; Flags: ignoreversion
Source: "..\build\buddy.zip"; DestDir: "{#ScriptsDir}"; Flags: ignoreversion
Source: "buddy.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{tmp}"; Flags: deleteafterinstall

; The Resolve watcher's files. Buddy writes them (not this installer), into
; the same folder as {app} - startup_manager.py's _WATCHER_DIR. The watcher
; is stopped first (CurUninstallStepChanged), since a running one holds
; watcher.lock open.
[UninstallDelete]
Type: files; Name: "{app}\resolve_watcher.py"
Type: files; Name: "{app}\resolve_watcher.log"
Type: files; Name: "{app}\watcher.lock"
Type: files; Name: "{app}\watcher.info"
Type: files; Name: "{app}\watcher.stop"
Type: files; Name: "{app}\watcher.pid"

[Code]
const
  { startup_manager.py's _RUN_KEY / _VALUE_NAME. }
  WatcherRunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  WatcherRunValue = 'BuddyResolveWatcher';

var
  DisclaimerPage: TOutputMsgWizardPage;
  ContentsPage: TOutputMsgWizardPage;
  DownloadPage: TDownloadWizardPage;
  PythonExe: string;
  NeedPython: Boolean;
  PipLines: Integer;
  { What pip's output said went wrong, across both passes (see OnPipLine). }
  PipNetworkError, PipFilesLocked, PipNoMatch: Boolean;
  PipLastError: string;

{ ------------------------------------------------------------------ Python }

{ The py launcher, by where Python's installers put it. Not just "py" from
  PATH: this process's PATH is the one it started with, so a launcher
  installed a moment ago by InstallPython isn't on it. }
function PyLauncher(): string;
begin
  Result := ExpandConstant('{localappdata}\Programs\Python\Launcher\py.exe');
  if not FileExists(Result) then
    Result := ExpandConstant('{win}\py.exe');
  if not FileExists(Result) then
    Result := 'py';
end;

{ The Python Resolve will use: whatever "py -3" resolves to. Only accepted
  at 3.10+ (PySide6's wheels need it). Writes exe + version check to a file
  because Exec can't capture output. }
function DetectPython(var Exe: string): Boolean;
var
  ResultCode: Integer;
  OutFile: string;
  Lines: TArrayOfString;
begin
  Result := False;
  Exe := '';
  OutFile := ExpandConstant('{tmp}\pydetect.txt');
  if FileExists(OutFile) then
    DeleteFile(OutFile);
  { The outer quotes are cmd /C's: it strips the first and last, keeping the
    quoted launcher path intact. }
  Exec(ExpandConstant('{cmd}'),
    '/C ""' + PyLauncher() + '" -3 -c "import sys;print(sys.executable);print(sys.version_info[:2] >= (3, 10))" > "' +
    OutFile + '" 2>nul"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('Python check via ' + PyLauncher());
  if FileExists(OutFile) and LoadStringsFromFile(OutFile, Lines) and (GetArrayLength(Lines) >= 2) then
  begin
    Exe := Trim(Lines[0]);
    Result := (Exe <> '') and FileExists(Exe) and (Trim(Lines[1]) = 'True');
  end;
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard();
begin
  DisclaimerPage := CreateOutputMsgPage(wpWelcome,
    'Before You Continue', 'A quick note',
    'This product is FREE. If you paid for this, go get your money back. If you try to '
    + 'steal this and sell it for money, shame on you.');

  ContentsPage := CreateOutputMsgPage(DisclaimerPage.ID,
    'What Gets Installed', 'Buddy needs a few things alongside it',
    'This installer will set up:' + #13#10#13#10
    + '*  Buddy, in DaVinci Resolve''s Workspace > Scripts menu' + #13#10
    + '*  Python {#PythonVersion} from python.org - only if this PC doesn''t already have Python 3.10 or newer' + #13#10
    + '*  The Python packages Buddy uses (PySide6, Pillow, NumPy, openpyxl, pynput, PyMuPDF, cryptography) - '
    + 'only the ones that are missing' + #13#10#13#10
    + 'Anything missing is downloaded while installing - up to about 330 MB, so it needs an '
    + 'internet connection and can take a few minutes.' + #13#10#13#10
    + 'Transcription (Whisper) is set up later from inside Buddy, only if you want it.');

  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
    'Downloading Python {#PythonVersion} from python.org...', @OnDownloadProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
  begin
    { Resolve creates this folder the first time it runs. }
    if not DirExists(ExpandConstant('{userappdata}\Blackmagic Design\DaVinci Resolve')) then
      SuppressibleMsgBox('DaVinci Resolve doesn''t seem to have been run on this PC yet. Buddy will still be ' +
                         'installed where Resolve looks for scripts - install and open Resolve to use it.',
                         mbInformation, MB_OK, IDOK);

    NeedPython := not DetectPython(PythonExe);
    if NeedPython then
    begin
      DownloadPage.Clear;
      DownloadPage.Add('{#PythonUrl}', 'python-installer.exe', '{#PythonSha256}');
      DownloadPage.Show;
      try
        try
          DownloadPage.Download;
        except
          if DownloadPage.AbortedByUser then
            Log('Python download cancelled.')
          else
            SuppressibleMsgBox('Python couldn''t be downloaded:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
              'Check the internet connection and run the installer again.', mbCriticalError, MB_OK, IDOK);
          Result := False;
        end;
      finally
        DownloadPage.Hide;
      end;
    end;
  end;
end;

function InstallPython(): Boolean;
var
  ResultCode: Integer;
begin
  WizardForm.StatusLabel.Caption := 'Installing Python {#PythonVersion} for this user - ' +
    'this can take several minutes...';
  { Python's installer reports no progress, so a moving bar instead of a
    full, frozen-looking one. }
  WizardForm.ProgressGauge.Style := npbstMarquee;
  { Per-user, with the py launcher (what Resolve uses to find Python) and pip.
    PrependPath=0: Buddy doesn't need Python on PATH, and it's not ours to change.
    Include_dev=0: C headers, only for compiling extensions; Buddy uses wheels.
    (Each part of Python's installer is a signed MSI; where Windows can't
    reach the certificate revocation servers - Windows Sandbox, some office
    networks - each one waits 2 minutes on that check, so fewer is faster.) }
  Exec(ExpandConstant('{tmp}\python-installer.exe'),
    '/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0 ' +
    'Include_pip=1 Include_dev=0 Include_tcltk=0 Include_test=0 Include_doc=0',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  WizardForm.ProgressGauge.Style := npbstNormal;
  Log(Format('Python installer exit code: %d', [ResultCode]));
  Result := DetectPython(PythonExe);
end;

{ ---------------------------------------------------------------- packages }

function ContainsAny(const Text: string; const Needles: array of string): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 0 to GetArrayLength(Needles) - 1 do
    if Pos(Needles[I], Text) > 0 then
    begin
      Result := True;
      Exit;
    end;
end;

procedure OnPipLine(const S: String; const Error, FirstLine: Boolean);
var
  T, L: string;
begin
  T := Trim(S);
  if T = '' then
    Exit;
  { With a callback, ExecAndLogOutput doesn't log by itself. }
  Log('pip: ' + T);
  PipLines := PipLines + 1;

  { Remember why it failed, so the final message can say something true
    instead of guessing. }
  L := Lowercase(T);
  if ContainsAny(L, ['newconnectionerror', 'failed to establish a new connection', 'getaddrinfo failed',
                     'max retries exceeded', 'read timed out', 'readtimeouterror', 'proxyerror',
                     'sslerror', 'certificate verify failed', 'connectionreseterror']) then
    PipNetworkError := True;
  if ContainsAny(L, ['winerror 5]', 'winerror 32]', 'access is denied', 'being used by another process']) then
    PipFilesLocked := True;
  if Pos('no matching distribution found', L) > 0 then
    PipNoMatch := True;
  if Pos('ERROR:', T) = 1 then
    PipLastError := T;

  { Show what pip is doing ("Collecting PySide6...", "Downloading ... 80 MB"),
    so a long download doesn't look like a hang. }
  if (Pos('Collecting', T) = 1) or (Pos('Downloading', T) = 1) or (Pos('Installing', T) = 1) or
     (Pos('Successfully', T) = 1) or (Pos('Requirement already', T) = 1) then
  begin
    WizardForm.FilenameLabel.Caption := Copy(T, 1, 110);
    WizardForm.ProgressGauge.Position := (PipLines mod 100);
  end;
end;

function PackagesPresent(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(PythonExe,
    '-c "import PySide6.QtWidgets, PySide6.QtMultimedia, PySide6.QtWebEngineWidgets, PIL, numpy, openpyxl, pynput, pymupdf, pymupdf4llm, cryptography"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function PipInstall(const Extra: string): Boolean;
var
  ResultCode: Integer;
begin
  PipLines := 0;
  WizardForm.ProgressGauge.Style := npbstNormal;
  WizardForm.ProgressGauge.Max := 100;
  Result := ExecAndLogOutput(PythonExe,
    '-m pip install --disable-pip-version-check --progress-bar off ' + Extra +
    ' -r "' + ExpandConstant('{tmp}\requirements.txt') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode, @OnPipLine) and (ResultCode = 0);
  Log(Format('pip install %s exit code: %d', [Extra, ResultCode]));
end;

{ Programs that may have Buddy's Python packages loaded: Resolve (Buddy runs
  inside its script host) and anything running from Buddy's Python itself
  (Buddy in the tray, its Resolve watcher). Windows won't let pip replace a
  loaded .pyd, so an update fails while any of these are open. }
function FindBlockingPrograms(): string;
var
  Locator, Service, Procs, P: Variant;
  I: Integer;
  PyDir, Name, Path, Found: string;
begin
  Result := '';
  PyDir := Lowercase(ExtractFilePath(PythonExe));
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Procs := Service.ExecQuery('SELECT Name, ExecutablePath FROM Win32_Process');
    for I := 0 to Procs.Count - 1 do
    begin
      P := Procs.ItemIndex(I);
      Name := P.Name;
      Name := Lowercase(Name);
      Path := '';
      if not VarIsNull(P.ExecutablePath) then
      begin
        Path := P.ExecutablePath;
        Path := Lowercase(Path);
      end;
      Found := '';
      if (Name = 'resolve.exe') or (Name = 'fuscript.exe') then
        Found := 'DaVinci Resolve'
      else if (PyDir <> '') and (Pos(PyDir, Path) = 1) then
        Found := 'Buddy (or another Python program)';
      if (Found <> '') and (Pos(Found, Result) = 0) then
        Result := Result + '*  ' + Found + #13#10;
    end;
  except
    Log('Process check failed: ' + GetExceptionMessage);
  end;
end;

{ Ask for those to be closed before pip runs. Cancel (or a /SUPPRESSMSGBOXES
  install) carries on anyway - pip may well not need to touch those files. }
procedure AskToCloseBlockingPrograms();
var
  Running: string;
begin
  WizardForm.StatusLabel.Caption := 'Checking whether DaVinci Resolve or Buddy is running...';
  Running := FindBlockingPrograms();
  while Running <> '' do
  begin
    Log('Still running: ' + Running);
    if SuppressibleMsgBox('Buddy needs to update some Python packages, but these are still running and ' +
         'may have those files open:' + #13#10#13#10 + Running + #13#10 +
         'Quit Buddy from the system tray and close DaVinci Resolve, then click Retry. ' +
         '(Cancel tries anyway.)', mbConfirmation, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
      Exit;
    Running := FindBlockingPrograms();
  end;
end;

procedure ReportPipFailure();
var
  Reason: string;
begin
  if PipFilesLocked then
    Reason := 'Some files were in use, so pip couldn''t replace them. Quit Buddy from the system tray ' +
              'and close DaVinci Resolve, then run this installer again.'
  else if PipNetworkError then
    Reason := 'pip couldn''t reach the Python package server (pypi.org). Check the internet ' +
              'connection, proxy or firewall, then run this installer again.'
  else if PipNoMatch then
    Reason := 'pip found no version of one of the packages for this Python (' + PythonExe + ') - ' +
              'it may be too new for that package yet. pip''s message below names the package.'
  else
    Reason := 'Run this installer again. If it still fails, the details below will say why.';
  if PipLastError <> '' then
    Reason := Reason + #13#10#13#10 + 'pip said: ' + Copy(PipLastError, 1, 300);

  SuppressibleMsgBox('Buddy is installed, but its Python packages couldn''t be installed.' + #13#10#13#10 +
    Reason + #13#10#13#10 +
    'Or install them yourself with:' + #13#10 +
    '"' + PythonExe + '" -m pip install PySide6 pillow numpy openpyxl pynput pymupdf pymupdf4llm cryptography' + #13#10#13#10 +
    'Full details are in the setup log:' + #13#10 + ExpandConstant('{log}'),
    mbError, MB_OK, IDOK);
end;

procedure ProvisionPython();
var
  Ok: Boolean;
begin
  if NeedPython and not InstallPython() then
  begin
    SuppressibleMsgBox('Python couldn''t be installed automatically. Install Python 3.10 or newer (64-bit) ' +
           'from python.org, then run this installer again.' + #13#10#13#10 +
           'Details are in the setup log:' + #13#10 + ExpandConstant('{log}'), mbError, MB_OK, IDOK);
    Exit;
  end;
  Log('Using Python: ' + PythonExe);

  WizardForm.StatusLabel.Caption := 'Checking Buddy''s Python packages...';
  if PackagesPresent() then
    Exit;

  if not NeedPython then
    AskToCloseBlockingPrograms();

  PipNetworkError := False;
  PipFilesLocked := False;
  PipNoMatch := False;
  PipLastError := '';
  WizardForm.StatusLabel.Caption := 'Downloading and installing Buddy''s Python packages ' +
    '(about 300 MB - this can take a few minutes)...';
  Ok := PipInstall('') and PackagesPresent();
  { A Python installed for all users (Program Files) can't be written to
    without admin - fall back to the user's own site-packages. }
  if not Ok then
    Ok := PipInstall('--user') and PackagesPresent();

  if not Ok then
    ReportPipFailure();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    ProvisionPython();
end;

{ --------------------------------------------------------------- uninstall }

{ Ends every process whose command line runs the app folder's
  resolve_watcher.py - matched on that full script path, so nothing else is
  ever touched. }
procedure KillWatcherProcesses();
var
  Locator, Service, Procs, P: Variant;
  I, Pid, ResultCode: Integer;
  Script, CmdLine: string;
begin
  Script := Lowercase(ExpandConstant('{app}\resolve_watcher.py'));
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Procs := Service.ExecQuery('SELECT ProcessId, CommandLine FROM Win32_Process');
    for I := 0 to Procs.Count - 1 do
    begin
      P := Procs.ItemIndex(I);
      if not VarIsNull(P.CommandLine) then
      begin
        CmdLine := P.CommandLine;
        if Pos(Script, Lowercase(CmdLine)) > 0 then
        begin
          Pid := P.ProcessId;
          Log(Format('Stopping the Resolve watcher (pid %d).', [Pid]));
          Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /PID ' + IntToStr(Pid), '',
            SW_HIDE, ewWaitUntilTerminated, ResultCode);
        end;
      end;
    end;
  except
    Log('Watcher process check failed: ' + GetExceptionMessage);
  end;
end;

{ Undo "Start Buddy automatically when Resolve starts": the Run value (so
  nothing starts at the next login), then the watcher running now. }
procedure StopResolveWatcher();
var
  LockFile: string;
  Waited: Integer;
begin
  if RegValueExists(HKCU, WatcherRunKey, WatcherRunValue) then
  begin
    RegDeleteValue(HKCU, WatcherRunKey, WatcherRunValue);
    Log('Removed the Resolve watcher''s startup entry.');
  end;
  { A watcher checks for the stop file about once a second and exits; its
    lock file can be deleted as soon as it has. }
  LockFile := ExpandConstant('{app}\watcher.lock');
  if FileExists(LockFile) then
  begin
    SaveStringToFile(ExpandConstant('{app}\watcher.stop'), 'stop', False);
    Waited := 0;
    while FileExists(LockFile) and not DeleteFile(LockFile) and (Waited < 5000) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
  end;
  { Anything still running: a watcher from before stop files existed, or
    one that didn't answer in time. }
  KillWatcherProcesses();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { usUninstall: before any file is removed, so [UninstallDelete] then finds
    the watcher's files unlocked. }
  if CurUninstallStep = usUninstall then
    StopResolveWatcher();
end;
