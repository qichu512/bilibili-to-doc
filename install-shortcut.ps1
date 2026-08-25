# 创建桌面快捷方式「B站视频转文档」（双击 install-shortcut.bat 调用本脚本）
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$lnkPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'B站视频转文档.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnkPath)
$s.TargetPath = 'wscript.exe'
$s.Arguments = '"' + (Join-Path $dir 'start.vbs') + '"'
$s.WorkingDirectory = $dir
$ico = Join-Path $dir 'app.ico'
if (Test-Path $ico) { $s.IconLocation = "$ico,0" }
$s.Description = 'B站视频转文档：提取 AI 字幕并整理为 Markdown 文档'
$s.Save()
Write-Host "已创建桌面快捷方式: $lnkPath"
