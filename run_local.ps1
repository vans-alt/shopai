# Starts all 6 services locally WITHOUT Docker. MongoDB + PostgreSQL must already be running on this PC.
# Edit the settings below if yours differ. Pass -NoDatabases to run in memory mode (no MongoDB/PostgreSQL).
param([switch]$NoDatabases)

$mongoUrl      = "mongodb://127.0.0.1:27017"
$pgHost        = "127.0.0.1"
$pgPort        = 5432
$pgUser        = "shopai"
$pgPassword    = "shopai_pw"
$pgDb          = "shopai"
$adminKey      = "dev-admin-key"
$gatewayPort   = 8010

$root = $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"
$services = @(@("catalog",8001), @("search",8002), @("chatbot",8003), @("cart",8004), @("order",8005), @("gateway",$gatewayPort))
foreach ($s in $services) {
    $name = $s[0]; $port = $s[1]
    $envs = "`$env:FRONTEND_DIR='$root\frontend';"
    if (-not $NoDatabases) {
        if ($name -eq "catalog") { $envs += " `$env:MONGO_URL='$mongoUrl'; `$env:ADMIN_KEY='$adminKey';" }
        if ($name -eq "order")   { $envs += " `$env:ORDER_DB_HOST='$pgHost'; `$env:ORDER_DB_PORT='$pgPort'; `$env:ORDER_DB_USER='$pgUser'; `$env:ORDER_DB_PASSWORD='$pgPassword'; `$env:ORDER_DB_NAME='$pgDb';" }
    }
    $cmd = "$envs cd '$root\services\$name'; & '$py' -m uvicorn app.main:app --port $port"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Start-Sleep -Seconds 2
}
Write-Host "Started. Open http://localhost:$gatewayPort   (status: /api/status)"
