{ state.pas: estado global del instalador, detección de lo instalado y lectura de /LOADINF y /SECRETS.
  Dueño: B3. }

var
  { --- detección (InitializeSetup) }
  gDataDir: String;             { carpeta de datos (CONTRATO §13.1); la de la v1 si se instaló en otra }
  gInstalledVersion: String;    { versión v2 instalada (registro), '' si no hay }
  gActiveVersion: String;       { versión activa según state\active.json, '' si no hay }
  gHasConfig: Boolean;          { hay config.json: se conserva la configuración (sede, grabaciones) }
  gHasUsers: Boolean;           { hay users.json: no se pide administrador }
  gV1Detected: Boolean;         { instalación v1 de install.ps1 (WinSW) en Program Files }
  gV1InstallDir: String;
  gWinBuild: Integer;
  gIsWin10: Boolean;
  { --- respuestas: asistente, /LOADINF y /SECRETS }
  gInfFile: String;
  gSecretAdminPassword: String;
  gSecretSiteToken: String;
  gSecretPgDsn: String;
  gSecretsLoaded: Boolean;
  { --- resultado de la instalación }
  gFailedStep: String;
  gFailedMessage: String;
  gFailedCode: Integer;
  gHealthOk: Boolean;
  gHealthMessage: String;
  gUpdaterLocked: Boolean;
  gStoppedOld: Boolean;          { se pararon los servicios de la versión anterior }
  gPostInstallReached: Boolean;
  gRecordingsDirCreated: Boolean;
  gPublicNetworks: String;
  gPublicNetworkCount: Integer;

function ProgramDir: String;
begin
  Result := ExpandConstant('{commonpf64}\VMSMultimarca');
end;

function DefaultDataDir: String;
begin
  Result := ExpandConstant('{commonappdata}\VMSMultimarca');
end;

function DefaultRecordingsDir: String;
begin
  Result := AddBackslash(gDataDir) + 'recordings';
end;

{ ------------------------------------------------------------------ /LOADINF }

{ Clave de la sección [Setup] del .inf de /LOADINF (las estándar de Inno las lee Inno; estas son nuestras). }
function InfGet(const Key, Default: String): String;
var
  Raw: AnsiString;
begin
  Result := Default;
  if gInfFile <> '' then
    Result := Trim(GetIniString('Setup', Key, Default, gInfFile));
  { Windows lee los .ini en ANSI salvo que estén en UTF-16. Un .inf guardado en UTF-8 llega como «Ã±» en lugar
    de «ñ»: se vuelve a decodificar como UTF-8. }
  if (Pos('Ã', Result) > 0) or (Pos('Â', Result) > 0) then
  begin
    Raw := Result;
    Result := Utf8Decode(Raw);
  end;
end;

function InfGetBool(const Key: String; const Default: Boolean): Boolean;
begin
  Result := Default;
  if gInfFile <> '' then
    Result := GetIniBool('Setup', Key, Default, gInfFile);
end;

{ ------------------------------------------------------------------ /SECRETS }

{ Lee secrets.json (objeto JSON con admin_password, site_token y pg_dsn) y lo BORRA (PLAN-V2 §1.4): los secretos
  nunca van en el .inf ni en la línea de órdenes. Devuelve '' o el error. }
function LoadSecretsFile(const FileName: String): String;
var
  Raw: AnsiString;
  Json: String;
begin
  Result := '';
  if not FileExists(FileName) then
  begin
    Result := FmtMessage(CustomMessage('ErrSecretsMissing'), [FileName]);
    Exit;
  end;
  if not LoadStringFromFile(FileName, Raw) then
  begin
    Result := FmtMessage(CustomMessage('ErrSecretsRead'), [FileName]);
    Exit;
  end;
  Json := Utf8Decode(Raw);
  if (Length(Json) > 0) and (Ord(Json[1]) = $FEFF) then
    Json := Copy(Json, 2, Length(Json));
  if Pos('{', Trim(Json)) <> 1 then
  begin
    Result := FmtMessage(CustomMessage('ErrSecretsFormat'), [FileName]);
    Exit;
  end;
  gSecretAdminPassword := JsonGetString(Json, 'admin_password');
  gSecretSiteToken := JsonGetString(Json, 'site_token');
  gSecretPgDsn := JsonGetString(Json, 'pg_dsn');
  gSecretsLoaded := True;
  if not DeleteFile(FileName) then
    Log('AVISO: no se pudo borrar el archivo de secretos ' + FileName + '; bórralo a mano.')
  else
    Log('Secretos leídos de /SECRETS y archivo borrado.');
