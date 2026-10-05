{ files.pas: lo que el instalador escribe por su cuenta (carpetas de datos, .env, config.json inicial, grupo
  «VMS Operadores», red privada) y la limpieza de la v1. Servicios, ACL, firewall y TLS: vmsctl.
  Dueño: B3. }

{ Ejecuta un programa del sistema sin analizar su texto (solo el código de salida). }
function RunSystemTool(const Exe, Params: String): Integer;
var
  Code: Integer;
begin
  Result := -1;
  Log('Ejecutando: ' + ExtractFileName(Exe) + ' ' + Params);
  if Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := Code
  else
    Log('No se pudo ejecutar ' + Exe + ': ' + SysErrorMessage(Code));
  Log('  -> código ' + IntToStr(Result));
end;

{ Deja un archivo solo para SYSTEM y Administradores (por SID; vale en Windows en cualquier idioma).
  Defensa en profundidad: «vmsctl acl apply» pone después la ACL definitiva con los SID de los servicios. }
procedure ProtectForAdmins(const Path: String; const IsDir: Boolean);
var
  Inh: String;
begin
  Inh := '';
  if IsDir then
    Inh := '(OI)(CI)';
  RunSystemTool(ExpandConstant('{sys}\icacls.exe'), AddQuotes(Path) + ' /inheritance:r /grant:r *S-1-5-18:' + Inh +
    'F *S-1-5-32-544:' + Inh + 'F');
end;

function EnsureDir(const Dir: String): Boolean;
begin
  Result := DirExists(Dir) or ForceDirectories(Dir);
  if not Result then
    Log('No se pudo crear la carpeta ' + Dir);
end;

{ ------------------------------------------------------------------ .env }

function EnvLineKey(const Line: String): String;
var
  T: String;
begin
  Result := '';
  T := Trim(Line);
  if (T = '') or (T[1] = '#') or (Pos('=', T) = 0) then
    Exit;
  Result := Trim(Copy(T, 1, Pos('=', T) - 1));
  if Pos('export ', Result) = 1 then
    Result := Trim(Copy(Result, 8, Length(Result)));
end;

function EnvLineHasValue(const Line: String): Boolean;
var
  V: String;
begin
  V := Trim(Copy(Line, Pos('=', Line) + 1, Length(Line)));
  Result := (V <> '') and (V <> '''''') and (V <> '""');
end;

{ Fija Key en el .env. Con OnlyIfMissing no toca un valor que ya exista (instalación o v1 anteriores). }
procedure EnvSet(var Lines: TArrayOfString; const Key, Value: String; const OnlyIfMissing: Boolean);
var
  I, N: Integer;
  Found: Boolean;
begin
  Found := False;
  N := GetArrayLength(Lines);
  for I := 0 to N - 1 do
  begin
    if EnvLineKey(Lines[I]) = Key then
    begin
      Found := True;
      if (not OnlyIfMissing) or (not EnvLineHasValue(Lines[I])) then
        Lines[I] := Key + '=' + EnvQuote(Value);
    end;
  end;
  if not Found then
  begin
    SetArrayLength(Lines, N + 1);
    Lines[N] := Key + '=' + EnvQuote(Value);
  end;
end;

{ Valor actual de Key en un .env ya cargado, sin comillas; '' si no está. Solo para valores sencillos
  (puertos), no para secretos. }
