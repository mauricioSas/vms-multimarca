{ events.pas: funciones de evento del instalador (Inno Setup 7). Orden de la instalación:
    InitializeSetup      secretos, Windows 10, negativa a bajar de versión, detección
    InitializeWizard     páginas propias con los valores de /LOADINF y /SECRETS
    PrepareToInstall     validación completa, cerrojo del actualizador, parada de servicios, puertos
    ssPostInstall        .env, config.json, updater.json, v1 → vmsctl (servicios y puntero, ACL, firewall, TLS)
                         → arranque
                         → «vmsctl health wait --timeout 120»
  Dueño: B3. }

{ ------------------------------------------------------------------ InitializeSetup }

function InitializeSetup: Boolean;
var
  SecretsFile, Err: String;
begin
  Result := True;
  gInfFile := VmsParamValue('LOADINF');
  if gInfFile <> '' then
    gInfFile := ExpandFileName(gInfFile);
  SecretsFile := VmsParamValue('SECRETS');
  if SecretsFile <> '' then
  begin
    Err := LoadSecretsFile(ExpandFileName(SecretsFile));
    if Err <> '' then
    begin
      Log('ERROR: ' + Err);
      SuppressibleMsgBox(Err, mbCriticalError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
  end;
  gMinFreeGB := MIN_FREE_GB_DEFAULT;
#ifdef TestBuild
  if VmsParamValue('MINFREEGB') <> '' then
  begin
    gMinFreeGB := StrToIntDef(VmsParamValue('MINFREEGB'), MIN_FREE_GB_DEFAULT);
    Log('Build de prueba: espacio mínimo para grabaciones = ' + IntToStr(gMinFreeGB) + ' GB');
  end;
#endif
  DetectInstalledState;
  gHasCentralUsers := FileExists(AddBackslash(gDataDir) + 'central\config\users.json');

  { Negativa a bajar de versión (PLAN-V2 §1.4, CONTRATO §13.7). }
  if (gInstalledVersion <> '') and (CompareSemVer(gInstalledVersion, '{#AppVersion}') > 0) then
  begin
    if VmsHasSwitch('ALLOWDOWNGRADE') then
      Log('ALLOWDOWNGRADE: se instala {#AppVersion} encima de ' + gInstalledVersion + ' (soporte técnico).')
    else
    begin
      Err := FmtMessage(CustomMessage('ErrDowngrade'), [gInstalledVersion, '{#AppVersion}']);
      Log('ERROR: ' + Err);
      SuppressibleMsgBox(Err, mbCriticalError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
  end;

  { Windows 10 / Server antiguo: se avisa y no se bloquea (decisión D11). }
  if gIsWin10 then
  begin
    Log('AVISO: ' + CustomMessage('WarnWin10'));
    if SuppressibleMsgBox(CustomMessage('WarnWin10'), mbInformation, MB_OKCANCEL, IDOK) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

{ ------------------------------------------------------------------ asistente }

procedure InitializeWizard;
begin
  CreateAllPages;
#ifdef TestBuild
  { Capturas del asistente en CI: la licencia ya aceptada para poder avanzar solo con «Siguiente». }
  if VmsParamValue('CAPTURESTATE') <> '' then
    WizardForm.LicenseAcceptedRadio.Checked := True;
#endif
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = RecPage.ID then
    Result := not ShowsRecPage
  else if PageID = SitePage.ID then
    Result := not ShowsSitePage
  else if PageID = CentralPage.ID then
    Result := not ShowsCentralPage
  else if PageID = SecPage.ID then
    Result := not NeedsAdminPassword
  else if PageID = NetPage.ID then
    Result := not ShowsNetPage
  else if PageID = ResultPage.ID then
    Result := WizardSilent;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Video: Boolean;
begin
  if CurPageID = RecPage.ID then
    RecInfoLabel.Caption := RecInfoText
  else if CurPageID = SitePage.ID then
  begin
    if SitePage.Values[2] = '' then
      SiteSuggest(nil);
  end
  else if CurPageID = NetPage.ID then
  begin
    Video := IsVideoRole;
    NetHttpsCheck.Visible := Video;
    NetHttpLabel.Visible := Video;
    NetHttpEdit.Visible := Video;
    NetHttpsLabel.Visible := Video;
    NetHttpsEdit.Visible := Video;
  end;
#ifdef TestBuild
  { Build de prueba: deja la página actual en un archivo para el capturador del e2e (tests/windows). }
  if VmsParamValue('CAPTURESTATE') <> '' then
    SaveStringToFile(VmsParamValue('CAPTURESTATE'),
      IntToStr(CurPageID) + '|' + WizardForm.PageNameLabel.Caption + #13#10, False);
#endif
end;

{ Comprobación de puertos antes de instalar (solo instalación nueva: si ya está instalado, los puertos los usan
  nuestros propios servicios, que se paran justo antes de copiar). El tipo de puesto y los puertos van en la orden:
  en una instalación nueva aún no están en el registro ni en el .env. }
function CheckPorts: String;
var
  Code: Integer;
begin
  Result := '';
  if (gActiveVersion <> '') or gV1Detected or not IsVideoRole then
    Exit;
  Code := RunVmsctl(TempVmsctl, 'ports check --role ' + Role + ' --http-port ' + HttpPort + ' --https-port ' +
    HttpsPort + ' --data-dir ' + AddQuotes(gDataDir));
  if Code = VMSCTL_PORT_IN_USE then
    Result := FmtMessage(CustomMessage('ErrPortInUse'), [gLastVmsctlMessage])
  else if Code <> VMSCTL_OK then
    Result := FmtMessage(CustomMessage('ErrPortsCheck'), [gLastVmsctlMessage]);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Err: String;
begin
  Result := True;
  { En silencio la validación completa va en PrepareToInstall (sale con código 7 y el motivo en el registro). }
  if WizardSilent then
    Exit;
  Err := '';
  if CurPageID = RecPage.ID then
    Err := ValidateRecPage
  else if CurPageID = SitePage.ID then
    Err := ValidateSitePage
  else if CurPageID = CentralPage.ID then
    Err := ValidateCentralPage
  else if CurPageID = SecPage.ID then
    Err := ValidateSecPage
  else if CurPageID = NetPage.ID then
  begin
    Err := ValidateNetPage;
    if Err = '' then
      Err := CheckPorts;
  end;
  if Err <> '' then
  begin
    MsgBox(Err, mbError, MB_OK);
    Result := False;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
var
  S: String;
  Free, Total: Int64;
begin
  S := MemoTypeInfo + NewLine + NewLine + MemoComponentsInfo + NewLine + NewLine;
  if MemoTasksInfo <> '' then
    S := S + MemoTasksInfo + NewLine + NewLine;
  S := S + CustomMessage('ReadyVersion') + NewLine + Space + '{#AppVersion}';
  if gInstalledVersion <> '' then
    S := S + ' ' + FmtMessage(CustomMessage('ReadyUpgradeFrom'), [gInstalledVersion]);
  S := S + NewLine + NewLine;
  if gV1Detected then
    S := S + CustomMessage('ReadyV1') + NewLine + Space + CustomMessage('ReadyV1Detail') + NewLine + NewLine;
  if IsVideoRole then
  begin
    S := S + CustomMessage('ReadyRecordings') + NewLine;
    if ShowsRecPage then
    begin
      S := S + Space + RecordingsDir + NewLine;
      if DiskFreeBytes(RecordingsDir, Free, Total) then
        S := S + Space + FmtMessage(CustomMessage('ReadyFree'), [IntToStr(GiB(Free))]) + NewLine;
      if IsSystemDrive(RecordingsDir) then
        S := S + Space + CustomMessage('ReadySystemDrive') + NewLine;
    end
    else
      S := S + Space + CustomMessage('ReadyKeepConfig') + NewLine;
    S := S + NewLine;
  end;
  if ShowsSitePage then
  begin
    S := S + CustomMessage('ReadySite') + NewLine + Space + SiteName + ' (' + SiteId + ')' + NewLine;
    if CentralUrl <> '' then
      S := S + Space + FmtMessage(CustomMessage('ReadyCentral'), [CentralUrl]) + NewLine;
    S := S + NewLine;
  end;
  if NeedsAdminPassword then
    S := S + CustomMessage('ReadyAdmin') + NewLine + Space + CustomMessage('ReadyAdminDetail') + NewLine + NewLine;
  if ShowsNetPage then
  begin
    S := S + CustomMessage('ReadyNetwork') + NewLine;
    if IsVideoRole then
    begin
      if HttpsEnabled then
        S := S + Space + FmtMessage(CustomMessage('ReadyHttps'), [HttpsPort]) + NewLine
      else
        S := S + Space + FmtMessage(CustomMessage('ReadyHttp'), [HttpPort]) + NewLine;
    end;
    if NetDomainCheck.Checked then
      S := S + Space + CustomMessage('ReadyProfilesDomain') + NewLine
    else
      S := S + Space + CustomMessage('ReadyProfilesPrivate') + NewLine;
    if gPublicNetworkCount > 0 then
    begin
      if NetPrivateCheck.Checked then
        S := S + Space + CustomMessage('ReadySetPrivate') + NewLine
      else
        S := S + Space + FmtMessage(CustomMessage('ReadyPublicWarning'), [gPublicNetworks]) + NewLine;
    end;
  end;
  Result := S;
end;

{ ------------------------------------------------------------------ PrepareToInstall }

{ Modo del repositorio de actualizaciones según la fuente (CONTRATO §15.1): file:/// = offline; si no, online. }
function UpdateMode(const Source: String): String;
begin
  if Pos('file:///', Lowercase(Source)) = 1 then
    Result := 'offline'
  else
    Result := 'online';
end;

{ ¿Trae este instalador el root de confianza de ese modo (updater\trusted\<modo>\1.root.json)? Sin él, el
  actualizador no puede verificar nada y se para al arrancar: una fuente sin su root no se acepta. }
function PayloadHasRoot(const Mode: String): Boolean;
begin
  Result := False;
#ifdef HasOnlineRoot
  if Mode = 'online' then
    Result := True;
#endif
#ifdef HasOfflineRoot
  if Mode = 'offline' then
    Result := True;
#endif
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Old: String;
  Code: Integer;
  UpdateSource: String;
begin
  Result := ValidateAll;
  if Result = '' then
  begin
    UpdateSource := InfGet('UpdateSource', '');
    if (UpdateSource <> '') and (Pos('https://', Lowercase(UpdateSource)) <> 1) and
      (Pos('http://', Lowercase(UpdateSource)) <> 1) and (Pos('file:///', Lowercase(UpdateSource)) <> 1) then
      Result := CustomMessage('ErrUpdateSource')
    else if (UpdateSource <> '') and not PayloadHasRoot(UpdateMode(UpdateSource)) then
      Result := FmtMessage(CustomMessage('ErrUpdateSourceNoRoot'), [UpdateMode(UpdateSource)]);
  end;
  if Result <> '' then
  begin
    Log('ERROR de validación: ' + Result);
    Exit;
  end;
  Old := ActiveVmsctl;
  if Old <> '' then
  begin
    { Cerrojo del actualizador (CONTRATO §15.2): que no haya dos instalaciones a la vez. }
    Code := RunVmsctl(Old, 'update lock --owner installer --ttl 3600');
    if Code = VMSCTL_OK then
      gUpdaterLocked := True
    else if gLastVmsctlErrorCode = 'busy' then
    begin
      Result := CustomMessage('ErrUpdaterBusy');
      Exit;
    end
    else
      Log('AVISO: no se obtuvo el cerrojo del actualizador (' + gLastVmsctlMessage + '); se sigue.');
    { vmshost.exe y los archivos de la versión activa están en uso mientras los servicios corren. }
    if RunVmsctl(Old, 'services stop') <> VMSCTL_OK then
    begin
      Result := FmtMessage(CustomMessage('ErrStopServices'), [gLastVmsctlMessage]);
      Exit;
    end;
    gStoppedOld := True;
  end;
  Result := CheckPorts;
  if Result <> '' then
    Log('ERROR: ' + Result);
end;

{ ------------------------------------------------------------------ después de copiar archivos }

procedure Fail(const StepName, Msg: String; const Code: Integer);
begin
  if gFailedStep = '' then
  begin
    gFailedStep := StepName;
    gFailedMessage := Msg;
    gFailedCode := Code;
  end;
  Log('ERROR en «' + StepName + '»: ' + Msg + ' (código ' + IntToStr(Code) + ')');
end;

{ Paso de vmsctl con la versión nueva. Si un paso anterior falló, no hace nada. }
function VmsctlStep(const Status, Args: String): Boolean;
var
  Code: Integer;
begin
  Result := False;
  if gFailedStep <> '' then
    Exit;
  WizardForm.StatusLabel.Caption := Status;
  Code := RunVmsctl(NewVmsctl, Args);
  if Code <> VMSCTL_OK then
    Fail(Status, gLastVmsctlMessage, Code)
  else
    Result := True;
end;

procedure WriteEnvAndConfig;
var
  Lines: TArrayOfString;
begin
  LoadEnv(Lines);
  { La v2 siempre tiene el motor como servicio aparte (CONTRATO §13.10). }
  EnvSet(Lines, 'VMS_ENGINE_MODE', 'attach', False);
  if IsVideoRole then
  begin
    EnvSet(Lines, 'VMS_HTTP_PORT', HttpPort, False);
    EnvSet(Lines, 'VMS_HTTPS_PORT', HttpsPort, False);
    if HttpPort <> DEFAULT_HTTP_PORT then
      EnvSet(Lines, 'VMS_AGENT_BACKEND_URL', 'http://127.0.0.1:' + HttpPort, False);
  end;
  if ShowsSitePage then
  begin
    EnvSet(Lines, 'VMS_SITE_ID', SiteId, False);
    if CentralUrl <> '' then
      EnvSet(Lines, 'VMS_CENTRAL_URL', CentralUrl, False);
    if SiteToken <> '' then
      EnvSet(Lines, 'VMS_SITE_TOKEN', SiteToken, False);
  end;
  if ShowsCentralPage then
    EnvSet(Lines, 'VMS_PG_DSN', PgDsn, False);
  if NeedsAdminPassword then
  begin
    { El backend crea «admin» con ella en el primer arranque si no hay usuarios (CONTRATO §12, 2026-10-04). }
    if IsCentralRole then
      EnvSet(Lines, 'VMS_CENTRAL_ADMIN_INITIAL_PASSWORD', AdminPassword, False)
    else
      EnvSet(Lines, 'VMS_ADMIN_INITIAL_PASSWORD', AdminPassword, False);
  end;
  if InfGet('UpdateSource', '') <> '' then
    EnvSet(Lines, 'VMS_UPDATE_SOURCE', InfGet('UpdateSource', ''), False);
  if not SaveEnv(Lines) then
    Fail(CustomMessage('StatusSettings'), FmtMessage(CustomMessage('ErrWriteFile'), [EnvPath]), 20);
  if ShowsSitePage and (gFailedStep = '') then
    if not SeedConfigJson(SiteId, SiteName, SiteCode, RecordingsDir) then
      Fail(CustomMessage('StatusSettings'),
        FmtMessage(CustomMessage('ErrWriteFile'), [AddBackslash(gDataDir) + 'config\config.json']), 20);
end;

{ updater\updater.json (carpeta de datos): el AppId de Inno, para que el actualizador ponga DisplayVersion en
  «Aplicaciones» tras cada actualización (CONTRATO §13.7). Si el archivo ya existe se conserva todo lo demás. }
procedure WriteUpdaterConfig;
var
  Path, S, Rest: String;
  Raw: AnsiString;
  Lines: TArrayOfString;
  P: Integer;
begin
  Path := AddBackslash(gDataDir) + 'updater\updater.json';
  S := '';
  if FileExists(Path) and LoadStringFromFile(Path, Raw) then
    S := Trim(Utf8Decode(Raw));
  if Pos('"inno_app_id"', S) > 0 then
    Exit;
  P := Pos('{', S);
  if S = '' then
    S := '{"inno_app_id": "{#AppIdPlain}"}'
  else if P = 0 then
  begin
    Log('AVISO: ' + Path + ' no es un objeto JSON: no se toca.');
    Exit;
  end
  else
  begin
    Rest := Trim(Copy(S, P + 1, Length(S)));
    if Copy(Rest, 1, 1) = '}' then
      S := '{"inno_app_id": "{#AppIdPlain}"' + Rest
    else
      S := '{"inno_app_id": "{#AppIdPlain}", ' + Rest;
  end;
  SetArrayLength(Lines, 1);
  Lines[0] := S;
  if not (EnsureDir(AddBackslash(gDataDir) + 'updater') and SaveStringsToUTF8FileWithoutBOM(Path, Lines, False)) then
    Log('AVISO: no se pudo escribir ' + Path + ': DisplayVersion no se actualizará con el actualizador.');
end;

{ --recordings-dir para services install y acl apply: la carpeta de grabaciones si está fuera de la carpeta de
  datos (la de dentro ya la cubre la ACL de la carpeta de datos). En una actualización, la del registro. }
function RecordingsArg: String;
var
  Dir: String;
begin
  Result := '';
  if not IsVideoRole then
    Exit;
  Dir := RecordingsDir;
  if Dir = '' then
    RegQueryStringValue(HKLM, VMS_REG_KEY, 'RecordingsDir', Dir);
  Dir := RemoveBackslash(Trim(Dir));
  if (Dir <> '') and (CompareText(Dir, RemoveBackslash(DefaultRecordingsDir)) <> 0) and DirExists(Dir) then
    Result := ' --recordings-dir ' + AddQuotes(Dir);
end;

procedure WriteRegistryState;
begin
  RegWriteStringValue(HKLM, VMS_REG_KEY, 'InstallDir', ProgramDir);
  RegWriteStringValue(HKLM, VMS_REG_KEY, 'DataDir', gDataDir);
  RegWriteStringValue(HKLM, VMS_REG_KEY, 'Role', Role);
  RegWriteStringValue(HKLM, VMS_REG_KEY, 'InstalledVersion', '{#AppVersion}');
  if ShowsRecPage then
  begin
    RegWriteStringValue(HKLM, VMS_REG_KEY, 'RecordingsDir', RecordingsDir);
    RegWriteStringValue(HKLM, VMS_REG_KEY, 'RecordingsDirCreated', BoolText(gRecordingsDirCreated));
  end;
end;

procedure PostInstall;
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := CustomMessage('StatusSettings');
  if not (EnsureDir(gDataDir) and EnsureDir(AddBackslash(gDataDir) + 'config') and
    EnsureDir(AddBackslash(gDataDir) + 'state') and EnsureDir(AddBackslash(gDataDir) + 'logs')) then
    Fail(CustomMessage('StatusSettings'), FmtMessage(CustomMessage('ErrCreateDir'), [gDataDir]), 20);
  if (gFailedStep = '') and not DirExists(AddBackslash(gDataDir) + 'secrets') then
  begin
    EnsureDir(AddBackslash(gDataDir) + 'secrets');
    ProtectForAdmins(AddBackslash(gDataDir) + 'secrets', True);
  end;
  if (gFailedStep = '') and ShowsRecPage then
    if not PrepareRecordingsDir(RecordingsDir) then
      Fail(CustomMessage('StatusSettings'), FmtMessage(CustomMessage('ErrCreateDir'), [RecordingsDir]), 20);
  if gFailedStep = '' then
    WriteEnvAndConfig;
  if gFailedStep = '' then
    WriteUpdaterConfig;
  WriteRegistryState;

  if gV1Detected then
    if VmsctlStep(CustomMessage('StatusMigrate'), 'migrate-from-v1') then
      RemoveV1Leftovers;
  { services install crea los servicios y apunta state\active.json a esta versión (sin «a prueba»): en una
    instalación nueva no hay puntero previo y «version switch» fallaría (CONTRATO §13.4). }
  VmsctlStep(CustomMessage('StatusServices'), 'services install --role ' + Role + ' --data-dir ' +
    AddQuotes(gDataDir) + RecordingsArg);
  VmsctlStep(CustomMessage('StatusAcl'), 'acl apply --role ' + Role + ' --data-dir ' + AddQuotes(gDataDir) +
    RecordingsArg);
  if gFailedStep = '' then
    EnsureOperatorsGroup;
  if IsVideoRole then
    VmsctlStep(CustomMessage('StatusKiosk'), 'kiosk rotate');
  if not IsViewerRole then
    VmsctlStep(CustomMessage('StatusFirewall'), 'firewall apply --role ' + Role + ' --profiles ' +
      FirewallProfiles);
  if HttpsEnabled then
    VmsctlStep(CustomMessage('StatusTls'), 'tls setup --hostname ' + AddQuotes(Lowercase(GetComputerNameString)));
  if (gFailedStep = '') and (gPublicNetworkCount > 0) and NetPrivateCheck.Checked then
    SetPublicNetworksPrivate;
  VmsctlStep(CustomMessage('StatusStart'), 'services start');

  if gFailedStep = '' then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('StatusHealth');
    Code := RunVmsctl(NewVmsctl, 'health wait --timeout 120');
    gHealthOk := Code = VMSCTL_OK;
    if not gHealthOk then
    begin
      gHealthMessage := gLastVmsctlMessage;
      Log('AVISO: el sistema no respondió bien en 120 s: ' + gHealthMessage);
    end;
  end;
  if gUpdaterLocked then
    if RunVmsctl(NewVmsctl, 'update unlock --owner installer') = VMSCTL_OK then
      gUpdaterLocked := False;
end;

procedure FillResultPage;
var
  S: String;
begin
  if gFailedStep <> '' then
    S := FmtMessage(CustomMessage('ResultFailed'), [gFailedStep, gFailedMessage])
  else if not gHealthOk then
    S := FmtMessage(CustomMessage('ResultHealthFailed'), [gHealthMessage])
  else
  begin
    S := CustomMessage('ResultOk');
    ResultSaveButton.Visible := False;
  end;
  S := S + #13#10#13#10 + FmtMessage(CustomMessage('ResultLog'), [ExpandConstant('{log}')]);
  ResultPage.RichEditViewer.Lines.Text := S;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    gPostInstallReached := True;
    PostInstall;
    FillResultPage;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  { 20: falló un paso de la instalación (el código de vmsctl va al registro); 12: instalado pero el sistema no
    respondió en 120 s. Nunca 1-8: son los códigos propios de Inno Setup. }
  Result := 0;
  if gFailedStep <> '' then
    Result := VMSCTL_WINDOWS_ERROR
  else if not gHealthOk then
    Result := VMSCTL_HEALTH_FAILED;
  if Result <> 0 then
    Log('Código de salida del instalador: ' + IntToStr(Result));
end;

procedure DeinitializeSetup;
begin
  { Instalación que no llegó al final (cancelada o fallida al copiar) después de parar la versión anterior: se
    vuelve a arrancar para no dejar la tienda sin grabar. }
  if gStoppedOld and not gPostInstallReached and (ActiveVmsctl <> '') then
  begin
    Log('La instalación no terminó: se vuelven a arrancar los servicios de la versión ' + gActiveVersion);
    RunVmsctl(ActiveVmsctl, 'services start');
  end;
  { Si se canceló después de pedir el cerrojo, se suelta (si no, caduca solo con el ttl). }
  if gUpdaterLocked and (ActiveVmsctl <> '') then
    RunVmsctl(ActiveVmsctl, 'update unlock --owner installer');
end;
