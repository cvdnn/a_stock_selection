# astock-dingpan 统一启动器（Windows PowerShell）
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $Py "$Root\scripts\run.py" @args