# Seguir desarrollando VMS Multimarca desde un PC con Windows

Guía para la persona. Claude Code lee además `CLAUDE.md`, en la raíz del repositorio, con todo el contexto técnico.

## 1. Instalar Claude Code (una vez)

1. Abre **PowerShell** (no hace falta que sea como administrador).
2. Pega esto y pulsa Enter:

   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```

3. Cierra PowerShell y abre otro. Escribe `claude` e inicia sesión con tu cuenta.

Claude Code en Windows necesita **Git**. Si te dice que falta, instálalo con `winget install Git.Git` y abre otra
ventana de PowerShell.

## 2. Descargar el proyecto y preparar el PC (una vez)

En PowerShell:

```powershell
git clone -b v2 https://github.com/mauricioSas/vms-multimarca.git C:\src\vms-multimarca
cd C:\src\vms-multimarca
powershell -ExecutionPolicy Bypass -File tools\windows\preparar-entorno.ps1
```

El script instala todo lo necesario y comprueba que funciona: Python, Rust, Node, el compilador de C++, el motor de
vídeo y las librerías. La primera vez tarda entre 20 y 40 minutos. Si Windows pide permiso, responde **Sí**. Si al
final dice que falta algo, cierra la ventana, abre otra y vuelve a lanzar la última línea.

## 3. Trabajar con Claude Code

```powershell
cd C:\src\vms-multimarca
claude
```

Primero dile: «Lee CLAUDE.md y sigue con lo pendiente». Ahí está todo lo que se hizo, el estado de tu cámara y lo
siguiente que hay que arreglar.

Para publicar una versión nueva, dile «publica una versión nueva». Él sabe cómo hacerlo (está en `CLAUDE.md`). La
versión aparece en tu programa, en el icono de VMS junto al reloj de Windows.
