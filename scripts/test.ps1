. "$PSScriptRoot/common.ps1"
if (-not (Test-Path -LiteralPath (Join-Path $PrivateRoot 'native.json'))) {
    Assert-Docker
    try {
        & docker compose --file compose.test.yaml --project-name vagasunb-extraordinaria-test up --build --abort-on-container-exit --exit-code-from tests
        Check-Exit 'testes em PostgreSQL efêmero isolado'
    } finally {
        & docker compose --file compose.test.yaml --project-name vagasunb-extraordinaria-test down
    }
    return
}
uv sync --frozen
Check-Exit 'dependências de teste'
& (Join-Path $ProjectRoot '.venv/Scripts/ruff.exe') check src tests
Check-Exit 'lint'
& (Join-Path $ProjectRoot '.venv/Scripts/pytest.exe') -q --disable-socket --allow-hosts=127.0.0.1,localhost
Check-Exit 'testes isolados'
