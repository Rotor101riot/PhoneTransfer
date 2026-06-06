$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
Set-Location $PSScriptRoot
.\gradlew.bat assembleDebug
