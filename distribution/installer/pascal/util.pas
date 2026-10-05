{ util.pas: utilidades sin estado (texto, versiones, JSON mínimo, parámetros). Dueño: B3.
  Pascal Script de Inno Setup 7: sin rutinas anidadas, sin Exit(valor), arrays con SetArrayLength. }

const
  VMS_REG_KEY = 'SOFTWARE\VMSMultimarca';
  VMS_OPERATORS_GROUP = 'VMS Operadores';
  VMS_MARKER_FILE = '.vms-multimarca';

{ ------------------------------------------------------------------ línea de órdenes }

{ Valor de /NOMBRE=valor (sin distinguir mayúsculas), o '' si no está. }
function VmsParamValue(const Name: String): String;
var
  I: Integer;
  P, Prefix: String;
begin
  Result := '';
  Prefix := '/' + Uppercase(Name) + '=';
  for I := 1 to ParamCount do
  begin
    P := ParamStr(I);
    if Pos(Prefix, Uppercase(P)) = 1 then
    begin
      Result := Copy(P, Length(Prefix) + 1, Length(P));
      Result := RemoveQuotes(Result);
    end;
  end;
end;

{ ¿Está /NOMBRE (o /NOMBRE=...) en la línea de órdenes? }
function VmsHasSwitch(const Name: String): Boolean;
var
  I: Integer;
  P: String;
begin
  Result := False;
  for I := 1 to ParamCount do
  begin
    P := Uppercase(ParamStr(I));
    if (P = '/' + Uppercase(Name)) or (Pos('/' + Uppercase(Name) + '=', P) = 1) then
      Result := True;
  end;
end;

{ ------------------------------------------------------------------ texto }

function BoolText(B: Boolean): String;
begin
  if B then
    Result := '1'
  else
    Result := '0';
end;

function IsDigitChar(C: Char): Boolean;
begin
  Result := (C >= '0') and (C <= '9');
end;

function IsAllDigits(const S: String): Boolean;
var
  I: Integer;
begin
  Result := Length(S) > 0;
  for I := 1 to Length(S) do
    if not IsDigitChar(S[I]) then
      Result := False;
end;

