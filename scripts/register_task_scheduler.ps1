# Script PowerShell para registrar Yedid Hub en el Programador de Tareas de Windows (Task Scheduler)
# Inicia automáticamente al iniciar sesión de usuario, en segundo plano, sin ventana de consola negra.

$TaskName = "YedidSmartHub_AutoStart"
$ActionScript = "C:\Yedid-Smart-Home\scripts\start_silent.vbs"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " REGISTRO DE YEDID HUB EN PROGRAMADOR DE TAREAS WINDOWS   " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Comprobar si ya existe la tarea y desinstalar la versión anterior si aplica
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Write-Host "[*] Tarea programada existente detectada. Reinstalando versión actualizada..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# 2. Configurar la acción (Ejecutar wscript.exe con start_silent.vbs)
$Action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"C:\Yedid-Smart-Home\scripts\start_silent.vbs`"" -WorkingDirectory "C:\Yedid-Smart-Home"

# 3. Configurar el disparador (Al iniciar sesión del usuario)
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# 4. Configurar opciones de continuidad operativa
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)

# 5. Registrar la tarea
try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Autoinicio silencioso de Yedid Smart Home Hub con prevención de duplicados y alta disponibilidad."
    Write-Host "[+] Tarea '$TaskName' registrada exitosamente en el Programador de Tareas de Windows." -ForegroundColor Green
    Write-Host "    Disparador: Al iniciar sesión ($env:USERNAME)" -ForegroundColor Gray
    Write-Host "    Acción: Silenciosa en segundo plano (wscript -> start_silent.vbs)" -ForegroundColor Gray
    Write-Host "    Recuperación ante fallos: Reinicio automático en 1 minuto" -ForegroundColor Gray
} catch {
    Write-Host "[-] Error registrando tarea programada: $_" -ForegroundColor Red
}
