$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -X utf8 server.py @args
