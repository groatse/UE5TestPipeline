"""Machine data collected at the start of every run, so runs are only compared like for like."""

from __future__ import annotations

import json
import platform
import subprocess

# One PowerShell call, so collecting takes about a second.
_QUERY = r"""
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object {
  @{ name = $_.Name; driver = $_.DriverVersion; refresh_hz = $_.CurrentRefreshRate;
     resolution = "$($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution)" } })
$vram = @(Get-ItemProperty 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' -ErrorAction SilentlyContinue |
  Where-Object { $_.'HardwareInformation.qwMemorySize' } | ForEach-Object {
  @{ name = $_.DriverDesc; vram_gb = [math]::Round($_.'HardwareInformation.qwMemorySize' / 1GB, 1) } })
$plan = (powercfg /getactivescheme) -replace '^.*\((.*)\).*$', '$1'
@{ machine = $env:COMPUTERNAME; cpu = $cpu.Name.Trim(); cores = $cpu.NumberOfCores; threads = $cpu.NumberOfLogicalProcessors;
   ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1); windows = "$($os.Caption) $($os.Version) build $($os.BuildNumber)";
   gpus = $gpus; vram = $vram; power_plan = $plan } | ConvertTo-Json -Depth 4 -Compress
"""


def collect() -> dict:
    """CPU, cores, RAM, GPUs with driver, VRAM and refresh rate, Windows build, power plan and machine name."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
                             capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        data = {"error": f"machine query failed: {e}"}
    data.setdefault("machine", platform.node())
    data["python"] = platform.python_version()
    return data
