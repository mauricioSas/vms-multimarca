{ vmsctl.pas: llamadas a vmsctl.exe (CONTRATO §14). El instalador no configura el equipo por su cuenta: todo
  lo que toca servicios, ACL, firewall, TLS y puntero de versión lo hace vmsctl. Nunca se analiza texto
  localizado de Windows: solo el código de salida (§14.2) y el JSON de --json. Dueño: B3. }

const
  VMSCTL_OK = 0;
  VMSCTL_USAGE = 2;
  VMSCTL_PORT_IN_USE = 10;
  VMSCTL_NO_PERMISSION = 11;
  VMSCTL_HEALTH_FAILED = 12;
  VMSCTL_WINDOWS_ERROR = 20;

var
  gLastVmsctlOutput: String;
  gLastVmsctlMessage: String;
  gLastVmsctlErrorCode: String;

{ vmsctl de la versión que se instala (solo existe después de copiar archivos). }
function NewVmsctl: String;
begin
  Result := ExpandConstant('{app}\versions\{#AppVersion}\bin\vmsctl.exe');
end;

{ vmsctl de la versión activa antes de instalar (para parar servicios y pedir el cerrojo); '' si no hay. }
function ActiveVmsctl: String;
begin
  Result := '';
  if gActiveVersion <> '' then
    if FileExists(ProgramDir + '\versions\' + gActiveVersion + '\bin\vmsctl.exe') then
      Result := ProgramDir + '\versions\' + gActiveVersion + '\bin\vmsctl.exe';
end;

{ Copia temporal del vmsctl nuevo, para comprobar puertos antes de instalar nada. }
function TempVmsctl: String;
begin
  Result := ExpandConstant('{tmp}\vmsctl.exe');
  if not FileExists(Result) then
    ExtractTemporaryFile('vmsctl.exe');
end;

{ Ejecuta «vmsctl <Args> --json» y devuelve el código de salida (-1 si no se pudo lanzar). Guarda la salida
  y el mensaje de error en español (error.message_es) en gLastVmsctl*. vmsctl nunca imprime secretos
  (CONTRATO §14.2), así que su salida va al registro de instalación. }
function RunVmsctl(const Exe, Args: String): Integer;
var
  Output: TExecOutput;
  OutLines, ErrLines: TArrayOfString;
  Code, I: Integer;
  Joined: String;
begin
  Result := -1;
  gLastVmsctlOutput := '';
  gLastVmsctlMessage := '';
  gLastVmsctlErrorCode := '';
  Log('vmsctl ' + Args);
  if (Exe = '') or not FileExists(Exe) then
  begin
    gLastVmsctlMessage := FmtMessage(CustomMessage('ErrVmsctlMissing'), [Exe]);
    Log(gLastVmsctlMessage);
    Exit;
  end;
  try
    if not ExecAndCaptureOutput(Exe, Args + ' --json', ExtractFileDir(Exe), SW_SHOWNORMAL, ewWaitUntilTerminated,
      Code, Output) then
    begin
      gLastVmsctlMessage := FmtMessage(CustomMessage('ErrVmsctlStart'), [Exe, SysErrorMessage(Code)]);
      Log(gLastVmsctlMessage);
      Exit;
    end;
  except
    gLastVmsctlMessage := FmtMessage(CustomMessage('ErrVmsctlStart'), [Exe, GetExceptionMessage]);
    Log(gLastVmsctlMessage);
    Exit;
  end;
  { Copias locales: GetArrayLength pide una variable, no un campo de registro. }
  OutLines := Output.StdOut;
  ErrLines := Output.StdErr;
  Joined := '';
  for I := 0 to GetArrayLength(OutLines) - 1 do
    Joined := Joined + OutLines[I];
  gLastVmsctlOutput := Joined;
  gLastVmsctlMessage := JsonGetString(Joined, 'message_es');
  { El primer "code" es el numérico de arriba; el texto (p. ej. "busy", "port_in_use") va dentro de "error". }
  if Pos('"error"', Joined) > 0 then
    gLastVmsctlErrorCode := JsonGetString(Copy(Joined, Pos('"error"', Joined), Length(Joined)), 'code');
  if (Code <> VMSCTL_OK) and (gLastVmsctlMessage = '') then
  begin
    for I := 0 to GetArrayLength(ErrLines) - 1 do
      gLastVmsctlMessage := Trim(gLastVmsctlMessage + ' ' + ErrLines[I]);
    if gLastVmsctlMessage = '' then
      gLastVmsctlMessage := FmtMessage(CustomMessage('ErrVmsctlCode'), [IntToStr(Code)]);
  end;
  Log('vmsctl ' + Args + ' -> código ' + IntToStr(Code));
  if Joined <> '' then
    Log('  ' + Joined);
  Result := Code;
end;
