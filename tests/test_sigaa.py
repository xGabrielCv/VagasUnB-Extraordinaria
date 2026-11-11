from urllib.parse import parse_qs

import httpx
import pytest

from vagasunb.sigaa_public import HOME, PATH, PublicSource, SourceError, parse_results

FORM = """<form id="formTurma" action="/sigaa/public/turmas/listar.jsf">
<input name="javax.faces.ViewState" value="state-synthetic"/>
<select name="formTurma:inputNivel"><option value="G">G</option></select>
<select name="formTurma:inputDepto"><option value="11">Unidade sintética</option></select>
<input name="formTurma:inputAno" value="2040"/>
<input name="formTurma:inputPeriodo" value="1"/>
<input type="submit" name="generated:button:42" value="Buscar"/></form>"""
RESULT = """<table class="listagem"><tr><th>Turma</th><th>Vagas</th><th>Ocupadas</th></tr>
<tr class="agrupador"><td><span class="tituloDisciplina">SYN0001 - Disciplina sintética</span></td></tr>
<tr class="linhaPar"><td>01</td><td>2040.1</td><td>Docente sintético (60h)</td><td>24M12</td><td></td><td>30</td><td>29</td><td>Sala sintética</td></tr></table>"""
EMPTY = '<ul class="erros"><li>Não foram encontrados resultados</li></ul>'


async def test_jsf_warmup_state_submit_and_session_recovery():
    paths, payloads = [], []
    expired = [True]

    def fake(request):
        paths.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, text="home" if request.url.path == HOME else FORM)
        payloads.append(parse_qs(request.content.decode()))
        if expired[0]:
            expired[0] = False
            return httpx.Response(302, headers={"Location": HOME})
        return httpx.Response(200, text=RESULT)

    source = PublicSource(httpx.MockTransport(fake))
    rows = await source.collect("11", "2040.1")
    assert paths == [HOME, PATH, PATH] * 2
    assert payloads[-1]["javax.faces.ViewState"] == ["state-synthetic"]
    assert payloads[-1]["generated:button:42"] == ["Buscar"]
    assert rows[0]["occupied"] == 29 and rows[0]["campus"] is None


def test_empty_is_explicit_and_unknown_not_zero():
    assert parse_results(EMPTY, "11", "2040.1") == []
    rows = parse_results(RESULT.replace("<td>29</td>", "<td>?</td>"), "11", "2040.1")
    assert rows[0]["occupied"] is None


@pytest.mark.parametrize(
    "html",
    [
        "",
        RESULT.replace("Ocupadas", "Changed"),
        RESULT.replace("<td>29</td>", ""),
        RESULT.replace("2040.1", "2040.2"),
        RESULT.replace("<td>29</td>", "<td>40</td>"),
        '<table class="listagem"><tr><th>Turma Vagas Ocupadas</th></tr></table>',
    ],
)
def test_layout_partial_and_invalid_counts_rejected(html):
    with pytest.raises(SourceError):
        parse_results(html, "11", "2040.1")


@pytest.mark.parametrize("code", [403, 429])
async def test_restrictions_stop_retries_and_open_circuit(code):
    count = [0]

    def fake(request):
        count[0] += 1
        return httpx.Response(code, headers={"Retry-After": "123"})

    source = PublicSource(httpx.MockTransport(fake))
    with pytest.raises(SourceError):
        await source.collect("11", "2040.1")
    with pytest.raises(SourceError, match="circuit_open"):
        await source.collect("11", "2040.1")
    assert count[0] == 1


async def test_changed_form_never_posts():
    calls = []

    def fake(request):
        calls.append(request.method)
        return httpx.Response(
            200, text=FORM.replace('name="javax.faces.ViewState"', 'name="changed"')
        )

    with pytest.raises(SourceError):
        await PublicSource(httpx.MockTransport(fake)).collect("11", "2040.1")
    assert "POST" not in calls