function HasControlChars(const S: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to Length(S) do
    if (Ord(S[I]) < 32) or (Ord(S[I]) = 127) then
      Result := True;
end;

// ¿Se puede guardar tal cual en el .env? Sin caracteres de control y sin la secuencia dólar-llave, que
// python-dotenv expande como variable incluso entre comillas simples (lo comprueba test_installer_script.py).
function IsEnvSafe(const S: String): Boolean;
begin
  Result := (not HasControlChars(S)) and (Pos('${', S) = 0);
end;

// Identificador de sede: 3-40 caracteres [a-z0-9-], sin guion al principio (VMS_SITE_ID, vms.core.naming.ID_PATTERN).
function IsValidSiteId(const S: String): Boolean;
var
  I: Integer;
  C: Char;
begin
  Result := (Length(S) >= 3) and (Length(S) <= 40);
  if not Result then
    Exit;
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if not (((C >= 'a') and (C <= 'z')) or IsDigitChar(C) or ((C = '-') and (I > 1))) then
      Result := False;
  end;
end;

{ Sugerencia de identificador a partir del código o el nombre: minúsculas, sin tildes ni espacios. }
function SuggestSiteId(const Source: String): String;
var
  I: Integer;
  C: Char;
  S, CS, Accents, Plain: String;
  P: Integer;
begin
  Result := '';
  Accents := 'áéíóúüñàèìòùç';
  Plain := 'aeiouunaeiouc';
  S := AnsiLowercase(Trim(Source));
  for I := 1 to Length(S) do
  begin
    C := S[I];
    CS := C;
    P := Pos(CS, Accents);
    if P > 0 then
      C := Plain[P];
    if ((C >= 'a') and (C <= 'z')) or IsDigitChar(C) then
      Result := Result + C
    else if (Length(Result) > 0) and (Result[Length(Result)] <> '-') then
      Result := Result + '-';
  end;
  while (Length(Result) > 0) and (Result[Length(Result)] = '-') do
    Result := Copy(Result, 1, Length(Result) - 1);
  if Length(Result) > 40 then
    Result := Copy(Result, 1, 40);
  if (Length(Result) > 0) and (Length(Result) < 3) then
    Result := 'sede-' + Result;
end;

{ Escapa una cadena para JSON. Los caracteres de control ya se rechazan al validar: aquí se cambian por un
  espacio por si acaso, para no generar nunca un JSON inválido. }
function JsonEscape(const S: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if C = '\' then
      Result := Result + '\\'
    else if C = '"' then
      Result := Result + '\"'
    else if Ord(C) < 32 then
      Result := Result + ' '
    else
      Result := Result + C;
  end;
end;

{ Valor para el .env entre comillas simples (python-dotenv: dentro de '...' solo se interpretan \\ y \'). }
function EnvQuote(const S: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '''';
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if C = '\' then
      Result := Result + '\\'
    else if C = '''' then
      Result := Result + '\'''
    else if Ord(C) >= 32 then
      Result := Result + C;
  end;
  Result := Result + '''';
end;

{ ------------------------------------------------------------------ JSON mínimo (salida de vmsctl, active.json) }

{ Primer valor de texto de "Key" en un JSON plano o anidado; '' si no está o no es texto. Entiende los
  escapes \" \\ \/ \n \t; \uXXXX se deja tal cual (los mensajes de vmsctl van en UTF-8, sin \u). }
function JsonGetString(const Json, Key: String): String;
var
  P, I: Integer;
  C: Char;
  Rest: String;
begin
  Result := '';
  P := Pos('"' + Key + '"', Json);
  if P = 0 then
    Exit;
  Rest := Copy(Json, P + Length(Key) + 2, Length(Json));
  I := 1;
  while (I <= Length(Rest)) and ((Rest[I] = ' ') or (Rest[I] = ':') or (Rest[I] = #9)) do
    I := I + 1;
  if (I > Length(Rest)) or (Rest[I] <> '"') then
    Exit;
  I := I + 1;
  while I <= Length(Rest) do
  begin
    C := Rest[I];
    if C = '"' then
      Exit;
    if (C = '\') and (I < Length(Rest)) then
    begin
      I := I + 1;
      C := Rest[I];
      if C = 'n' then
        C := #10
      else if C = 't' then
        C := #9
      else if C = 'u' then
        Result := Result + '\';
    end;
    Result := Result + C;
    I := I + 1;
  end;
end;

{ ------------------------------------------------------------------ versiones SemVer }

{ Parte numérica N (0, 1 o 2) de "X.Y.Z[-pre]". }
function SemVerPart(const V: String; N: Integer): Integer;
var
  Core: String;
  Parts: TArrayOfString;
begin
  Result := 0;
  Core := V;
  if Pos('-', Core) > 0 then
    Core := Copy(Core, 1, Pos('-', Core) - 1);
  Parts := StringSplit(Core, ['.'], stAll);
  if GetArrayLength(Parts) > N then
    Result := StrToIntDef(Parts[N], 0);
end;

function SemVerPre(const V: String): String;
begin
  Result := '';
  if Pos('-', V) > 0 then
    Result := Copy(V, Pos('-', V) + 1, Length(V));
end;

{ Compara identificadores de prerelease (SemVer 2.0 §11): numéricos por valor, el resto por texto. }
function ComparePre(const A, B: String): Integer;
var
  PA, PB: TArrayOfString;
  I, N, NA, NB: Integer;
begin
  Result := 0;
  if A = B then
    Exit;
  if A = '' then
  begin
    Result := 1;
    Exit;
  end;
  if B = '' then
  begin
    Result := -1;
    Exit;
  end;
  PA := StringSplit(A, ['.'], stAll);
  PB := StringSplit(B, ['.'], stAll);
  N := GetArrayLength(PA);
  if GetArrayLength(PB) < N then
    N := GetArrayLength(PB);
  for I := 0 to N - 1 do
  begin
    if Result = 0 then
    begin
      if IsAllDigits(PA[I]) and IsAllDigits(PB[I]) then
      begin
        NA := StrToIntDef(PA[I], 0);
        NB := StrToIntDef(PB[I], 0);
        if NA < NB then
          Result := -1
        else if NA > NB then
          Result := 1;
      end
      else if IsAllDigits(PA[I]) then
        Result := -1
      else if IsAllDigits(PB[I]) then
        Result := 1
      else if CompareStr(PA[I], PB[I]) < 0 then
        Result := -1
      else if CompareStr(PA[I], PB[I]) > 0 then
        Result := 1;
    end;
  end;
  if Result = 0 then
  begin
    if GetArrayLength(PA) < GetArrayLength(PB) then
      Result := -1
    else if GetArrayLength(PA) > GetArrayLength(PB) then
      Result := 1;
  end;
end;

{ -1 si A < B, 0 si iguales, 1 si A > B. }
function CompareSemVer(const A, B: String): Integer;
var
  I, XA, XB: Integer;
begin
  Result := 0;
  for I := 0 to 2 do
  begin
    if Result = 0 then
    begin
      XA := SemVerPart(A, I);
      XB := SemVerPart(B, I);
      if XA < XB then
        Result := -1
      else if XA > XB then
        Result := 1;
    end;
  end;
  if Result = 0 then
    Result := ComparePre(SemVerPre(A), SemVerPre(B));
end;

{ ------------------------------------------------------------------ rutas }

{ ¿Es la raíz de una unidad o de un recurso compartido? ('D:\', 'D:', '\\servidor\recurso') }
function IsRootPath(const Path: String): Boolean;
var
  P: String;
begin
  P := RemoveBackslash(Trim(Path));
  Result := (Length(P) <= 2) or SameText(P, RemoveBackslash(ExtractFileDrive(P)));
end;

function IsAbsolutePath(const Path: String): Boolean;
begin
  Result := ((Length(Path) >= 3) and (Path[2] = ':') and (Path[3] = '\')) or (Pos('\\', Path) = 1);
end;

{ ¿Está Path dentro de Base (o es Base)? }
function PathIsInside(const Path, Base: String): Boolean;
begin
  Result := PathSame(RemoveBackslash(Path), RemoveBackslash(Base)) or
    PathStartsWith(AddBackslash(RemoveBackslash(Path)), AddBackslash(RemoveBackslash(Base)), True);
end;

function GiB(const Bytes: Int64): Int64;
begin
  Result := Bytes div 1073741824;
end;
