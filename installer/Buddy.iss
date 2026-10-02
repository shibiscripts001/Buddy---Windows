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
;      failure says why instead of guessing;
;   4. optionally adds Start menu / desktop shortcuts that open Buddy without
;      Resolve's Scripts menu: pythonw.exe of that same Python running the
;      same Buddy.py, so both ways start one app, from one install, and
;      updates reach both. (A Buddy started this way reaches Resolve only
;      in Resolve Studio, with its "External scripting using" set to Local.
;      The free version runs Buddy only from Workspace > Scripts, and only
;      up to 21.0.4: from 21.1 on, only Studio supports Python.)
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
; Where InstallPython puts it (its per-user default, passed explicitly): the
; shortcuts are made before Python is installed, so they need to know.
#define PythonDir "{localappdata}\Programs\Python\Python313"
; main.py's SetCurrentProcessExplicitAppUserModelID: a shortcut carrying the
; same ID is the one Buddy's taskbar button belongs to (and pins as).
#define AppUserModelID "Buddy.ResolveTools"
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
; Inno Setup 6.7+ turns on Windows' RedirectionGuard by default, and - despite
; its docs - the programs Setup runs get it too. It makes any junction a
; non-admin user made untraversable (WinError 448), and pip resolves every
; PATH entry after installing a package with scripts (cffi, PySide6), so one
; user junction on PATH (e.g. a tool's bin folder) failed the whole install.
; Setup runs per user without admin, so the guard protects nothing here.
#if Ver >= EncodeVer(6, 7, 0)
RedirectionGuard=no
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedLabel=Buddy is installed.%n%nOpen DaVinci Resolve and choose Workspace > Scripts > Buddy, or (with Resolve Studio) use Buddy's Start menu or desktop shortcut if you added one. If Resolve was already open, restart it so the menu picks Buddy up. If Buddy was running, quit it from the system tray first.

SelectTasksLabel2=Shortcuts open Buddy without going through Resolve's Scripts menu. Only DaVinci Resolve Studio lets a Buddy opened this way connect to it, with Preferences > System > General > "External scripting using" set to Local. In the free version, open Buddy from Workspace > Scripts instead.

[CustomMessages]
ShortcutsGroup=Shortcuts (connect to DaVinci Resolve Studio only):

[Tasks]
Name: "startmenuicon"; Description: "Add Buddy to the &Start menu"; GroupDescription: "{cm:ShortcutsGroup}"
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "{cm:ShortcutsGroup}"; Flags: unchecked

[Files]
Source: "..\build\Buddy.py"; DestDir: "{#ScriptsDir}"; Flags: ignoreversion
Source: "..\build\buddy.zip"; DestDir: "{#ScriptsDir}"; Flags: ignoreversion
Source: "buddy.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "check_packages.py"; DestDir: "{tmp}"; Flags: deleteafterinstall

; Removed again by the uninstaller.
[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{code:BuddyPythonw}"; Parameters: """{#ScriptsDir}\Buddy.py"""; WorkingDir: "{#ScriptsDir}"; IconFilename: "{app}\buddy.ico"; Comment: "Buddy for DaVinci Resolve"; AppUserModelID: "{#AppUserModelID}"; Tasks: startmenuicon
Name: "{autodesktop}\{#AppName}"; Filename: "{code:BuddyPythonw}"; Parameters: """{#ScriptsDir}\Buddy.py"""; WorkingDir: "{#ScriptsDir}"; IconFilename: "{app}\buddy.ico"; Comment: "Buddy for DaVinci Resolve"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

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
  { What pip's output said went wrong (see OnPipLine). }
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
    + '*  If you want them, Start menu and desktop shortcuts to open Buddy without Resolve''s menu '
    + '(these connect to DaVinci Resolve Studio only)' + #13#10
    + '*  Python {#PythonVersion} from python.org - only if this PC doesn''t already have Python 3.10 or newer' + #13#10
    + '*  The Python packages Buddy uses (PySide6, Pillow, NumPy, openpyxl, pynput, PyMuPDF, cryptography) - '
    + 'only the ones that are missing' + #13#10#13#10
    + 'Anything missing is downloaded while installing - up to about 330 MB, so it needs an '
    + 'internet connection and can take a few minutes.' + #13#10#13#10
    + 'Transcription (Whisper) is set up later from inside Buddy, only if you want it.' + #13#10#13#10
    + 'Free version of DaVinci Resolve: Buddy runs from Workspace > Scripts only up to Resolve 21.0.4. '
    + 'From 21.1 on, only DaVinci Resolve Studio supports Python scripts.');

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
    '/quiet InstallAllUsers=0 TargetDir="' + ExpandConstant('{#PythonDir}') + '" ' +
    'PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0 ' +
    'Include_pip=1 Include_dev=0 Include_tcltk=0 Include_test=0 Include_doc=0',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  WizardForm.ProgressGauge.Style := npbstNormal;
  Log(Format('Python installer exit code: %d', [ResultCode]));
  Result := DetectPython(PythonExe);
end;

{ What the shortcuts run ([Icons]): the windowless pythonw.exe of the Python
  Resolve uses. They're made before ssPostInstall installs a missing Python,
  so that one is named by where InstallPython puts it. A Python without a
  pythonw.exe gets its python.exe (a console window behind Buddy). }
function BuddyPythonw(Param: string): string;
var
  Dir: string;
begin
  if NeedPython then
    Dir := ExpandConstant('{#PythonDir}')
  else
    Dir := RemoveBackslashUnlessRoot(ExtractFilePath(PythonExe));
  Result := AddBackslash(Dir) + 'pythonw.exe';
  if not NeedPython and not FileExists(Result) then
    Result := PythonExe;
  Log('Shortcuts run: ' + Result);
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

{ -I everywhere Python runs below: Resolve's fuscript runs Python isolated
  (no per-user site-packages in %APPDATA%\Python), so a package there is
  missing as far as Buddy is concerned. Checking and installing isolated too
  means "present" is what Buddy will actually find, and pip installs into
  the Python's own site-packages instead of calling a per-user copy
  "already satisfied". }
function PackagesPresent(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(PythonExe,
    '-I -c "import PySide6.QtWidgets, PySide6.QtMultimedia, PySide6.QtWebEngineWidgets, PIL, numpy, openpyxl, pynput, pymupdf, pymupdf4llm, cryptography"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  { Present is not enough: each must be at a version requirements.txt allows
    (its lower bounds are security floors), or pip upgrades it. }
  if Result then
    Result := Exec(PythonExe,
      '-I "' + ExpandConstant('{tmp}\check_packages.py') + '" "' + ExpandConstant('{tmp}\requirements.txt') + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function PipInstall(): Boolean;
var
  ResultCode: Integer;
begin
  PipLines := 0;
  WizardForm.ProgressGauge.Style := npbstNormal;
  WizardForm.ProgressGauge.Max := 100;
  Result := ExecAndLogOutput(PythonExe,
    { --no-warn-script-location: Python isn't put on PATH on purpose, and the
      check behind that warning is what walked PATH (see RedirectionGuard). }
    '-I -m pip install --disable-pip-version-check --no-warn-script-location --progress-bar off' +
    ' -r "' + ExpandConstant('{tmp}\requirements.txt') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode, @OnPipLine) and (ResultCode = 0);
  Log(Format('pip install exit code: %d', [ResultCode]));
end;

{ Programs that may have Buddy's Python packages loaded: Resolve (Buddy runs
  inside its script host) and anything running from Buddy's Python itself
  (Buddy in the tray). Windows won't let pip replace a loaded .pyd, so an
  update fails while any of these are open. The Resolve watcher runs from
  that Python too but isn't counted: it imports only the standard library,
  so it holds nothing pip replaces - and it has no tray icon to quit it by,
  so asking would just loop. }
function FindBlockingPrograms(): string;
var
  Locator, Service, Procs, P: Variant;
  I: Integer;
  PyDir, Watcher, Name, Path, CmdLine, Found: string;
begin
  Result := '';
  PyDir := Lowercase(ExtractFilePath(PythonExe));
  Watcher := Lowercase(ExpandConstant('{app}\resolve_watcher.py'));
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Procs := Service.ExecQuery('SELECT Name, ExecutablePath, CommandLine FROM Win32_Process');
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
      CmdLine := '';
      if not VarIsNull(P.CommandLine) then
      begin
        CmdLine := P.CommandLine;
        CmdLine := Lowercase(CmdLine);
      end;
      Found := '';
      if (Name = 'resolve.exe') or (Name = 'fuscript.exe') then
        Found := 'DaVinci Resolve'
      else if Pos(Watcher, CmdLine) > 0 then
        Log('Ignoring the Resolve watcher (stdlib only): ' + CmdLine)
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
    '"' + PythonExe + '" -s -m pip install "PySide6>=6.10,<7" "pillow>=12.3" numpy openpyxl pynput "pymupdf>=1.26.7" pymupdf4llm "cryptography>=50.0"' + #13#10#13#10 +
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
  { No --user fallback: Resolve never looks in the per-user site-packages
    (see PackagesPresent), so it could only report success while Buddy still
    can't import anything. A Python installed for all users (Program Files)
    needs the manual command from an admin prompt instead. }
  Ok := PipInstall() and PackagesPresent();

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