end;

{ ------------------------------------------------------------------ detección }

function ReadActiveVersion(const DataDir: String): String;
var
  Raw: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(AddBackslash(DataDir) + 'state\active.json', Raw) then
    Result := JsonGetString(Utf8Decode(Raw), 'active');
end;

{ La v1 (install.ps1) dejaba los envoltorios de WinSW en <instalación>\services\VMS*.xml y el código en
  <instalación>\vms. Si el XML fija otra carpeta de datos (-DataDir), se respeta. }
procedure DetectV1;
var
  Xml: AnsiString;
  S, Marker: String;
  P, Q: Integer;
begin
  gV1InstallDir := ProgramDir;
  gV1Detected := FileExists(gV1InstallDir + '\services\VMSBackend.xml') or
    FileExists(gV1InstallDir + '\services\VMSCentral.xml') or
    FileExists(gV1InstallDir + '\vms\__init__.py');
  if not gV1Detected then
    Exit;
  Log('Instalación v1 (install.ps1) detectada en ' + gV1InstallDir);
  if LoadStringFromFile(gV1InstallDir + '\services\VMSBackend.xml', Xml) then
  begin
    S := Utf8Decode(Xml);
    Marker := 'name="VMS_DATA_DIR" value="';
    P := Pos(Marker, S);
    if P > 0 then
    begin
      S := Copy(S, P + Length(Marker), Length(S));
      Q := Pos('"', S);
      if Q > 1 then
      begin
        S := Copy(S, 1, Q - 1);
        StringChangeEx(S, '&amp;', '&', True);
        StringChangeEx(S, '&apos;', '''', True);
        if DirExists(S) then
        begin
          gDataDir := S;
          Log('La v1 usaba la carpeta de datos ' + S + ': se conserva.');
        end;
      end;
    end;
  end;
end;

procedure DetectInstalledState;
var
  V: String;
  Version: TWindowsVersion;
  Sim: String;
begin
  gDataDir := DefaultDataDir;
  RegQueryStringValue(HKLM, VMS_REG_KEY, 'DataDir', gDataDir);
  if gDataDir = '' then
    gDataDir := DefaultDataDir;
  gInstalledVersion := '';
  if RegQueryStringValue(HKLM, VMS_REG_KEY, 'InstalledVersion', V) then
    gInstalledVersion := Trim(V);
  { La clave de desinstalación la actualiza también el actualizador (CONTRATO §13.7). Manda la más nueva. }
  if RegQueryStringValue(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdPlain}_is1',
    'DisplayVersion', V) then
    if (gInstalledVersion = '') or (CompareSemVer(Trim(V), gInstalledVersion) > 0) then
      gInstalledVersion := Trim(V);
  DetectV1;
  gActiveVersion := ReadActiveVersion(gDataDir);
  gHasConfig := FileExists(AddBackslash(gDataDir) + 'config\config.json');
  gHasUsers := FileExists(AddBackslash(gDataDir) + 'config\users.json');
  GetWindowsVersionEx(Version);
  gWinBuild := Version.Build;
#ifdef TestBuild
  Sim := VmsParamValue('SIMULATEWINBUILD');
  if Sim <> '' then
  begin
    gWinBuild := StrToIntDef(Sim, gWinBuild);
    Log('Build de prueba: se simula Windows con número de compilación ' + IntToStr(gWinBuild));
  end;
#endif
  { Windows 10 22H2 = 19045; Windows 11 empieza en 22000. Server 2022 = 20348 (admitido). }
  if Version.ProductType = VER_NT_WORKSTATION then
    gIsWin10 := gWinBuild < 22000
  else
    gIsWin10 := gWinBuild < 20348;
  Log('Estado: instalada=' + gInstalledVersion + ' activa=' + gActiveVersion + ' datos=' + gDataDir +
    ' config=' + BoolText(gHasConfig) + ' usuarios=' + BoolText(gHasUsers) + ' v1=' + BoolText(gV1Detected) +
    ' build=' + IntToStr(gWinBuild));
end;
