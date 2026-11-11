# 🎓 VagasUnB (Extraordinária)

<p align="center">
<strong>Encontre disciplinas, acompanhe vagas e veja a operação em um painel local.</strong><br>
Catálogo público • Até 10 disciplinas • Alertas preparados • Demonstração offline
</p>

<p align="center">
<img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white">
<img alt="PostgreSQL 17" src="https://img.shields.io/badge/PostgreSQL-17-4169E1?style=for-the-badge&logo=postgresql&logoColor=white">
<img alt="Docker preparado" src="https://img.shields.io/badge/Docker-preparado-2496ED?style=for-the-badge&logo=docker&logoColor=white">
<img alt="Telegram preparado" src="https://img.shields.io/badge/Telegram-preparado-26A5E4?style=for-the-badge&logo=telegram&logoColor=white">
<a href="LICENSE"><img alt="Licença MIT" src="https://img.shields.io/badge/Licen%C3%A7a-MIT-2EA44F?style=for-the-badge"></a>
</p>

---

## ✨ O que faz

- 🔎 Busca local por código, nome, turma, campus e unidade.
- 📚 Coleta centralizada das unidades e níveis públicos do período escolhido.
- 🔔 Monitoramento de uma, várias ou todas as turmas de até dez disciplinas.
- 📊 Dashboard com cobertura, intervalo real, fila e controles de coleta/envio.
- 🔒 Coleta mínima de dados, identificador protegido e exclusão pelo bot.

## 🚀 Começar no Windows

Requisitos: PowerShell 5.1+, [uv](https://docs.astral.sh/uv/) e Docker Desktop com Linux containers.
O setup não instala ferramentas do sistema. Instalação nativa existente é reutilizada.

```powershell
git clone https://github.com/xGabrielCv/VagasUnB-Extraordinaria.git
cd VagasUnB-Extraordinaria
.\scripts\setup.ps1
.\scripts\start.ps1
```

Abra **http://127.0.0.1:8765** e cadastre sua senha no primeiro acesso.
Não há senha padrão nem cadastro por e-mail. Depois, a troca de senha fica no painel.

Sem Docker, com binários PostgreSQL 17 já disponíveis:

```powershell
.\scripts\setup.ps1 -NativePgBin 'C:\caminho\postgresql\bin'
.\scripts\start.ps1
```

| Ação | Comando |
|---|---|
| Iniciar | `.\scripts\start.ps1` |
| Parar | `.\scripts\stop.ps1` |
| Reiniciar | `.\scripts\restart.ps1` |
| Ver estado | `.\scripts\status.ps1` |
| Abrir painel | `.\scripts\open-dashboard.ps1` |
| Testar | `.\scripts\test.ps1` |

**Reiniciar preserva banco, configurações e chaves.** Use `-NoBrowser` para não abrir uma aba.
No Linux: `sh scripts/linux.sh setup`, `start`, `status` ou `stop`.

## 🧪 Demo ou catálogo público

A instalação começa em **demo offline**, sem Telegram, token ou credenciais acadêmicas.
No simulador, envie `/start`, busque `DEMO0001`, escolha turmas e confirme.
Altere 0 → 1 vaga e observe novamente após o tempo de confirmação para experimentar um alerta simulado.

Para dados reais, escolha **Catálogo público SIGAA** na configuração do painel,
informe explicitamente o período letivo e salve. Use **Atualizar catálogo agora**.
Unidades e níveis são descobertos automaticamente; Telegram continua desligado ou simulado.
O painel mostra cobertura e falhas. Dados incertos não viram zero vagas.

O intervalo desejado aceita **5 segundos**. A duração da coleta e os limites do SIGAA
impõem o intervalo efetivo: isso não garante baixar todas as unidades a cada 5 s.
Atualizar a tela ou buscar disciplina consulta apenas o banco local.

## ◷ Operação e ajuda

Use **?** ao lado de cada opção: passe o mouse, clique/toque para fixar ou use Tab e Enter.
Escape fecha a ajuda. Datas operacionais usam **horário de Brasília**.
Em **Funcionamento e horários**, abra/feche novos monitoramentos e pause atualizações
ou avisos separadamente. Consultar, cancelar e apagar dados continua disponível.
Cada atividade aceita agenda com datas, dias e faixa horária; 22h–02h termina no dia seguinte.
A pausa manual prevalece sobre a agenda. Avisos antigos são revalidados ao reabrir.
Escolha atualizações automáticas ou somente manuais em **Configurações**.

---

## 💚 Privacidade e suporte

Gratuito e **independente da UnB**. Não realiza nem verifica matrícula.
Um alerta não garante vaga. Não solicitamos senha do SIGAA, matrícula, telefone ou e-mail.
Identificadores protegidos são dados pseudonimizados; o Telegram tem processamento próprio.
Segredos e dados operacionais ficam fora do Git. Não compartilhe a pasta `.private`.

[🐛 Suporte e sugestões](https://github.com/xGabrielCv/VagasUnB-Extraordinaria/issues) ·
[💻 Código-fonte](https://github.com/xGabrielCv/VagasUnB-Extraordinaria) · [📄 MIT](LICENSE)

Cliente público adaptado do [Followw UnB](https://github.com/xGabrielCv/2026-2-Followw).
Créditos e licença em [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