function EnvGet(var Lines: TArrayOfString; const Key: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to GetArrayLength(Lines) - 1 do
    if EnvLineKey(Lines[I]) = Key then
    begin
      Result := Trim(Copy(Lines[I], Pos('=', Lines[I]) + 1, Length(Lines[I])));
      Result := RemoveQuotes(Result);
      if (Length(Result) >= 2) and (Result[1] = '''') and (Result[Length(Result)] = '''') then
        Result := Copy(Result, 2, Length(Result) - 2);
    end;
end;

function EnvPath: String;
begin
  Result := AddBackslash(gDataDir) + '.env';
end;

function LoadEnv(var Lines: TArrayOfString): Boolean;
begin
  SetArrayLength(Lines, 0);
  Result := FileExists(EnvPath) and LoadStringsFromFile(EnvPath, Lines);
end;

function SaveEnv(var Lines: TArrayOfString): Boolean;
var
  I, N: Integer;
  AllLines: TArrayOfString;
begin
  N := GetArrayLength(Lines);
  if not FileExists(EnvPath) then
  begin
    { Se crea con solo la cabecera y se protege ANTES de escribir secretos en él. }
    SetArrayLength(AllLines, N + 1);
    AllLines[0] := '# VMS Multimarca: ajustes de este equipo (lo crea el instalador; ver docs/INSTALACION-WINDOWS.md)';
    SaveStringsToUTF8FileWithoutBOM(EnvPath, AllLines, False);
    ProtectForAdmins(EnvPath, False);
    for I := 0 to N - 1 do
      AllLines[I + 1] := Lines[I];
  end
  else
  begin
    SetArrayLength(AllLines, N);
    for I := 0 to N - 1 do
      AllLines[I] := Lines[I];
  end;
  Result := SaveStringsToUTF8FileWithoutBOM(EnvPath, AllLines, False);
  if not Result then
    Log('No se pudo escribir ' + EnvPath);
  { Otra vez al final: si la escritura sustituyó el archivo, habría heredado los permisos de la carpeta. }
  ProtectForAdmins(EnvPath, False);
end;

{ ------------------------------------------------------------------ config.json inicial }

{ Solo en una instalación nueva (sin config.json): sede y carpeta de grabaciones. El resto lo pone el backend
  con sus valores por defecto (CONTRATO §3.3: AppConfig, CONFIG_VERSION = 2). Nunca se sobrescribe. }
function SeedConfigJson(const SiteId, SiteName, SiteCode, RecordingsDir: String): Boolean;
var
  Lines: TArrayOfString;
  Path, Rec: String;
begin
  Path := AddBackslash(gDataDir) + 'config\config.json';
  Result := True;
  if FileExists(Path) then
    Exit;
  EnsureDir(AddBackslash(gDataDir) + 'config');
  Rec := 'null';
  if (RecordingsDir <> '') and not PathSame(RemoveBackslash(RecordingsDir), DefaultRecordingsDir) then
    Rec := '"' + JsonEscape(RemoveBackslash(RecordingsDir)) + '"';
  SetArrayLength(Lines, 12);
  Lines[0] := '{';
  Lines[1] := '  "version": 2,';
  Lines[2] := '  "settings": {';
  Lines[3] := '    "site": {';
  Lines[4] := '      "id": "' + JsonEscape(SiteId) + '",';
  Lines[5] := '      "name": "' + JsonEscape(SiteName) + '",';
  Lines[6] := '      "code": "' + JsonEscape(SiteCode) + '"';
  Lines[7] := '    },';
  Lines[8] := '    "recording": {"recordings_dir": ' + Rec + '}';
  Lines[9] := '  },';
  Lines[10] := '  "x-installer": {"version": "{#AppVersion}"}';
  Lines[11] := '}';
  Result := SaveStringsToUTF8FileWithoutBOM(Path, Lines, False);
  Log('config.json inicial escrito (sede ' + SiteId + ').');
end;

{ ------------------------------------------------------------------ grabaciones }

{ Crea la carpeta de grabaciones y, si la crea el instalador, deja una marca: solo una carpeta con esa marca se
  borra al desinstalar con «/PURGE» (nunca una carpeta que ya existía, ni la raíz de un disco). }
function PrepareRecordingsDir(const Dir: String): Boolean;
var
  Marker: TArrayOfString;
begin
  Result := True;
  if Dir = '' then
    Exit;
  if not DirExists(Dir) then
  begin
    Result := ForceDirectories(Dir);
    if Result then
    begin
      gRecordingsDirCreated := True;
      SetArrayLength(Marker, 1);
      Marker[0] := 'Carpeta de grabaciones creada por el instalador de VMS Multimarca {#AppVersion}.';
      SaveStringsToUTF8FileWithoutBOM(AddBackslash(Dir) + VMS_MARKER_FILE, Marker, False);
    end;
  end;
end;

{ ------------------------------------------------------------------ grupo «VMS Operadores» }

{ Grupo local que puede leer kiosk.token (CONTRATO §13.2 y §17.1). Se crea si no existe y se mete al usuario
  que instala. Solo códigos de salida de net.exe: nunca su texto (está localizado). }
procedure EnsureOperatorsGroup;
var
  NetExe, User: String;
begin
  NetExe := ExpandConstant('{sys}\net.exe');
  if RunSystemTool(NetExe, 'localgroup ' + AddQuotes(VMS_OPERATORS_GROUP)) <> 0 then
    if RunSystemTool(NetExe, 'localgroup ' + AddQuotes(VMS_OPERATORS_GROUP) + ' /add /comment:' +
      AddQuotes(CustomMessage('OperatorsGroupComment'))) <> 0 then
      Log('AVISO: no se pudo crear el grupo ' + VMS_OPERATORS_GROUP);
  User := GetUserNameString;
  if (User <> '') and (Pos('\', User) = 0) then
    User := GetComputerNameString + '\' + User;
  { Si ya es miembro, net devuelve error (1378): no importa. }
  RunSystemTool(NetExe, 'localgroup ' + AddQuotes(VMS_OPERATORS_GROUP) + ' ' + AddQuotes(User) + ' /add');
end;

{ ------------------------------------------------------------------ perfil de red }

{ Redes activas marcadas como Públicas (MSFT_NetConnectionProfile por WMI, sin PowerShell ni texto localizado). }
function DetectPublicNetworks(var Names: String): Integer;
var
  Locator, Service, Items, Item: Variant;
  I, N: Integer;
  NetName, Alias: String;
begin
  Result := 0;
  Names := '';
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('', 'root\StandardCimv2');
    Items := Service.ExecQuery('SELECT Name, InterfaceAlias FROM MSFT_NetConnectionProfile WHERE NetworkCategory = 0');
    N := Items.Count;
    for I := 0 to N - 1 do
    begin
      Item := Items.ItemIndex(I);
      NetName := Item.Name;
      Alias := Item.InterfaceAlias;
      if Names <> '' then
        Names := Names + ', ';
      Names := Names + '«' + NetName + '» (' + Alias + ')';
      Result := Result + 1;
    end;
  except
    Log('No se pudo consultar el perfil de red: ' + GetExceptionMessage);
  end;
end;

{ Cambia a Privada las redes Públicas (lo mismo que Set-NetConnectionProfile, por WMI). Solo con permiso
  expreso: casilla del asistente o SetPrivateNetwork=1 en el .inf. }
function SetPublicNetworksPrivate: Integer;
var
  Locator, Service, Items, Item: Variant;
  I, N: Integer;
  NetName: String;
begin
  Result := 0;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('', 'root\StandardCimv2');
    Items := Service.ExecQuery('SELECT * FROM MSFT_NetConnectionProfile WHERE NetworkCategory = 0');
    N := Items.Count;
    for I := 0 to N - 1 do
    begin
      Item := Items.ItemIndex(I);
      NetName := Item.Name;
      Item.NetworkCategory := 1;
      Item.Put_();
      Log('Red «' + NetName + '» cambiada a Privada.');
      Result := Result + 1;
    end;
  except
    Log('No se pudo cambiar el perfil de red: ' + GetExceptionMessage);
  end;
end;

{ ------------------------------------------------------------------ v1 }

{ Restos de la v1 de install.ps1 en Program Files, una vez migrados los servicios. Nunca toca datos. }
procedure RemoveV1Leftovers;
var
  Base: String;
begin
  Base := gV1InstallDir;
  DelTree(Base + '\vms', True, True, True);
  DelTree(Base + '\analytics', True, True, True);
  DelTree(Base + '\central', True, True, True);
  DelTree(Base + '\deploy', True, True, True);
  DelTree(Base + '\python', True, True, True);
  DelTree(Base + '\.venv', True, True, True);
  DelTree(Base + '\services', True, True, True);
  DelTree(Base + '\downloads', True, True, True);
  DelTree(Base + '\models', True, True, True);
  DeleteFile(Base + '\bin\mediamtx.exe');
  DeleteFile(Base + '\bin\MEDIAMTX-LICENSE.txt');
  DeleteFile(Base + '\pyproject.toml');
  DeleteFile(Base + '\.env.example');
  DeleteFile(Base + '\LEEME.md');
  DeleteFile(Base + '\THIRD_PARTY_NOTICES.txt');
  DeleteFile(Base + '\requirements-vms.txt');
  DeleteFile(Base + '\requirements-analytics.txt');
  DeleteFile(Base + '\requirements-central.txt');
  Log('Restos de la v1 eliminados de ' + Base + ' (los datos siguen en ' + gDataDir + ').');
end;
