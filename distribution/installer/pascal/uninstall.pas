{ uninstall.pas: desinstalación (PLAN-V2 §1.4). Pregunta «¿Conservar grabaciones y configuración?» (Sí por
  defecto); en silencio se conservan salvo /PURGE. Servicios y firewall: vmsctl. Dueño: B3. }

var
  gUninstPurge: Boolean;
  gUninstDataDir: String;
  gUninstRecDir: String;

{ vmsctl con el que desinstalar: el de la versión activa, si no el de esta versión, si no cualquiera. }
function UninstallVmsctl: String;
var
  Active, Base: String;
  Rec: TFindRec;
begin
  Base := ExpandConstant('{app}\versions');
  Result := '';
  Active := ReadActiveVersion(gUninstDataDir);
  if (Active <> '') and FileExists(Base + '\' + Active + '\bin\vmsctl.exe') then
    Result := Base + '\' + Active + '\bin\vmsctl.exe'
  else if FileExists(Base + '\{#AppVersion}\bin\vmsctl.exe') then
    Result := Base + '\{#AppVersion}\bin\vmsctl.exe'
  else if FindFirst(Base + '\*', Rec) then
  begin
    try
      repeat
        if (Result = '') and (Rec.Name <> '.') and (Rec.Name <> '..') and
          FileExists(Base + '\' + Rec.Name + '\bin\vmsctl.exe') then
          Result := Base + '\' + Rec.Name + '\bin\vmsctl.exe';
      until not FindNext(Rec);
    finally
      FindClose(Rec);
    end;
  end;
end;

{ settings.recording.recordings_dir de config.json ('' si no está o es null). Se lee antes de borrar los datos. }
function ConfigRecordingsDir(const DataDir: String): String;
var
  Raw: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(AddBackslash(DataDir) + 'config\config.json', Raw) then
    Result := JsonGetString(Utf8Decode(Raw), 'recordings_dir');
end;

{ Solo se borra una carpeta de datos que no sea la raíz de un disco ni una carpeta del sistema. }
function SafeToPurge(const Dir: String): Boolean;
begin
  Result := (Dir <> '') and IsAbsolutePath(Dir) and not IsRootPath(Dir) and
    not PathSame(RemoveBackslash(Dir), RemoveBackslash(ExpandConstant('{commonappdata}'))) and
    not PathIsInside(ExpandConstant('{win}'), Dir) and not PathIsInside(Dir, ExpandConstant('{win}')) and
    not PathSame(RemoveBackslash(Dir), RemoveBackslash(ExpandConstant('{commonpf64}')));
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  gUninstDataDir := ExpandConstant('{commonappdata}\VMSMultimarca');
  RegQueryStringValue(HKLM, VMS_REG_KEY, 'DataDir', gUninstDataDir);
  gUninstRecDir := '';
  RegQueryStringValue(HKLM, VMS_REG_KEY, 'RecordingsDir', gUninstRecDir);
  { Tras reinstalar encima de datos conservados, el registro ya no la tiene: se lee de config.json. }
  if gUninstRecDir = '' then
    gUninstRecDir := ConfigRecordingsDir(gUninstDataDir);
  if VmsHasSwitch('PURGE') then
    gUninstPurge := True
  else if UninstallSilent then
    gUninstPurge := False
  else
    gUninstPurge := SuppressibleMsgBox(FmtMessage(CustomMessage('UninstKeepData'), [gUninstDataDir]),
      mbConfirmation, MB_YESNO, IDYES) = IDNO;
  if gUninstPurge then
    Log('Desinstalación con borrado de datos y grabaciones (/PURGE o respuesta «No»).')
  else
    Log('Desinstalación conservando datos y grabaciones en ' + gUninstDataDir);
end;

procedure UninstallServices;
var
  Exe, Purge: String;
begin
  Exe := UninstallVmsctl;
  if Exe = '' then
  begin
    Log('AVISO: no encuentro vmsctl.exe: no se pueden quitar servicios ni reglas de firewall.');
    Exit;
  end;
  Purge := '';
  if gUninstPurge then
    Purge := ' --purge';
  UninstallProgressForm.StatusLabel.Caption := CustomMessage('StatusUninstServices');
  RunVmsctl(Exe, 'services stop');
  RunVmsctl(Exe, 'services uninstall' + Purge);
  RunVmsctl(Exe, 'firewall remove');
end;

procedure PurgeData;
begin
  if SafeToPurge(gUninstDataDir) then
  begin
    if DelTree(gUninstDataDir, True, True, True) then
      Log('Carpeta de datos borrada: ' + gUninstDataDir)
    else
      Log('AVISO: no se pudo borrar del todo ' + gUninstDataDir);
  end
  else
    Log('AVISO: la carpeta de datos ' + gUninstDataDir + ' no se borra por seguridad (raíz o carpeta del sistema).');
  { La carpeta de grabaciones solo si la creó el instalador (su marca está dentro) y no es la raíz de un disco. }
  if (gUninstRecDir <> '') and SafeToPurge(gUninstRecDir) and
    FileExists(AddBackslash(gUninstRecDir) + VMS_MARKER_FILE) then
  begin
    if DelTree(gUninstRecDir, True, True, True) then
      Log('Carpeta de grabaciones borrada: ' + gUninstRecDir);
  end
  else if gUninstRecDir <> '' then
    Log('La carpeta de grabaciones ' + gUninstRecDir + ' no la creó el instalador: se conserva.');
  RunSystemTool(ExpandConstant('{sys}\net.exe'), 'localgroup ' + AddQuotes(VMS_OPERATORS_GROUP) + ' /delete');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    UninstallServices
  else if CurUninstallStep = usPostUninstall then
  begin
    { Versiones y ranuras que instaló el actualizador (no constan en el registro de desinstalación). }
    DelTree(ExpandConstant('{app}\versions'), True, True, True);
    DelTree(ExpandConstant('{app}\updater'), True, True, True);
    DelTree(ExpandConstant('{app}\bin'), True, True, True);
    RegDeleteKeyIncludingSubkeys(HKLM, VMS_REG_KEY);
    if gUninstPurge then
      PurgeData;
  end;
end;
